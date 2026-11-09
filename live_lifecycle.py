"""
Autonomous live-match lifecycle for football and basketball.

Durable, resumable stages:

    DISCOVERED -> IDENTITY_RESOLVED -> TRACKED -> PREMATCH_DATA_READY
    -> PREMATCH_PREDICTION_CREATED -> PREMATCH_SNAPSHOT_SAVED -> LIVE
    -> INPLAY_OBSERVATION_RECORDED -> FINAL_RESULT_PENDING
    -> FINAL_RESULT_VERIFIED -> PREDICTION_GRADED
    -> HISTORICAL_RECORD_UPDATED -> MODEL_EVALUATED
    -> LEARNING_UPDATE_COMPLETED

Guarantees implemented here:

* unresolved fixtures stay DISCOVERED and never reach prediction;
* TRACKED requires both canonical team identities;
* stage transitions are legal, conditional and restart-safe;
* retry backoff is enforced through ``next_retry_at``;
* provider selection is driven by ``provider_registry`` capabilities;
* graded outcomes come from the durable verified-result observation, never a
  refetch requirement;
* historical persistence happens only after grading and is retried until it
  succeeds, so the lifecycle never advances past an incomplete historical
  update.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import config
import lifecycle_storage as lstore
import market_grading
import provider_registry
from historical_match_policy import (
    BASKETBALL_FINISHED_STATUSES,
    FOOTBALL_FINISHED_STATUSES,
    get_basketball_match_points,
    get_football_match_goals,
    get_regulation_goals,
    get_status_short,
    is_finished_match,
    is_postponed_or_cancelled,
)

STAGES = [
    "DISCOVERED",
    "IDENTITY_RESOLVED",
    "TRACKED",
    "PREMATCH_DATA_READY",
    "PREMATCH_PREDICTION_CREATED",
    "PREMATCH_SNAPSHOT_SAVED",
    "LIVE",
    "INPLAY_OBSERVATION_RECORDED",
    "FINAL_RESULT_PENDING",
    "FINAL_RESULT_VERIFIED",
    "PREDICTION_GRADED",
    "HISTORICAL_RECORD_UPDATED",
    "MODEL_EVALUATED",
    "LEARNING_UPDATE_COMPLETED",
]

STAGE_INDEX = {stage: index for index, stage in enumerate(STAGES)}

LEGAL_TRANSITIONS: Dict[str, set] = {
    "DISCOVERED": {"IDENTITY_RESOLVED"},
    "IDENTITY_RESOLVED": {"TRACKED"},
    "TRACKED": {"PREMATCH_DATA_READY"},
    "PREMATCH_DATA_READY": {"PREMATCH_PREDICTION_CREATED"},
    "PREMATCH_PREDICTION_CREATED": {"PREMATCH_SNAPSHOT_SAVED"},
    "PREMATCH_SNAPSHOT_SAVED": {"LIVE", "FINAL_RESULT_PENDING"},
    "LIVE": {"INPLAY_OBSERVATION_RECORDED", "FINAL_RESULT_PENDING"},
    "INPLAY_OBSERVATION_RECORDED": {"INPLAY_OBSERVATION_RECORDED", "FINAL_RESULT_PENDING"},
    "FINAL_RESULT_PENDING": {"FINAL_RESULT_VERIFIED"},
    "FINAL_RESULT_VERIFIED": {"PREDICTION_GRADED"},
    "PREDICTION_GRADED": {"HISTORICAL_RECORD_UPDATED"},
    "HISTORICAL_RECORD_UPDATED": {"MODEL_EVALUATED"},
    "MODEL_EVALUATED": {"LEARNING_UPDATE_COMPLETED", "MODEL_EVALUATED"},
    "LEARNING_UPDATE_COMPLETED": set(),
}

FOOTBALL_COMPETITIONS = list(getattr(config, "ALLOWED_LEAGUE_IDS", [39, 140, 135, 78, 61, 2, 3, 5, 1, 4, 88, 94]))
BASKETBALL_COMPETITIONS = list(getattr(config, "ALLOWED_BASKETBALL_LEAGUE_IDS", [12]))

DISCOVERY_HORIZON_DAYS = 14
GRADING_RULES_VERSION = "phase2.market_grading.v1"
INPLAY_CYCLE_BUCKET_MINUTES = 5


class LifecycleTransitionError(RuntimeError):
    """Raised when a stage change is not legal for the stored lifecycle."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse(value: Any) -> Optional[dt.datetime]:
    from time_utils import parse_utc_datetime

    return parse_utc_datetime(value) if value else None


def _items(result: Any) -> List[Dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, dict):
        for key in ("fixtures", "games", "data", "items", "results"):
            value = result.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []
    if isinstance(result, (list, tuple)):
        for part in result:
            if isinstance(part, list):
                return [item for item in part if isinstance(item, dict)]
    return []


def calendar_days(horizon_days: int = DISCOVERY_HORIZON_DAYS, today: Optional[dt.date] = None) -> List[str]:
    base = today or dt.datetime.now(dt.timezone.utc).date()
    return [(base + dt.timedelta(days=offset)).isoformat() for offset in range(0, int(horizon_days) + 1)]


def _retry_pending(row: Dict[str, Any]) -> bool:
    """True when the stored retry time has not arrived yet."""
    next_retry = _parse(row.get("next_retry_at"))
    return next_retry is not None and next_retry > dt.datetime.now(dt.timezone.utc)


