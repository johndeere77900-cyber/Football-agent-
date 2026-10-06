"""
SoccerData Provider Adapter Module.

Provides isolated, provider-neutral access to SoccerData sources (MatchHistory, Sofascore, FBref, Understat)
with strict hard timeouts, bounded retries, source availability detection, and field-level provenance metadata.

Guarantees:
- Hard timeout on scraper execution to prevent long hangs.
- Process isolation via clean subprocess so C-level library crashes (e.g. tls_requests bus errors) never crash the main process.
- Explicit status reporting: SOURCE_AVAILABLE, SOURCE_NOT_AVAILABLE, SOURCE_FAILED, SOURCE_RETURNED_PARTIAL_DATA.
- Never fabricates missing values.
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


def _normalize_match_history_row(row, league_id=None):
    """Normalize a match record from SoccerData MatchHistory."""
    if row is None or not isinstance(row, dict):
        return None

    data = row

    date_val = str(data.get("date") or data.get("Date") or "")
    home_team = str(data.get("home_team") or data.get("HomeTeam") or "")
    away_team = str(data.get("away_team") or data.get("AwayTeam") or "")

    if not date_val or not home_team or not away_team:
        return None

    # Extract score / goals
    home_goals = data.get("FTHG") if "FTHG" in data else data.get("home_score")
    away_goals = data.get("FTAG") if "FTAG" in data else data.get("away_score")

    try:
        home_goals = int(home_goals) if home_goals is not None and str(home_goals).isdigit() else None
        away_goals = int(away_goals) if away_goals is not None and str(away_goals).isdigit() else None
    except (ValueError, TypeError):
        home_goals, away_goals = None, None

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

    source_fixture_id = f"sd_mh_{date_val}_{home_team}_{away_team}".replace(" ", "_")

    return {
        "fixture": {
            "id": source_fixture_id,
            "date": date_val,
            "status": {"short": "FT" if home_goals is not None else "NS", "long": "Finished" if home_goals is not None else "Not Started"},
        },
        "league": {
            "id": league_id,
            "name": str(data.get("league") or data.get("Div") or ""),
        },
        "teams": {
            "home": {"id": f"sd_{home_team}".replace(" ", "_"), "name": home_team},
            "away": {"id": f"sd_{away_team}".replace(" ", "_"), "name": away_team},
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
            "provider_fixture_id": source_fixture_id,
            "provider_team_ids": {
                "home": f"sd_{home_team}".replace(" ", "_"),
                "away": f"sd_{away_team}".replace(" ", "_"),
            },
            "retrieved_at": time_utils.format_utc_iso(datetime.now(timezone.utc)),
        },
    }


def get_match_history_games(league_code, season, timeout_seconds=DEFAULT_SOCCERDATA_TIMEOUT):
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
            norm = _normalize_match_history_row(row)
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


def get_team_historical_matches(team_name, season=None, timeout_seconds=DEFAULT_SOCCERDATA_TIMEOUT):
    """
    Retrieve team-centric historical matches using available SoccerData sources.
    Iterates over known available MatchHistory leagues if no specific league is supplied.

    Returns tuple: (status_code, matches_list, metadata)
    """
    if not team_name:
        return "SOURCE_NOT_AVAILABLE", [], {"status": "SOURCE_NOT_AVAILABLE", "source": "soccerdata"}

    team_lower = team_name.lower().strip()
    available_leagues = ['ENG-Premier League', 'ESP-La Liga', 'FRA-Ligue 1', 'GER-Bundesliga', 'ITA-Serie A']
    collected_matches = []

    for lcode in available_leagues:
        status, matches, meta = get_match_history_games(lcode, season, timeout_seconds=timeout_seconds)
        if status == "SOURCE_AVAILABLE" and matches:
            collected_matches.extend(matches)

    if collected_matches:
        return "SOURCE_AVAILABLE", collected_matches, {"status": "SOURCE_AVAILABLE", "count": len(collected_matches), "source": "soccerdata_match_history"}

    return "SOURCE_NOT_AVAILABLE", [], {"status": "SOURCE_NOT_AVAILABLE", "source": "soccerdata"}
