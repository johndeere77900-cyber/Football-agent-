"""
SoccerData Provider Adapter Module.

Provides isolated, provider-neutral access to SoccerData sources (MatchHistory, Sofascore, FBref, Understat)
with strict hard timeouts, bounded retries, source availability detection, and field-level provenance metadata.

Guarantees:
- Hard timeout on scraper execution to prevent long hangs.
- Process isolation via clean subprocess so C-level library crashes (e.g. tls_requests bus errors) never crash the main process.
- Explicit status reporting: SOURCE_AVAILABLE, SOURCE_NOT_AVAILABLE, SOURCE_FAILED, SOURCE_RETURNED_PARTIAL_DATA.
- Never fabricates missing values or provider identities.
- Complete provider provenance tracking.
"""

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
import time_utils

logger = logging.getLogger(__name__)

# Standard default timeout for SoccerData calls in seconds
DEFAULT_SOCCERDATA_TIMEOUT = 60.0

# Mapping league ID to SoccerData FBref code strings (only supported FBref leagues)
LEAGUE_TO_SD_FBREF_CODE = {
    39: "ENG-Premier League",
    140: "ESP-La Liga",
    135: "ITA-Serie A",
    78: "GER-Bundesliga",
    61: "FRA-Ligue 1",
    1: "INT-World Cup",
}


def _parse_row_season(raw_s, default_s=None):
    """Parse raw season from row dictionary into integer year (e.g. '2024', '2425' -> 2024)."""
    if raw_s is None or str(raw_s).strip().lower() in ("", "none", "nan"):
        return default_s
    s_str = str(raw_s).strip()
    if s_str.isdigit():
        if len(s_str) == 4:
            val = int(s_str)

            # Preserve literal calendar/start years before compact season keys.
            if 1900 <= val <= 2100:
                return val

            # Compact consecutive season key, e.g. 2122 -> 2021-22.
            first_two = int(s_str[:2])
            last_two = int(s_str[2:])

            if last_two == (first_two + 1) % 100:
                return 2000 + first_two

            return default_s

        if len(s_str) == 2:
            return 2000 + int(s_str)

    if len(s_str) >= 4 and s_str[:4].isdigit():
        return int(s_str[:4])
    return default_s