def _fail(row: Dict[str, Any], error: Exception, error_class: str = "provider_error") -> Dict[str, Any]:
    retry = int(row.get("retry_count") or 0) + 1
    next_retry = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=min(60, 2 ** min(retry, 5)))
    ).isoformat()
    updated = lstore.update_lifecycle(
        row["lifecycle_id"],
        retry_count=retry,
        error_state=str(error)[:500],
        error_class=error_class,
        next_retry_at=next_retry,
    )
    return updated or row


def _advance(row: Dict[str, Any], to_stage: str, **fields) -> Dict[str, Any]:
    """Advance one legal step, clearing resolved retry/error state."""
    current = row.get("stage")
    if current == to_stage:
        if fields:
            return lstore.update_lifecycle(row["lifecycle_id"], **fields) or row
        return row
    if to_stage not in LEGAL_TRANSITIONS.get(str(current), set()):
        raise LifecycleTransitionError(f"illegal transition {current} -> {to_stage}")

    fields.setdefault("retry_count", 0)
    fields["error_state"] = None
    fields["error_class"] = None
    fields["next_retry_at"] = None
    updated = lstore.transition_lifecycle(
        row["lifecycle_id"],
        to_stage,
        from_stages=[current],
        **fields,
    )
    if updated is None:
        # Another worker already advanced this record; reload instead of forcing.
        return lstore.get_lifecycle(row["lifecycle_id"]) or row
    return updated


# ---------------------------------------------------------------------------
# Provider adapters (routing decisions come from provider_registry)
# ---------------------------------------------------------------------------

def _football_result(fixture_id: int) -> Optional[Dict[str, Any]]:
    import api_football

    try:
        payload = api_football.get_fixture_result(fixture_id)
        provider_registry.record_health("api_football", ok=bool(payload))
        return payload if isinstance(payload, dict) else None
    except Exception as exc:
        provider_registry.record_health("api_football", ok=False, failure_category=type(exc).__name__)
        return None


def _basketball_result(game_id: int) -> Optional[Dict[str, Any]]:
    import basketball_api

    try:
        payload = basketball_api.get_game_result(game_id)
        provider_registry.record_health("api_basketball", ok=bool(payload))
        return payload if isinstance(payload, dict) else None
    except Exception as exc:
        provider_registry.record_health("api_basketball", ok=False, failure_category=type(exc).__name__)
        return None


_ADAPTERS = {
    ("football", "api_football"): _football_result,
    ("basketball", "api_basketball"): _basketball_result,
}


def _fetch_result(sport: str, provider: str, fixture_id: int) -> Optional[Dict[str, Any]]:
    adapter = _ADAPTERS.get((sport, provider))
    return adapter(fixture_id) if adapter else None


def _live_index(sport: str, provider: str) -> Dict[str, Dict[str, Any]]:
    if sport == "football" and provider == "api_football":
        import api_football

        try:
            items = _items(api_football.get_live_fixtures())
        except Exception:
            return {}
        return {str((f.get("fixture") or {}).get("id")): f for f in items if (f.get("fixture") or {}).get("id") is not None}

    if sport == "basketball" and provider == "api_basketball":
        import basketball_api

        try:
            items = _items(basketball_api.get_live_games())
        except Exception:
            return {}
        return {str(g.get("id")): g for g in items if g.get("id") is not None}

    return {}


def _is_stale(observed_at: Any, context: str, field: str) -> bool:
    """Reject an observation older than the configured freshness limit."""
    limit = provider_registry.freshness_limit_minutes(context, field)
    observed = _parse(observed_at)
    if limit is None or observed is None:
        return False
    return (dt.datetime.now(dt.timezone.utc) - observed) > dt.timedelta(minutes=limit)


def _providers(sport: str, context: str, field: str, league_id: Any) -> List[str]:
    try:
        league = int(league_id) if league_id is not None else None
    except (TypeError, ValueError):
        league = None
    return [entry["provider"] for entry in provider_registry.route_field(sport, field, context, league)]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _football_fixtures_for_date(date_str: str) -> List[Dict[str, Any]]:
    if not _providers("football", "discovery", "fixtures", None):
        return []
    try:
        from data_resolver import DataResolver

        resolver = DataResolver()
        fixtures = _items(resolver.get_fixtures_for_date(date_str))
    except Exception as exc:
        provider_registry.record_health("api_football", ok=False, failure_category=type(exc).__name__)
        return []

    accepted = []
    for fixture in fixtures:
        league = fixture.get("league") or {}
        if league.get("id") not in FOOTBALL_COMPETITIONS:
            continue
        provenance = fixture.get("provider_provenance") or {}
        provider = provenance.get("provider") or fixture.get("source") or "api_football"
        # Only providers verified for current discovery may enter the lifecycle.
        if not provider_registry.supports(provider, "discovery", "fixtures", league.get("id")):
            continue
        accepted.append(fixture)
    return accepted


