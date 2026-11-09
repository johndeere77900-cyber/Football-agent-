"""
Autonomous live-match lifecycle for football and basketball.

Implements the durable, resumable stages of Phase 2 on top of the existing
prediction, provider and grading services:

    DISCOVERED -> IDENTITY_RESOLVED -> TRACKED -> PREMATCH_DATA_READY
    -> PREMATCH_PREDICTION_CREATED -> PREMATCH_SNAPSHOT_SAVED -> LIVE
    -> INPLAY_OBSERVATION_RECORDED -> FINAL_RESULT_PENDING
    -> FINAL_RESULT_VERIFIED -> PREDICTION_GRADED
    -> HISTORICAL_RECORD_UPDATED -> MODEL_EVALUATED
    -> LEARNING_UPDATE_COMPLETED

State is durable in ``live_match_lifecycle``; every stage is idempotent and a
restart resumes from the stored stage instead of repeating side effects. A
match is never treated as finished because a provider stopped listing it: it
must reach ``FINAL_RESULT_VERIFIED`` through verified final-result evidence.

This module performs no historical acquisition. The only historical write is
``storage.save_verified_live_result()``, which requires a tracked lifecycle
record plus verified result evidence.
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

STAGE_INDEX = {stage: i for i, stage in enumerate(STAGES)}

FOOTBALL_COMPETITIONS = list(getattr(config, "ALLOWED_LEAGUE_IDS", [39, 140, 135, 78, 61, 2, 3, 5, 1, 4, 88, 94]))
BASKETBALL_COMPETITIONS = list(getattr(config, "ALLOWED_BASKETBALL_LEAGUE_IDS", [12]))

DISCOVERY_HORIZON_DAYS = 14
GRADING_RULES_VERSION = "phase2.market_grading.v1"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse(value: Any) -> Optional[dt.datetime]:
    from time_utils import parse_utc_datetime

    return parse_utc_datetime(value) if value else None


def _items(result: Any) -> List[Dict[str, Any]]:
    """Normalise a provider/resolver return value into a list of records."""
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


def _stage_at_least(row: Dict[str, Any], stage: str) -> bool:
    return STAGE_INDEX.get(row.get("stage") or "", -1) >= STAGE_INDEX.get(stage, 99)


def _advance(row: Dict[str, Any], stage: str, **extra) -> Dict[str, Any]:
    if row.get("stage") == stage:
        return row
    updated = lstore.update_lifecycle(row["lifecycle_id"], stage=stage, **extra)
    return updated or row


def _fail(row: Dict[str, Any], error: Exception, error_class: str = "provider_error") -> Dict[str, Any]:
    retry = int(row.get("retry_count") or 0) + 1
    next_retry = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=min(60, 2 ** min(retry, 5)))
    ).isoformat()
    return lstore.update_lifecycle(
        row["lifecycle_id"],
        retry_count=retry,
        error_state=str(error)[:500],
        error_class=error_class,
        next_retry_at=next_retry,
    ) or row


def calendar_days(horizon_days: int = DISCOVERY_HORIZON_DAYS, today: Optional[dt.date] = None) -> List[str]:
    base = today or dt.datetime.now(dt.timezone.utc).date()
    return [(base + dt.timedelta(days=offset)).isoformat() for offset in range(0, int(horizon_days) + 1)]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _football_fixtures_for_date(date_str: str) -> List[Dict[str, Any]]:
    try:
        from data_resolver import DataResolver

        resolver = DataResolver()
        fixtures = _items(resolver.get_fixtures_for_date(date_str))
        provider_registry.record_health("api_football", ok=True)
        return [f for f in fixtures if (f.get("league") or {}).get("id") in FOOTBALL_COMPETITIONS]
    except Exception as exc:  # provider/network failure is recoverable
        provider_registry.record_health("api_football", ok=False, failure_category=type(exc).__name__)
        return []


def _basketball_games_for_date(date_str: str) -> List[Dict[str, Any]]:
    import basketball_api

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
    return {
        "sport": "football",
        "provider": "api_football",
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


def _canonical_team_id(name: Optional[str], provider: str, provider_id: Any, league_id: Any) -> Optional[Any]:
    if not name:
        return None
    try:
        import team_identity

        return team_identity.resolve_canonical_team_id(name, provider, provider_id, league_id=league_id)
    except Exception:
        return None


def _persist_identity(identity: Dict[str, Any]) -> Dict[str, Any]:
    home_canonical = _canonical_team_id(
        identity.get("home_name"), identity["provider"], identity.get("home_provider_id"), identity.get("league_id")
    )
    away_canonical = _canonical_team_id(
        identity.get("away_name"), identity["provider"], identity.get("away_provider_id"), identity.get("league_id")
    )
    stage = "IDENTITY_RESOLVED" if home_canonical and away_canonical else "DISCOVERED"
    record = {
        "lifecycle_id": f"{identity['sport']}:{identity['provider_fixture_id']}",
        "sport": identity["sport"],
        "canonical_fixture_key": identity["provider_fixture_id"],
        "provider": identity["provider"],
        "provider_fixture_id": identity["provider_fixture_id"],
        "league_id": identity.get("league_id"),
        "season": identity.get("season"),
        "home_team_id": home_canonical or identity.get("home_provider_id"),
        "away_team_id": away_canonical or identity.get("away_provider_id"),
        "kickoff_at": identity.get("kickoff_at"),
        "stage": stage,
        "provider_status": identity.get("status"),
    }
    row = lstore.upsert_lifecycle(record)
    if row.get("stage") in ("DISCOVERED", "IDENTITY_RESOLVED") and stage == "IDENTITY_RESOLVED":
        row = _advance(row, "TRACKED")
    return row


def discover(sport: Optional[str] = None, horizon_days: int = DISCOVERY_HORIZON_DAYS) -> Dict[str, Any]:
    """Discover fixtures across a rolling horizon for every configured competition."""
    sports = [sport] if sport in ("football", "basketball") else ["football", "basketball"]
    discovered: List[Dict[str, Any]] = []
    failures = 0

    for day in calendar_days(horizon_days):
        if "football" in sports:
            for fixture in _football_fixtures_for_date(day):
                identity = _football_identity(fixture)
                if identity is None:
                    failures += 1
                    continue
                discovered.append(_persist_identity(identity))
        if "basketball" in sports:
            for game in _basketball_games_for_date(day):
                identity = _basketball_identity(game)
                if identity is None:
                    failures += 1
                    continue
                discovered.append(_persist_identity(identity))

    unique = {row["lifecycle_id"]: row for row in discovered}
    return {
        "stage": "discover",
        "horizon_days": horizon_days,
        "competitions": {"football": FOOTBALL_COMPETITIONS, "basketball": BASKETBALL_COMPETITIONS},
        "discovered": len(unique),
        "identity_failures": failures,
    }


# ---------------------------------------------------------------------------
# Pre-match prediction
# ---------------------------------------------------------------------------

def _snapshot_key(sport: str, fixture_id: Any, context: str, kickoff: Any, prediction_timestamp: str) -> str:
    minute = (prediction_timestamp or "")[:16]
    return f"{sport}:{fixture_id}:{context}:{kickoff or 'unknown'}:{minute}"


def _calibrator(sport: str, league_id: Any, season: Any, cutoff: Any):
    try:
        import calibration

        return calibration.load_active_calibrator(sport=sport, league_id=league_id, season=season, cutoff_timestamp=cutoff)
    except Exception:
        return None


def _prematch_football(row: Dict[str, Any]) -> Dict[str, Any]:
    import storage

    fixture_id = int(row["provider_fixture_id"])
    fixture = _football_result(fixture_id)
    if not fixture:
        return _fail(row, RuntimeError("prematch fixture payload unavailable"))

    kickoff = ((fixture.get("fixture") or {}).get("date")) or row.get("kickoff_at")
    kickoff_dt = _parse(kickoff)
    now = dt.datetime.now(dt.timezone.utc)
    if kickoff_dt and kickoff_dt <= now:
        return lstore.update_lifecycle(row["lifecycle_id"], stage="TRACKED", error_state="kickoff_passed_before_prematch") or row

    row = _advance(row, "PREMATCH_DATA_READY")
    import main as app_main

    league_avg = float(
        getattr(config, "LEAGUE_AVG_GOALS", {}).get(row.get("league_id"), getattr(config, "LEAGUE_AVG_GOALS_FALLBACK", 1.35))
    )
    prediction = app_main.predict_fixture(fixture, league_avg, provider_meta={"provider": "api_football"})
    if not isinstance(prediction, dict) or prediction.get("insufficient_data"):
        return _fail(row, RuntimeError(str(prediction.get("reason") if isinstance(prediction, dict) else "invalid prediction")), "model_error")

    row = _advance(row, "PREMATCH_PREDICTION_CREATED")
    timestamp = _now()
    key = _snapshot_key("football", fixture_id, "PRE_MATCH", kickoff, timestamp)
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
        features=prediction.get("features"),
        confidence=prediction.get("confidence"),
        prediction_record=prediction,
        idempotency_key=key,
        lifecycle_id=row["lifecycle_id"],
        model_inputs=prediction.get("model_inputs"),
    )
    return _advance(row, "PREMATCH_SNAPSHOT_SAVED", error_state=None, error_class=None, retry_count=0) | {"snapshot_id": snapshot_id}


def _prematch_basketball(row: Dict[str, Any]) -> Dict[str, Any]:
    import basketball_api
    import basketball_model
    import storage

    game_id = int(row["provider_fixture_id"])
    game = basketball_api.get_game_result(game_id)
    if not game:
        return _fail(row, RuntimeError("prematch game payload unavailable"))

    kickoff = game.get("date") or row.get("kickoff_at")
    kickoff_dt = _parse(kickoff)
    now = dt.datetime.now(dt.timezone.utc)
    if kickoff_dt and kickoff_dt <= now:
        return lstore.update_lifecycle(row["lifecycle_id"], stage="TRACKED", error_state="kickoff_passed_before_prematch") or row

    row = _advance(row, "PREMATCH_DATA_READY")
    timestamp = _now()
    prediction = basketball_model.predict_game(
        game,
        prediction_timestamp=timestamp,
        data_cutoff_timestamp=timestamp,
        calibrator=_calibrator("basketball", row.get("league_id"), row.get("season"), timestamp),
    )
    if not isinstance(prediction, dict) or prediction.get("insufficient_data"):
        return _fail(row, RuntimeError(str(prediction.get("reason") if isinstance(prediction, dict) else "invalid prediction")), "model_error")

    row = _advance(row, "PREMATCH_PREDICTION_CREATED")
    key = _snapshot_key("basketball", game_id, "PRE_MATCH", kickoff, timestamp)
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
    return _advance(row, "PREMATCH_SNAPSHOT_SAVED", error_state=None, error_class=None, retry_count=0) | {"snapshot_id": snapshot_id}


def prematch(limit: Optional[int] = None) -> Dict[str, Any]:
    rows = lstore.list_lifecycles(
        stages=["DISCOVERED", "IDENTITY_RESOLVED", "TRACKED", "PREMATCH_DATA_READY", "PREMATCH_PREDICTION_CREATED"],
        limit=limit or 500,
    )
    processed = 0
    skipped = 0
    saved = 0
    for row in rows:
        if _stage_at_least(row, "PREMATCH_SNAPSHOT_SAVED"):
            skipped += 1
            continue
        try:
            result = _prematch_football(row) if row["sport"] == "football" else _prematch_basketball(row)
            processed += 1
            if (result or {}).get("stage") == "PREMATCH_SNAPSHOT_SAVED":
                saved += 1
        except Exception as exc:
            _fail(row, exc)
    return {"stage": "prematch", "considered": len(rows), "processed": processed, "snapshots_saved": saved, "skipped": skipped}


# ---------------------------------------------------------------------------
# Live monitoring
# ---------------------------------------------------------------------------

def _football_result(fixture_id: int) -> Optional[Dict[str, Any]]:
    import api_football

    try:
        return api_football.get_fixture_result(fixture_id)
    except Exception as exc:
        provider_registry.record_health("api_football", ok=False, failure_category=type(exc).__name__)
        return None


def _basketball_result(game_id: int) -> Optional[Dict[str, Any]]:
    import basketball_api

    try:
        return basketball_api.get_game_result(game_id)
    except Exception as exc:
        provider_registry.record_health("api_basketball", ok=False, failure_category=type(exc).__name__)
        return None


def _live_index(sport: str) -> Dict[str, Dict[str, Any]]:
    if sport == "football":
        import api_football

        try:
            items = _items(api_football.get_live_fixtures())
        except Exception:
            return {}
        index = {}
        for fixture in items:
            fid = (fixture.get("fixture") or {}).get("id")
            if fid is not None:
                index[str(fid)] = fixture
        return index

    import basketball_api

    try:
        items = _items(basketball_api.get_live_games())
    except Exception:
        return {}
    index = {}
    for game in items:
        gid = game.get("id")
        if gid is not None:
            index[str(gid)] = game
    return index


def _observation_fields(sport: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {"status": get_status_short(payload, sport=sport)}
    if sport == "football":
        goals = payload.get("goals") or {}
        fields["score"] = {"home": goals.get("home"), "away": goals.get("away")}
        status = (payload.get("fixture") or {}).get("status") or {}
        fields["clock"] = status.get("elapsed")
        if isinstance(payload.get("statistics"), (list, dict)):
            fields["statistics"] = payload.get("statistics")
    else:
        scores = payload.get("scores") or {}
        fields["score"] = {
            "home": (scores.get("home") or {}).get("total"),
            "away": (scores.get("away") or {}).get("total"),
        }
        status = payload.get("status") or {}
        fields["clock"] = status.get("timer") or status.get("long")
    return fields


def monitor(limit: Optional[int] = None) -> Dict[str, Any]:
    rows = lstore.list_lifecycles(
        stages=["TRACKED", "PREMATCH_DATA_READY", "PREMATCH_PREDICTION_CREATED", "PREMATCH_SNAPSHOT_SAVED", "LIVE"],
        limit=limit or 500,
    )
    live_by_sport = {"football": _live_index("football"), "basketball": _live_index("basketball")}
    observed = 0
    now = dt.datetime.now(dt.timezone.utc)

    for row in rows:
        payload = live_by_sport.get(row["sport"], {}).get(str(row["provider_fixture_id"]))
        if payload is not None:
            fields = _observation_fields(row["sport"], payload)
            stamp = _now()
            lstore.record_observation(
                lifecycle_id=row["lifecycle_id"],
                provider=row["provider"] or row["sport"],
                context="live",
                observation_timestamp=stamp,
                provider_status=fields.get("status"),
                observed_fields=fields,
                payload=payload,
                parser_version=provider_registry.get_provider(row["provider"] or "")["parser_version"]
                if provider_registry.get_provider(row["provider"] or "")
                else None,
                provenance={"provider": row["provider"], "retrieved_at": stamp, "lifecycle_context": "live"},
                provider_fixture_id=row["provider_fixture_id"],
                observation_key=f"{row['lifecycle_id']}|live|{stamp[:16]}",
            )
            observed += 1
            if row.get("stage") != "LIVE":
                _advance(row, "LIVE", provider_status=fields.get("status"))
            elif row.get("provider_status") != fields.get("status"):
                lstore.update_lifecycle(row["lifecycle_id"], provider_status=fields.get("status"))
            continue

        kickoff_dt = _parse(row.get("kickoff_at"))
        if kickoff_dt and now - kickoff_dt > dt.timedelta(hours=3):
            lstore.update_lifecycle(row["lifecycle_id"], stage="FINAL_RESULT_PENDING")

    return {"stage": "live", "considered": len(rows), "live_observations": observed}


# ---------------------------------------------------------------------------
# Final-result verification
# ---------------------------------------------------------------------------

def _verify_football_result(row: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    status = get_status_short(payload, sport="football")
    fixture = payload.get("fixture") or {}
    teams = payload.get("teams") or {}
    evidence = {
        "provider": "api_football",
        "retrieved_at": _now(),
        "provider_fixture_id": str(fixture.get("id")),
        "status": status,
        "home_id": (teams.get("home") or {}).get("id"),
        "away_id": (teams.get("away") or {}).get("id"),
        "score": payload.get("score"),
        "goals": payload.get("goals"),
    }
    if str(fixture.get("id")) != str(row["provider_fixture_id"]):
        return False, {**evidence, "reason": "fixture_identity_mismatch"}
    if is_postponed_or_cancelled(payload, sport="football"):
        return False, {**evidence, "reason": "postponed_or_cancelled"}
    if status not in FOOTBALL_FINISHED_STATUSES:
        return False, {**evidence, "reason": "not_finished"}
    if get_football_match_goals(payload) is None:
        return False, {**evidence, "reason": "missing_goals"}
    if status in ("AET", "PEN") and get_regulation_goals(payload) is None:
        evidence["reason"] = "missing_regulation_score"
        evidence["markets_requiring_regulation"] = "fail_closed"
    return True, evidence


def _verify_basketball_result(row: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    status = get_status_short(payload, sport="basketball")
    teams = payload.get("teams") or {}
    evidence = {
        "provider": "api_basketball",
        "retrieved_at": _now(),
        "provider_fixture_id": str(payload.get("id")),
        "status": status,
        "home_id": (teams.get("home") or {}).get("id"),
        "away_id": (teams.get("away") or {}).get("id"),
        "scores": payload.get("scores"),
    }
    if str(payload.get("id")) != str(row["provider_fixture_id"]):
        return False, {**evidence, "reason": "game_identity_mismatch"}
    if is_postponed_or_cancelled(payload, sport="basketball"):
        return False, {**evidence, "reason": "postponed_or_cancelled"}
    if status not in BASKETBALL_FINISHED_STATUSES:
        return False, {**evidence, "reason": "not_finished"}
    if get_basketball_match_points(payload) is None:
        return False, {**evidence, "reason": "missing_points"}
    return True, evidence


def verify_results(limit: Optional[int] = None) -> Dict[str, Any]:
    import storage

    rows = lstore.list_lifecycles(
        stages=["LIVE", "INPLAY_OBSERVATION_RECORDED", "FINAL_RESULT_PENDING"],
        limit=limit or 500,
    )
    verified = 0
    pending = 0
    for row in rows:
        try:
            if row["sport"] == "football":
                payload = _football_result(int(row["provider_fixture_id"]))
                ok, evidence = (_verify_football_result(row, payload) if payload else (False, {"reason": "provider_unavailable"}))
            else:
                payload = _basketball_result(int(row["provider_fixture_id"]))
                ok, evidence = (_verify_basketball_result(row, payload) if payload else (False, {"reason": "provider_unavailable"}))

            if not ok:
                pending += 1
                lstore.update_lifecycle(row["lifecycle_id"], provider_status=evidence.get("status"), error_state=evidence.get("reason"))
                continue

            row = _advance(row, "FINAL_RESULT_VERIFIED")
            lstore.record_observation(
                lifecycle_id=row["lifecycle_id"],
                provider=evidence["provider"],
                context="final_result",
                observation_timestamp=evidence["retrieved_at"],
                provider_status=evidence.get("status"),
                observed_fields=evidence,
                payload=payload,
                provenance={"provider": evidence["provider"], "verified": True},
                provider_fixture_id=row["provider_fixture_id"],
                observation_key=f"{row['lifecycle_id']}|final_result|{evidence.get('status')}|{evidence['retrieved_at'][:16]}",
            )
            save = storage.save_verified_live_result(
                lifecycle_id=row["lifecycle_id"],
                sport=row["sport"],
                fixture=payload,
                evidence=evidence,
                source=evidence["provider"],
            )
            if save.get("saved"):
                _advance(lstore.get_lifecycle(row["lifecycle_id"]) or row, "HISTORICAL_RECORD_UPDATED")
                verified += 1
            else:
                lstore.update_lifecycle(row["lifecycle_id"], error_state=save.get("reason"))
        except Exception as exc:
            _fail(row, exc)
    return {"stage": "verify-results", "considered": len(rows), "verified": verified, "still_pending": pending}


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def _predicted_selection(markets: Dict[str, Any], market: str) -> Optional[Dict[str, Any]]:
    values = markets.get(market)
    if not isinstance(values, dict):
        return None

    if market == "match_result":
        candidates = {k: values.get(k) for k in ("home_win", "draw", "away_win")}
    elif market == "double_chance":
        candidates = {k: values.get(k) for k in ("home_or_draw", "home_or_away", "draw_or_away")}
    elif market == "btts":
        candidates = {k: values.get(k) for k in ("yes", "no")}
    elif market == "over_under":
        candidates = {k: values.get(k) for k in ("over", "under")}
    elif market == "moneyline":
        candidates = {k: values.get(k) for k in ("home_win", "away_win")}
    elif market == "total_points":
        candidates = {k: values.get(k) for k in ("over", "under")}
    else:
        return None

    numeric = {k: v for k, v in candidates.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if not numeric:
        return None
    selection = max(numeric, key=numeric.get)
    return {"selection": selection, "probability": float(numeric[selection]), "line": values.get("line")}


def _settle(sport: str, market: str, selection: str, payload: Dict[str, Any], line: Any) -> Optional[bool]:
    if sport == "football":
        reg = get_regulation_goals(payload)
        total = get_football_match_goals(payload)
        if market in ("match_result", "double_chance"):
            if reg is None:
                return None
            home, away = reg
            outcome = "home_win" if home > away else ("away_win" if away > home else "draw")
            if market == "match_result":
                return selection == outcome
            return (
                (outcome == "home_win" and selection == "home_or_draw")
                or (outcome == "draw" and selection in ("home_or_draw", "draw_or_away"))
                or (outcome == "away_win" and selection in ("home_or_away", "draw_or_away"))
            )
        if total is None:
            return None
        home, away = total
        if market == "btts":
            both = home > 0 and away > 0
            return selection == ("yes" if both else "no")
        if market == "over_under":
            if not isinstance(line, (int, float)):
                return None
            goals = home + away
            if goals == float(line):
                return None  # push is not a win or a loss
            return selection == ("over" if goals > float(line) else "under")
        return None

    points = get_basketball_match_points(payload)
    if points is None:
        return None
    home, away = points
    if market == "moneyline":
        outcome = "home_win" if home > away else "away_win"
        return selection == outcome
    if market == "total_points":
        if not isinstance(line, (int, float)):
            return None
        total = home + away
        if total == float(line):
            return None
        return selection == ("over" if total > float(line) else "under")
    return None


def grade(limit: Optional[int] = None) -> Dict[str, Any]:
    import storage

    rows = lstore.list_lifecycles(
        stages=["HISTORICAL_RECORD_UPDATED", "PREDICTION_GRADED", "FINAL_RESULT_VERIFIED"],
        limit=limit or 500,
    )
    graded_rows = 0
    graded_markets = 0

    for row in rows:
        payload = (
            _football_result(int(row["provider_fixture_id"]))
            if row["sport"] == "football"
            else _basketball_result(int(row["provider_fixture_id"]))
        )
        if not payload:
            continue
        ok = is_finished_match(payload, sport=row["sport"])
        if not ok:
            continue

        try:
            snapshots = storage.get_prediction_snapshots(fixture_id=int(row["provider_fixture_id"]), sport=row["sport"], limit=50)
        except Exception:
            snapshots = []

        evidence = {"provider": row["provider"], "status": get_status_short(payload, sport=row["sport"]), "graded_at": _now()}
        for snapshot in snapshots:
            markets = snapshot.get("markets") or {}
            for market in ("match_result", "double_chance", "btts", "over_under", "moneyline", "total_points"):
                selection = _predicted_selection(markets, market)
                if selection is None:
                    continue
                key = f"{snapshot['id']}:{market}:{GRADING_RULES_VERSION}"
                if lstore.get_grading(key) is not None:
                    continue
                won = _settle(row["sport"], market, selection["selection"], payload, selection.get("line"))
                lstore.save_grading(
                    {
                        "snapshot_id": snapshot["id"],
                        "lifecycle_id": row["lifecycle_id"],
                        "sport": row["sport"],
                        "market": market,
                        "predicted_output": selection,
                        "actual_outcome": {
                            "won": won,
                            "status": evidence["status"],
                            "regulation_goals": get_regulation_goals(payload) if row["sport"] == "football" else None,
                            "match_goals": get_football_match_goals(payload) if row["sport"] == "football" else get_basketball_match_points(payload),
                        },
                        "settlement_status": "PENDING" if won is None else ("WON" if won else "LOST"),
                        "settlement_rules_version": GRADING_RULES_VERSION,
                        "result_provenance": evidence,
                        "grading_key": key,
                    }
                )
                graded_markets += 1

        if row.get("stage") != "PREDICTION_GRADED":
            _advance(row, "PREDICTION_GRADED")
        graded_rows += 1

    return {"stage": "grade", "lifecycles": graded_rows, "markets_graded": graded_markets}


# ---------------------------------------------------------------------------
# Controlled learning
# ---------------------------------------------------------------------------

def learn(limit: Optional[int] = None) -> Dict[str, Any]:
    import calibration

    outcomes = []
    for sport in ("football", "basketball"):
        try:
            outcomes.append(calibration.evaluate_and_promote(sport=sport, cutoff_timestamp=_now()))
        except Exception as exc:
            outcomes.append({"sport": sport, "status": "error", "reason": str(exc)})

    for row in lstore.list_lifecycles(stages=["HISTORICAL_RECORD_UPDATED", "PREDICTION_GRADED"], limit=limit or 500):
        if row.get("stage") == "PREDICTION_GRADED":
            _advance(row, "MODEL_EVALUATED")
    return {"stage": "learn", "evaluations": outcomes}