def _normalize_fbref_row(row, league_id=None, season=None):
    """Normalize a match record from SoccerData FBref."""
    if row is None or not isinstance(row, dict):
        return None

    data = row

    date_val = str(data.get("date") or data.get("Date") or "")
    home_team = str(data.get("home_team") or data.get("Home") or data.get("home") or "")
    away_team = str(data.get("away_team") or data.get("Away") or data.get("away") or "")

    if not date_val or not home_team or not away_team or date_val in ("NaT", "None"):
        return None

    raw_game_id = data.get("game_id") or data.get("game") or data.get("id")
    provider_fixture_id = None
    if raw_game_id is not None and not isinstance(raw_game_id, bool):
        s_val = str(raw_game_id).strip()
        if s_val and s_val.lower() not in ("nan", "none"):
            provider_fixture_id = s_val

    raw_s = data.get("season") or data.get("Season")
    parsed_raw = _parse_row_season(raw_s, default_s=None)

    if season is not None:
        if parsed_raw is not None and parsed_raw != season:
            return None
        row_season = season
    else:
        row_season = parsed_raw

    if row_season is None:
        return None

    import hashlib
    syn_str = f"sd_fbref_{league_id}_{row_season}_{date_val}_{home_team}_{away_team}"
    fixture_id = int(hashlib.sha256(syn_str.encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF

    home_goals = data.get("home_score")
    away_goals = data.get("away_score")

    if home_goals is None or away_goals is None:
        score_str = str(data.get("score") or data.get("Score") or "")
        if "–" in score_str or "-" in score_str:
            sep = "–" if "–" in score_str else "-"
            parts = score_str.split(sep)
            if len(parts) == 2:
                try:
                    home_goals = int(parts[0].strip())
                    away_goals = int(parts[1].strip())
                except (ValueError, TypeError):
                    pass

    try:
        home_goals = int(home_goals) if home_goals is not None and str(home_goals).strip().isdigit() else None
        away_goals = int(away_goals) if away_goals is not None and str(away_goals).strip().isdigit() else None
    except (ValueError, TypeError):
        home_goals, away_goals = None, None

    # Extract real provider team IDs if present as positive ints; otherwise leave None
    raw_h_id = data.get("home_team_id") or data.get("home_id")
    raw_a_id = data.get("away_team_id") or data.get("away_id")

    home_id = None
    if raw_h_id is not None and not isinstance(raw_h_id, bool) and str(raw_h_id).strip().lower() not in ("nan", "none", ""):
        try:
            val = int(str(raw_h_id).strip())
            if val > 0:
                home_id = val
        except (ValueError, TypeError):
            home_id = None

    away_id = None
    if raw_a_id is not None and not isinstance(raw_a_id, bool) and str(raw_a_id).strip().lower() not in ("nan", "none", ""):
        try:
            val = int(str(raw_a_id).strip())
            if val > 0:
                away_id = val
        except (ValueError, TypeError):
            away_id = None

    field_availability = {
        "fixture": True,
        "teams": True,
        "score": (home_goals is not None and away_goals is not None),
        "shots": False,
        "shots_on_target": False,
        "corners": False,
        "cards": False,
        "xG": False,
    }

    return {
        "fixture": {
            "id": fixture_id,
            "date": date_val,
            "status": {"short": "FT" if home_goals is not None else "NS", "long": "Finished" if home_goals is not None else "Not Started"},
        },
        "league": {
            "id": league_id,
            "season": row_season,
            "name": str(data.get("league") or data.get("Comp") or ""),
        },
        "teams": {
            "home": {"id": home_id, "name": home_team},
            "away": {"id": away_id, "name": away_team},
        },
        "goals": {
            "home": home_goals,
            "away": away_goals,
        },
        "statistics": {
            "shots": None,
            "shots_on_target": None,
            "corners": None,
            "cards": None,
            "xG": None,
        },
        "field_availability": field_availability,
        "provider_provenance": {
            "provider": "soccerdata_fbref",
            "provider_type": "primary",
            "provider_fixture_id": provider_fixture_id,
            "provider_team_ids": {
                "home": home_id,
                "away": away_id,
            },
            "retrieved_at": time_utils.format_utc_iso(datetime.now(timezone.utc)),
        },
    }


def _normalize_match_history_row(row, league_id=None, season=None):
    """Normalize a match record from SoccerData MatchHistory."""
    if row is None or not isinstance(row, dict):
        return None

    data = row

    date_val = str(data.get("date") or data.get("Date") or "")
    home_team = str(data.get("home_team") or data.get("HomeTeam") or "")
    away_team = str(data.get("away_team") or data.get("AwayTeam") or "")

    if not date_val or not home_team or not away_team or date_val in ("NaT", "None"):
        return None

    # Require real numeric provider game/event ID
    raw_game_id = data.get("game_id") or data.get("game") or data.get("id")
    if raw_game_id is None:
        return None

    raw_str = str(raw_game_id).strip().lower()
    if raw_str in ("", "nan", "none"):
        return None

    try:
        fixture_id = int(raw_str)
        if fixture_id <= 0:
            return None
    except (ValueError, TypeError):
        return None

    raw_s = data.get("season") or data.get("Season")
    parsed_raw = _parse_row_season(raw_s, default_s=None)

    if season is not None:
        if parsed_raw is not None and parsed_raw != season:
            return None
        row_season = season
    else:
        row_season = parsed_raw

    if row_season is None:
        return None

    # Extract score / goals
    home_goals = data.get("FTHG") if "FTHG" in data else data.get("home_score")
    away_goals = data.get("FTAG") if "FTAG" in data else data.get("away_score")

    try:
        home_goals = int(home_goals) if home_goals is not None and str(home_goals).strip().isdigit() else None
        away_goals = int(away_goals) if away_goals is not None and str(away_goals).strip().isdigit() else None
    except (ValueError, TypeError):
        home_goals, away_goals = None, None

    # Extract real provider team IDs if present as positive ints; otherwise leave None
    raw_h_id = data.get("home_team_id") or data.get("home_id")
    raw_a_id = data.get("away_team_id") or data.get("away_id")

    home_id = None
    if raw_h_id is not None and not isinstance(raw_h_id, bool) and str(raw_h_id).strip().lower() not in ("nan", "none", ""):
        try:
            val = int(str(raw_h_id).strip())
            if val > 0:
                home_id = val
        except (ValueError, TypeError):
            home_id = None

    away_id = None
    if raw_a_id is not None and not isinstance(raw_a_id, bool) and str(raw_a_id).strip().lower() not in ("nan", "none", ""):
        try:
            val = int(str(raw_a_id).strip())
            if val > 0:
                away_id = val
        except (ValueError, TypeError):
            away_id = None

    # Match statistics
    shots = {}
    if "HS" in data and data["HS"] is not None:
        try:
            shots["home"] = int(data["HS"])
        except (ValueError, TypeError):
            pass
    if "AS" in data and data["AS"] is not None:
        try:
            shots["away"] = int(data["AS"])
        except (ValueError, TypeError):
            pass

    shots_on_target = {}
    if "HST" in data and data["HST"] is not None:
        try:
            shots_on_target["home"] = int(data["HST"])
        except (ValueError, TypeError):
            pass
    if "AST" in data and data["AST"] is not None:
        try:
            shots_on_target["away"] = int(data["AST"])
        except (ValueError, TypeError):
            pass

    corners = {}
    if "HC" in data and data["HC"] is not None:
        try:
            corners["home"] = int(data["HC"])
        except (ValueError, TypeError):
            pass
    if "AC" in data and data["AC"] is not None:
        try:
            corners["away"] = int(data["AC"])
        except (ValueError, TypeError):
            pass

    cards = {}
    if "HY" in data and "HR" in data:
        try:
            cards["home"] = {"yellow": int(data["HY"]), "red": int(data.get("HR", 0))}
        except (ValueError, TypeError):
            pass
    if "AY" in data and "AR" in data:
        try:
            cards["away"] = {"yellow": int(data["AY"]), "red": int(data.get("AR", 0))}
        except (ValueError, TypeError):
            pass

    field_availability = {
        "fixture": True,
        "teams": True,
        "score": (home_goals is not None and away_goals is not None),
        "shots": bool(shots),
        "shots_on_target": bool(shots_on_target),
        "corners": bool(corners),
        "cards": bool(cards),
        "xG": False,  # MatchHistory doesn't provide xG
    }

    return {
        "fixture": {
            "id": fixture_id,
            "date": date_val,
            "status": {"short": "FT" if home_goals is not None else "NS", "long": "Finished" if home_goals is not None else "Not Started"},
        },
        "league": {
            "id": league_id,
            "season": row_season,
            "name": str(data.get("league") or data.get("Div") or ""),
        },
        "teams": {
            "home": {"id": home_id, "name": home_team},
            "away": {"id": away_id, "name": away_team},
        },
        "goals": {
            "home": home_goals,
            "away": away_goals,
        },
        "statistics": {
            "shots": shots if shots else None,
            "shots_on_target": shots_on_target if shots_on_target else None,
            "corners": corners if corners else None,
            "cards": cards if cards else None,
            "xG": None,
        },
        "field_availability": field_availability,
        "provider_provenance": {
            "provider": "soccerdata_match_history",
            "provider_type": "primary",
            "provider_fixture_id": fixture_id,
            "provider_team_ids": {
                "home": home_id,
                "away": away_id,
            },
            "retrieved_at": time_utils.format_utc_iso(datetime.now(timezone.utc)),
        },
    }


def _normalize_sofascore_row(row, league_id=None, season=None):
    """Normalize a match record from SoccerData Sofascore."""
    if row is None or not isinstance(row, dict):
        return None

    data = row

    date_val = str(data.get("date") or "")
    home_team = str(data.get("home_team") or "")
    away_team = str(data.get("away_team") or "")

    if not date_val or not home_team or not away_team or date_val in ("NaT", "None"):
        return None

    # Require real numeric provider game/event ID (BIGINT strict positive int)
    raw_game_id = data.get("game_id") or data.get("game") or data.get("id")
    if raw_game_id is None:
        return None

    raw_str = str(raw_game_id).strip().lower()
    if raw_str in ("", "nan", "none"):
        return None

    try:
        fixture_id = int(raw_str)
        if fixture_id <= 0:
            return None
    except (ValueError, TypeError):
        return None

    raw_s = data.get("season") or data.get("Season")
    parsed_raw = _parse_row_season(raw_s, default_s=None)

    if season is not None:
        if parsed_raw is not None and parsed_raw != season:
            return None
        row_season = season
    else:
        row_season = parsed_raw

    if row_season is None:
        return None

    # Extract score / goals
    raw_h_score = data.get("home_score")
    raw_a_score = data.get("away_score")

    home_goals = None
    away_goals = None

    if raw_h_score is not None and str(raw_h_score).strip().lower() not in ("nan", "none", ""):
        try:
            home_goals = int(float(raw_h_score))
        except (ValueError, TypeError):
            home_goals = None

    if raw_a_score is not None and str(raw_a_score).strip().lower() not in ("nan", "none", ""):
        try:
            away_goals = int(float(raw_a_score))
        except (ValueError, TypeError):
            away_goals = None

    # Extract raw team IDs if present as positive ints; otherwise leave None (do NOT fabricate team IDs)
    raw_h_id = data.get("home_team_id") or data.get("home_id")
    raw_a_id = data.get("away_team_id") or data.get("away_id")

    home_id = None
    if raw_h_id is not None and not isinstance(raw_h_id, bool) and str(raw_h_id).strip().lower() not in ("nan", "none", ""):
        try:
            val = int(str(raw_h_id).strip())
            if val > 0:
                home_id = val
        except (ValueError, TypeError):
            home_id = None

    away_id = None
    if raw_a_id is not None and not isinstance(raw_a_id, bool) and str(raw_a_id).strip().lower() not in ("nan", "none", ""):
        try:
            val = int(str(raw_a_id).strip())
            if val > 0:
                away_id = val
        except (ValueError, TypeError):
            away_id = None

    # Determine status
    raw_status = str(data.get("status") or "").upper()
    if home_goals is not None and away_goals is not None:
        if "AET" in raw_status or "EXTRA" in raw_status:
            short_status = "AET"
            long_status = "After Extra Time"
        elif "PEN" in raw_status or "PENALTY" in raw_status:
            short_status = "PEN"
            long_status = "Penalties"
        else:
            short_status = "FT"
            long_status = "Finished"
    else:
        if "PST" in raw_status or "POSTPONED" in raw_status:
            short_status = "PST"
            long_status = "Postponed"
        elif "CANC" in raw_status or "CANCELLED" in raw_status:
            short_status = "CANC"
            long_status = "Cancelled"
        elif "SUSP" in raw_status or "SUSPENDED" in raw_status:
            short_status = "SUSP"
            long_status = "Suspended"
        else:
            short_status = "NS"
            long_status = "Not Started"

    field_availability = {
        "fixture": True,
        "teams": True,
        "score": (home_goals is not None and away_goals is not None),
        "shots": False,
        "shots_on_target": False,
        "corners": False,
        "cards": False,
        "xG": False,
    }

    return {
        "fixture": {
            "id": fixture_id,
            "date": date_val,
            "status": {"short": short_status, "long": long_status},
        },
        "league": {
            "id": league_id,
            "season": row_season,
            "name": str(data.get("league") or ""),
        },
        "teams": {
            "home": {"id": home_id, "name": home_team},
            "away": {"id": away_id, "name": away_team},
        },
        "goals": {
            "home": home_goals,
            "away": away_goals,
        },
        "statistics": {
            "shots": None,
            "shots_on_target": None,
            "corners": None,
            "cards": None,
            "xG": None,
        },
        "field_availability": field_availability,
        "provider_provenance": {
            "provider": "soccerdata_sofascore",
            "provider_type": "primary",
            "provider_fixture_id": fixture_id,
            "provider_team_ids": {
                "home": home_id,
                "away": away_id,
            },
            "retrieved_at": time_utils.format_utc_iso(datetime.now(timezone.utc)),
        },
    }


def get_fbref_games(league_code, season, timeout_seconds=DEFAULT_SOCCERDATA_TIMEOUT, league_id=None, max_retries=2):
    """
    Retrieve matches for league_code (e.g., 'ENG-Premier League', 'ESP-La Liga') and season via SoccerData FBref.
    Executes in a clean subprocess to prevent C-level library crashes from affecting the main process.

    Returns tuple: (status_code, matches_list, metadata)
    """
    import time
    if not league_code:
        return "SOURCE_NOT_AVAILABLE", [], {"status": "SOURCE_NOT_AVAILABLE", "source": "soccerdata_fbref"}

    if timeout_seconds is None or timeout_seconds <= 0:
        return "SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": "Deadline expired before execution", "source": "soccerdata_fbref"}

    fbref_season = _format_sofascore_season(season) if season is not None else season

    code_str = f"""
import json, sys
try:
    import soccerdata as sd

    fb = sd.FBref(leagues={repr(league_code)}, seasons={repr(fbref_season)})
    df = fb.read_schedule()
    if df is None or getattr(df, 'empty', True):
        print("SOCCERDATA_JSON_START\\n[]\\nSOCCERDATA_JSON_END")
    else:
        df_reset = df.reset_index() if hasattr(df, 'reset_index') else df
        records = df_reset.to_dict(orient='records')
        print("SOCCERDATA_JSON_START\\n" + json.dumps(records, default=str) + "\\nSOCCERDATA_JSON_END")
except Exception as exc:
    response = getattr(exc, "response", None)
    http_status = getattr(response, "status_code", None)
    if http_status is None:
        http_status = getattr(response, "status", None)
    if http_status is None:
        http_status = getattr(exc, "status_code", None)
    if http_status is None:
        http_status = getattr(exc, "code", None)

    try:
        if isinstance(http_status, bool):
            raise ValueError("Boolean is not an HTTP status")
        http_status = int(http_status)
        if not 100 <= http_status <= 599:
            http_status = None
    except (TypeError, ValueError):
        http_status = None

    error_payload = {{
        "error": str(exc),
        "error_type": type(exc).__name__,
        "http_status": http_status,
    }}
    sys.stdout.write(
        "SOCCERDATA_ERROR_START\n"
        + json.dumps(error_payload, default=str)
        + "\nSOCCERDATA_ERROR_END\n"
    )
    sys.exit(1)
"""

    last_error = None
    proc = None
    start_time = time.monotonic()

    for attempt in range(1, max_retries + 1):
        elapsed = time.monotonic() - start_time
        remaining = timeout_seconds - elapsed
        if remaining <= 0:
            last_error = f"Deadline ({timeout_seconds}s) expired before attempt {attempt}"
            logger.warning(f"SoccerData FBref deadline expired before attempt {attempt}/{max_retries}")
            return "SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": last_error, "source": "soccerdata_fbref"}

        attempt_timeout = min(remaining, timeout_seconds)

        try:
            proc = subprocess.run(
                [sys.executable, "-c", code_str],
                capture_output=True,
                text=True,
                timeout=attempt_timeout,
            )
            stdout_text = proc.stdout if proc and proc.stdout else ""
            start_marker = "SOCCERDATA_JSON_START"
            end_marker = "SOCCERDATA_JSON_END"
            if proc.returncode == 0 and start_marker in stdout_text and end_marker in stdout_text:
                try:
                    s_idx = stdout_text.find(start_marker) + len(start_marker)
                    e_idx = stdout_text.find(end_marker, s_idx)
                    json_str = stdout_text[s_idx:e_idx].strip()
                    raw_records = json.loads(json_str)
                    break
                except json.JSONDecodeError as exc:
                    err_msg = f"JSON parse error on stdout between boundary markers: {exc}"
                    logger.warning(f"Attempt {attempt}/{max_retries} - {err_msg}")
                    return "SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": err_msg, "stderr": proc.stderr, "source": "soccerdata_fbref"}
            else:
                err_start_marker = "SOCCERDATA_ERROR_START"
                err_end_marker = "SOCCERDATA_ERROR_END"
                err_payload = None
                if err_start_marker in stdout_text and err_end_marker in stdout_text:
                    try:
                        s_idx = stdout_text.find(err_start_marker) + len(err_start_marker)
                        e_idx = stdout_text.find(err_end_marker, s_idx)
                        err_json_str = stdout_text[s_idx:e_idx].strip()
                        parsed_err = json.loads(err_json_str)
                        if isinstance(parsed_err, dict):
                            err_payload = parsed_err
                    except (json.JSONDecodeError, ValueError):
                        err_payload = None

                if err_payload:
                    err_type = err_payload.get("error_type") or "SubprocessError"
                    err_desc = err_payload.get("error") or "Unknown error"
                    last_error = f"{err_type}: {err_desc}"

                    http_status = err_payload.get("http_status")
                    if isinstance(http_status, bool):
                        http_status = None
                    if isinstance(http_status, int) and http_status in (401, 403, 404):
                        logger.info(f"Unrecoverable provider HTTP error {http_status} detected ({last_error}). Skipping remaining retries.")
                        meta = {
                            "status": "SOURCE_FAILED",
                            "error": last_error,
                            "stderr": proc.stderr if proc else None,
                            "source": "soccerdata_fbref",
                        }
                        return "SOURCE_FAILED", [], meta
                else:
                    stderr_msg = proc.stderr.strip() if proc and proc.stderr else f"Subprocess exited with returncode {proc.returncode if proc else 'N/A'}"
                    last_error = f"Missing boundary markers or non-zero exit code: {stderr_msg}"

                logger.warning(f"Attempt {attempt}/{max_retries} - SoccerData FBref subprocess failed: {last_error}")
        except subprocess.TimeoutExpired as exc:
            last_error = f"Subprocess timed out after {timeout_seconds}s"
            logger.warning(f"Attempt {attempt}/{max_retries} - SoccerData FBref timeout: {last_error}")
        except Exception as exc:
            last_error = f"Subprocess execution error: {exc}"
            logger.warning(f"Attempt {attempt}/{max_retries} - {last_error}")

    else:
        return "SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": last_error, "source": "soccerdata_fbref"}

    if not raw_records:
        meta = {
            "status": "SOURCE_NOT_AVAILABLE",
            "error": "No data returned or empty response",
            "source": "soccerdata_fbref",
        }
        return "SOURCE_NOT_AVAILABLE", [], meta

    normalized_matches = []
    try:
        for row in raw_records:
            norm = _normalize_fbref_row(row, league_id=league_id, season=season)
            if norm:
                normalized_matches.append(norm)

        if not normalized_matches:
            meta = {
                "status": "SOURCE_RETURNED_PARTIAL_DATA",
                "count": 0,
                "source": "soccerdata_fbref",
            }
            return "SOURCE_RETURNED_PARTIAL_DATA", [], meta

        meta = {
            "status": "SOURCE_AVAILABLE",
            "count": len(normalized_matches),
            "source": "soccerdata_fbref",
        }
        return "SOURCE_AVAILABLE", normalized_matches, meta

    except Exception as exc:
        meta = {
            "status": "SOURCE_FAILED",
            "error": str(exc),
            "source": "soccerdata_fbref",
        }
        return "SOURCE_FAILED", [], meta