def _basketball_games_for_date(date_str: str) -> List[Dict[str, Any]]:
    import basketball_api

    providers = _providers("basketball", "discovery", "fixtures", BASKETBALL_COMPETITIONS[0])
    if "api_basketball" not in providers:
        return []
    attempts = (
        lambda: basketball_api.get_games_by_date(date_str, league_id=BASKETBALL_COMPETITIONS[0]),
        lambda: basketball_api.get_games_by_date(date_str),
    )
    for attempt in attempts:
        try:
            games = _items(attempt())
        except TypeError:
            continue
        except Exception as exc:
            provider_registry.record_health("api_basketball", ok=False, failure_category=type(exc).__name__)
            return []
        provider_registry.record_health("api_basketball", ok=True)
        return [g for g in games if (g.get("league") or {}).get("id") in BASKETBALL_COMPETITIONS]
    return []


def _football_identity(fixture: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fixture_data = fixture.get("fixture") or {}
    league = fixture.get("league") or {}
    teams = fixture.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    fixture_id = fixture_data.get("id")
    if not fixture_id or not league.get("id"):
        return None
    provenance = fixture.get("provider_provenance") or {}
    return {
        "sport": "football",
        "provider": provenance.get("provider") or fixture.get("source") or "api_football",
        "provider_fixture_id": str(fixture_id),
        "league_id": league.get("id"),
        "season": league.get("season"),
        "kickoff_at": fixture_data.get("date"),
        "home_provider_id": home.get("id"),
        "away_provider_id": away.get("id"),
        "home_name": home.get("name"),
        "away_name": away.get("name"),
        "status": (fixture_data.get("status") or {}).get("short"),
    }


def _basketball_identity(game: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    league = game.get("league") or {}
    teams = game.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    game_id = game.get("id")
    if not game_id or not league.get("id"):
        return None
    return {
        "sport": "basketball",
        "provider": "api_basketball",
        "provider_fixture_id": str(game_id),
        "league_id": league.get("id"),
        "season": league.get("season"),
        "kickoff_at": game.get("date"),
        "home_provider_id": home.get("id"),
        "away_provider_id": away.get("id"),
        "home_name": home.get("name"),
        "away_name": away.get("name"),
        "status": get_status_short(game, sport="basketball"),
    }


def _canonical_team_id(name: Optional[str], provider: str, provider_id: Any, league_id: Any) -> Optional[str]:
    if not name:
        return None
    try:
        import team_identity

        resolved = team_identity.resolve_canonical_team_id(name, provider, provider_id, league_id=league_id)
    except Exception:
        return None
    return str(resolved) if resolved is not None else None


def _persist_identity(identity: Dict[str, Any]) -> Dict[str, Any]:
    home_canonical = _canonical_team_id(
        identity.get("home_name"), identity["provider"], identity.get("home_provider_id"), identity.get("league_id")
    )
    away_canonical = _canonical_team_id(
        identity.get("away_name"), identity["provider"], identity.get("away_provider_id"), identity.get("league_id")
    )

    existing = lstore.get_lifecycle_by_fixture(identity["sport"], identity["provider_fixture_id"])
    if existing is not None:
        updates: Dict[str, Any] = {}
        # Never overwrite an already verified canonical identity.
        if not existing.get("home_team_id") and home_canonical:
            updates["home_team_id"] = home_canonical
        if not existing.get("away_team_id") and away_canonical:
            updates["away_team_id"] = away_canonical
        if updates:
            existing = lstore.update_lifecycle(existing["lifecycle_id"], **updates) or existing
        if (
            existing.get("stage") in ("DISCOVERED", "IDENTITY_RESOLVED")
            and existing.get("home_team_id")
            and existing.get("away_team_id")
        ):
            existing = _advance(existing, "IDENTITY_RESOLVED")
            existing = _advance(existing, "TRACKED")
        return existing

    record = {
        "lifecycle_id": f"{identity['sport']}:{identity['provider_fixture_id']}",
        "sport": identity["sport"],
        "canonical_fixture_key": identity["provider_fixture_id"],
        "provider": identity["provider"],
        "provider_fixture_id": identity["provider_fixture_id"],
        "league_id": identity.get("league_id"),
        "season": identity.get("season"),
        # Canonical identity fields never carry provider-native ids.
        "home_team_id": home_canonical,
        "away_team_id": away_canonical,
        "kickoff_at": identity.get("kickoff_at"),
        "stage": "DISCOVERED",
        "provider_status": identity.get("status"),
    }
    row = lstore.upsert_lifecycle(record)
    if row.get("stage") == "DISCOVERED" and row.get("home_team_id") and row.get("away_team_id"):
        row = _advance(row, "IDENTITY_RESOLVED")
        row = _advance(row, "TRACKED")
    return row


def discover(sport: Optional[str] = None, horizon_days: int = DISCOVERY_HORIZON_DAYS) -> Dict[str, Any]:
    sports = [sport] if sport in ("football", "basketball") else ["football", "basketball"]
    rows: List[Dict[str, Any]] = []
    identity_failures = 0

    for day in calendar_days(horizon_days):
        if "football" in sports:
            for fixture in _football_fixtures_for_date(day):
                identity = _football_identity(fixture)
                if identity is None:
                    identity_failures += 1
                    continue
                rows.append(_persist_identity(identity))
        if "basketball" in sports:
            for game in _basketball_games_for_date(day):
                identity = _basketball_identity(game)
                if identity is None:
                    identity_failures += 1
                    continue
                rows.append(_persist_identity(identity))

    unique = {row["lifecycle_id"]: row for row in rows if row}
    return {
        "stage": "discover",
        "horizon_days": horizon_days,
        "competitions": {"football": FOOTBALL_COMPETITIONS, "basketball": BASKETBALL_COMPETITIONS},
        "discovered": len(unique),
        "tracked": sum(1 for row in unique.values() if row.get("stage") == "TRACKED"),
        "unresolved": sum(1 for row in unique.values() if row.get("stage") == "DISCOVERED"),
        "identity_failures": identity_failures,
    }


# ---------------------------------------------------------------------------
# Pre-match prediction
# ---------------------------------------------------------------------------

def _snapshot_key(sport: str, fixture_id: Any, context: str, kickoff: Any) -> str:
    """Stable per-cycle key: a retry reuses it, a new cycle differs."""
    return f"{sport}:{fixture_id}:{context}:{kickoff or 'unknown'}"


def _calibrator(sport: str, league_id: Any, season: Any, cutoff: Any):
    try:
        import calibration

        return calibration.load_active_calibrator(
            sport=sport, league_id=league_id, season=season, cutoff_timestamp=cutoff
        )
    except Exception:
        return None


def _persist_cycle_key(row: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Persist the cycle key before the snapshot side effect."""
    if row.get("prediction_cycle_key") == key:
        return row
    updated = lstore.update_lifecycle(row["lifecycle_id"], prediction_cycle_key=key)
    return updated or row


def _prematch_football(row: Dict[str, Any]) -> Dict[str, Any]:
    import storage

    fixture_id = int(row["provider_fixture_id"])
    fixture = _fetch_result("football", row.get("provider") or "api_football", fixture_id)
    if not fixture:
        return _fail(row, RuntimeError("prematch fixture payload unavailable"))

    kickoff = ((fixture.get("fixture") or {}).get("date")) or row.get("kickoff_at")
    kickoff_dt = _parse(kickoff)
    if kickoff_dt is None or kickoff_dt <= dt.datetime.now(dt.timezone.utc):
        return lstore.update_lifecycle(
            row["lifecycle_id"], error_state="kickoff_passed_before_prematch", error_class="timing"
        ) or row

    row = _advance(row, "PREMATCH_DATA_READY")
    import main as app_main

    league_avg = float(
        getattr(config, "LEAGUE_AVG_GOALS", {}).get(row.get("league_id"), getattr(config, "LEAGUE_AVG_GOALS_FALLBACK", 1.35))
    )
    prediction = app_main.predict_fixture(fixture, league_avg, provider_meta={"provider": row.get("provider")})
    if not isinstance(prediction, dict) or prediction.get("insufficient_data"):
        reason = prediction.get("reason") if isinstance(prediction, dict) else "invalid prediction"
        return _fail(row, RuntimeError(str(reason)), "model_error")

    row = _advance(row, "PREMATCH_PREDICTION_CREATED")
    timestamp = _now()
    key = _snapshot_key("football", fixture_id, "PRE_MATCH", kickoff)
    row = _persist_cycle_key(row, key)

    snapshot_id = storage.save_prediction_snapshot(
        fixture_id=fixture_id,
        prediction_timestamp=timestamp,
        kickoff_at=kickoff,
        sport="football",
        league_id=row.get("league_id"),
        season=row.get("season"),
        canonical_home_id=row.get("home_team_id"),
        canonical_away_id=row.get("away_team_id"),
        home_team=prediction.get("home_team"),
        away_team=prediction.get("away_team"),
        prediction_context="PRE_MATCH",
        data_cutoff_timestamp=prediction.get("data_cutoff_timestamp") or timestamp,
        markets=prediction.get("markets"),
        features=prediction.get("model_inputs", {}).get("features") if isinstance(prediction.get("model_inputs"), dict) else None,
        confidence=prediction.get("confidence"),
        prediction_record=prediction,
        idempotency_key=key,
        lifecycle_id=row["lifecycle_id"],
        model_inputs=prediction.get("model_inputs"),
    )
    row = _advance(row, "PREMATCH_SNAPSHOT_SAVED")
    return {**row, "snapshot_id": snapshot_id}


def _prematch_basketball(row: Dict[str, Any]) -> Dict[str, Any]:
    import basketball_model
    import storage

    game_id = int(row["provider_fixture_id"])
    game = _fetch_result("basketball", row.get("provider") or "api_basketball", game_id)
    if not game:
        return _fail(row, RuntimeError("prematch game payload unavailable"))

    kickoff = game.get("date") or row.get("kickoff_at")
    kickoff_dt = _parse(kickoff)
    if kickoff_dt is None or kickoff_dt <= dt.datetime.now(dt.timezone.utc):
        return lstore.update_lifecycle(
            row["lifecycle_id"], error_state="kickoff_passed_before_prematch", error_class="timing"
        ) or row

    row = _advance(row, "PREMATCH_DATA_READY")
    timestamp = _now()
    prediction = basketball_model.predict_game(
        game,
        prediction_timestamp=timestamp,
        data_cutoff_timestamp=timestamp,
        calibrator=_calibrator("basketball", row.get("league_id"), row.get("season"), timestamp),
    )
    if not isinstance(prediction, dict) or prediction.get("insufficient_data"):
        reason = prediction.get("reason") if isinstance(prediction, dict) else "invalid prediction"
        return _fail(row, RuntimeError(str(reason)), "model_error")

    row = _advance(row, "PREMATCH_PREDICTION_CREATED")
    key = _snapshot_key("basketball", game_id, "PRE_MATCH", kickoff)
    row = _persist_cycle_key(row, key)

    snapshot_id = storage.save_prediction_snapshot(
        fixture_id=game_id,
        prediction_timestamp=timestamp,
        kickoff_at=kickoff,
        sport="basketball",
        league_id=row.get("league_id"),
        season=row.get("season"),
        canonical_home_id=row.get("home_team_id"),
        canonical_away_id=row.get("away_team_id"),
        home_team=prediction.get("home_team"),
        away_team=prediction.get("away_team"),
        prediction_context="PRE_MATCH",
        data_cutoff_timestamp=prediction.get("data_cutoff_timestamp") or timestamp,
        markets=prediction.get("markets"),
        features=prediction.get("model_inputs"),
        confidence=prediction.get("confidence"),
        prediction_record=prediction,
        idempotency_key=key,
        lifecycle_id=row["lifecycle_id"],
        model_inputs=prediction.get("model_inputs"),
    )
    row = _advance(row, "PREMATCH_SNAPSHOT_SAVED")
    return {**row, "snapshot_id": snapshot_id}


def prematch(limit: Optional[int] = None) -> Dict[str, Any]:
    rows = lstore.list_lifecycles(
        stages=["DISCOVERED", "IDENTITY_RESOLVED", "TRACKED", "PREMATCH_DATA_READY", "PREMATCH_PREDICTION_CREATED"],
        limit=limit or 500,
    )
    processed = saved = unresolved = skipped = 0
    for row in rows:
        if row.get("stage") == "DISCOVERED" or not row.get("home_team_id") or not row.get("away_team_id"):
            unresolved += 1
            continue
        if _retry_pending(row):
            skipped += 1
            continue
        try:
            result = _prematch_football(row) if row["sport"] == "football" else _prematch_basketball(row)
            processed += 1
            if (result or {}).get("stage") == "PREMATCH_SNAPSHOT_SAVED":
                saved += 1
        except LifecycleTransitionError as exc:
            _fail(row, exc, "transition_error")
        except Exception as exc:
            _fail(row, exc)
    return {
        "stage": "prematch",
        "considered": len(rows),
        "processed": processed,
        "snapshots_saved": saved,
        "unresolved_skipped": unresolved,
        "retry_deferred": skipped,
    }


# ---------------------------------------------------------------------------
# Live monitoring and in-play snapshots
# ---------------------------------------------------------------------------

def _observation_fields(sport: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {"status": get_status_short(payload, sport=sport)}
    if sport == "football":
        goals = payload.get("goals") or {}
        status = (payload.get("fixture") or {}).get("status") or {}
        fields["score"] = {"home": goals.get("home"), "away": goals.get("away")}
        fields["clock"] = status.get("elapsed")
        fields["period"] = status.get("short")
        statistics = payload.get("statistics")
        if isinstance(statistics, (list, dict)):
            fields["statistics"] = statistics
        if isinstance(payload.get("events"), list):
            fields["events"] = payload.get("events")
    else:
        scores = payload.get("scores") or {}
        status = payload.get("status") or {}
        fields["score"] = {
            "home": (scores.get("home") or {}).get("total"),
            "away": (scores.get("away") or {}).get("total"),
        }
        fields["clock"] = status.get("timer") or status.get("long")
        fields["period"] = status.get("short")
    return fields


def _inplay_snapshot(row: Dict[str, Any], payload: Dict[str, Any]) -> Optional[int]:
    """Create a separate immutable in-play snapshot when the model supports it."""
    if row["sport"] != "football":
        # The basketball model has no in-play path; recording the limitation is
        # preferable to inventing an unsupported in-play prediction.
        lstore.record_observation(
            lifecycle_id=row["lifecycle_id"],
            provider=row.get("provider") or "api_basketball",
            context="live",
            observation_timestamp=_now(),
            provider_status=get_status_short(payload, sport="basketball"),
            observed_fields={"inplay_prediction": "unsupported_for_basketball_model"},
            provenance={"provider": row.get("provider"), "supported": False},
            provider_fixture_id=row["provider_fixture_id"],
            observation_key=f"{row['lifecycle_id']}|inplay_unsupported|{_now()[:16]}",
        )
        return None

    import main as app_main
    import storage

    kickoff = ((payload.get("fixture") or {}).get("date")) or row.get("kickoff_at")
    league_avg = float(
        getattr(config, "LEAGUE_AVG_GOALS", {}).get(row.get("league_id"), getattr(config, "LEAGUE_AVG_GOALS_FALLBACK", 1.35))
    )
    prediction = app_main.predict_fixture(payload, league_avg, provider_meta={"provider": row.get("provider")})
    if not isinstance(prediction, dict) or prediction.get("insufficient_data"):
        return None

    elapsed = ((payload.get("fixture") or {}).get("status") or {}).get("elapsed") or 0
    try:
        bucket = int(elapsed) // INPLAY_CYCLE_BUCKET_MINUTES
    except (TypeError, ValueError):
        bucket = 0
    key = f"{_snapshot_key('football', row['provider_fixture_id'], 'LIVE', kickoff)}:{bucket}"
    row = _persist_cycle_key(row, key)

    return storage.save_prediction_snapshot(
        fixture_id=int(row["provider_fixture_id"]),
        prediction_timestamp=_now(),
        kickoff_at=kickoff,
        sport="football",
        league_id=row.get("league_id"),
        season=row.get("season"),
        canonical_home_id=row.get("home_team_id"),
        canonical_away_id=row.get("away_team_id"),
        home_team=prediction.get("home_team"),
        away_team=prediction.get("away_team"),
        prediction_context="LIVE",
        data_cutoff_timestamp=prediction.get("data_cutoff_timestamp") or _now(),
        markets=prediction.get("markets"),
        confidence=prediction.get("confidence"),
        prediction_record=prediction,
        idempotency_key=key,
        lifecycle_id=row["lifecycle_id"],
        model_inputs=prediction.get("model_inputs"),
        features=prediction.get("model_inputs", {}).get("features") if isinstance(prediction.get("model_inputs"), dict) else None,
    )


def monitor(limit: Optional[int] = None) -> Dict[str, Any]:
    rows = lstore.list_lifecycles(
        stages=["PREMATCH_SNAPSHOT_SAVED", "LIVE", "INPLAY_OBSERVATION_RECORDED"],
        limit=limit or 500,
    )
    observed = inplay_snapshots = 0
    now = dt.datetime.now(dt.timezone.utc)

    for row in rows:
        if _retry_pending(row):
            continue
        sport = row["sport"]
        providers = _providers(sport, "live", "score", row.get("league_id"))
        payload = None
        for provider in providers:
            payload = _live_index(sport, provider).get(str(row["provider_fixture_id"]))
            if payload is not None:
                break

        if payload is not None:
            status = get_status_short(payload, sport=sport)
            if _is_stale(payload.get("retrieved_at"), "live", "score"):
                continue
            fields = _observation_fields(sport, payload)
            stamp = _now()
            lstore.record_observation(
                lifecycle_id=row["lifecycle_id"],
                provider=row.get("provider") or providers[0],
                context="live",
                observation_timestamp=stamp,
                provider_status=status,
                observed_fields=fields,
                payload=payload,
                parser_version=(provider_registry.get_provider(row.get("provider") or "") or {}).get("parser_version"),
                provenance={"provider": row.get("provider"), "retrieved_at": stamp, "lifecycle_context": "live"},
                provider_fixture_id=row["provider_fixture_id"],
                observation_key=f"{row['lifecycle_id']}|live|{stamp[:16]}",
            )
            observed += 1
            if row.get("stage") == "PREMATCH_SNAPSHOT_SAVED":
                row = _advance(row, "LIVE", provider_status=status)
            row = _advance(row, "INPLAY_OBSERVATION_RECORDED", provider_status=status)
            try:
                if _inplay_snapshot(row, payload) is not None:
                    inplay_snapshots += 1
            except LifecycleTransitionError:
                pass
            except Exception as exc:
                _fail(row, exc, "inplay_prediction_error")
            continue

        kickoff_dt = _parse(row.get("kickoff_at"))
        if kickoff_dt and now - kickoff_dt > dt.timedelta(hours=3):
            try:
                _advance(row, "FINAL_RESULT_PENDING")
            except LifecycleTransitionError:
                pass

    return {"stage": "live", "considered": len(rows), "live_observations": observed, "inplay_snapshots": inplay_snapshots}


# ---------------------------------------------------------------------------
# Final-result verification
# ---------------------------------------------------------------------------

def _verify_football(row: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    status = get_status_short(payload, sport="football")
    fixture = payload.get("fixture") or {}
    teams = payload.get("teams") or {}
    league = payload.get("league") or {}
    evidence = {
        "provider": row.get("provider") or "api_football",
        "sport": "football",
        "retrieved_at": _now(),
        "provider_fixture_id": str(fixture.get("id")),
        "status": status,
        "home_id": (teams.get("home") or {}).get("id"),
        "away_id": (teams.get("away") or {}).get("id"),
        "league_id": league.get("id"),
        "season": league.get("season"),
        "goals": payload.get("goals"),
        "score": payload.get("score"),
    }
    if str(fixture.get("id")) != str(row["provider_fixture_id"]):
        return False, {**evidence, "reason": "fixture_identity_mismatch"}
    if row.get("league_id") and str(league.get("id")) != str(row["league_id"]):
        return False, {**evidence, "reason": "competition_mismatch"}
    if row.get("season") and str(league.get("season")) != str(row["season"]):
        return False, {**evidence, "reason": "season_mismatch"}
    if is_postponed_or_cancelled(payload, sport="football"):
        return False, {**evidence, "reason": "postponed_or_cancelled"}
    if status not in FOOTBALL_FINISHED_STATUSES:
        return False, {**evidence, "reason": "not_finished"}
    if get_football_match_goals(payload) is None:
        return False, {**evidence, "reason": "missing_goals"}
    return True, evidence


def _verify_basketball(row: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    status = get_status_short(payload, sport="basketball")
    teams = payload.get("teams") or {}
    league = payload.get("league") or {}
    evidence = {
        "provider": row.get("provider") or "api_basketball",
        "sport": "basketball",
        "retrieved_at": _now(),
        "provider_fixture_id": str(payload.get("id")),
        "status": status,
        "home_id": (teams.get("home") or {}).get("id"),
        "away_id": (teams.get("away") or {}).get("id"),
        "league_id": league.get("id"),
        "season": league.get("season"),
        "scores": payload.get("scores"),
    }
    if str(payload.get("id")) != str(row["provider_fixture_id"]):
        return False, {**evidence, "reason": "game_identity_mismatch"}
    if row.get("league_id") and str(league.get("id")) != str(row["league_id"]):
        return False, {**evidence, "reason": "competition_mismatch"}
    if row.get("season") and str(league.get("season")) != str(row["season"]):
        return False, {**evidence, "reason": "season_mismatch"}
    if is_postponed_or_cancelled(payload, sport="basketball"):
        return False, {**evidence, "reason": "postponed_or_cancelled"}
    if status not in BASKETBALL_FINISHED_STATUSES:
        return False, {**evidence, "reason": "not_finished"}
    if get_basketball_match_points(payload) is None:
        return False, {**evidence, "reason": "missing_points"}
    return True, evidence


def _record_verification(row: Dict[str, Any], payload: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    lstore.record_observation(
        lifecycle_id=row["lifecycle_id"],
        provider=evidence["provider"],
        context="final_result",
        observation_timestamp=evidence["retrieved_at"],
        provider_status=evidence.get("status"),
        observed_fields=evidence,
        payload=payload,
        parser_version=(provider_registry.get_provider(evidence["provider"]) or {}).get("parser_version"),
        provenance={"provider": evidence["provider"], "verified": True},
        provider_fixture_id=row["provider_fixture_id"],
        observation_key=f"{row['lifecycle_id']}|final_result|{evidence.get('status')}",
    )
    return _advance(row, "FINAL_RESULT_VERIFIED", provider_status=evidence.get("status"))


# ---------------------------------------------------------------------------
# Grading from the durable verified observation
# ---------------------------------------------------------------------------

def _predicted_selection(markets: Dict[str, Any], market: str) -> Optional[Dict[str, Any]]:
    values = markets.get(market)
    if not isinstance(values, dict):
        return None

    keys = {
        "match_result": ("home_win", "draw", "away_win"),
        "double_chance": ("home_or_draw", "home_or_away", "away_or_draw", "draw_or_away"),
        "btts": ("yes", "no"),
        "over_under": ("over", "under"),
        "moneyline": ("home_win", "away_win"),
        "total_points": ("over", "under"),
    }.get(market)
    if not keys:
        return None

    numeric = {
        key: values.get(key)
        for key in keys
        if isinstance(values.get(key), (int, float)) and not isinstance(values.get(key), bool)
    }
    if not numeric:
        return None
    selection = max(numeric, key=numeric.get)
    return {"selection": selection, "probability": float(numeric[selection]), "line": values.get("line")}


def _line_key(line: Any) -> Optional[str]:
    if not isinstance(line, (int, float)):
        return None
    return str(line).replace(".", "_")


def _settle_football(market: str, selection: str, graded: Dict[str, Any], line: Any) -> Optional[bool]:
    """Settle from the central market_grading record (policy-driven)."""
    goal_markets = graded.get("goal_markets") or {}
    if market == "match_result":
        record = goal_markets.get("match_result")
        return None if not isinstance(record, dict) else record.get("outcome") == selection
    if market == "double_chance":
        record = (goal_markets.get("double_chance") or {}).get(selection)
        return None if not isinstance(record, dict) else bool(record.get("won"))
    if market == "btts":
        record = (goal_markets.get("btts") or {}).get(selection)
        return None if not isinstance(record, dict) else bool(record.get("won"))
    if market == "over_under":
        key_line = _line_key(line)
        if key_line is None:
            return None
        record = (goal_markets.get("over_under") or {}).get(f"{selection}_{key_line}")
        return None if not isinstance(record, dict) else bool(record.get("won"))
    return None


def _settle_basketball(market: str, selection: str, graded: Dict[str, Any], line: Any) -> Optional[bool]:
    if not graded.get("settleable"):
        return None
    if market == "moneyline":
        record = graded.get("moneyline") or {}
        return bool(record.get(selection)) if selection in record else None
    if market == "total_points":
        total = (graded.get("total_points") or {}).get("total")
        if not isinstance(line, (int, float)) or total is None:
            return None
        difference = total - float(line)
        if difference == 0:
            return None
        return selection == ("over" if difference > 0 else "under")
    return None


def _grade_row(row: Dict[str, Any], payload: Dict[str, Any]) -> int:
    """Grade every eligible snapshot once from the verified result payload."""
    import storage

    graded = (
        market_grading.grade_fixture_markets(payload)
        if row["sport"] == "football"
        else market_grading.grade_basketball_markets(payload)
    )
    try:
        snapshots = storage.get_prediction_snapshots(
            fixture_id=int(row["provider_fixture_id"]), sport=row["sport"], limit=100
        )
    except Exception:
        snapshots = []

    graded_markets = 0
    for snapshot in snapshots:
        markets = snapshot.get("markets") or {}
        for market in ("match_result", "double_chance", "btts", "over_under", "moneyline", "total_points"):
            selection = _predicted_selection(markets, market)
            if selection is None:
                continue
            key = f"{snapshot['id']}:{market}:{GRADING_RULES_VERSION}"
            if lstore.get_grading(key) is not None:
                continue
            won = (
                _settle_football(market, selection["selection"], graded, selection.get("line"))
                if row["sport"] == "football"
                else _settle_basketball(market, selection["selection"], graded, selection.get("line"))
            )
            lstore.save_grading(
                {
                    "snapshot_id": snapshot["id"],
                    "lifecycle_id": row["lifecycle_id"],
                    "sport": row["sport"],
                    "market": market,
                    "predicted_output": selection,
                    "actual_outcome": {
                        "won": won,
                        "status": get_status_short(payload, sport=row["sport"]),
                        "regulation_goals": get_regulation_goals(payload) if row["sport"] == "football" else None,
                        "match_goals": (
                            get_football_match_goals(payload)
                            if row["sport"] == "football"
                            else get_basketball_match_points(payload)
                        ),
                        "central_grading": graded,
                    },
                    "settlement_status": "PENDING" if won is None else ("WON" if won else "LOST"),
                    "settlement_rules_version": GRADING_RULES_VERSION,
                    "result_provenance": {
                        "provider": row.get("provider"),
                        "status": get_status_short(payload, sport=row["sport"]),
                    },
                    "grading_key": key,
                }
            )
            graded_markets += 1
    return graded_markets


# ---------------------------------------------------------------------------
# Result pipeline: verify -> grade -> persist -> historical update
# ---------------------------------------------------------------------------

def verify_results(limit: Optional[int] = None) -> Dict[str, Any]:
    import storage

    rows = lstore.list_lifecycles(
        stages=["FINAL_RESULT_PENDING", "FINAL_RESULT_VERIFIED", "PREDICTION_GRADED"],
        limit=limit or 500,
    )
    verified = graded_rows = persisted = retrying = 0

    for row in rows:
        if _retry_pending(row):
            retrying += 1
            continue
        try:
            if row.get("stage") == "FINAL_RESULT_PENDING":
                payload = None
                for provider in _providers(row["sport"], "final_result", "score", row.get("league_id")):
                    payload = _fetch_result(row["sport"], provider, int(row["provider_fixture_id"]))
                    if payload:
                        break
                if not payload:
                    _fail(row, RuntimeError("final result provider unavailable"))
                    retrying += 1
                    continue
                ok, evidence = (
                    _verify_football(row, payload)
                    if row["sport"] == "football"
                    else _verify_basketball(row, payload)
                )
                if not ok:
                    lstore.update_lifecycle(
                        row["lifecycle_id"],
                        provider_status=evidence.get("status"),
                        error_state=evidence.get("reason"),
                        error_class="result_not_verified",
                    )
                    retrying += 1
                    continue
                row = _record_verification(row, payload, evidence)
                verified += 1

            observation = lstore.get_latest_final_observation(row["lifecycle_id"])
            if not observation or not isinstance(observation.get("payload"), dict):
                lstore.update_lifecycle(
                    row["lifecycle_id"], error_state="verified_observation_missing", error_class="recovery"
                )
                retrying += 1
                continue
            payload = observation["payload"]

            if row.get("stage") == "FINAL_RESULT_VERIFIED":
                _grade_row(row, payload)
                row = _advance(row, "PREDICTION_GRADED")
                graded_rows += 1

            save = storage.save_verified_live_result(
                lifecycle_id=row["lifecycle_id"],
                sport=row["sport"],
                fixture=payload,
                evidence=observation.get("observed_fields") or {},
                source=observation.get("provider"),
            )
            if save.get("saved"):
                _advance(row, "HISTORICAL_RECORD_UPDATED")
                persisted += 1
            else:
                # Stay PREDICTION_GRADED so the next eligible run retries the write.
                lstore.update_lifecycle(
                    row["lifecycle_id"],
                    error_state=str(save.get("reason"))[:200],
                    error_class="historical_persistence_failed",
                )
                retrying += 1
        except LifecycleTransitionError as exc:
            _fail(row, exc, "transition_error")
            retrying += 1
        except Exception as exc:
            _fail(row, exc)
            retrying += 1

    return {
        "stage": "verify-results",
        "considered": len(rows),
        "verified": verified,
        "graded_rows": graded_rows,
        "persisted": persisted,
        "deferred": retrying,
    }


# ---------------------------------------------------------------------------
# Controlled learning
# ---------------------------------------------------------------------------

def learn(limit: Optional[int] = None) -> Dict[str, Any]:
    import calibration

    evaluations: Dict[str, Dict[str, Any]] = {}
    for sport in ("football", "basketball"):
        try:
            evaluations[sport] = calibration.evaluate_and_promote(sport=sport, cutoff_timestamp=_now())
        except Exception as exc:
            evaluations[sport] = {"sport": sport, "status": "error", "reason": str(exc)}

    rows = lstore.list_lifecycles(
        stages=["HISTORICAL_RECORD_UPDATED", "MODEL_EVALUATED"],
        limit=limit or 500,
    )
    completed = 0
    for row in rows:
        outcome = evaluations.get(row["sport"]) or {}
        status = outcome.get("status")
        try:
            if row.get("stage") == "HISTORICAL_RECORD_UPDATED":
                row = _advance(row, "MODEL_EVALUATED")
            if status in ("promoted", "candidate_kept"):
                _advance(row, "LEARNING_UPDATE_COMPLETED", error_state=None)
                completed += 1
            else:
                lstore.update_lifecycle(
                    row["lifecycle_id"],
                    error_state=str(outcome.get("reason") or status or "learning_not_completed")[:200],
                    error_class="learning_not_completed",
                )
        except LifecycleTransitionError as exc:
            _fail(row, exc, "transition_error")

    return {"stage": "learn", "evaluations": evaluations, "learning_completed": completed}
