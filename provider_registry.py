"""
Authoritative provider capability registry and field-level routing.

Every entry below records only capabilities that the repository already
integrates and therefore can call. A provider is never advertised as
live-capable or final-result-capable merely because a historical endpoint
exists: the ``capabilities`` mapping is authoritative and unverified
capabilities are excluded from routing.

Routing is field-level. For a requested field the registry returns the
eligible providers ordered by lifecycle stage, verification state, health,
freshness limit and competition coverage. Conflicting observations are never
averaged; callers record every observation with its provenance.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Iterable, List, Optional

FOOTBALL_COMPETITIONS = [39, 140, 135, 78, 61, 2, 3, 5, 1, 4, 88, 94]
BASKETBALL_COMPETITIONS = [12]

LIFECYCLE_CONTEXTS = ("discovery", "prematch", "live", "final_result")

# Capability states. Only VERIFIED entries may be routed to.
VERIFIED = "verified"
UNVERIFIED = "unverified"
UNAVAILABLE = "unavailable"
HISTORICAL_ONLY = "historical_only"

# Maximum age (minutes) of an observation for a field, by lifecycle context.
# A live score/status must be much fresher than pre-match statistics.
FRESHNESS_LIMITS_MINUTES = {
    ("live", "status"): 15,
    ("live", "score"): 15,
    ("live", "clock"): 15,
    ("live", "statistics"): 20,
    ("final_result", "status"): 72 * 60,
    ("final_result", "score"): 72 * 60,
    ("final_result", "statistics"): 72 * 60,
    ("prematch", "statistics"): 24 * 60,
    ("prematch", "form"): 24 * 60,
    ("prematch", "h2h"): 48 * 60,
    ("prematch", "standings"): 24 * 60,
    ("discovery", "fixtures"): 24 * 60,
}

_FOOTBALL_FIELDS = [
    "fixtures",
    "status",
    "score",
    "clock",
    "statistics",
    "form",
    "h2h",
    "standings",
]


def _entry(
    name: str,
    sport: str,
    competitions: Iterable[int],
    base_url: str,
    endpoints: Dict[str, str],
    capabilities: Dict[str, str],
    fields: Iterable[str],
    auth_env: Optional[str],
    rate_limit: Optional[str],
    id_namespace: str,
    parser_version: str,
    provenance: str,
    freshness_minutes: int,
    limitations: Iterable[str] = (),
) -> Dict[str, Any]:
    return {
        "provider": name,
        "sport": sport,
        "competitions": sorted(competitions),
        "base_url": base_url,
        "endpoints": dict(endpoints),
        "capabilities": dict(capabilities),
        "fields": sorted(fields),
        "auth_env": auth_env,
        "rate_limit": rate_limit,
        "provider_id_namespace": id_namespace,
        "parser_version": parser_version,
        "provenance": provenance,
        "freshness_minutes": freshness_minutes,
        "known_limitations": list(limitations),
        "health": "unknown",
        "last_success_at": None,
        "last_failure_at": None,
        "last_failure_category": None,
    }


PROVIDERS: Dict[str, Dict[str, Any]] = {
    # ---------------------------------------------------------------- football
    "api_football": _entry(
        name="api_football",
        sport="football",
        competitions=FOOTBALL_COMPETITIONS,
        base_url="https://v3.football.api-sports.io",
        endpoints={
            "fixtures_by_date": "/fixtures",
            "live_fixtures": "/fixtures?live=all",
            "fixture_result": "/fixtures",
            "team_statistics": "/teams/statistics",
            "head_to_head": "/fixtures/headtohead",
            "recent_form": "/fixtures",
            "standings": "/standings",
        },
        capabilities={
            "discovery": VERIFIED,
            "prematch": VERIFIED,
            "live": VERIFIED,
            "final_result": VERIFIED,
        },
        fields=_FOOTBALL_FIELDS,
        auth_env="API_FOOTBALL_KEY",
        rate_limit="config.API_FOOTBALL_DAILY_CREDIT_LIMIT per day",
        id_namespace="api_football",
        parser_version="api_football.v1",
        provenance="api_football",
        freshness_minutes=15,
        limitations=[
            "Statistics are supplied only for fixtures the provider covers.",
            "Historical budget is a separate, smaller quota pool.",
        ],
    ),
    "espn_soccer": _entry(
        name="espn_soccer",
        sport="football",
        competitions=FOOTBALL_COMPETITIONS,
        base_url="https://site.api.espn.com/apis/site/v2/sports/soccer/",
        endpoints={
            "scoreboard": "/scoreboard?dates={YYYYMMDD}",
            "match_summary": "/summary?event={event_id}",
        },
        capabilities={
            "discovery": VERIFIED,
            "prematch": VERIFIED,
            "live": UNVERIFIED,
            "final_result": UNVERIFIED,
        },
        fields=["fixtures", "status", "score", "statistics"],
        auth_env=None,
        rate_limit="politeness limited; cached",
        id_namespace="espn",
        parser_version="espn_soccer.v1",
        provenance="espn_soccer",
        freshness_minutes=30,
        limitations=[
            "Existing parser extracts possession, shots, shots on target, corners and cards.",
            "xG is not established by the existing parser.",
            "Scoreboard presence is not final-result evidence until verified.",
        ],
    ),
    "fotmob": _entry(
        name="fotmob",
        sport="football",
        competitions=FOOTBALL_COMPETITIONS,
        base_url="https://www.fotmob.com/api/data",
        endpoints={
            "matches_by_date": "/matches?date={YYYYMMDD}",
            "match_details": "/matchDetails?matchId={match_id}",
        },
        capabilities={
            "discovery": VERIFIED,
            "prematch": VERIFIED,
            "live": UNVERIFIED,
            "final_result": UNVERIFIED,
        },
        fields=["fixtures", "statistics"],
        auth_env=None,
        rate_limit="politeness limited; cached provider listing per day",
        id_namespace="fotmob",
        parser_version="fotmob_enrichment.v1",
        provenance="fotmob",
        freshness_minutes=60,
        limitations=[
            "Supplies possession, xG, shots, shots on target, corners and cards when present.",
            "Not every match or season supplies xG; absence is recorded, never inferred.",
        ],
    ),
    # --------------------------------------------------------------- basketball
    "api_basketball": _entry(
        name="api_basketball",
        sport="basketball",
        competitions=BASKETBALL_COMPETITIONS,
        base_url="https://v1.basketball.api-sports.io",
        endpoints={
            "games_by_date": "/games?date={YYYY-MM-DD}",
            "live_games": "/games?live=all",
            "game_lookup": "/games?id={game_id}",
            "team_statistics": "/statistics?team={team_id}&league={league_id}&season={season}",
        },
        capabilities={
            "discovery": VERIFIED,
            "prematch": VERIFIED,
            "live": VERIFIED,
            "final_result": VERIFIED,
        },
        fields=["fixtures", "status", "score", "clock", "statistics"],
        auth_env="API_BASKETBALL_KEY",
        rate_limit="config.API_BASKETBALL_DAILY_CREDIT_LIMIT per day",
        id_namespace="api_basketball",
        parser_version="basketball_api.v1",
        provenance="api_basketball",
        freshness_minutes=15,
    ),
    "espn_nba": _entry(
        name="espn_nba",
        sport="basketball",
        competitions=BASKETBALL_COMPETITIONS,
        base_url="https://site.api.espn.com/apis/site/v2/sports/basketball/nba",
        endpoints={"scoreboard": "/scoreboard"},
        capabilities={
            "discovery": VERIFIED,
            "prematch": UNVERIFIED,
            "live": UNVERIFIED,
            "final_result": UNVERIFIED,
        },
        fields=["fixtures", "status", "score"],
        auth_env=None,
        rate_limit="politeness limited; cached",
        id_namespace="espn",
        parser_version="espn_nba.v1",
        provenance="espn_nba",
        freshness_minutes=30,
        limitations=[
            "Existing integration is the historical basketball acquisition route.",
            "Live and final-result use is unverified and therefore not routed.",
        ],
    ),
    # -------------------------------------------------- historical-only sources
    "football_data_csv": _entry(
        name="football_data_csv",
        sport="football",
        competitions=FOOTBALL_COMPETITIONS,
        base_url="https://www.football-data.co.uk/mmz4281",
        endpoints={"season_csv": "/{season_code}/{division_code}.csv"},
        capabilities={
            "discovery": HISTORICAL_ONLY,
            "prematch": HISTORICAL_ONLY,
            "live": HISTORICAL_ONLY,
            "final_result": HISTORICAL_ONLY,
        },
        fields=["fixtures", "statistics"],
        auth_env=None,
        rate_limit="static files",
        id_namespace="football_data_csv",
        parser_version="football_csv.v1",
        provenance="football_data_csv",
        freshness_minutes=0,
        limitations=["Historical results only; never a live observation."],
    ),
    "openfootball": _entry(
        name="openfootball",
        sport="football",
        competitions=FOOTBALL_COMPETITIONS,
        base_url="https://raw.githubusercontent.com/openfootball",
        endpoints={"json": "/football.json/master/{season}/{competition_file}", "txt": "/{repository}/master/{path}"},
        capabilities={
            "discovery": HISTORICAL_ONLY,
            "prematch": HISTORICAL_ONLY,
            "live": HISTORICAL_ONLY,
            "final_result": HISTORICAL_ONLY,
        },
        fields=["fixtures"],
        auth_env=None,
        rate_limit="static files",
        id_namespace="openfootball",
        parser_version="openfootball.v1",
        provenance="openfootball",
        freshness_minutes=0,
        limitations=["Historical fixtures and results only."],
    ),
    "openligadb": _entry(
        name="openligadb",
        sport="football",
        competitions=[78],
        base_url="https://api.openligadb.de",
        endpoints={"matchdata": "/getmatchdata/{league}/{season}"},
        capabilities={
            "discovery": HISTORICAL_ONLY,
            "prematch": HISTORICAL_ONLY,
            "live": HISTORICAL_ONLY,
            "final_result": HISTORICAL_ONLY,
        },
        fields=["fixtures"],
        auth_env=None,
        rate_limit="politeness limited",
        id_namespace="openligadb",
        parser_version="openligadb.v1",
        provenance="openligadb",
        freshness_minutes=0,
        limitations=["Coverage is limited to the Bundesliga competition it serves."],
    ),
    "fbref_soccerdata": _entry(
        name="fbref_soccerdata",
        sport="football",
        competitions=FOOTBALL_COMPETITIONS,
        base_url="https://fbref.com/",
        endpoints={},
        capabilities={
            "discovery": HISTORICAL_ONLY,
            "prematch": HISTORICAL_ONLY,
            "live": HISTORICAL_ONLY,
            "final_result": HISTORICAL_ONLY,
        },
        fields=["statistics"],
        auth_env=None,
        rate_limit="not routable",
        id_namespace="fbref",
        parser_version="soccerdata.v1",
        provenance="fbref",
        freshness_minutes=0,
        limitations=[
            "Automated access previously returned HTTP 403; access controls are never bypassed.",
            "Historical data must never be presented as a live observation.",
        ],
    ),
    "sofascore": _entry(
        name="sofascore",
        sport="football",
        competitions=FOOTBALL_COMPETITIONS,
        base_url="https://api.sofascore.com/api/v1/",
        endpoints={},
        capabilities={
            "discovery": UNAVAILABLE,
            "prematch": UNAVAILABLE,
            "live": UNAVAILABLE,
            "final_result": UNAVAILABLE,
        },
        fields=[],
        auth_env=None,
        rate_limit="not routable",
        id_namespace="sofascore",
        parser_version="none",
        provenance="sofascore",
        freshness_minutes=0,
        limitations=[
            "Earlier automated requests returned HTTP 403; treated as unavailable.",
            "Access controls are never bypassed and endpoints are never invented.",
        ],
    ),
}


def get_provider(name: str) -> Optional[Dict[str, Any]]:
    """Return a provider entry by name."""
    if not isinstance(name, str):
        return None
    return PROVIDERS.get(name.strip().lower())


def list_providers(sport: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return provider entries, optionally filtered by sport."""
    entries = list(PROVIDERS.values())
    if sport:
        sport_clean = str(sport).lower()
        entries = [e for e in entries if e["sport"] == sport_clean]
    return entries