def get_match_history_games(league_code, season, timeout_seconds=DEFAULT_SOCCERDATA_TIMEOUT, league_id=None, max_retries=2):
    """
    Retrieve matches for league_code (e.g., 'ENG-Premier League', 'ESP-La Liga') and season via SoccerData MatchHistory.
    Executes in a clean subprocess to prevent C-level library crashes from affecting the main process.

    Returns tuple: (status_code, matches_list, metadata)
    """
    import time
    if not league_code:
        return "SOURCE_NOT_AVAILABLE", [], {"status": "SOURCE_NOT_AVAILABLE", "source": "soccerdata_match_history"}

    if timeout_seconds is None or timeout_seconds <= 0:
        return "SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": "Deadline expired before execution", "source": "soccerdata_match_history"}

    match_history_season = season
    if season is not None and str(season).isdigit() and len(str(season)) == 4:
        start_year = int(season)
        if 1900 <= start_year <= 2100:
            match_history_season = f"{start_year}-{(start_year + 1) % 100:02d}"

    code_str = f"""
import json, sys
try:
    import soccerdata as sd
    import soccerdata._config as cfg

    custom_leagues = {{
        'NED-Eredivisie': {{'MatchHistory': 'N1'}},
        'POR-Primeira Liga': {{'MatchHistory': 'P1'}},
    }}
    for k, v in custom_leagues.items():
        if k not in cfg.LEAGUE_DICT:
            cfg.LEAGUE_DICT[k] = v
        else:
            cfg.LEAGUE_DICT[k].update(v)

    if hasattr(sd.MatchHistory, '_all_leagues_dict'):
        delattr(sd.MatchHistory, '_all_leagues_dict')

    mh = sd.MatchHistory(leagues={repr(league_code)}, seasons={repr(match_history_season)})
    df = mh.read_games()
    if df is None or getattr(df, 'empty', True):
        print("SOCCERDATA_JSON_START\\n[]\\nSOCCERDATA_JSON_END")
    else:
        df_reset = df.reset_index() if hasattr(df, 'reset_index') else df
        records = df_reset.to_dict(orient='records')
        print("SOCCERDATA_JSON_START\\n" + json.dumps(records, default=str) + "\\nSOCCERDATA_JSON_END")
except Exception as exc:
    response = getattr(exc, "response", None)
    http_status = getattr(response, "status_code", None)
    if http_status is None:
        http_status = getattr(response, "status", None)
    if http_status is None:
        http_status = getattr(exc, "status_code", None)
    if http_status is None:
        http_status = getattr(exc, "code", None)

    try:
        if isinstance(http_status, bool):
            raise ValueError("Boolean is not an HTTP status")
        http_status = int(http_status)
        if not 100 <= http_status <= 599:
            http_status = None
    except (TypeError, ValueError):
        http_status = None

    error_payload = {{
        "error": str(exc),
        "error_type": type(exc).__name__,
        "http_status": http_status,
    }}
    sys.stdout.write(
        "SOCCERDATA_ERROR_START\n"
        + json.dumps(error_payload, default=str)
        + "\nSOCCERDATA_ERROR_END\n"
    )
    sys.exit(1)
"""

    last_error = None
    proc = None
    start_time = time.monotonic()

    for attempt in range(1, max_retries + 1):
        elapsed = time.monotonic() - start_time
        remaining = timeout_seconds - elapsed
        if remaining <= 0:
            last_error = f"Deadline ({timeout_seconds}s) expired before attempt {attempt}"
            logger.warning(f"SoccerData MatchHistory deadline expired before attempt {attempt}/{max_retries}")
            return "SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": last_error, "source": "soccerdata_match_history"}

        attempt_timeout = min(remaining, timeout_seconds)

        try:
            proc = subprocess.run(
                [sys.executable, "-c", code_str],
                capture_output=True,
                text=True,
                timeout=attempt_timeout,
            )
            stdout_text = proc.stdout if proc and proc.stdout else ""
            start_marker = "SOCCERDATA_JSON_START"
            end_marker = "SOCCERDATA_JSON_END"
            if proc.returncode == 0 and start_marker in stdout_text and end_marker in stdout_text:
                try:
                    s_idx = stdout_text.find(start_marker) + len(start_marker)
                    e_idx = stdout_text.find(end_marker, s_idx)
                    json_str = stdout_text[s_idx:e_idx].strip()
                    raw_records = json.loads(json_str)
                    break
                except json.JSONDecodeError as exc:
                    err_msg = f"JSON parse error on stdout between boundary markers: {exc}"
                    logger.warning(f"Attempt {attempt}/{max_retries} - {err_msg}")
                    return "SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": err_msg, "stderr": proc.stderr, "source": "soccerdata_match_history"}
            else:
                err_start_marker = "SOCCERDATA_ERROR_START"
                err_end_marker = "SOCCERDATA_ERROR_END"
                err_payload = None
                if err_start_marker in stdout_text and err_end_marker in stdout_text:
                    try:
                        s_idx = stdout_text.find(err_start_marker) + len(err_start_marker)
                        e_idx = stdout_text.find(err_end_marker, s_idx)
                        err_json_str = stdout_text[s_idx:e_idx].strip()
                        parsed_err = json.loads(err_json_str)
                        if isinstance(parsed_err, dict):
                            err_payload = parsed_err
                    except (json.JSONDecodeError, ValueError):
                        err_payload = None

                if err_payload:
                    err_type = err_payload.get("error_type") or "SubprocessError"
                    err_desc = err_payload.get("error") or "Unknown error"
                    last_error = f"{err_type}: {err_desc}"

                    http_status = err_payload.get("http_status")
                    if isinstance(http_status, bool):
                        http_status = None
                    if isinstance(http_status, int) and http_status in (401, 403, 404):
                        logger.info(f"Unrecoverable provider HTTP error {http_status} detected ({last_error}). Skipping remaining retries.")
                        meta = {
                            "status": "SOURCE_FAILED",
                            "error": last_error,
                            "stderr": proc.stderr if proc else None,
                            "source": "soccerdata_match_history",
                        }
                        return "SOURCE_FAILED", [], meta
                else:
                    stderr_msg = proc.stderr.strip() if proc and proc.stderr else f"Subprocess exited with returncode {proc.returncode if proc else 'N/A'}"
                    last_error = f"Missing boundary markers or non-zero exit code: {stderr_msg}"

                logger.warning(f"Attempt {attempt}/{max_retries} - SoccerData MatchHistory subprocess failed: {last_error}")
        except subprocess.TimeoutExpired as exc:
            last_error = f"Subprocess timed out after {timeout_seconds}s"
            logger.warning(f"Attempt {attempt}/{max_retries} - SoccerData MatchHistory timeout: {last_error}")
        except Exception as exc:
            last_error = f"Subprocess execution error: {exc}"
            logger.warning(f"Attempt {attempt}/{max_retries} - {last_error}")

    else:
        # All retries exhausted due to transient subprocess error or non-zero exit code
        return "SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": last_error, "source": "soccerdata_match_history"}

    if not raw_records:
        meta = {
            "status": "SOURCE_NOT_AVAILABLE",
            "error": "No data returned or empty response",
            "source": "soccerdata_match_history",
        }
        return "SOURCE_NOT_AVAILABLE", [], meta

    normalized_matches = []
    try:
        for row in raw_records:
            norm = _normalize_match_history_row(row, league_id=league_id, season=season)
            if norm:
                normalized_matches.append(norm)

        if not normalized_matches:
            meta = {
                "status": "SOURCE_RETURNED_PARTIAL_DATA",
                "count": 0,
                "source": "soccerdata_match_history",
            }
            return "SOURCE_RETURNED_PARTIAL_DATA", [], meta

        meta = {
            "status": "SOURCE_AVAILABLE",
            "count": len(normalized_matches),
            "source": "soccerdata_match_history",
        }
        return "SOURCE_AVAILABLE", normalized_matches, meta

    except Exception as exc:
        meta = {
            "status": "SOURCE_FAILED",
            "error": str(exc),
            "source": "soccerdata_match_history",
        }
        return "SOURCE_FAILED", [], meta


