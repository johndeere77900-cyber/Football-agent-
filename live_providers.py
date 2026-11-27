"""Provider adapters for the live match lifecycle.

Only feeds whose responses this repository has actually inspected are
implemented here. ESPN's public soccer scoreboard and FotMob's public day
listing are keyless JSON endpoints; each adapter translates the native payload
into the API-Football-shaped fixture dict the rest of the pipeline already
understands, and stamps the provider that produced it so no provider's
identity is ever attributed to another provider's fixture.

These providers are discovery fallbacks only: the lifecycle calls them for a
date when API-Football returned nothing, and looks a single fixture up again by
its provider-native id for pre-match data and final-result verification. Live
in-play monitoring is not advertised because neither feed is polled for clock
or event data in this repository.
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
import urllib.request
from typing import Any, Dict, List, Optional

import config

USER_AGENT = "FootballAgent/phase2-live-lifecycle"
TIMEOUT_SECONDS = 20
CACHE_TTL_SECONDS = 300

# Mirrors espn_football_stats.ESPN_LEAGUES; duplicated so this module does not
# import the historical enrichment script (which opens storage at import time).
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_LEAGUES = {
    1: "fifa.world",
    2: "uefa.champions",
    3: "uefa.europa",
    4: "uefa.euro",
    5: "uefa.nations",
    39: "eng.1",
    61: "fra.1",
    78: "ger.1",
    88: "ned.1",
    94: "por.1",
    135: "ita.1",
    140: "esp.1",
}

FOTMOB_DAY = "https://www.fotmob.com/api/data/matches?date={date}"

# FotMob publishes competition names with inconsistent spacing and branding.
FOTMOB_LEAGUE_ALIASES = {
    "premier league": 39,
    "english premier league": 39,
    "laliga": 140,
    "laliga ea sports": 140,
    "la liga": 140,
    "spanish laliga": 140,
    "serie a": 135,
    "italian serie a": 135,
    "bundesliga": 78,
    "german bundesliga": 78,
    "ligue 1": 61,
    "french ligue 1": 61,
    "champions league": 2,
    "uefa champions league": 2,
    "europa league": 3,
    "uefa europa league": 3,
    "nations league": 5,
    "uefa nations league": 5,
    "world cup": 1,
    "fifa world cup": 1,
    "euro": 4,
    "uefa euro": 4,
    "european championship": 4,
    "uefa european championship": 4,
    "eredivisie": 88,
    "dutch eredivisie": 88,
    "liga portugal": 94,
    "primeira liga": 94,
}

# Youth, reserve, women's and friendly fixtures share senior competition names.
NON_SENIOR_MARKERS = (
    "u17",
    "u18",
    "u19",
    "u20",
    "u21",
    "u23",
    "youth",
    "reserve",
    "women",
    "ladies",
    "academy",
    "friendly",
)

_CACHE: Dict[str, tuple] = {}
_LOCK = threading.Lock()


def _now_ts() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


def _cached(key: str, loader):
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and _now_ts() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    value = loader()
    with _LOCK:
        _CACHE[key] = (_now_ts(), value)
    return value


def _get_json(url: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("provider payload is not a JSON object")
    return payload


def season_for(league_id: Any, date_str: Any) -> Optional[int]:
    """Season a fixture belongs to, using the declared competition window."""
    try:
        league = int(league_id)
        day = _dt.date.fromisoformat(str(date_str)[:10])
    except (TypeError, ValueError):
        return None
    window = (getattr(config, "COMPETITION_SEASON_WINDOWS", {}) or {}).get(("football", league)) or {}
    start_month = (window.get("start") or (0, 8, 1))[1]
    end_year_offset = (window.get("end") or (1, 6, 15))[0]
    if end_year_offset == 0:
        # Single-calendar-year tournament (World Cup, Euros).
        return day.year
    return day.year if day.month >= start_month else day.year - 1


def _neighbour_dates(kickoff: Any) -> List[str]:
    try:
        day = _dt.date.fromisoformat(str(kickoff)[:10])
    except (TypeError, ValueError):
        return []
    return [(day + _dt.timedelta(days=offset)).isoformat() for offset in (-1, 0, 1)]


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# ESPN soccer scoreboard
# ---------------------------------------------------------------------------

def _espn_status(event: Dict[str, Any]) -> str:
    status = event.get("status") or {}
    kind = status.get("type") or {}
    state = str(kind.get("state") or "").lower()
    detail = str(kind.get("detail") or "").upper()
    name = str(kind.get("name") or "").upper()
    if state == "post":
        if "PEN" in detail or "PEN" in name:
            return "PEN"
        if "AET" in detail or "AET" in name or "OT" in detail:
            return "AET"
        return "FT"
    if state == "in":
        return "LIVE"
    return "NS"


def _espn_event_to_fixture(event: Dict[str, Any], league_id: int) -> Optional[Dict[str, Any]]:
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    competition = competitions[0]
    sides: Dict[str, Dict[str, Any]] = {}
    for competitor in competition.get("competitors") or []:
        side = competitor.get("homeAway")
        if side in ("home", "away"):
            sides[side] = competitor
    home, away = sides.get("home"), sides.get("away")
    if not home or not away:
        return None

    def participant(competitor: Dict[str, Any]) -> Dict[str, Any]:
        team = competitor.get("team") or {}
        return {
            "id": _int_or_none(team.get("id")),
            "name": team.get("displayName") or team.get("name"),
            "score": _int_or_none(competitor.get("score")),
        }

    home_side, away_side = participant(home), participant(away)
    fixture_id = _int_or_none(event.get("id"))
    if (
        fixture_id is None
        or not home_side["id"]
        or not away_side["id"]
        or not home_side["name"]
        or not away_side["name"]
    ):
        return None

    date = event.get("date")
    season = _int_or_none((event.get("season") or {}).get("year")) or season_for(league_id, date)
    if season is None:
        return None
    status_obj = event.get("status") or {}
    return {
        "fixture": {
            "id": fixture_id,
            "date": date,
            "status": {
                "short": _espn_status(event),
                "long": ((status_obj.get("type") or {}).get("description")),
                "elapsed": None,
            },
        },
        "league": {"id": int(league_id), "season": int(season), "name": None},
        "teams": {
            "home": {"id": home_side["id"], "name": home_side["name"]},
            "away": {"id": away_side["id"], "name": away_side["name"]},
        },
        "goals": {"home": home_side["score"], "away": away_side["score"]},
        "provider_provenance": {
            "provider": "espn_soccer",
            "provider_fixture_id": str(fixture_id),
            "parser_version": "espn_soccer.live.v1",
            "endpoint": "scoreboard",
        },
    }


def _espn_scoreboard(slug: str, date_str: str) -> Dict[str, Any]:
    date = str(date_str)[:10].replace("-", "")

    def load() -> Dict[str, Any]:
        return _get_json(f"{ESPN_SITE}/{slug}/scoreboard?dates={date}")

    return _cached(f"espn:{slug}:{date}", load)


def espn_soccer_fixtures(date_str: str, league_ids: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Every ESPN soccer fixture listed for one UTC date, mapped to our shape."""
    allowed = {int(item) for item in league_ids} if league_ids is not None else None
    fixtures: List[Dict[str, Any]] = []
    first_error: Optional[Exception] = None
    for league_id, slug in ESPN_LEAGUES.items():
        if allowed is not None and league_id not in allowed:
            continue
        try:
            payload = _espn_scoreboard(slug, date_str)
        except Exception as exc:  # network/parse failure for this competition
            first_error = first_error or exc
            continue
        for event in payload.get("events") or []:
            fixture = _espn_event_to_fixture(event, league_id)
            if fixture is not None:
                fixtures.append(fixture)
    if first_error is not None and not fixtures:
        raise RuntimeError(f"espn_soccer scoreboard error: {type(first_error).__name__}: {first_error}")
    return fixtures


