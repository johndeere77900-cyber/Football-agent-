"""Corrected-observation recording and controlled grading replay.

Providers occasionally correct a score, status or kickoff after a result was
verified. The lifecycle must handle that without:

* rewriting an immutable PRE_MATCH/LIVE prediction snapshot;
* overwriting the only copy of the earlier observation;
* silently re-grading or double-grading;
* pretending the model knew the corrected value at prediction time.

This module therefore only appends evidence:

1. ``record_correction`` stores the corrected provider observation with its
   reason, source, retrieval time and a link to the observation it corrects,
   and returns an impact report. It never mutates snapshots, grading rows or
   lifecycle stage.
2. ``replay_grading`` re-verifies the corrected payload and writes a separate,
   clearly-labelled grading row (``...:replay:<observation key>``). The
   original grading row and every prediction snapshot remain untouched, so a
   replay is additive, idempotent and auditable.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

import lifecycle_storage as lstore
import live_lifecycle


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _status_short(sport: Optional[str], payload: Dict[str, Any]) -> Optional[str]:
    if sport == "football":
        status = (payload.get("fixture") or {}).get("status") or {}
    else:
        status = payload.get("status") or {}
    if isinstance(status, dict):
        return status.get("short")
    return None


def _latest_result_observation(lifecycle_id: str) -> Optional[Dict[str, Any]]:
    for observation in lstore.get_observations(lifecycle_id, limit=100):
        context = observation.get("lifecycle_context")
        if context in ("final_result", "correction") and isinstance(observation.get("payload"), dict):
            return observation
    return None


def _impact(row: Dict[str, Any]) -> Dict[str, Any]:
    import storage

    try:
        snapshots = storage.get_prediction_snapshots(
            fixture_id=int(row.get("provider_fixture_id") or 0), sport=row.get("sport"), limit=100
        )
    except Exception:
        snapshots = []
    return {
        "lifecycle_id": row.get("lifecycle_id"),
        "sport": row.get("sport"),
        "stage": row.get("stage"),
        "provider": row.get("provider"),
        "provider_fixture_id": row.get("provider_fixture_id"),
        "snapshot_ids": [snapshot.get("id") for snapshot in snapshots],
        "snapshots_preserved": True,
        "requires_grading_replay": bool(snapshots),
        "historical_records_touched": False,
    }


def record_correction(
    lifecycle_id: str,
    *,
    field: str,
    corrected_value: Any,
    reason: str,
    provider: Optional[str] = None,
    corrected_payload: Optional[Dict[str, Any]] = None,
    observed_fields: Optional[Dict[str, Any]] = None,
    operator: str = "manual",
) -> Dict[str, Any]:
    """Append a corrected observation and return its impact report.

    ``corrected_payload`` may be supplied in full (recommended for a corrected
    final result). Otherwise the previous final-result payload is copied and
    only ``field`` is replaced, so unrelated evidence is preserved.
    """
    row = lstore.get_lifecycle(lifecycle_id)
    if row is None:
        raise ValueError(f"Unknown lifecycle_id: {lifecycle_id}")

    previous = _latest_result_observation(lifecycle_id)
    if corrected_payload is None:
        if previous is None:
            raise ValueError(
                "No previous final-result observation to correct; supply corrected_payload explicitly."
            )
        corrected_payload = dict(previous.get("payload") or {})
        if field in ("goals", "scores"):
            corrected_payload[field] = corrected_value
        elif field == "status":
            key = "fixture" if row.get("sport") == "football" else ""
            if key:
                fixture = dict(corrected_payload.get(key) or {})
                fixture["status"] = {"short": corrected_value}
                corrected_payload[key] = fixture
            else:
                corrected_payload["status"] = {"short": corrected_value}
        else:
            corrected_payload[field] = corrected_value

    stamp = _now()
    source = provider or row.get("provider") or "unknown"
    observation_key = f"{lifecycle_id}|correction|{stamp}"
    lstore.record_observation(
        lifecycle_id=lifecycle_id,
        provider=source,
        context="correction",
        observation_timestamp=stamp,
        provider_status=_status_short(row.get("sport"), corrected_payload),
        observed_fields=observed_fields or {"field": field, "corrected_value": corrected_value},
        payload=corrected_payload,
        parser_version=None,
        provenance={
            "provider": source,
            "corrects_observation_key": (previous or {}).get("observation_key"),
            "original_observation_retained": True,
            "reason": reason,
            "operator": operator,
            "recorded_at": stamp,
        },
        provider_fixture_id=row.get("provider_fixture_id"),
        observation_key=observation_key,
    )
    lstore.record_event(
        "result_conflict_detected",
        sport=row.get("sport"),
        lifecycle_id=lifecycle_id,
        provider=source,
        stage=row.get("stage"),
        outcome="correction_recorded",
        detail={
            "field": field,
            "corrected_value": corrected_value,
            "reason": reason,
            "corrects_observation_key": (previous or {}).get("observation_key"),
            "original_observation_retained": True,
        },
        event_key=f"correction|{lifecycle_id}|{observation_key}",
    )
    impact = _impact(row)
    impact.update(
        {
            "correction_observation_key": observation_key,
            "corrected_field": field,
            "corrected_value": corrected_value,
            "reason": reason,
            "original_observation_key": (previous or {}).get("observation_key"),
        }
    )
    return impact


def replay_grading(lifecycle_id: str, *, reason: str, operator: str = "manual") -> Dict[str, Any]:
    """Re-grade from the latest correction, additively and idempotently."""
    row = lstore.get_lifecycle(lifecycle_id)
    if row is None:
        raise ValueError(f"Unknown lifecycle_id: {lifecycle_id}")

    observations = lstore.get_observations(lifecycle_id, limit=100)
    correction = next(
        (obs for obs in observations if obs.get("lifecycle_context") == "correction" and isinstance(obs.get("payload"), dict)),
        None,
    )
    if correction is None:
        raise ValueError("No correction observation exists for this lifecycle; record one first.")
    payload = correction["payload"]

    ok, evidence = (
        live_lifecycle._verify_football(row, payload)
        if row.get("sport") == "football"
        else live_lifecycle._verify_basketball(row, payload)
    )
    if not ok:
        lstore.record_event(
            "result_conflict_detected",
            sport=row.get("sport"),
            lifecycle_id=lifecycle_id,
            provider=correction.get("provider"),
            outcome=f"replay_not_verified:{evidence.get('reason')}",
            detail=evidence,
            event_key=f"replay-not-verified|{lifecycle_id}|{correction.get('observation_key')}",
        )
        return {
            "lifecycle_id": lifecycle_id,
            "replayed": False,
            "reason": evidence.get("reason"),
            "evidence": evidence,
        }

    graded = live_lifecycle._grade_row(
        row,
        payload,
        key_suffix=f"replay:{correction.get('observation_key')}",
        provenance_extra={
            "replay": True,
            "replay_reason": reason,
            "replay_operator": operator,
            "corrects_observation_key": correction.get("observation_key"),
            "original_observation_retained": True,
        },
    )
    lstore.record_event(
        "prediction_graded",
        sport=row.get("sport"),
        lifecycle_id=lifecycle_id,
        provider=correction.get("provider"),
        stage=row.get("stage"),
        outcome="replay",
        detail={
            "graded_rows": graded,
            "reason": reason,
            "correction_observation_key": correction.get("observation_key"),
            "snapshots_unchanged": True,
        },
        event_key=f"replay|{lifecycle_id}|{correction.get('observation_key')}",
    )
    impact = _impact(row)
    impact.update(
        {
            "replayed": True,
            "graded_rows": graded,
            "reason": reason,
            "correction_observation_key": correction.get("observation_key"),
        }
    )
    return impact


def corrections_for(lifecycle_id: str) -> List[Dict[str, Any]]:
    """All recorded corrections for one lifecycle, newest first."""
    observations = lstore.get_observations(lifecycle_id, limit=200)
    return [
        {
            "observation_key": obs.get("observation_key"),
            "provider": obs.get("provider"),
            "recorded_at": obs.get("observation_timestamp"),
            "observed_fields": obs.get("observed_fields"),
            "provenance": obs.get("provenance"),
        }
        for obs in observations
        if obs.get("lifecycle_context") == "correction"
    ]
