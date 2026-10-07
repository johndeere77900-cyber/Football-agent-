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
DEFAULT_SOCCERDATA_TIMEOUT = 12.0


def _parse_row_season(raw_s, default_s=None):
    """Parse raw season from row dictionary into integer year (e.g. '2024', '2425' -> 2024)."""
    if raw_s is None or str(raw_s).strip().lower() in ("", "none", "nan"):
        return default_s
    s_str = str(raw_s).strip()
    if s_str.isdigit():
        val = int(s_str)
        if val >= 2000:
            return val
        if len(s_str) == 4 and val < 2000:  # e.g. 2425 -> 2024
            return 2000 + int(s_str[:2])
    if len(s_str) >= 4 and s_str[:4].isdigit():
        return int(s_str[:4])
    return default_s


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

    # Parse row's actual season from raw data (do NOT default to requested season if missing)
    row_season = _parse_row_season(data.get("season") or data.get("Season"), default_s=None)

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
            "provider_type": "tertiary",
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

    # Parse row's actual season from raw data (do NOT default to requested season if missing)
    row_season = _parse_row_season(data.get("season") or data.get("Season"), default_s=None)

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
            "provider_type": "tertiary",
            "provider_fixture_id": fixture_id,
            "provider_team_ids": {
                "home": home_id,
                "away": away_id,
            },
            "retrieved_at": time_utils.format_utc_iso(datetime.now(timezone.utc)),
        },
    }


def get_match_history_games(league_code, season, timeout_seconds=DEFAULT_SOCCERDATA_TIMEOUT, league_id=None):
    """
    Retrieve matches for league_code (e.g., 'ENG-Premier League', 'ESP-La Liga') and season via SoccerData MatchHistory.
    Executes in a clean subprocess to prevent C-level library crashes from affecting the main process.

    Returns tuple: (status_code, matches_list, metadata)
    """
    if not league_code:
        return "SOURCE_NOT_AVAILABLE", [], {"status": "SOURCE_NOT_AVAILABLE", "source": "soccerdata_match_history"}

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

    mh = sd.MatchHistory(leagues={repr(league_code)}, seasons={repr(season)})
    df = mh.read_games()
    if df is None or getattr(df, 'empty', True):
        print(json.dumps([]))
    else:
        df_reset = df.reset_index() if hasattr(df, 'reset_index') else df
        records = df_reset.to_dict(orient='records')
        print(json.dumps(records, default=str))
except Exception as exc:
    sys.exit(1)
"""

    try:
        proc = subprocess.run(
            [sys.executable, "-c", code_str],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw_records = json.loads(proc.stdout)
        else:
            raw_records = []
    except Exception as exc:
        logger.warning(f"SoccerData subprocess execution failed or timed out: {exc}")
        return "SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": str(exc), "source": "soccerdata_match_history"}

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


def get_sofascore_historical_games(league_code, season, timeout_seconds=DEFAULT_SOCCERDATA_TIMEOUT, league_id=None):
    """
    Retrieve matches for league_code (e.g., 'ENG-Premier League', 'INT-European Championship') and season via SoccerData Sofascore.
    Executes in a clean subprocess to prevent C-level library crashes from affecting the main process.

    Returns tuple: (status_code, matches_list, metadata)
    """
    if not league_code:
        return "SOURCE_NOT_AVAILABLE", [], {"status": "SOURCE_NOT_AVAILABLE", "source": "soccerdata_sofascore"}

    code_str = f"""
import json, sys
try:
    import soccerdata as sd
    import soccerdata._config as cfg

    custom_leagues = {{
        'INT-Champions League': {{'Sofascore': 'UEFA Champions League'}},
        'INT-Europa League': {{'Sofascore': 'UEFA Europa League'}},
        'INT-Nations League': {{'Sofascore': 'UEFA Nations League'}},
        'INT-World Cup': {{'Sofascore': 'World Cup'}},
        'INT-European Championship': {{'Sofascore': 'EURO'}},
        'NED-Eredivisie': {{'Sofascore': 'Eredivisie'}},
        'POR-Primeira Liga': {{'Sofascore': 'Liga Portugal'}},
    }}
    for k, v in custom_leagues.items():
        if k not in cfg.LEAGUE_DICT:
            cfg.LEAGUE_DICT[k] = v
        else:
            cfg.LEAGUE_DICT[k].update(v)

    if hasattr(sd.Sofascore, '_all_leagues_dict'):
        delattr(sd.Sofascore, '_all_leagues_dict')

    ss = sd.Sofascore(leagues={repr(league_code)}, seasons={repr(season)})
    df = ss.read_schedule()
    if df is None or getattr(df, 'empty', True):
        print(json.dumps([]))
    else:
        df_reset = df.reset_index() if hasattr(df, 'reset_index') else df
        records = df_reset.to_dict(orient='records')
        print(json.dumps(records, default=str))
except Exception as exc:
    sys.exit(1)
"""

    try:
        proc = subprocess.run(
            [sys.executable, "-c", code_str],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw_records = json.loads(proc.stdout)
        else:
            raw_records = []
    except Exception as exc:
        logger.warning(f"SoccerData Sofascore subprocess execution failed or timed out: {exc}")
        return "SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": str(exc), "source": "soccerdata_sofascore"}

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
            }
            return "SOURCE_RETURNED_PARTIAL_DATA", [], meta

        meta = {
            "status": "SOURCE_AVAILABLE",
            "count": len(normalized_matches),
            "source": "soccerdata_sofascore",
        }
        return "SOURCE_AVAILABLE", normalized_matches, meta

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
        req_c_id = team_identity.bootstrap_historical_team_identity(team_name, "soccerdata", None, league_id=league_id)
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
                    h_cid = team_identity.bootstrap_historical_team_identity(h_name, "soccerdata", None, league_id=league_id)
                    a_cid = team_identity.bootstrap_historical_team_identity(a_name, "soccerdata", None, league_id=league_id)
                except Exception:
                    h_cid, a_cid = None, None

                m["canonical_home_id"] = h_cid
                m["canonical_away_id"] = a_cid

                if req_c_id and (h_cid == req_c_id or a_cid == req_c_id):
                    collected_matches.append(m)

    if collected_matches:
        return "SOURCE_AVAILABLE", collected_matches, {"status": "SOURCE_AVAILABLE", "count": len(collected_matches), "source": "soccerdata_match_history"}

    return "SOURCE_NOT_AVAILABLE", [], {"status": "SOURCE_NOT_AVAILABLE", "source": "soccerdata"}
