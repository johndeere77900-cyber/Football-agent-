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

# FotMob publishes several different competitions under the same name (its
# "Premier League" label covers England, Russia, Ukraine, Belarus, Wales,
# Azerbaijan, Bosnia, Armenia, Singapore and Tanzania). The country code on the
# listing disambiguates them, so a name that maps to a configured competition is
# only accepted when the listing's country matches that competition.
FOTMOB_LEAGUE_COUNTRIES = {
    39: {"ENG"},
    140: {"ESP"},
    135: {"ITA"},
    78: {"GER"},
    61: {"FRA"},
    88: {"NED"},
    94: {"POR"},
    2: {"INT", "UEFA", "EUR", "WORLD"},
    3: {"INT", "UEFA", "EUR", "WORLD"},
    5: {"INT", "UEFA", "EUR", "WORLD"},
    1: {"INT", "WORLD"},
    4: {"INT", "UEFA", "EUR", "WORLD"},
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


def configured_league_name(league_id: Any) -> Optional[str]:
    """Canonical name for a configured competition.

    The prediction path rejects a fixture whose league identity has no name, so
    a keyless discovery adapter must supply the configured competition's name
    instead of a provider label or None.
    """
    try:
        wanted = int(league_id)
    except (TypeError, ValueError):
        return None
    for name, mapped in (getattr(config, "LEAGUE_NAME_TO_ID", {}) or {}).items():
        try:
            if int(mapped) == wanted:
                return str(name).title()
        except (TypeError, ValueError):
            continue
    return None


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
        "league": {
            "id": int(league_id),
            "season": int(season),
            "name": configured_league_name(league_id),
        },
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


def fotmob_league_id(name: Any, ccode: Any = None) -> Optional[int]:
    """Configured competition id for a FotMob league listing, or None.

    The name alone is ambiguous across countries, so a mapping is only trusted
    when the listing's country code matches the competition it maps to. A
    listing without a country code never establishes a competition identity.
    """
    normalized = _normalize_league_name(name)
    if not normalized or any(marker in normalized for marker in NON_SENIOR_MARKERS):
        return None
    if normalized in FOTMOB_LEAGUE_ALIASES:
        league_id = FOTMOB_LEAGUE_ALIASES[normalized]
    else:
        mapped = getattr(config, "LEAGUE_NAME_TO_ID", {}).get(normalized)
        league_id = int(mapped) if mapped is not None else None
    if league_id is None:
        return None
    expected = FOTMOB_LEAGUE_COUNTRIES.get(league_id)
    if expected is not None:
        country = str(ccode or "").strip().upper()
        if country not in expected:
            return None
    return league_id


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
        "league": {
            "id": int(league_id),
            "season": int(season),
            "name": configured_league_name(league_id),
        },
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
        league_id = fotmob_league_id(league.get("name"), league.get("ccode"))
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
                league_id = fotmob_league_id(league.get("name"), league.get("ccode"))
                if league_id is None:
                    return None
                return _fotmob_match_to_fixture(match, league_id)
    return None


# ---------------------------------------------------------------------------
# ESPN soccer team schedule (pre-match team history)
# ---------------------------------------------------------------------------
#
# The scoreboard supplies fixtures for one day; a pre-match team-statistics
# feature needs a team's season history. ESPN publishes the same events under
# the team schedule endpoint, in the same event namespace as the scoreboard, so
# the history and the fixture being predicted share one provider namespace.

ESPN_TEAM_SCHEDULE = "{site}/{slug}/teams/{team_id}/schedule?season={season}"


def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _espn_competition_status(competition: Dict[str, Any]) -> str:
    """Status short code from a competition object (team-schedule payloads)."""
    status = competition.get("status") if isinstance(competition.get("status"), dict) else {}
    kind = status.get("type") if isinstance(status.get("type"), dict) else {}
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


def _espn_competitor_score(competitor: Dict[str, Any]) -> Optional[int]:
    """ESPN scores appear either as a scalar or as a {"value": n} object."""
    score = competitor.get("score")
    if isinstance(score, dict):
        score = score.get("value")
    return _int_or_none(score)


def _espn_event_to_match(event: Dict[str, Any], league_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """One ESPN event mapped to the API-Football match shape used by features."""
    competitions = event.get("competitions") or []
    if not competitions or not isinstance(competitions[0], dict):
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

    def side(competitor: Dict[str, Any]) -> Dict[str, Any]:
        team = competitor.get("team") or {}
        return {
            "id": _int_or_none(team.get("id")),
            "name": team.get("displayName") or team.get("name"),
            "score": _espn_competitor_score(competitor),
        }

    home_side, away_side = side(home), side(away)
    event_id = _int_or_none(event.get("id"))
    date = event.get("date")
    if (
        event_id is None
        or not date
        or not home_side["id"]
        or not away_side["id"]
        or not home_side["name"]
        or not away_side["name"]
    ):
        return None

    status = _espn_competition_status(competition)
    season = _int_or_none((event.get("season") or {}).get("year")) or season_for(league_id, date)
    return {
        "fixture": {
            "id": event_id,
            "date": date,
            "status": {"short": status, "long": None, "elapsed": None},
        },
        "league": {
            "id": int(league_id) if league_id is not None else None,
            "season": int(season) if season is not None else None,
        },
        "teams": {
            "home": {"id": home_side["id"], "name": home_side["name"]},
            "away": {"id": away_side["id"], "name": away_side["name"]},
        },
        "goals": {
            "home": home_side["score"] if status in ("FT", "AET", "PEN") else None,
            "away": away_side["score"] if status in ("FT", "AET", "PEN") else None,
        },
        "provider_provenance": {
            "provider": "espn_soccer",
            "provider_fixture_id": str(event_id),
            "parser_version": "espn_soccer.team_history.v1",
            "endpoint": "team_schedule",
        },
    }


def espn_soccer_team_matches(league_id: Any, team_id: Any, season: Any) -> List[Dict[str, Any]]:
    """A team's ESPN season history, mapped to the match shape features use."""
    try:
        league = int(league_id)
        team = int(team_id)
        year = int(season)
    except (TypeError, ValueError):
        return []
    slug = ESPN_LEAGUES.get(league)
    if not slug:
        return []
    url = ESPN_TEAM_SCHEDULE.format(site=ESPN_SITE, slug=slug, team_id=team, season=year)

    def load() -> Dict[str, Any]:
        return _get_json(url)

    try:
        payload = _cached(f"espn:schedule:{slug}:{team}:{year}", load)
    except Exception:
        return []
    matches: List[Dict[str, Any]] = []
    for event in payload.get("events") or []:
        match = _espn_event_to_match(event, league)
        if match is not None:
            matches.append(match)
    return matches


# ---------------------------------------------------------------------------
# FotMob team fixtures (pre-match team history)
# ---------------------------------------------------------------------------

FOTMOB_TEAM = "https://www.fotmob.com/api/data/teams?id={team_id}"


def fotmob_team_matches(team_id: Any) -> List[Dict[str, Any]]:
    """A team's FotMob fixture list, mapped to the match shape features use."""
    try:
        team = int(team_id)
    except (TypeError, ValueError):
        return []

    def load() -> Dict[str, Any]:
        return _get_json(FOTMOB_TEAM.format(team_id=team))

    try:
        payload = _cached(f"fotmob:team:{team}", load)
    except Exception:
        return []

    fixtures = (((payload.get("fixtures") or {}).get("allFixtures") or {}).get("fixtures")) or []
    matches: List[Dict[str, Any]] = []
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue
        match = _fotmob_team_fixture(fixture)
        if match is not None:
            matches.append(match)
    return matches


def _fotmob_team_fixture(fixture: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One FotMob team fixture mapped to the match shape features use.

    The team endpoint does not identify the competition, so no competition or
    season identity is asserted here; only the fields the provider supplied.
    """
    home = fixture.get("home") or {}
    away = fixture.get("away") or {}
    fixture_id = _int_or_none(fixture.get("id"))
    status = fixture.get("status") or {}
    date = status.get("utcTime")
    home_id = _int_or_none(home.get("id"))
    away_id = _int_or_none(away.get("id"))
    home_name, away_name = home.get("name"), away.get("name")
    if (
        fixture_id is None
        or not date
        or not home_id
        or not away_id
        or not home_name
        or not away_name
    ):
        return None
    short = _fotmob_status(fixture)
    finished = short in ("FT", "AET", "PEN")
    return {
        "fixture": {
            "id": fixture_id,
            "date": date,
            "status": {"short": short, "long": None, "elapsed": None},
        },
        "league": {"id": None, "season": None},
        "teams": {
            "home": {"id": home_id, "name": home_name},
            "away": {"id": away_id, "name": away_name},
        },
        "goals": {
            "home": _int_or_none(home.get("score")) if finished else None,
            "away": _int_or_none(away.get("score")) if finished else None,
        },
        "provider_provenance": {
            "provider": "fotmob",
            "provider_fixture_id": str(fixture_id),
            "parser_version": "fotmob.team_history.v1",
            "endpoint": "team_fixtures",
        },
    }


def team_stats_from_matches(
    matches: List[Dict[str, Any]],
    team_id: Any,
    provider: str,
) -> Optional[Dict[str, Any]]:
    """Season goal averages derived from a provider's own match history.

    The aggregation only counts completed matches whose scores the provider
    actually supplied, and only matches where the team appears under the same
    provider-native id, so nothing is inferred or borrowed from another
    provider's namespace. Provenance records the provider, the team id and the
    number of matches used.
    """
    played = 0
    goals_for = 0
    goals_against = 0
    for match in matches or []:
        if not isinstance(match, dict):
            continue
        status = ((match.get("fixture") or {}).get("status") or {}).get("short")
        if status not in ("FT", "AET", "PEN"):
            continue
        goals = match.get("goals") or {}
        home_goals, away_goals = goals.get("home"), goals.get("away")
        if home_goals is None or away_goals is None:
            continue
        teams = match.get("teams") or {}
        home_id = (teams.get("home") or {}).get("id")
        away_id = (teams.get("away") or {}).get("id")
        if str(home_id) == str(team_id):
            goals_for += int(home_goals)
            goals_against += int(away_goals)
            played += 1
        elif str(away_id) == str(team_id):
            goals_for += int(away_goals)
            goals_against += int(home_goals)
            played += 1
    if played == 0:
        return None
    gf_avg = round(goals_for / played, 2)
    ga_avg = round(goals_against / played, 2)
    return {
        "fixtures": {"played": {"home": None, "away": None, "total": played}},
        "goals": {
            "for": {
                "total": {"home": None, "away": None, "total": goals_for},
                "average": {"home": None, "away": None, "total": str(gf_avg)},
            },
            "against": {
                "total": {"home": None, "away": None, "total": goals_against},
                "average": {"home": None, "away": None, "total": str(ga_avg)},
            },
        },
        "provider_provenance": {
            "provider": provider,
            "provider_type": "derived_team_history",
            "provider_team_id": str(team_id),
            "matches_used": played,
            "retrieved_at": _iso_now(),
        },
    }


# ---------------------------------------------------------------------------
# ESPN NBA scoreboard and team schedule
# ---------------------------------------------------------------------------
#
# ESPN's NBA scoreboard publishes the same event ids the historical acquisition
# route already consumes. The lifecycle adapter maps them to the API-Basketball
# game shape the model and the grading policy understand, while stamping the
# ESPN provider namespace so an ESPN event id is never mistaken for an
# API-Basketball game id.

ESPN_NBA_SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
NBA_LEAGUE_ID = 12


def basketball_season_for(date_str: Any) -> Optional[int]:
    """Start-year season label, matching the API-Basketball convention."""
    try:
        day = _dt.date.fromisoformat(str(date_str)[:10])
    except (TypeError, ValueError):
        return None
    return day.year if day.month >= 8 else day.year - 1


def _espn_nba_status(status: Dict[str, Any]) -> str:
    """Basketball status short code from an ESPN status object."""
    status = status if isinstance(status, dict) else {}
    kind = status.get("type") if isinstance(status.get("type"), dict) else {}
    name = str(kind.get("name") or "").upper()
    detail = str(kind.get("detail") or "").upper()
    if "POSTPONED" in name or "POSTPONED" in detail:
        return "PST"
    if "CANCELED" in name or "CANCELLED" in name or "CANCELED" in detail:
        return "CANC"
    if "SUSPENDED" in name or "ABANDONED" in name:
        return "ABD"
    state = str(kind.get("state") or "").lower()
    if state == "post" or kind.get("completed"):
        return "AOT" if ("OT" in detail or "OVERTIME" in detail) else "FT"
    if state == "in":
        period = _int_or_none(status.get("period"))
        if period is not None and 1 <= period <= 4:
            return f"Q{period}"
        return "OT"
    return "NS"


def _espn_nba_season_type(event: Dict[str, Any], competition: Dict[str, Any]) -> Optional[int]:
    season = event.get("season") or competition.get("season") or {}
    return _int_or_none((season.get("type")) if isinstance(season, dict) else None)


def _espn_nba_game(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One ESPN NBA scoreboard event mapped to the API-Basketball shape."""
    competitions = event.get("competitions") or []
    if not competitions or not isinstance(competitions[0], dict):
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
            "score": _espn_competitor_score(competitor),
        }

    home_side, away_side = participant(home), participant(away)
    game_id = _int_or_none(event.get("id"))
    date = event.get("date")
    season = basketball_season_for(date)
    if (
        game_id is None
        or not date
        or season is None
        or not home_side["id"]
        or not away_side["id"]
        or not home_side["name"]
        or not away_side["name"]
    ):
        return None
    status = _espn_nba_status(competition.get("status") or {})
    season_type = _espn_nba_season_type(event, competition)
    return {
        "id": game_id,
        "date": date,
        "status": {"short": status, "long": None, "timer": None},
        "league": {"id": NBA_LEAGUE_ID, "season": season, "name": "NBA"},
        "teams": {
            "home": {"id": home_side["id"], "name": home_side["name"]},
            "away": {"id": away_side["id"], "name": away_side["name"]},
        },
        "scores": {
            "home": {"total": home_side["score"]},
            "away": {"total": away_side["score"]},
        },
        "provider_provenance": {
            "provider": "espn_nba",
            "provider_fixture_id": str(game_id),
            "provider_team_ids": {"home": str(home_side["id"]), "away": str(away_side["id"])},
            "parser_version": "espn_nba.live.v1",
            "endpoint": "scoreboard",
            "espn_season_type": season_type,
            "espn_season_year": (event.get("season") or {}).get("year")
            if isinstance(event.get("season"), dict)
            else None,
        },
    }


def _espn_nba_scoreboard(date_str: str) -> Dict[str, Any]:
    compact = str(date_str)[:10].replace("-", "")

    def load() -> Dict[str, Any]:
        return _get_json(f"{ESPN_NBA_SITE}/scoreboard?dates={compact}")

    return _cached(f"espn_nba:{compact}", load)


def espn_nba_games(date_str: str) -> List[Dict[str, Any]]:
    """Every ESPN NBA game listed for one UTC date, mapped to our game shape."""
    payload = _espn_nba_scoreboard(date_str)
    games: List[Dict[str, Any]] = []
    for event in payload.get("events") or []:
        game = _espn_nba_game(event)
        if game is not None:
            games.append(game)
    return games


def espn_nba_game(event_id: Any, kickoff: Any) -> Optional[Dict[str, Any]]:
    """Look one ESPN NBA game up by its provider-native event id."""
    for day in _neighbour_dates(kickoff) or [str(kickoff)[:10]]:
        try:
            payload = _espn_nba_scoreboard(day)
        except Exception:
            continue
        for event in payload.get("events") or []:
            if str(event.get("id")) != str(event_id):
                continue
            return _espn_nba_game(event)
    return None


def espn_nba_team_scoring(team_id: Any, season: Any, before: Any) -> Optional[Dict[str, Any]]:
    """Season scoring averages for one NBA team, from ESPN's team schedule.

    Only completed games dated strictly before ``before`` are counted, so a
    pre-match feature is point-in-time. Averages are per game and are computed
    only from scores ESPN actually published.
    """
    try:
        team = int(team_id)
        start_season = int(season)
    except (TypeError, ValueError):
        return None
    cutoff = _dt.datetime.fromisoformat(str(before).replace("Z", "+00:00")) if before else None
    espn_year = start_season + 1  # ESPN labels an NBA season by its ending year

    def load() -> Dict[str, Any]:
        return _get_json(f"{ESPN_NBA_SITE}/teams/{team}/schedule?season={espn_year}")

    try:
        payload = _cached(f"espn_nba:schedule:{team}:{espn_year}", load)
    except Exception:
        return None

    played = 0
    points_for = 0
    points_against = 0
    for event in payload.get("events") or []:
        competitions = event.get("competitions") or []
        if not competitions or not isinstance(competitions[0], dict):
            continue
        competition = competitions[0]
        if _espn_nba_status(competition.get("status") or {}) not in ("FT", "AOT"):
            continue
        date = event.get("date")
        played_at = None
        if isinstance(date, str):
            try:
                played_at = _dt.datetime.fromisoformat(date.replace("Z", "+00:00"))
            except ValueError:
                played_at = None
        if played_at is None or (cutoff is not None and played_at >= cutoff):
            continue
        event_season = event.get("season") or {}
        if isinstance(event_season, dict) and _int_or_none(event_season.get("year")) not in (None, espn_year):
            continue

        scores: Dict[str, Optional[int]] = {}
        for competitor in competition.get("competitors") or []:
            side = competitor.get("homeAway")
            if side in ("home", "away"):
                scores[side] = _espn_competitor_score(competitor)
        if scores.get("home") is None or scores.get("away") is None:
            continue

        own = None
        opponent = None
        for competitor in competition.get("competitors") or []:
            team_obj = competitor.get("team") or {}
            if _int_or_none(team_obj.get("id")) == team:
                own = competitor.get("homeAway")
                opponent = "away" if own == "home" else "home"
                break
        if own is None or opponent is None:
            continue
        points_for += int(scores[own])
        points_against += int(scores[opponent])
        played += 1

    if played == 0:
        return None
    return {
        "points": {
            "for": {"average": {"all": round(points_for / played, 2)}},
            "against": {"average": {"all": round(points_against / played, 2)}},
        },
        # ``matches`` is the sample-count key basketball_model and the quality
        # gate read; ``games_played`` states the same count explicitly.
        "matches": played,
        "games_played": played,
        "provider_provenance": {
            "provider": "espn_nba",
            "provider_type": "derived_team_schedule",
            "provider_team_id": str(team),
            "games_used": played,
            "espn_season_year": espn_year,
            "retrieved_at": _iso_now(),
        },
    }