def _format_sofascore_season(val):
    """
    Normalize a requested season to SoccerData's YYYY-YY format.

    Examples:
        2020       -> 2020-21
        2021       -> 2021-22
        2024       -> 2024-25
        "2020-21"  -> 2020-21
        "2020-2021" -> 2020-21
        "2021/22"  -> 2021-22
        "2122"     -> 2021-22
        "2425"     -> 2024-25

    Four-digit calendar years between 1900 and 2100 are treated as
    requested season start years. Four-digit compact season keys outside
    that range are interpreted as YYZZ only when the years are consecutive.
    """
    if val is None:
        return None

    raw = str(val).strip()

    if not raw or raw.lower() in {"none", "nan"}:
        return None

    # Explicit ranges: YYYY-YY, YYYY-YYYY, or YYYY/YY.
    separator = "-" if "-" in raw else "/" if "/" in raw else None

    if separator:
        parts = [part.strip() for part in raw.split(separator)]

        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            start_year = int(parts[0])

            if len(parts[0]) == 4 and 1900 <= start_year <= 2100:
                end_part = parts[1]

                if len(end_part) == 4:
                    end_year = int(end_part)
                    if end_year == start_year + 1:
                        return f"{start_year}-{str(end_year)[-2:]}"
                elif len(end_part) == 2:
                    expected_end = str(start_year + 1)[-2:]
                    if end_part == expected_end:
                        return f"{start_year}-{expected_end}"

        # Preserve unsupported formats rather than silently inventing a year.
        return raw

    if not raw.isdigit():
        return raw

    if len(raw) == 2:
        start_year = 2000 + int(raw)
        return f"{start_year}-{str(start_year + 1)[-2:]}"

    if len(raw) == 4:
        number = int(raw)

        # Normal requested season start year, e.g. 2021 -> 2021-22.
        if 1900 <= number <= 2100:
            return f"{number}-{str(number + 1)[-2:]}"

        # Compact consecutive season key, e.g. 2122 -> 2021-22.
        first_two = int(raw[:2])
        last_two = int(raw[2:])

        if last_two == (first_two + 1) % 100:
            start_year = 2000 + first_two
            return f"{start_year}-{str(start_year + 1)[-2:]}"

    return raw


