"""Fixture-level multi-provider discovery, matching and field-level merging.

Phase 2 discovered a day with all-or-nothing fallback: the first provider that
returned anything won and every other eligible provider was skipped. That
architecture cannot merge complementary observations (for example ESPN's
kickoff and score with FotMob's xG) and it hides provider disagreements.

This module queries **every** eligible provider for a day, normalises each
fixture without losing its provider-native id, resolves canonical team
identity, matches the same real-world fixture across providers, and merges
fields with field-level provenance. It never:

* averages scores;
* overwrites a higher-quality observation with a lower-quality one;
* merges fixtures on team-name similarity alone when ambiguity remains;
* reuses one provider's native id as another provider's id.

Every attempt, conflict and unmatched record is returned so the lifecycle can
persist it as an auditable event.
"""

from __future__ import annotations

import datetime as _dt
import inspect
import os
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import config
import enrichment_status
import provider_health
import provider_registry
import time_utils

FOOTBALL_COMPETITIONS = list(getattr(config, "ALLOWED_LEAGUE_IDS", [39, 140, 135, 78, 61, 2, 3, 5, 1, 4, 88, 94]))
BASKETBALL_COMPETITIONS = list(getattr(config, "ALLOWED_BASKETBALL_LEAGUE_IDS", [12]))

KICKOFF_TOLERANCE_MINUTES = 240
KICKOFF_CONFLICT_MINUTES = 5

# Field preference is per field, not per provider. A provider that wins
# discovery does not automatically win every field.
FOOTBALL_FIELD_PREFERENCE: Dict[str, List[str]] = {
    "fixtures": ["api_football", "espn_soccer", "fotmob"],
    "kickoff": ["api_football", "espn_soccer", "fotmob"],
    "status": ["api_football", "espn_soccer", "fotmob"],
    "score": ["api_football", "espn_soccer", "fotmob"],
    "statistics": ["fotmob", "espn_soccer", "api_football"],
    "xg": ["fotmob"],
    "possession": ["fotmob", "espn_soccer"],
}
BASKETBALL_FIELD_PREFERENCE: Dict[str, List[str]] = {
    "fixtures": ["api_basketball", "espn_nba"],
    "kickoff": ["api_basketball", "espn_nba"],
    "status": ["api_basketball", "espn_nba"],
    "score": ["api_basketball", "espn_nba"],
    "statistics": ["api_basketball", "espn_nba"],
}

FINISHED_STATUSES = {"FT", "AET", "PEN", "AOT"}
LIVE_STATUSES = {"1H", "2H", "HT", "ET", "P", "BT", "Q1", "Q2", "Q3", "Q4", "OT", "LIVE"}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _preference(sport: str, field: str) -> List[str]:
    table = FOOTBALL_FIELD_PREFERENCE if sport == "football" else BASKETBALL_FIELD_PREFERENCE
    return table.get(field, table["fixtures"])


def _rank(sport: str, field: str, provider: str) -> int:
    order = _preference(sport, field)
    try:
        return order.index(provider)
    except ValueError:
        return len(order)


def _minute(value: Any) -> Optional[_dt.datetime]:
    parsed = time_utils.parse_utc_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def _score_of(fixture: Dict[str, Any], sport: str) -> Optional[Tuple[Optional[int], Optional[int]]]:
    if sport == "football":
        goals = fixture.get("goals") or {}
        home, away = goals.get("home"), goals.get("away")
    else:
        scores = fixture.get("scores") or {}
        home = (scores.get("home") or {}).get("total")
        away = (scores.get("away") or {}).get("total")
    if home is None and away is None:
        return None
    try:
        return (int(home) if home is not None else None, int(away) if away is not None else None)
    except (TypeError, ValueError):
        return None


def _status_of(fixture: Dict[str, Any], sport: str) -> str:
    status = fixture.get("fixture", {}).get("status") if sport == "football" else fixture.get("status")
    if isinstance(status, dict):
        return str(status.get("short") or "").upper()
    return ""


def _provider_of(fixture: Dict[str, Any], default: str) -> str:
    provenance = fixture.get("provider_provenance") or {}
    return str(provenance.get("provider") or fixture.get("source") or default)