def espn_soccer_fixture(league_id: Any, fixture_id: Any, kickoff: Any) -> Optional[Dict[str, Any]]:
    """Look one ESPN fixture up by its provider-native event id."""
    try:
        league = int(league_id)
    except (TypeError, ValueError):
        return None
    slug = ESPN_LEAGUES.get(league)
    if not slug:
        return None
    for day in _neighbour_dates(kickoff) or [str(kickoff)[:10]]:
        try:
            payload = _espn_scoreboard(slug, day)
        except Exception:
            continue
        for event in payload.get("events") or []:
            if str(event.get("id")) != str(fixture_id):
                continue
            fixture = _espn_event_to_fixture(event, league)
            if fixture is not None:
                return fixture
    return None


# ---------------------------------------------------------------------------
# FotMob day listing
# ---------------------------------------------------------------------------

def _normalize_league_name(name: Any) -> str:
    return " ".join(str(name or "").lower().replace("-", " ").split())


def fotmob_league_id(name: Any) -> Optional[int]:
    normalized = _normalize_league_name(name)
    if not normalized or any(marker in normalized for marker in NON_SENIOR_MARKERS):
        return None
    if normalized in FOTMOB_LEAGUE_ALIASES:
        return FOTMOB_LEAGUE_ALIASES[normalized]
    mapped = getattr(config, "LEAGUE_NAME_TO_ID", {}).get(normalized)
    return int(mapped) if mapped is not None else None