def get_sofascore_historical_games(league_code, season, timeout_seconds=DEFAULT_SOCCERDATA_TIMEOUT, league_id=None, max_retries=2):
    """
    Retrieve matches for league_code (e.g., 'ENG-Premier League', 'INT-European Championship') and season via SoccerData Sofascore.
    Executes in a clean subprocess to prevent C-level library crashes from affecting the main process.

    Returns tuple: (status_code, matches_list, metadata)
    """
    import time
    if not league_code:
        return "SOURCE_NOT_AVAILABLE", [], {"status": "SOURCE_NOT_AVAILABLE", "source": "soccerdata_sofascore"}

    if timeout_seconds is None or timeout_seconds <= 0:
        return "SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": "Deadline expired before execution", "source": "soccerdata_sofascore"}

    sofascore_season = _format_sofascore_season(season) if season is not None else season

    code_str = """
import json, sys
try:
    import soccerdata as sd
    import soccerdata._config as cfg

    custom_leagues = {
        'INT-Champions League': {'Sofascore': 'UEFA Champions League'},
        'INT-Europa League': {'Sofascore': 'UEFA Europa League'},
        'INT-Nations League': {'Sofascore': 'UEFA Nations League'},
        'INT-World Cup': {'Sofascore': 'World Cup'},
        'INT-European Championship': {'Sofascore': 'EURO'},
        'NED-Eredivisie': {'Sofascore': 'Eredivisie'},
        'POR-Primeira Liga': {'Sofascore': 'Liga Portugal'},
    }
    for k, v in custom_leagues.items():
        if k not in cfg.LEAGUE_DICT:
            cfg.LEAGUE_DICT[k] = v
        else:
            cfg.LEAGUE_DICT[k].update(v)

    if hasattr(sd.Sofascore, '_all_leagues_dict'):
        delattr(sd.Sofascore, '_all_leagues_dict')

    ss = sd.Sofascore(leagues=__LEAGUE_CODE__, seasons=__SOFASCORE_SEASON__)
    df = ss.read_schedule()
    if df is None or getattr(df, 'empty', True):
        output_payload = {
            "records": [],
            "team_id_map_size": 0,
            "enrichment_error": None,
        }
        print(
            "SOCCERDATA_JSON_START\\n"
            + json.dumps(output_payload)
            + "\\nSOCCERDATA_JSON_END"
        )
    else:
        df_reset = df.reset_index() if hasattr(df, 'reset_index') else df
        records = df_reset.to_dict(orient='records')

        team_id_map = {}
        enrichment_error = None

        def _has_valid_provider_team_id(val):
            if val is None or isinstance(val, bool):
                return False
            try:
                s = str(val).strip()
                return s.isdigit() and int(s) > 0
            except (ValueError, TypeError):
                return False

        def _rec_has_real_team_ids(rec):
            if not isinstance(rec, dict):
                return False
            h = rec.get("home_team_id") or rec.get("home_id")
            a = rec.get("away_team_id") or rec.get("away_id")
            return _has_valid_provider_team_id(h) and _has_valid_provider_team_id(a)

        schedule_has_real_team_ids = bool(records) and all(_rec_has_real_team_ids(r) for r in records)

        if not schedule_has_real_team_ids:
            try:
                df_seasons = ss.read_seasons()

                if df_seasons is None or getattr(df_seasons, "empty", True):
                    raise RuntimeError("Sofascore season metadata unavailable")

                def _requested_start_year(value):
                    raw = str(value).strip()

                    if not raw or raw.lower() in {"none", "nan"}:
                        return None

                    # Requested seasons are normalized before being passed
                    # to SoccerData, e.g. "2021-22".
                    if "-" in raw or "/" in raw:
                        separator = "-" if "-" in raw else "/"
                        parts = [part.strip() for part in raw.split(separator)]

                        if len(parts) == 2 and len(parts[0]) == 4 and parts[0].isdigit():
                            start_year = int(parts[0])
                            if 1900 <= start_year <= 2100:
                                return start_year

                        return None

                    if not raw.isdigit():
                        return None

                    if len(raw) == 4:
                        number = int(raw)

                        # A four-digit requested calendar/start year is literal.
                        if 1900 <= number <= 2100:
                            return number

                        # Compact consecutive season, e.g. 2122 -> 2021-22.
                        first_two = int(raw[:2])
                        last_two = int(raw[2:])

                        if last_two == (first_two + 1) % 100:
                            return 2000 + first_two

                        return None

                    if len(raw) == 2:
                        return 2000 + int(raw)

                    return None

                def _provider_season_start_year(value):
                    raw = str(value).strip()

                    if not raw or raw.lower() in {"none", "nan"}:
                        return None

                    # Provider metadata can contain an explicit season range.
                    if "-" in raw or "/" in raw:
                        separator = "-" if "-" in raw else "/"
                        parts = [part.strip() for part in raw.split(separator)]

                        if len(parts) == 2 and len(parts[0]) == 4 and parts[0].isdigit():
                            start_year = int(parts[0])
                            if 1900 <= start_year <= 2100:
                                return start_year

                        return None

                    if not raw.isdigit():
                        return None

                    if len(raw) == 4:
                        number = int(raw)

                        # Preserve literal calendar/start years before compact season keys.
                        if 1900 <= number <= 2100:
                            return number

                        # Provider compact season key, e.g. 2122 -> 2021-22.
                        first_two = int(raw[:2])
                        last_two = int(raw[2:])

                        if last_two == (first_two + 1) % 100:
                            return 2000 + first_two

                        return None

                    if len(raw) == 2:
                        return 2000 + int(raw)

                    return None

                # Compare against the same normalized season sent to SoccerData.
                req_season_val = __SOFASCORE_SEASON__

                requested_season = _requested_start_year(req_season_val)

                max_requests = 10
                request_count = 0

                for (lkey, skey), s_row in df_seasons.iterrows():
                    if request_count >= max_requests:
                        enrichment_error = "Request bound reached prior to complete round coverage"
                        break

                    # Only enrich teams for the requested historical season.
                    season_key = _provider_season_start_year(skey)

                    if requested_season is None or season_key is None:
                        continue

                    if season_key != requested_season:
                        continue

                    u_league_id = s_row.get("league_id")
                    u_season_id = s_row.get("season_id")

                    if u_league_id is None or u_season_id is None:
                        continue

                    url1 = (
                        f"https://api.sofascore.com/api/v1/"
                        f"unique-tournament/{u_league_id}/season/{u_season_id}/rounds"
                    )

                    fpath1 = ss.data_dir / f"matches/rounds_{lkey}_{skey}.json"
                    ss.get(url1, fpath1)
                    request_count += 1

                    if not fpath1.exists():
                        continue

                    with open(fpath1, "r", encoding="utf-8") as fp1:
                        season_data = json.load(fp1)

                    for r_info in season_data.get("rounds", []):
                        if request_count >= max_requests:
                            enrichment_error = "Request bound reached prior to complete round coverage"
                            break

                        r_num = r_info.get("round")

                        if r_num is None:
                            continue

                        url2 = (
                            f"https://api.sofascore.com/api/v1/"
                            f"unique-tournament/{u_league_id}/season/{u_season_id}"
                            f"/events/round/{r_num}"
                        )

                        fpath2 = (
                            ss.data_dir
                            / f"matches/round_matches_{lkey}_{skey}_{r_num}.json"
                        )

                        ss.get(url2, fpath2)
                        request_count += 1

                        if not fpath2.exists():
                            continue

                        with open(fpath2, "r", encoding="utf-8") as fp2:
                            m_data = json.load(fp2)

                        for ev in m_data.get("events", []):
                            gid = ev.get("id")
                            home_team = ev.get("homeTeam") or {}
                            away_team = ev.get("awayTeam") or {}

                            hid = home_team.get("id")
                            aid = away_team.get("id")

                            if gid is None or hid is None or aid is None:
                                continue

                            try:
                                gid_int = int(gid)
                                hid_int = int(hid)
                                aid_int = int(aid)
                            except (ValueError, TypeError):
                                continue

                            if gid_int <= 0 or hid_int <= 0 or aid_int <= 0:
                                continue

                            team_id_map[str(gid_int)] = (hid_int, aid_int)

            except Exception as exc:
                enrichment_error = str(exc)

        # Attach only REAL Sofascore provider team IDs.
        for rec in records:
            gid_raw = rec.get("game_id") or rec.get("game") or rec.get("id")

            try:
                gid = str(int(gid_raw))
            except (ValueError, TypeError):
                continue

            team_ids = team_id_map.get(gid)

            if team_ids:
                rec["home_team_id"] = team_ids[0]
                rec["away_team_id"] = team_ids[1]

        output_payload = {
            "records": records,
            "team_id_map_size": len(team_id_map),
            "enrichment_error": enrichment_error,
        }
        print(
            "SOCCERDATA_JSON_START\\n"
            + json.dumps(output_payload, default=str)
            + "\\nSOCCERDATA_JSON_END"
        )
except Exception as exc:
    response = getattr(exc, "response", None)
    http_status = getattr(response, "status_code", None)
    if http_status is None:
        http_status = getattr(response, "status", None)
    if http_status is None:
        http_status = getattr(exc, "status_code", None)
    if http_status is None:
        http_status = getattr(exc, "code", None)

    try:
        if isinstance(http_status, bool):
            raise ValueError("Boolean is not an HTTP status")
        http_status = int(http_status)
        if not 100 <= http_status <= 599:
            http_status = None
    except (TypeError, ValueError):
        http_status = None

    error_payload = {
        "error": str(exc),
        "error_type": type(exc).__name__,
        "http_status": http_status,
    }
    sys.stdout.write(
        "SOCCERDATA_ERROR_START\n"
        + json.dumps(error_payload, default=str)
        + "\nSOCCERDATA_ERROR_END\n"
    )
    sys.exit(1)
""".replace("__LEAGUE_CODE__", repr(league_code)).replace("__SOFASCORE_SEASON__", repr(sofascore_season))

    last_error = None
    proc = None
    enrichment_error = None
    start_time = time.monotonic()

    for attempt in range(1, max_retries + 1):
        elapsed = time.monotonic() - start_time
        remaining = timeout_seconds - elapsed
        if remaining <= 0:
            last_error = f"Deadline ({timeout_seconds}s) expired before attempt {attempt}"
            logger.warning(f"SoccerData Sofascore deadline expired before attempt {attempt}/{max_retries}")
            return "SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": last_error, "source": "soccerdata_sofascore"}

        attempt_timeout = min(remaining, timeout_seconds)

        try:
            proc = subprocess.run(
                [sys.executable, "-c", code_str],
                capture_output=True,
                text=True,
                timeout=attempt_timeout,
            )
            stdout_text = proc.stdout if proc and proc.stdout else ""
            start_marker = "SOCCERDATA_JSON_START"
            end_marker = "SOCCERDATA_JSON_END"
            if proc.returncode == 0 and start_marker in stdout_text and end_marker in stdout_text:
                try:
                    s_idx = stdout_text.find(start_marker) + len(start_marker)
                    e_idx = stdout_text.find(end_marker, s_idx)
                    json_str = stdout_text[s_idx:e_idx].strip()
                    payload = json.loads(json_str)
                    if isinstance(payload, dict) and "records" in payload:
                        raw_records = payload.get("records", [])
                        enrichment_error = payload.get("enrichment_error")
                    elif isinstance(payload, list):
                        raw_records = payload
                        enrichment_error = None
                    else:
                        raw_records = []
                        enrichment_error = None
                    break
                except json.JSONDecodeError as exc:
                    err_msg = f"JSON parse error on stdout between boundary markers: {exc}"
                    logger.warning(f"Attempt {attempt}/{max_retries} - {err_msg}")
                    return "SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": err_msg, "stderr": proc.stderr, "source": "soccerdata_sofascore"}
            else:
                err_start_marker = "SOCCERDATA_ERROR_START"
                err_end_marker = "SOCCERDATA_ERROR_END"
                err_payload = None
                if err_start_marker in stdout_text and err_end_marker in stdout_text:
                    try:
                        s_idx = stdout_text.find(err_start_marker) + len(err_start_marker)
                        e_idx = stdout_text.find(err_end_marker, s_idx)
                        err_json_str = stdout_text[s_idx:e_idx].strip()
                        parsed_err = json.loads(err_json_str)
                        if isinstance(parsed_err, dict):
                            err_payload = parsed_err
                    except (json.JSONDecodeError, ValueError):
                        err_payload = None

                if err_payload:
                    err_type = err_payload.get("error_type") or "SubprocessError"
                    err_desc = err_payload.get("error") or "Unknown error"
                    last_error = f"{err_type}: {err_desc}"

                    http_status = err_payload.get("http_status")
                    if isinstance(http_status, bool):
                        http_status = None
                    if isinstance(http_status, int) and http_status in (401, 403, 404):
                        logger.info(f"Unrecoverable provider HTTP error {http_status} detected ({last_error}). Skipping remaining retries.")
                        meta = {
                            "status": "SOURCE_FAILED",
                            "error": last_error,
                            "stderr": proc.stderr if proc else None,
                            "target_url": "https://api.sofascore.com/api/v1/config/default-unique-tournaments/EN/football",
                            "source": "soccerdata_sofascore",
                        }
                        return "SOURCE_FAILED", [], meta
                else:
                    stderr_msg = proc.stderr.strip() if proc and proc.stderr else f"Subprocess exited with returncode {proc.returncode if proc else 'N/A'}"
                    last_error = f"Missing boundary markers or non-zero exit code: {stderr_msg}"

                logger.warning(f"Attempt {attempt}/{max_retries} - SoccerData Sofascore subprocess failed: {last_error}")
        except subprocess.TimeoutExpired as exc:
            last_error = f"Subprocess timed out after {timeout_seconds}s"
            logger.warning(f"Attempt {attempt}/{max_retries} - SoccerData Sofascore timeout: {last_error}")
        except Exception as exc:
            last_error = f"Subprocess execution error: {exc}"
            logger.warning(f"Attempt {attempt}/{max_retries} - {last_error}")

    else:
        # All retries exhausted due to transient subprocess error or non-zero exit code
        meta = {
            "status": "SOURCE_FAILED",
            "error": last_error,
            "stderr": proc.stderr if proc else None,
            "target_url": "https://api.sofascore.com/api/v1/config/default-unique-tournaments/EN/football",
            "source": "soccerdata_sofascore",
        }
        return "SOURCE_FAILED", [], meta

    if not raw_records:
        meta = {
            "status": "SOURCE_NOT_AVAILABLE",
            "error": "No data returned or empty response",
            "source": "soccerdata_sofascore",
        }
        return "SOURCE_NOT_AVAILABLE", [], meta

    normalized_matches = []
    try:
        for row in raw_records:
            norm = _normalize_sofascore_row(row, league_id=league_id, season=season)
            if norm:
                normalized_matches.append(norm)

        if not normalized_matches:
            meta = {
                "status": "SOURCE_RETURNED_PARTIAL_DATA",
                "count": 0,
                "source": "soccerdata_sofascore",
                "enrichment_error": enrichment_error,
            }
            return "SOURCE_RETURNED_PARTIAL_DATA", [], meta

        # A Sofascore fixture is storage-ready only when it has:
        # - real numeric fixture ID
        # - real positive provider home team ID
        # - real positive provider away team ID
        # Never report SOURCE_AVAILABLE for records that cannot pass
        # the downstream historical storage boundary.

        storage_ready_matches = []

        for match in normalized_matches:
            fixture = match.get("fixture", {})
            teams = match.get("teams", {})
            home = teams.get("home", {})
            away = teams.get("away", {})

            fixture_id = fixture.get("id")
            home_id = home.get("id")
            away_id = away.get("id")

            if (
                isinstance(fixture_id, int)
                and not isinstance(fixture_id, bool)
                and fixture_id > 0
                and isinstance(home_id, int)
                and not isinstance(home_id, bool)
                and home_id > 0
                and isinstance(away_id, int)
                and not isinstance(away_id, bool)
                and away_id > 0
            ):
                storage_ready_matches.append(match)

        if not storage_ready_matches:
            meta = {
                "status": "SOURCE_RETURNED_PARTIAL_DATA",
                "count": 0,
                "raw_count": len(normalized_matches),
                "storage_ready_count": 0,
                "team_id_map_size": len(team_id_map) if 'team_id_map' in locals() else 0,
                "enrichment_error": enrichment_error,
                "source": "soccerdata_sofascore",
            }
            return "SOURCE_RETURNED_PARTIAL_DATA", [], meta

        meta = {
            "status": "SOURCE_AVAILABLE",
            "count": len(storage_ready_matches),
            "raw_count": len(normalized_matches),
            "storage_ready_count": len(storage_ready_matches),
            "team_id_map_size": len(team_id_map) if 'team_id_map' in locals() else 0,
            "enrichment_error": enrichment_error,
            "source": "soccerdata_sofascore",
        }

        return "SOURCE_AVAILABLE", storage_ready_matches, meta

    except Exception as exc:
        meta = {
            "status": "SOURCE_FAILED",
            "error": str(exc),
            "source": "soccerdata_sofascore",
        }
        return "SOURCE_FAILED", [], meta