def _native_id_of(fixture: Dict[str, Any], sport: str) -> Optional[str]:
    if sport == "football":
        value = (fixture.get("fixture") or {}).get("id")
    else:
        value = fixture.get("id")
    return str(value) if value not in (None, "") else None


def _team_names(fixture: Dict[str, Any]) -> Tuple[str, str, Any, Any]:
    teams = fixture.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    return (
        str(home.get("name") or "").strip(),
        str(away.get("name") or "").strip(),
        home.get("id"),
        away.get("id"),
    )


def _canonical_team_key(
    name: str,
    provider: str,
    provider_id: Any,
    league_id: Any,
    sport: str,
) -> Tuple[str, str]:
    """Return (kind, key). Canonical ids win; a normalized name is the fallback."""
    try:
        import team_identity

        resolved = team_identity.resolve_canonical_team_id(
            name, provider, provider_id, league_id=league_id, sport=sport, auto_register=False
        )
        if resolved:
            return ("canonical", str(resolved))
        normalized = team_identity.normalize_team_name(name)
    except Exception:
        normalized = " ".join(str(name or "").lower().split())
    return ("name", normalized)


def _observations(fixture: Dict[str, Any], sport: str, provider: str, retrieved_at: str) -> Dict[str, Any]:
    provenance = fixture.get("provider_provenance") or {}
    observation: Dict[str, Any] = {
        "provider": provider,
        "provider_fixture_id": _native_id_of(fixture, sport),
        "retrieved_at": retrieved_at,
        "parser_version": provenance.get("parser_version"),
        "status": _status_of(fixture, sport),
        "kickoff_at": (fixture.get("fixture", {}) or {}).get("date") if sport == "football" else fixture.get("date"),
        "score": _score_of(fixture, sport),
        "field_provenance": {},
    }
    for field in ("kickoff", "status", "score"):
        observation["field_provenance"][field] = {
            "provider": provider,
            "provider_fixture_id": observation["provider_fixture_id"],
            "retrieved_at": retrieved_at,
            "parser_version": observation["parser_version"],
        }
    return observation


def _annotate(fixture: Dict[str, Any], sport: str, provider: str, retrieved_at: str) -> Dict[str, Any]:
    annotated = dict(fixture)
    observation = _observations(fixture, sport, provider, retrieved_at)
    annotated.setdefault("provider_provenance", {})
    annotated["provider_provenance"] = dict(annotated["provider_provenance"])
    annotated["provider_provenance"].setdefault("retrieved_at", retrieved_at)
    annotated["provider_fixture_ids"] = {provider: observation["provider_fixture_id"]}
    annotated["source_observations"] = {provider: observation}
    annotated["field_provenance"] = dict(observation["field_provenance"])
    annotated["match_status"] = "single_source"
    annotated["identity_basis"] = "provider_native"
    annotated["conflicts"] = []
    return annotated


def _group_key(fixture: Dict[str, Any], sport: str, provider: str, league_id: Any) -> Tuple[Any, ...]:
    home_name, away_name, home_id, away_id = _team_names(fixture)
    home_kind, home_key = _canonical_team_key(home_name, provider, home_id, league_id, sport)
    away_kind, away_key = _canonical_team_key(away_name, provider, away_id, league_id, sport)
    season = (fixture.get("league") or {}).get("season")
    return (league_id, season, home_key, away_key, home_kind, away_kind)


def _ambiguous(fixture: Dict[str, Any], sport: str, provider: str, retrieved_at: str, reason: str, candidates: List[str]) -> Dict[str, Any]:
    record = _annotate(fixture, sport, provider, retrieved_at)
    record["match_status"] = "unresolved"
    record["identity_unresolved_reason"] = reason
    record["identity_candidates"] = candidates
    return record