def _fotmob_status(match: Dict[str, Any]) -> str:
    status = match.get("status") or {}
    reason = status.get("reason") if isinstance(status.get("reason"), dict) else {}
    short = str(reason.get("short") or "").upper()
    if status.get("cancelled") or status.get("awarded") and not status.get("finished"):
        return "CANC"
    if status.get("finished"):
        if short in ("PEN", "PENS", "AP") or "PEN" in short:
            return "PEN"
        if short in ("AET", "ET"):
            return "AET"
        return "FT"
    if status.get("started"):
        return "LIVE"
    return "NS"


def _fotmob_match_to_fixture(match: Dict[str, Any], league_id: int) -> Optional[Dict[str, Any]]:
    home = match.get("home") or {}
    away = match.get("away") or {}
    fixture_id = _int_or_none(match.get("id"))
    date = (match.get("status") or {}).get("utcTime")
    if fixture_id is None or not date:
        return None
    home_id = _int_or_none(home.get("id"))
    away_id = _int_or_none(away.get("id"))
    home_name, away_name = home.get("name"), away.get("name")
    if not home_id or not away_id or not home_name or not away_name:
        return None
    season = season_for(league_id, date)
    if season is None:
        return None
    return {
        "fixture": {
            "id": fixture_id,
            "date": date,
            "status": {
                "short": _fotmob_status(match),
                "long": ((match.get("status") or {}).get("reason") or {}).get("long")
                if isinstance((match.get("status") or {}).get("reason"), dict)
                else None,
                "elapsed": None,
            },
        },
        "league": {"id": int(league_id), "season": int(season), "name": None},
        "teams": {
            "home": {"id": home_id, "name": home_name},
            "away": {"id": away_id, "name": away_name},
        },
        "goals": {"home": _int_or_none(home.get("score")), "away": _int_or_none(away.get("score"))},
        "provider_provenance": {
            "provider": "fotmob",
            "provider_fixture_id": str(fixture_id),
            "parser_version": "fotmob.live.v1",
            "endpoint": "matches_by_date",
        },
    }


def _fotmob_day(date_str: str) -> Dict[str, Any]:
    compact = str(date_str)[:10].replace("-", "")

    def load() -> Dict[str, Any]:
        return _get_json(FOTMOB_DAY.format(date=compact))

    return _cached(f"fotmob:{compact}", load)


def fotmob_fixtures(date_str: str, league_ids: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Every configured-competition FotMob match for one UTC date."""
    allowed = {int(item) for item in league_ids} if league_ids is not None else None
    payload = _fotmob_day(date_str)
    fixtures: List[Dict[str, Any]] = []
    for league in payload.get("leagues") or []:
        league_id = fotmob_league_id(league.get("name"))
        if league_id is None:
            continue
        if allowed is not None and league_id not in allowed:
            continue
        for match in league.get("matches") or []:
            fixture = _fotmob_match_to_fixture(match, league_id)
            if fixture is not None:
                fixtures.append(fixture)
    return fixtures


def fotmob_fixture(fixture_id: Any, kickoff: Any) -> Optional[Dict[str, Any]]:
    """Look one FotMob match up by its provider-native match id."""
    for day in _neighbour_dates(kickoff) or [str(kickoff)[:10]]:
        try:
            payload = _fotmob_day(day)
        except Exception:
            continue
        for league in payload.get("leagues") or []:
            for match in league.get("matches") or []:
                if str(match.get("id")) != str(fixture_id):
                    continue
                league_id = fotmob_league_id(league.get("name"))
                if league_id is None:
                    return None
                return _fotmob_match_to_fixture(match, league_id)
    return None