def supports(
    provider_name: str,
    context: str,
    field: Optional[str] = None,
    league_id: Optional[int] = None,
) -> bool:
    """True only when the capability is verified and the competition is covered."""
    entry = get_provider(provider_name)
    if entry is None:
        return False
    if context not in LIFECYCLE_CONTEXTS:
        return False
    if entry["capabilities"].get(context) != VERIFIED:
        return False
    if field is not None and field not in entry["fields"]:
        return False
    if league_id is not None and int(league_id) not in entry["competitions"]:
        return False
    return True


def freshness_limit_minutes(context: str, field: Optional[str]) -> Optional[int]:
    """Maximum acceptable observation age for a field in a lifecycle context."""
    return FRESHNESS_LIMITS_MINUTES.get((context, field))


def record_health(
    provider_name: str,
    *,
    ok: bool,
    failure_category: Optional[str] = None,
    observed_at: Optional[str] = None,
) -> None:
    """Record a health observation for a provider in memory for this process."""
    entry = get_provider(provider_name)
    if entry is None:
        return
    stamp = observed_at or _dt.datetime.now(_dt.timezone.utc).isoformat()
    if ok:
        entry["health"] = "healthy"
        entry["last_success_at"] = stamp
        entry["last_failure_category"] = None
    else:
        entry["health"] = "failing"
        entry["last_failure_at"] = stamp
        entry["last_failure_category"] = failure_category or "unknown"


def route_field(
    sport: str,
    field: str,
    context: str,
    league_id: Optional[int] = None,
    avoid: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Return eligible providers for one field, best first.

    Only verified, competition-covered capabilities are returned. Providers
    marked failing are demoted behind healthy ones but remain available as
    fallbacks; unavailable and historical-only providers are never returned
    for a current observation.
    """
    skip = {str(a).lower() for a in (avoid or [])}
    eligible: List[Dict[str, Any]] = []
    for entry in list_providers(sport):
        if entry["provider"] in skip:
            continue
        if not supports(entry["provider"], context, field=field, league_id=league_id):
            continue
        eligible.append(entry)

    def _sort_key(entry: Dict[str, Any]):
        health_rank = 0 if entry["health"] in ("healthy", "unknown") else 1
        return (health_rank, entry["freshness_minutes"], entry["provider"])

    return sorted(eligible, key=_sort_key)


def capabilities_snapshot(sport: Optional[str] = None) -> Dict[str, Any]:
    """Serialisable snapshot of the registry, used by diagnostics and workflows."""
    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "providers": [
            {
                k: v
                for k, v in entry.items()
            }
            for entry in list_providers(sport)
        ],
    }