def _merge_cluster(
    sport: str,
    members: List[Tuple[str, str, Dict[str, Any]]],
) -> Dict[str, Any]:
    """Merge observations of one real-world fixture.

    ``members`` are (provider, retrieved_at, annotated_fixture) tuples.
    The highest-preferred provider for the ``fixtures`` field supplies the
    record shape and its native id; every other provider id is preserved.
    """
    members = sorted(members, key=lambda item: _rank(sport, "fixtures", item[0]))
    primary_provider, primary_at, primary = members[0]
    merged = dict(primary)
    merged["source_observations"] = {}
    merged["provider_fixture_ids"] = {}
    merged["field_provenance"] = {}
    conflicts: List[Dict[str, Any]] = []

    for provider, retrieved_at, fixture in members:
        observation = _observations(fixture, sport, provider, retrieved_at)
        merged["source_observations"][provider] = observation
        if observation["provider_fixture_id"] is not None:
            merged["provider_fixture_ids"][provider] = observation["provider_fixture_id"]

    merged["provider_fixture_ids"].setdefault(
        primary_provider, _native_id_of(primary, sport)
    )

    for field in ("kickoff", "status", "score"):
        ordered = sorted(
            ((p, at, f) for p, at, f in members if _has_field(f, field, sport)),
            key=lambda item: _rank(sport, field, item[0]),
        )
        if not ordered:
            continue
        winner_provider, winner_at, winner_fixture = ordered[0]
        merged["field_provenance"][field] = {
            "provider": winner_provider,
            "provider_fixture_id": _native_id_of(winner_fixture, sport),
            "retrieved_at": winner_at,
            "parser_version": (winner_fixture.get("provider_provenance") or {}).get("parser_version"),
        }
        if field == "kickoff":
            value = _kickoff_of(winner_fixture, sport)
            if value is not None:
                if sport == "football":
                    merged.setdefault("fixture", {})
                    merged["fixture"] = dict(merged["fixture"])
                    merged["fixture"]["date"] = value
                else:
                    merged["date"] = value
        elif field == "status":
            status = _status_of(winner_fixture, sport)
            if sport == "football":
                merged.setdefault("fixture", {})
                merged["fixture"] = dict(merged["fixture"])
                merged["fixture"]["status"] = {"short": status, "long": None, "elapsed": None}
            else:
                merged["status"] = {"short": status, "long": None, "timer": None}

        values = {}
        for provider, _at, fixture in ordered:
            if field == "kickoff":
                values[provider] = _kickoff_iso(fixture, sport)
            elif field == "status":
                values[provider] = _status_of(fixture, sport)
            else:
                values[provider] = _score_of(fixture, sport)
        distinct = {repr(v) for v in values.values() if v is not None}
        if len(distinct) > 1:
            severity = "conflict"
            if field == "kickoff":
                parsed = [(_minute(v), p) for p, v in values.items() if _minute(v)]
                if len(parsed) == 2:
                    delta = abs((parsed[0][0] - parsed[1][0]).total_seconds()) / 60.0
                    if delta <= KICKOFF_CONFLICT_MINUTES:
                        continue
            conflicts.append(
                {
                    "field": field,
                    "observations": {
                        provider: {
                            "value": values[provider],
                            "retrieved_at": at,
                            "provider_fixture_id": _native_id_of(fixture, sport),
                        }
                        for provider, at, fixture in members
                    },
                    "severity": severity,
                    "resolution": "canonical_unresolved",
                }
            )
            merged.setdefault("unresolved_fields", []).append(field)

    if sport == "football":
        merged["goals"] = _pick_dict_field(members, "goals", "score", sport, merged)
    else:
        merged["scores"] = _pick_dict_field(members, "scores", "score", sport, merged)

    merged["conflicts"] = conflicts
    merged["match_status"] = "merged" if len(members) > 1 else "single_source"
    merged["identity_basis"] = "league_season_canonical_or_normalized_teams"
    merged["merged_providers"] = [p for p, _a, _f in members]
    merged["unresolved_fields"] = sorted(set(merged.get("unresolved_fields", [])))
    return merged


def _kickoff_of(fixture: Dict[str, Any], sport: str) -> Optional[str]:
    value = (fixture.get("fixture") or {}).get("date") if sport == "football" else fixture.get("date")
    return str(value) if value else None


def _kickoff_iso(fixture: Dict[str, Any], sport: str) -> Optional[str]:
    parsed = _minute(_kickoff_of(fixture, sport))
    return parsed.isoformat() if parsed else _kickoff_of(fixture, sport)


