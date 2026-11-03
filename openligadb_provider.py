"""
OpenLigaDB historical acquisition adapter.

The API provides season discovery and native match ids. Only competitions whose
exact shortcut is returned by discovery are used; no shortcut or season is
assumed to exist.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

import team_identity
import time_utils


PROVIDER = "openligadb"
API_BASE = "https://api.openligadb.de"
DOCUMENTATION_URL = "https://api.openligadb.de/index.html"
REPOSITORY = "https://github.com/OpenLigaDB/OpenLigaDB-Samples"
RATE_LIMIT_PER_MINUTE = 60

# Only the verified first Bundesliga is enabled. The exact shortcut is read from
# discovery; this value is the expected shortcut, never a substitute for it.
LEAGUE_MAPPINGS = {
    78: {
        "name": "Bundesliga",
        "expected_shortcut": "bl1",
        "name_tokens": ("1. fussball-bundesliga", "1. fußball-bundesliga"),
    },
}

_DISCOVERY_CACHE: dict[int, tuple[str, list[dict[str, Any]], dict[str, Any]]] = {}
_RATE_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0


def _utc_now() -> str:
    return time_utils.format_utc_iso(datetime.now(timezone.utc))


def _rate_limit() -> None:
    """Respect the documented 60 requests/minute/IP limit."""
    global _LAST_REQUEST_AT
    minimum_interval = 60.0 / RATE_LIMIT_PER_MINUTE
    with _RATE_LOCK:
        elapsed = time.monotonic() - _LAST_REQUEST_AT
        wait_for = minimum_interval - elapsed
        if wait_for > 0:
            time.sleep(wait_for)
        _LAST_REQUEST_AT = time.monotonic()


def _get_json(url: str, timeout: float = 20.0) -> tuple[str, Any, dict[str, Any]]:
    _rate_limit()
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Football-agent-historical/1.0",
            "Accept": "application/json",
        },
    )
    meta: dict[str, Any] = {
        "source_url": url,
        "retrieved_at": _utc_now(),
        "provider": PROVIDER,
    }
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = int(getattr(response, "status", 200))
            body = response.read().decode("utf-8-sig", errors="replace")
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        meta.update({"http_status": status_code, "error": f"HTTP {status_code}"})
        if status_code in (401, 403):
            return "ACCESS_DENIED", None, {**meta, "error_category": "access_restricted"}
        if status_code == 404:
            return "UNSUPPORTED_COVERAGE", None, {**meta, "error_category": "unsupported_coverage"}
        if status_code == 429:
            return "RATE_LIMITED", None, {**meta, "error_category": "rate_limited"}
        return "SOURCE_FAILED", None, {**meta, "error_category": "network_error"}
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        text = str(reason).lower()
        category = "timeout" if ("timeout" in text or "timed out" in text) else "network_error"
        status = "TIMEOUT" if category == "timeout" else "SOURCE_FAILED"
        return status, None, {**meta, "error": str(reason), "error_category": category}

    meta["http_status"] = status_code
    if status_code != 200:
        return "SOURCE_FAILED", None, {**meta, "error_category": "network_error"}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return "PARSER_ERROR", None, {
            **meta,
            "error_category": "parsing_error",
            "error": f"invalid JSON: {exc}",
        }
    return "SOURCE_AVAILABLE", payload, meta


def _discover_league(
    league_id: int,
    season: int,
    *,
    timeout: float,
) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    cache_key = int(season)
    cached = _DISCOVERY_CACHE.get(cache_key)
    if cached and cached[0] == "SOURCE_AVAILABLE":
        status, leagues, meta = cached
        return status, _select_league(leagues, league_id), meta

    url = f"{API_BASE}/getavailableleagues/{int(season)}"
    status, payload, meta = _get_json(url, timeout=timeout)
    meta.update({"source": PROVIDER, "secret_required": False, "operation": "season_discovery"})
    if status != "SOURCE_AVAILABLE":
        return status, None, meta
    if not isinstance(payload, list):
        return "PARSER_ERROR", None, {
            **meta,
            "status": "PARSER_ERROR",
            "error_category": "parsing_error",
            "error": "season discovery response is not a list",
        }
    if not payload:
        return "SOURCE_EMPTY", None, {
            **meta,
            "status": "SOURCE_EMPTY",
            "error": "empty league discovery is not acquisition success",
        }
    _DISCOVERY_CACHE[cache_key] = ("SOURCE_AVAILABLE", payload, meta)
    return status, _select_league(payload, league_id), meta


def _select_league(leagues: list[dict[str, Any]], league_id: int) -> dict[str, Any] | None:
    mapping = LEAGUE_MAPPINGS.get(int(league_id))
    if not mapping:
        return None
    expected = str(mapping["expected_shortcut"]).strip()
    for league in leagues:
        if not isinstance(league, dict):
            continue
        sport = league.get("sport") if isinstance(league.get("sport"), dict) else {}
        sport_name = str(sport.get("sportName") or "").strip().lower()
        if sport_name and "fußball" not in sport_name and "fussball" not in sport_name:
            continue
        if str(league.get("leagueShortcut") or "").strip() != expected:
            continue
        name = str(league.get("leagueName") or "").strip().lower()
        if "bundesliga" not in name:
            continue
        if mapping.get("name_tokens") and not any(token in name for token in mapping["name_tokens"]):
            continue
        return league
    return None


def _parse_utc(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _number(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _select_fulltime_result(match: dict[str, Any]) -> dict[str, Any] | None:
    results = match.get("matchResults")
    if not isinstance(results, list):
        return None
    valid = [row for row in results if isinstance(row, dict)]
    if not valid:
        return None

    def result_kind(row: dict[str, Any]) -> str:
        return str(row.get("resultTypeKind") or "").strip().lower()

    preferred = (
        "after90minutes",
        "after90",
        "endergebnis",
        "fulltime",
        "final",
    )
    for kind in preferred:
        for row in valid:
            if result_kind(row) == kind:
                return row
    for row in valid:
        if row.get("resultTypeID") == 2:
            return row
    return max(
        valid,
        key=lambda row: (
            int(row.get("resultOrderID") or 0)
            if str(row.get("resultOrderID") or "0").lstrip("-").isdigit()
            else 0
        ),
    )


def _select_halftime_result(match: dict[str, Any]) -> dict[str, Any] | None:
    """Return only the halftime result row; the fulltime row is never reused."""
    results = match.get("matchResults")
    if not isinstance(results, list):
        return None
    for row in results:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("resultTypeKind") or "").strip().lower()
        if kind in {"halftime", "half-time", "ht"} or row.get("resultTypeID") == 1:
            return row
    return None


def _resolve_team(
    name: str,
    provider_team_id: int,
    league_id: int,
    cache: dict[str, str | None],
) -> str | None:
    if name in cache:
        return cache[name]
    canonical = team_identity.bootstrap_historical_team_identity(
        raw_name=name,
        provider=PROVIDER,
        provider_team_id=provider_team_id,
        league_id=league_id,
        sport="football",
    )
    cache[name] = canonical
    return canonical


def get_games(
    league_id: int,
    season: int,
    *,
    timeout: float = 20.0,
    completed_only: bool = True,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Discover the exact league shortcut and fetch one season's match data."""
    if int(league_id) not in LEAGUE_MAPPINGS:
        return "UNSUPPORTED_COVERAGE", [], {
            "status": "UNSUPPORTED_COVERAGE",
            "error_category": "unsupported_coverage",
            "reason": "no_openligadb_mapping",
            "source": PROVIDER,
        }
    discover_status, league, discovery_meta = _discover_league(
        int(league_id), int(season), timeout=timeout
    )
    if discover_status != "SOURCE_AVAILABLE":
        return discover_status, [], {
            **discovery_meta,
            "status": discover_status,
            "source": PROVIDER,
        }
    if not league:
        return "UNSUPPORTED_COVERAGE", [], {
            **discovery_meta,
            "status": "UNSUPPORTED_COVERAGE",
            "error_category": "unsupported_coverage",
            "reason": "exact_league_shortcut_not_returned_for_season",
            "source": PROVIDER,
            "requested_season": int(season),
        }
    shortcut = str(league.get("leagueShortcut") or "").strip()
    if not shortcut:
        return "PARSER_ERROR", [], {
            **discovery_meta,
            "status": "PARSER_ERROR",
            "error_category": "parsing_error",
            "error": "discovered league has no shortcut",
            "source": PROVIDER,
        }

    url = f"{API_BASE}/getmatchdata/{shortcut}/{int(season)}"
    fetch_status, payload, meta = _get_json(url, timeout=timeout)
    meta.update(
        {
            "source": PROVIDER,
            "repository": REPOSITORY,
            "documentation_url": DOCUMENTATION_URL,
            "secret_required": False,
            "operation": "match_data",
            "requested_season": int(season),
            "league_shortcut": shortcut,
            "discovered_league": league,
        }
    )
    if fetch_status != "SOURCE_AVAILABLE":
        return fetch_status, [], meta
    if not isinstance(payload, list):
        return "PARSER_ERROR", [], {
            **meta,
            "status": "PARSER_ERROR",
            "error_category": "parsing_error",
            "error": "match response is not a list",
        }
    if not payload:
        return "SOURCE_EMPTY", [], {
            **meta,
            "status": "SOURCE_EMPTY",
            "error": "HTTP 200 with an empty match array is not acquisition success",
        }

    team_cache: dict[str, str | None] = {}
    fixtures: list[dict[str, Any]] = []
    rejected = 0
    skipped_incomplete = 0
    for match in payload:
        if not isinstance(match, dict):
            rejected += 1
            continue
        match_id = _number(match.get("matchID"))
        if not match_id or match_id <= 0:
            rejected += 1
            continue
        kickoff = _parse_utc(match.get("matchDateTimeUTC"))
        if not kickoff:
            rejected += 1
            continue
        home = match.get("team1") if isinstance(match.get("team1"), dict) else {}
        away = match.get("team2") if isinstance(match.get("team2"), dict) else {}
        home_id = _number(home.get("teamId"))
        away_id = _number(away.get("teamId"))
        home_name = str(home.get("teamName") or "").strip()
        away_name = str(away.get("teamName") or "").strip()
        if not home_id or not away_id or not home_name or not away_name or home_id == away_id:
            rejected += 1
            continue

        finished = match.get("matchIsFinished") is True
        result = _select_fulltime_result(match) if finished else None
        home_goals = _number(result.get("pointsTeam1")) if result else None
        away_goals = _number(result.get("pointsTeam2")) if result else None
        if finished and result is not None and home_goals is not None and away_goals is not None:
            status_short = "FT"
        else:
            status_short = "NS"
            home_goals = None
            away_goals = None
        if completed_only and status_short not in ("FT", "AET", "PEN"):
            skipped_incomplete += 1
            continue

        canonical_home = _resolve_team(home_name, home_id, int(league_id), team_cache)
        canonical_away = _resolve_team(away_name, away_id, int(league_id), team_cache)
        if not canonical_home or not canonical_away:
            rejected += 1
            continue

        group = match.get("group") if isinstance(match.get("group"), dict) else {}
        stage = str(group.get("groupName") or "").strip() or None
        score = {
            "halftime": None,
            "fulltime": (
                {"home": home_goals, "away": away_goals}
                if home_goals is not None
                else None
            ),
            "extratime": None,
            "penalty_shootout": None,
            "result_types": match.get("matchResults") if isinstance(match.get("matchResults"), list) else [],
        }
        halftime = _select_halftime_result(match) if finished else None
        if halftime is not None:
            halftime_home = _number(halftime.get("pointsTeam1"))
            halftime_away = _number(halftime.get("pointsTeam2"))
            if halftime_home is not None and halftime_away is not None:
                score["halftime"] = {"home": halftime_home, "away": halftime_away}
        fixtures.append(
            {
                "fixture": {
                    "id": int(match_id),
                    "date": kickoff,
                    "status": {"short": status_short, "long": "Finished" if status_short == "FT" else "Not finished"},
                    "date_precision": "datetime_utc",
                    "kickoff_time_known": True,
                    "kickoff_time_precision": "second_utc",
                    "timezone_known": True,
                    "round": str(group.get("groupName") or "").strip() or None,
                },
                "league": {
                    "id": int(league_id),
                    "season": int(season),
                    "name": str(league.get("leagueName") or LEAGUE_MAPPINGS[int(league_id)]["name"]),
                },
                "teams": {
                    "home": {"id": int(home_id), "name": home_name},
                    "away": {"id": int(away_id), "name": away_name},
                },
                "goals": {"home": home_goals, "away": away_goals},
                "score": score,
                "canonical_home_id": canonical_home,
                "canonical_away_id": canonical_away,
                "stage": stage,
                "provider_provenance": {
                    "provider": PROVIDER,
                    "providers": [PROVIDER],
                    "provider_type": "official_api",
                    "repository": REPOSITORY,
                    "documentation_url": DOCUMENTATION_URL,
                    "source_url": url,
                    "source_format": "json",
                    "requested_season": int(season),
                    "league_id": int(league_id),
                    "league_shortcut": shortcut,
                    "league_name": league.get("leagueName"),
                    "retrieved_at": meta["retrieved_at"],
                    "provider_fixture_id": int(match_id),
                    "provider_team_ids": {"home": int(home_id), "away": int(away_id)},
                    "match_is_finished": bool(finished),
                    "match_datetime_utc": match.get("matchDateTimeUTC"),
                    "match_datetime_source": match.get("matchDateTime"),
                    "time_zone_id": match.get("timeZoneID"),
                    "result_type": result.get("resultTypeKind") if result else None,
                    "result_type_id": result.get("resultTypeID") if result else None,
                    "goals": match.get("goals") if isinstance(match.get("goals"), list) else [],
                },
            }
        )

    return "SOURCE_AVAILABLE", fixtures, {
        **meta,
        "status": "SOURCE_AVAILABLE",
        "source_declared_match_count": len(payload),
        "fixtures_returned": len(fixtures),
        "rejected_matches": rejected,
        "skipped_incomplete_matches": skipped_incomplete,
        "completed_only": bool(completed_only),
        "rate_limit_per_minute": RATE_LIMIT_PER_MINUTE,
    }


def clear_discovery_cache() -> None:
    """Clear per-process season discovery cache (used by controlled health checks)."""
    _DISCOVERY_CACHE.clear()
