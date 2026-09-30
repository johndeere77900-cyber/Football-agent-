"""
Centralized match status and score semantics policy for football and basketball.

Defines authoritative status classifications (finished, live, postponed/cancelled)
and score parsing (regular goals vs extra time vs penalty shootouts).
"""

from typing import Any, Dict, Optional, Tuple

FOOTBALL_FINISHED_STATUSES = {"FT", "AET", "PEN"}
FOOTBALL_LIVE_STATUSES = {"1H", "2H", "HT", "ET", "BT", "P", "SUSP", "INT"}
FOOTBALL_POSTPONED_CANC_STATUSES = {"PST", "CANC", "ABD", "AWD", "WO"}

BASKETBALL_FINISHED_STATUSES = {"FT", "AOT"}
BASKETBALL_LIVE_STATUSES = {"Q1", "Q2", "Q3", "Q4", "OT", "HT", "BT"}
BASKETBALL_POSTPONED_CANC_STATUSES = {"PST", "CANC", "ABD"}


def parse_strict_int(val: Any) -> Optional[int]:
    """Strictly parse non-boolean integers."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        val_str = val.strip()
        if not val_str:
            return None
        if val_str.startswith("-") or val_str.startswith("+"):
            digits = val_str[1:]
        else:
            digits = val_str
        if digits.isdigit():
            try:
                return int(val_str)
            except ValueError:
                return None
    return None


def get_status_short(fixture: Dict[str, Any], sport: str = "football") -> str:
    """Extract status short code safely."""
    if not isinstance(fixture, dict):
        return ""
    if sport.lower() == "basketball":
        status_obj = fixture.get("status")
    else:
        status_obj = fixture.get("fixture", {}).get("status")

    if isinstance(status_obj, dict):
        short = status_obj.get("short")
        return str(short).strip() if short else ""
    return ""


def is_finished_match(fixture: Dict[str, Any], sport: str = "football") -> bool:
    """
    Return True if match is completed (FT, AET, PEN for football; FT, AOT for basketball).
    """
    status = get_status_short(fixture, sport=sport)
    if sport.lower() == "basketball":
        return status in BASKETBALL_FINISHED_STATUSES
    return status in FOOTBALL_FINISHED_STATUSES


def is_live_match(fixture: Dict[str, Any], sport: str = "football") -> bool:
    """Return True if match is currently live."""
    status = get_status_short(fixture, sport=sport)
    if sport.lower() == "basketball":
        return status in BASKETBALL_LIVE_STATUSES
    return status in FOOTBALL_LIVE_STATUSES


def is_postponed_or_cancelled(fixture: Dict[str, Any], sport: str = "football") -> bool:
    """Return True if match was postponed, cancelled, or abandoned."""
    status = get_status_short(fixture, sport=sport)
    if sport.lower() == "basketball":
        return status in BASKETBALL_POSTPONED_CANC_STATUSES
    return status in FOOTBALL_POSTPONED_CANC_STATUSES


def get_football_match_goals(fixture: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """
    Return valid numeric goals (home, away) for a football fixture, or None if missing/invalid.

    Uses `goals` object representing regular/extra time goals (excluding penalty shootout goals).
    """
    if not isinstance(fixture, dict):
        return None

    goals_obj = fixture.get("goals")
    if not isinstance(goals_obj, dict):
        return None

    h = parse_strict_int(goals_obj.get("home"))
    a = parse_strict_int(goals_obj.get("away"))

    if h is None or h < 0 or a is None or a < 0:
        return None

    return h, a


def get_basketball_match_points(game: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """
    Return valid numeric points (home, away) for a basketball game, or None if missing/invalid.
    """
    if not isinstance(game, dict):
        return None

    scores_obj = game.get("scores")
    if not isinstance(scores_obj, dict):
        return None

    home_score = scores_obj.get("home")
    away_score = scores_obj.get("away")

    h_pts = parse_strict_int(home_score.get("total")) if isinstance(home_score, dict) else None
    a_pts = parse_strict_int(away_score.get("total")) if isinstance(away_score, dict) else None

    if h_pts is None or h_pts < 0 or a_pts is None or a_pts < 0:
        return None

    return h_pts, a_pts


def get_score_breakdown(fixture: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract structured fulltime, extratime, and penalty score details for football.
    """
    if not isinstance(fixture, dict):
        return {"fulltime": (None, None), "extratime": (None, None), "penalty": (None, None)}

    score_obj = fixture.get("score")
    if not isinstance(score_obj, dict):
        return {"fulltime": (None, None), "extratime": (None, None), "penalty": (None, None)}

    def _parse_pair(sub_key):
        sub = score_obj.get(sub_key)
        if isinstance(sub, dict):
            return parse_strict_int(sub.get("home")), parse_strict_int(sub.get("away"))
        return None, None

    return {
        "fulltime": _parse_pair("fulltime"),
        "extratime": _parse_pair("extratime"),
        "penalty": _parse_pair("penalty"),
    }


def get_match_outcome(fixture: Dict[str, Any], sport: str = "football") -> Optional[str]:
    """
    Determine actual 1X2 / Moneyline match outcome ("home_win", "draw", "away_win").
    """
    if sport.lower() == "basketball":
        res = get_basketball_match_points(fixture)
    else:
        res = get_football_match_goals(fixture)

    if res is None:
        return None

    h, a = res
    if h > a:
        return "home_win"
    if a > h:
        return "away_win"
    return "draw"