def _has_field(fixture: Dict[str, Any], field: str, sport: str) -> bool:
    if field == "kickoff":
        return bool(_kickoff_of(fixture, sport))
    if field == "status":
        return bool(_status_of(fixture, sport))
    return _score_of(fixture, sport) is not None


def _pick_dict_field(
    members: List[Tuple[str, str, Dict[str, Any]]],
    key: str,
    rank_field: str,
    sport: str,
    merged: Dict[str, Any],
) -> Dict[str, Any]:
    ordered = sorted(members, key=lambda item: _rank(sport, rank_field, item[0]))
    for _provider, _at, fixture in ordered:
        value = fixture.get(key)
        if isinstance(value, dict) and value.get("home") is not None:
            return dict(value)
    return dict(merged.get(key) or {})


def cluster_fixtures(
    sport: str,
    annotated: List[Tuple[str, str, Dict[str, Any]]],
) -> Dict[str, Any]:
    """Cluster annotated fixtures and merge each unambiguous cluster.

    ``annotated`` contains (provider, retrieved_at, fixture) for every fixture
    returned by every provider. A cluster with more than one fixture from the
    same provider, or with swapped home/away identities, stays unresolved.
    """
    groups: Dict[Tuple[Any, ...], List[Tuple[str, str, Dict[str, Any]]]] = {}
    invalid: List[Dict[str, Any]] = []

    for provider, retrieved_at, fixture in annotated:
        league_id = (fixture.get("league") or {}).get("id")
        home_name, away_name, _hid, _aid = _team_names(fixture)
        if not home_name or not away_name or not league_id:
            invalid.append(
                _ambiguous(fixture, sport, provider, retrieved_at, "missing_team_or_league_identity", [])
            )
            continue
        groups.setdefault(_group_key(fixture, sport, provider, league_id), []).append(
            (provider, retrieved_at, fixture)
        )

    # Detect the same two teams recorded in opposite home/away order by different
    # providers. Those are never merged, but they are also not silently accepted
    # as two independent fixtures.
    unordered: Dict[Tuple[Any, ...], List[Tuple[Any, ...]]] = {}
    for key in groups:
        league_id, season, home_key, away_key, _hk, _ak = key
        unordered.setdefault((league_id, season) + tuple(sorted((home_key, away_key))), []).append(key)
    swapped = {
        key
        for keys in unordered.values()
        if len(keys) > 1
        for key in keys
    }

    merged: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = list(invalid)
    for key, members in groups.items():
        providers = [p for p, _a, _f in members]
        if key in swapped:
            for provider, at, fixture in members:
                unmatched.append(
                    _ambiguous(fixture, sport, provider, at, "home_away_orientation_conflict", providers)
                )
            continue
        if len(providers) != len(set(providers)):
            provider, at, fixture = members[0]
            unmatched.append(
                _ambiguous(fixture, sport, provider, at, "duplicate_provider_records", sorted(providers))
            )
            continue
        merged.append(_merge_cluster(sport, members))

    return {"fixtures": merged, "unmatched": unmatched}


# ---------------------------------------------------------------------------
# Provider discovery
# ---------------------------------------------------------------------------

def _api_football_loader(date_str: str, league_ids: Optional[Iterable[int]]):
    import os

    import config as _config

    # DataResolver swallows the missing-credential error and returns an empty
    # list. That would make discovery look like an honest "no fixtures" answer,
    # so the adapter raises instead and the health ledger records the real
    # CREDENTIAL/CONFIGURATION failure.
    if not (os.environ.get("API_FOOTBALL_KEY") or getattr(_config, "API_FOOTBALL_KEY", None)):
        raise RuntimeError("API_FOOTBALL_KEY is not configured (configuration_error)")

    from data_resolver import DataResolver

    payload = DataResolver().get_fixtures_for_date(date_str)
    # The resolver returns either a fixture list or (fixtures, metadata).
    if isinstance(payload, tuple):
        payload = payload[0] if payload else []
    if isinstance(payload, dict):
        payload = payload.get("fixtures") or payload.get("data") or []
    result = []
    allowed = set(int(x) for x in (league_ids or FOOTBALL_COMPETITIONS))
    for fixture in payload or []:
        if isinstance(fixture, dict) and (fixture.get("league") or {}).get("id") in allowed:
            result.append(fixture)
    return result