def get_team_historical_matches(team_name, season=None, timeout_seconds=DEFAULT_SOCCERDATA_TIMEOUT, league_id=None):
    """
    Retrieve team-centric historical matches using available SoccerData sources.
    Iterates over known available MatchHistory leagues if no specific league is supplied.
    Filters matches using exact canonical team identity resolution via team_identity.py.

    Returns tuple: (status_code, matches_list, metadata)
    """
    if not team_name:
        return "SOURCE_NOT_AVAILABLE", [], {"status": "SOURCE_NOT_AVAILABLE", "source": "soccerdata"}

    try:
        import team_identity
        req_c_id = team_identity.resolve_canonical_team_id(team_name, "soccerdata", None, league_id=league_id, auto_register=False)
    except Exception:
        req_c_id = None

    available_leagues = ['ENG-Premier League', 'ESP-La Liga', 'FRA-Ligue 1', 'GER-Bundesliga', 'ITA-Serie A']
    collected_matches = []

    for lcode in available_leagues:
        status, matches, meta = get_match_history_games(lcode, season, timeout_seconds=timeout_seconds, league_id=league_id)
        if status == "SOURCE_AVAILABLE" and matches:
            for m in matches:
                h_name = m.get("teams", {}).get("home", {}).get("name", "")
                a_name = m.get("teams", {}).get("away", {}).get("name", "")
                try:
                    h_cid = team_identity.resolve_canonical_team_id(h_name, "soccerdata", None, league_id=league_id, auto_register=False)
                    a_cid = team_identity.resolve_canonical_team_id(a_name, "soccerdata", None, league_id=league_id, auto_register=False)
                except Exception:
                    h_cid, a_cid = None, None

                m["canonical_home_id"] = h_cid
                m["canonical_away_id"] = a_cid

                if req_c_id and (h_cid == req_c_id or a_cid == req_c_id):
                    collected_matches.append(m)

    if collected_matches:
        return "SOURCE_AVAILABLE", collected_matches, {"status": "SOURCE_AVAILABLE", "count": len(collected_matches), "source": "soccerdata_match_history"}

    return "SOURCE_NOT_AVAILABLE", [], {"status": "SOURCE_NOT_AVAILABLE", "source": "soccerdata"}