def _api_basketball_loader(date_str: str, league_ids: Optional[Iterable[int]]):
    import basketball_api

    try:
        games = basketball_api.get_games_by_date(date_str, league_id=(list(league_ids or BASKETBALL_COMPETITIONS))[0])
    except TypeError:
        games = basketball_api.get_games_by_date(date_str)
    return list(games or [])


def _invoke_loader(
    loader: Callable[..., List[Dict[str, Any]]],
    date_str: str,
    competitions: List[int],
) -> List[Dict[str, Any]]:
    """Call a discovery adapter, tolerating a one-argument loader."""
    try:
        signature = inspect.signature(loader)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(
            parameter.kind == parameter.VAR_POSITIONAL for parameter in signature.parameters.values()
        )
    except (TypeError, ValueError):
        positional, has_varargs = [None, None], False
    if has_varargs or len(positional) >= 2:
        return loader(date_str, competitions)
    return loader(date_str)


def default_adapters(sport: str) -> Dict[str, Callable[[str, Optional[Iterable[int]]], List[Dict[str, Any]]]]:
    import live_providers

    if sport == "football":
        return {
            "api_football": _api_football_loader,
            "espn_soccer": live_providers.espn_soccer_fixtures,
            "fotmob": live_providers.fotmob_fixtures,
        }
    return {
        "api_basketball": _api_basketball_loader,
        "espn_nba": lambda date_str, league_ids=None: live_providers.espn_nba_games(date_str),
    }


def discover(
    sport: str,
    date_str: str,
    league_ids: Optional[Iterable[int]] = None,
    *,
    adapters: Optional[Dict[str, Callable[[str, Optional[Iterable[int]]], List[Dict[str, Any]]]]] = None,
    max_attempts: int = 2,
    sleep_fn: Optional[Callable[[float], None]] = None,
    respect_isolation: bool = True,
) -> Dict[str, Any]:
    """Query every eligible provider for one day and merge what they return."""
    if sport not in ("football", "basketball"):
        raise ValueError("sport must be 'football' or 'basketball'")
    competitions = list(league_ids or (FOOTBALL_COMPETITIONS if sport == "football" else BASKETBALL_COMPETITIONS))
    adapters = adapters or default_adapters(sport)
    attempts: List[Dict[str, Any]] = []
    annotated: List[Tuple[str, str, Dict[str, Any]]] = []
    errors: List[str] = []
    provider_counts: Dict[str, int] = {}

    for provider, loader in adapters.items():
        if not any(
            provider_registry.supports(provider, "discovery", "fixtures", league)
            for league in competitions
        ):
            entry = provider_registry.get_provider(provider) or {}
            auth_env = entry.get("auth_env")
            missing_credential = bool(auth_env) and not (
                os.environ.get(auth_env) or getattr(config, auth_env, None)
            )
            attempts.append(
                {
                    "provider": provider,
                    "outcome": "not_eligible",
                    "reason": "credential_missing" if missing_credential else "capability_not_verified_for_competition",
                    "credential_env": auth_env if missing_credential else None,
                }
            )
            continue
        if respect_isolation and not provider_health.allows_request(provider):
            attempts.append(
                {
                    "provider": provider,
                    "outcome": "skipped",
                    "reason": "provider_isolated",
                    "health": provider_health.get_health(provider),
                }
            )
            continue

        started = _dt.datetime.now(_dt.timezone.utc)
        kwargs: Dict[str, Any] = {"max_attempts": max(1, max_attempts)}
        if sleep_fn is not None:
            kwargs["sleep_fn"] = sleep_fn
        try:
            fixtures, meta = provider_health.call_with_retry(
                provider, lambda loader=loader: _invoke_loader(loader, date_str, competitions), **kwargs
            )
        except Exception as exc:  # noqa: BLE001 - failure is data, not a crash
            category = provider_health.classify_exception(exc)
            attempts.append(
                {
                    "provider": provider,
                    "outcome": "failure",
                    "failure_category": category,
                    "error": provider_health.redact(exc),
                    "retrieved_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "duration_ms": round((_dt.datetime.now(_dt.timezone.utc) - started).total_seconds() * 1000, 1),
                }
            )
            errors.append(f"{provider}: {category}: {provider_health.redact(exc, 200)}")
            continue

        retrieved_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        fixtures = list(fixtures or [])
        provider_counts[provider] = len(fixtures)
        attempts.append(
            {
                "provider": provider,
                "outcome": "success" if fixtures else "empty",
                "count": len(fixtures),
                "retrieved_at": retrieved_at,
                "duration_ms": round((_dt.datetime.now(_dt.timezone.utc) - started).total_seconds() * 1000, 1),
                "attempts": meta.get("attempts"),
                "empty_is_not_proof": not fixtures,
            }
        )
        for fixture in fixtures:
            league_id = (fixture.get("league") or {}).get("id")
            if league_id not in competitions:
                continue
            provider = _provider_of(fixture, provider)
            annotated.append((provider, retrieved_at, _annotate(fixture, sport, provider, retrieved_at)))

    clustered = cluster_fixtures(sport, annotated)
    fixtures = clustered["fixtures"]
    unmatched = clustered["unmatched"]
    return {
        "sport": sport,
        "date": date_str,
        "competitions": competitions,
        "attempts": attempts,
        "providers_queried": [a["provider"] for a in attempts if a.get("outcome") != "not_eligible"],
        "provider_counts": provider_counts,
        "fixtures": fixtures,
        "unmatched": unmatched,
        "merged_count": sum(1 for f in fixtures if f.get("match_status") == "merged"),
        "unmatched_count": len(unmatched),
        "conflicts": [c for f in fixtures for c in f.get("conflicts", [])],
        "errors": errors,
        "retrieved_at": _now(),
    }


# ---------------------------------------------------------------------------
# Field-level enrichment merging
# ---------------------------------------------------------------------------

def merge_stat_observations(
    observations: Iterable[Dict[str, Any]],
    sport: str = "football",
    expected_fields: Iterable[str] = (),
) -> Dict[str, Any]:
    """Merge statistics from several providers with per-field provenance.

    ``observations`` items are ``{"provider", "provider_fixture_id",
    "retrieved_at", "parser_version", "statistics"}``. Higher-preferred
    providers win per field; disagreements are preserved, never averaged.
    Fields absent from every observation are classified explicitly.
    """
    observations = [o for o in observations if isinstance(o, dict)]
    merged: Dict[str, Dict[str, Any]] = {}
    values: Dict[str, Any] = {}
    conflicts: List[Dict[str, Any]] = []
    ordered = sorted(observations, key=lambda o: _rank(sport, "statistics", str(o.get("provider"))))

    for observation in ordered:
        provider = str(observation.get("provider"))
        stats = observation.get("statistics") or {}
        for field, value in stats.items():
            if value is None:
                continue
            if field not in values:
                values[field] = value
                merged[field] = {
                    "value": value,
                    "provider": provider,
                    "provider_fixture_id": observation.get("provider_fixture_id"),
                    "retrieved_at": observation.get("retrieved_at"),
                    "parser_version": observation.get("parser_version"),
                }
            elif values[field] != value:
                conflicts.append(
                    {
                        "field": field,
                        "observations": {
                            merged[field]["provider"]: merged[field]["value"],
                            provider: value,
                        },
                        "resolution": "canonical_unresolved",
                    }
                )

    outcomes = enrichment_status.classify_fields(
        values,
        list(expected_fields) or list(values.keys()),
        provider=ordered[0].get("provider") if ordered else None,
    )
    for field, outcome in outcomes.items():
        if outcome["state"] == enrichment_status.CONFLICTING_OBSERVATIONS:
            continue
        if field in values and field in merged:
            merged[field]["state"] = enrichment_status.PRESENT
        elif field not in merged:
            merged[field] = outcome
    unresolved = {c["field"] for c in conflicts}
    for field in unresolved:
        merged[field]["state"] = enrichment_status.CONFLICTING_OBSERVATIONS
    return {
        "fields": merged,
        "conflicts": conflicts,
        "coverage": enrichment_status.coverage_summary(
            {field: {"state": entry.get("state", enrichment_status.NOT_ACQUIRED)} for field, entry in merged.items()}
        ),
    }
