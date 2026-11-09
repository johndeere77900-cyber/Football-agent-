"""
Durable storage for the autonomous live-match lifecycle.

Implements the Phase 2 persistent state described in
``migrations/phase2_lifecycle_postgres.sql`` and
``migrations/phase2_lifecycle_sqlite.sql`` using the existing storage
conventions (a single ``_connect()`` abstraction that resolves to PostgreSQL
when Neon is configured and SQLite otherwise).

Every write is idempotent: lifecycle records are unique per canonical fixture,
observations and grading records carry stable idempotency keys, and repeated
calls return the existing row instead of inserting duplicates.

No migration is applied here for production: the DDL in this module is the
SQLite/local execution path and mirrors the proposal files. Neon schema
changes remain owned by ChatGPT.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Dict, List, Optional

import config
import storage

SCHEMA_READY = False


class Phase2SchemaMissing(RuntimeError):
    """Raised when production PostgreSQL lacks the reviewed Phase 2 schema."""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _json(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, default=str)


def _loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _dialect() -> str:
    conn, db_type = storage._connect()
    conn.close()
    return db_type


def ensure_schema() -> None:
    """Create the lifecycle tables/columns if they are absent (local SQLite path)."""
    global SCHEMA_READY
    if SCHEMA_READY:
        return
    conn, db_type = storage._connect()
    try:
        if db_type == "postgres":
            # Production schema is owned by ChatGPT. The application validates
            # that the reviewed migration has been applied and never executes
            # PostgreSQL DDL, so startup cannot silently change the database.
            required = {
                "prediction_snapshots": {"idempotency_key", "lifecycle_id", "model_inputs_json"},
                "live_match_lifecycle": {"prediction_cycle_key"},
                "live_match_observations": set(),
                "prediction_grading_ledger": set(),
                "model_calibration_versions": set(),
            }
            missing = []
            with conn.cursor() as cur:
                for table, columns in required.items():
                    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                    if cur.fetchone()[0] is None:
                        missing.append(table)
                        continue
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                        (table,),
                    )
                    present = {row[0] for row in cur.fetchall()}
                    missing.extend(f"{table}.{column}" for column in sorted(columns - present))
            if missing:
                raise Phase2SchemaMissing(
                    "Phase 2 PostgreSQL schema is missing or out of date ("
                    + ", ".join(missing)
                    + "). Apply the reviewed migration migrations/phase2_lifecycle_postgres.sql "
                    "before running the lifecycle; the application never creates PostgreSQL schema."
                )
            SCHEMA_READY = True
            return
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS live_match_lifecycle (
                    lifecycle_id TEXT PRIMARY KEY,
                    sport TEXT NOT NULL,
                    canonical_fixture_key TEXT NOT NULL,
                    provider TEXT,
                    provider_fixture_id TEXT,
                    league_id INTEGER,
                    season INTEGER,
                    home_team_id TEXT,
                    away_team_id TEXT,
                    kickoff_at TEXT,
                    stage TEXT NOT NULL,
                    provider_status TEXT,
                    first_discovered_at TEXT NOT NULL,
                    last_processed_at TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    error_state TEXT,
                    error_class TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (sport, canonical_fixture_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS live_match_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lifecycle_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provider_fixture_id TEXT,
                    observation_timestamp TEXT NOT NULL,
                    lifecycle_context TEXT NOT NULL,
                    provider_status TEXT,
                    observed_fields_json TEXT,
                    payload_json TEXT,
                    parser_version TEXT,
                    provenance_json TEXT,
                    observation_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS prediction_grading_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER,
                    lifecycle_id TEXT,
                    sport TEXT NOT NULL,
                    market TEXT NOT NULL,
                    predicted_output_json TEXT,
                    actual_outcome_json TEXT,
                    settlement_status TEXT NOT NULL,
                    settlement_rules_version TEXT,
                    result_provenance_json TEXT,
                    graded_at TEXT NOT NULL,
                    grading_key TEXT NOT NULL UNIQUE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_calibration_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sport TEXT NOT NULL,
                    competition_scope TEXT NOT NULL,
                    model_version TEXT,
                    calibration_version TEXT NOT NULL,
                    training_cutoff TEXT,
                    training_sample_count INTEGER,
                    evaluation_metrics_json TEXT,
                    status TEXT NOT NULL,
                    promoted_at TEXT,
                    rollback_reference TEXT,
                    dataset_identity TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (sport, competition_scope, calibration_version)
                )
                """
            )
            for column, ctype in (
                ("idempotency_key", "TEXT"),
                ("lifecycle_id", "TEXT"),
                ("model_inputs_json", "TEXT"),
            ):
                storage._ensure_column_sqlite(conn, "prediction_snapshots", column, ctype)
            storage._ensure_column_sqlite(conn, "live_match_lifecycle", "prediction_cycle_key", "TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_pred_snapshots_idem ON prediction_snapshots (idempotency_key) WHERE idempotency_key IS NOT NULL"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pred_snapshots_lifecycle ON prediction_snapshots (lifecycle_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_stage ON live_match_lifecycle (sport, stage)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_observations_lifecycle ON live_match_observations (lifecycle_id, observation_timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_grading_snapshot ON prediction_grading_ledger (snapshot_id)"
            )
            conn.commit()
        SCHEMA_READY = True
    finally:
        conn.close()


_LIFECYCLE_FIELDS = (
    "lifecycle_id",
    "sport",
    "canonical_fixture_key",
    "provider",
    "provider_fixture_id",
    "league_id",
    "season",
    "home_team_id",
    "away_team_id",
    "kickoff_at",
    "stage",
    "provider_status",
    "first_discovered_at",
    "last_processed_at",
    "retry_count",
    "next_retry_at",
    "error_state",
    "error_class",
    "created_at",
    "updated_at",
    "prediction_cycle_key",
)


def _lifecycle_row(row) -> Dict[str, Any]:
    return dict(zip(_LIFECYCLE_FIELDS, row))


def upsert_lifecycle(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Insert a lifecycle record idempotently and return the stored row.

    The canonical fixture key is unique per sport, so rediscovery refreshes
    identifiers but never creates a duplicate tracked match.
    """
    ensure_schema()
    sport = str(record.get("sport") or "football").lower()
    fixture_key = str(record.get("canonical_fixture_key") or "").strip()
    if not fixture_key:
        raise ValueError("canonical_fixture_key is required")
    lifecycle_id = str(record.get("lifecycle_id") or f"{sport}:{fixture_key}")
    now = _now()

    conn, db_type = storage._connect()
    try:
        values = (
            lifecycle_id,
            sport,
            fixture_key,
            record.get("provider"),
            str(record.get("provider_fixture_id")) if record.get("provider_fixture_id") is not None else None,
            record.get("league_id"),
            record.get("season"),
            str(record["home_team_id"]) if record.get("home_team_id") is not None else None,
            str(record["away_team_id"]) if record.get("away_team_id") is not None else None,
            record.get("kickoff_at"),
            record.get("stage") or "DISCOVERED",
            record.get("provider_status"),
            now,
            None,
            0,
            None,
            None,
            None,
            now,
            now,
            record.get("prediction_cycle_key"),
        )
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO live_match_lifecycle (
                        lifecycle_id, sport, canonical_fixture_key, provider,
                        provider_fixture_id, league_id, season, home_team_id,
                        away_team_id, kickoff_at, stage, provider_status,
                        first_discovered_at, last_processed_at, retry_count,
                        next_retry_at, error_state, error_class, created_at, updated_at, prediction_cycle_key
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (sport, canonical_fixture_key) DO NOTHING
                    """,
                    values,
                )
            conn.commit()
        else:
            conn.execute(
                """
                INSERT OR IGNORE INTO live_match_lifecycle (
                    lifecycle_id, sport, canonical_fixture_key, provider,
                    provider_fixture_id, league_id, season, home_team_id,
                    away_team_id, kickoff_at, stage, provider_status,
                    first_discovered_at, last_processed_at, retry_count,
                    next_retry_at, error_state, error_class, created_at, updated_at, prediction_cycle_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()
    finally:
        conn.close()

    stored = get_lifecycle_by_fixture(sport, fixture_key)
    if stored is None:
        raise RuntimeError("lifecycle record could not be persisted")
    return stored


def get_lifecycle(lifecycle_id: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    conn, db_type = storage._connect()
    try:
        placeholder = "%s" if db_type == "postgres" else "?"
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {', '.join(_LIFECYCLE_FIELDS)} FROM live_match_lifecycle WHERE lifecycle_id = {placeholder}",
                    (lifecycle_id,),
                )
                row = cur.fetchone()
        else:
            row = conn.execute(
                f"SELECT {', '.join(_LIFECYCLE_FIELDS)} FROM live_match_lifecycle WHERE lifecycle_id = {placeholder}",
                (lifecycle_id,),
            ).fetchone()
        return _lifecycle_row(row) if row else None
    finally:
        conn.close()


def get_lifecycle_by_fixture(sport: str, canonical_fixture_key: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    conn, db_type = storage._connect()
    try:
        ph = "%s" if db_type == "postgres" else "?"
        sql = (
            f"SELECT {', '.join(_LIFECYCLE_FIELDS)} FROM live_match_lifecycle "
            f"WHERE sport = {ph} AND canonical_fixture_key = {ph}"
        )
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(sql, (str(sport).lower(), str(canonical_fixture_key)))
                row = cur.fetchone()
        else:
            row = conn.execute(sql, (str(sport).lower(), str(canonical_fixture_key))).fetchone()
        return _lifecycle_row(row) if row else None
    finally:
        conn.close()


def update_lifecycle(lifecycle_id: str, expected_stage: Optional[str] = None, **updates) -> Optional[Dict[str, Any]]:
    """
    Update a lifecycle record.

    ``expected_stage`` makes the write conditional so two workers cannot both
    apply the same transition. Returns None when the condition no longer holds.
    """
    ensure_schema()
    allowed = {
        "stage",
        "provider_status",
        "last_processed_at",
        "retry_count",
        "next_retry_at",
        "error_state",
        "error_class",
        "kickoff_at",
        "provider_fixture_id",
        "home_team_id",
        "away_team_id",
        "prediction_cycle_key",
    }
    fields = {k: v for k, v in updates.items() if k in allowed}
    fields["updated_at"] = _now()
    if "last_processed_at" not in fields and "stage" in fields:
        fields["last_processed_at"] = fields["updated_at"]

    conn, db_type = storage._connect()
    try:
        ph = "%s" if db_type == "postgres" else "?"
        assignments = ", ".join(f"{name} = {ph}" for name in fields)
        conditions = f"lifecycle_id = {ph}"
        params = list(fields.values()) + [lifecycle_id]
        if expected_stage is not None:
            conditions += f" AND stage = {ph}"
            params.append(expected_stage)
        sql = f"UPDATE live_match_lifecycle SET {assignments} WHERE {conditions}"
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(sql, params)
                changed = cur.rowcount
            conn.commit()
        else:
            changed = conn.execute(sql, params).rowcount
            conn.commit()
        if expected_stage is not None and changed == 0:
            return None
    finally:
        conn.close()
    return get_lifecycle(lifecycle_id)


def transition_lifecycle(lifecycle_id: str, to_stage: str, from_stages=None, **fields):
    """Move a lifecycle to ``to_stage`` only from an allowed current stage."""
    row = get_lifecycle(lifecycle_id)
    if row is None:
        return None
    if from_stages and row.get("stage") not in from_stages:
        return None
    fields["stage"] = to_stage
    return update_lifecycle(lifecycle_id, expected_stage=row.get("stage"), **fields)


def get_latest_final_observation(lifecycle_id: str) -> Optional[Dict[str, Any]]:
    """Most recent durable verified-result observation for a lifecycle."""
    for observation in get_observations(lifecycle_id, limit=20):
        if observation.get("lifecycle_context") == "final_result" and isinstance(observation.get("payload"), dict):
            return observation
    return None


def list_lifecycles(
    sport: Optional[str] = None,
    stages: Optional[List[str]] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    ensure_schema()
    conn, db_type = storage._connect()
    ph = "%s" if db_type == "postgres" else "?"
    clauses = []
    params: List[Any] = []
    if sport:
        clauses.append(f"sport = {ph}")
        params.append(str(sport).lower())
    if stages:
        placeholders = ", ".join(ph for _ in stages)
        clauses.append(f"stage IN ({placeholders})")
        params.extend(stages)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT {', '.join(_LIFECYCLE_FIELDS)} FROM live_match_lifecycle "
        f"{where} ORDER BY kickoff_at ASC NULLS LAST, created_at ASC LIMIT {ph}"
    )
    if db_type != "postgres":
        sql = sql.replace(" NULLS LAST", "")
    params.append(int(limit))
    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        else:
            rows = conn.execute(sql, params).fetchall()
        return [_lifecycle_row(r) for r in rows]
    finally:
        conn.close()


def record_observation(
    lifecycle_id: str,
    provider: str,
    context: str,
    observation_timestamp: str,
    provider_status: Optional[str] = None,
    observed_fields: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    parser_version: Optional[str] = None,
    provenance: Optional[Dict[str, Any]] = None,
    provider_fixture_id: Optional[str] = None,
    observation_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Append a time-stamped observation. Repeats of the same key are ignored."""
    ensure_schema()
    key = observation_key or f"{lifecycle_id}|{provider}|{context}|{observation_timestamp}"
    now = _now()
    fields_json = _json(observed_fields or {})
    payload_json = _json(payload)
    provenance_json = _json(provenance or {"provider": provider})

    conn, db_type = storage._connect()
    try:
        values = (
            lifecycle_id,
            provider,
            str(provider_fixture_id) if provider_fixture_id is not None else None,
            observation_timestamp,
            context,
            provider_status,
            fields_json,
            payload_json,
            parser_version,
            provenance_json,
            key,
            now,
        )
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO live_match_observations (
                        lifecycle_id, provider, provider_fixture_id, observation_timestamp,
                        lifecycle_context, provider_status, observed_fields_json, payload_json,
                        parser_version, provenance_json, observation_key, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (observation_key) DO NOTHING
                    """,
                    values,
                )
            conn.commit()
        else:
            conn.execute(
                """
                INSERT OR IGNORE INTO live_match_observations (
                    lifecycle_id, provider, provider_fixture_id, observation_timestamp,
                    lifecycle_context, provider_status, observed_fields_json, payload_json,
                    parser_version, provenance_json, observation_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()
    finally:
        conn.close()
    return {
        "lifecycle_id": lifecycle_id,
        "provider": provider,
        "observation_timestamp": observation_timestamp,
        "provider_status": provider_status,
        "observed_fields": observed_fields or {},
        "observation_key": key,
    }


def get_observations(lifecycle_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    ensure_schema()
    conn, db_type = storage._connect()
    ph = "%s" if db_type == "postgres" else "?"
    sql = (
        "SELECT provider, provider_fixture_id, observation_timestamp, lifecycle_context, "
        "provider_status, observed_fields_json, payload_json, parser_version, provenance_json, "
        "observation_key, created_at FROM live_match_observations "
        f"WHERE lifecycle_id = {ph} ORDER BY observation_timestamp DESC, id DESC LIMIT {ph}"
    )
    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(sql, (lifecycle_id, int(limit)))
                rows = cur.fetchall()
        else:
            rows = conn.execute(sql, (lifecycle_id, int(limit))).fetchall()
        return [
            {
                "provider": r[0],
                "provider_fixture_id": r[1],
                "observation_timestamp": r[2],
                "lifecycle_context": r[3],
                "provider_status": r[4],
                "observed_fields": _loads(r[5]),
                "payload": _loads(r[6]),
                "parser_version": r[7],
                "provenance": _loads(r[8]),
                "observation_key": r[9],
                "created_at": r[10],
            }
            for r in rows
        ]
    finally:
        conn.close()


def save_grading(record: Dict[str, Any]) -> Dict[str, Any]:
    """Append-only grading record; a repeated grading key returns the stored row."""
    ensure_schema()
    key = str(record.get("grading_key") or "").strip()
    if not key:
        raise ValueError("grading_key is required")
    existing = get_grading(key)
    if existing is not None:
        return existing

    now = _now()
    values = (
        record.get("snapshot_id"),
        record.get("lifecycle_id"),
        str(record.get("sport") or "football").lower(),
        record.get("market"),
        _json(record.get("predicted_output")),
        _json(record.get("actual_outcome")),
        record.get("settlement_status") or "PENDING",
        record.get("settlement_rules_version"),
        _json(record.get("result_provenance")),
        now,
        key,
    )

    conn, db_type = storage._connect()
    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO prediction_grading_ledger (
                        snapshot_id, lifecycle_id, sport, market, predicted_output_json,
                        actual_outcome_json, settlement_status, settlement_rules_version,
                        result_provenance_json, graded_at, grading_key
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (grading_key) DO NOTHING
                    """,
                    values,
                )
            conn.commit()
        else:
            conn.execute(
                """
                INSERT OR IGNORE INTO prediction_grading_ledger (
                    snapshot_id, lifecycle_id, sport, market, predicted_output_json,
                    actual_outcome_json, settlement_status, settlement_rules_version,
                    result_provenance_json, graded_at, grading_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()
    finally:
        conn.close()
    return get_grading(key) or {"grading_key": key}


def get_grading(grading_key: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    conn, db_type = storage._connect()
    ph = "%s" if db_type == "postgres" else "?"
    sql = (
        "SELECT snapshot_id, lifecycle_id, sport, market, predicted_output_json, "
        "actual_outcome_json, settlement_status, settlement_rules_version, "
        "result_provenance_json, graded_at, grading_key "
        f"FROM prediction_grading_ledger WHERE grading_key = {ph}"
    )
    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(sql, (grading_key,))
                row = cur.fetchone()
        else:
            row = conn.execute(sql, (grading_key,)).fetchone()
        if not row:
            return None
        return {
            "snapshot_id": row[0],
            "lifecycle_id": row[1],
            "sport": row[2],
            "market": row[3],
            "predicted_output": _loads(row[4]),
            "actual_outcome": _loads(row[5]),
            "settlement_status": row[6],
            "settlement_rules_version": row[7],
            "result_provenance": _loads(row[8]),
            "graded_at": row[9],
            "grading_key": row[10],
        }
    finally:
        conn.close()


def save_calibration_version(record: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a candidate/active/rejected calibration version idempotently."""
    ensure_schema()
    sport = str(record.get("sport") or "football").lower()
    scope = str(record.get("competition_scope") or "all")
    version = str(record.get("calibration_version") or "").strip()
    if not version:
        raise ValueError("calibration_version is required")
    now = _now()
    values = (
        sport,
        scope,
        record.get("model_version"),
        version,
        record.get("training_cutoff"),
        record.get("training_sample_count"),
        _json(record.get("evaluation_metrics")),
        record.get("status") or "candidate",
        record.get("promoted_at"),
        record.get("rollback_reference"),
        record.get("dataset_identity"),
        now,
    )
    conn, db_type = storage._connect()
    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO model_calibration_versions (
                        sport, competition_scope, model_version, calibration_version,
                        training_cutoff, training_sample_count, evaluation_metrics_json,
                        status, promoted_at, rollback_reference, dataset_identity, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (sport, competition_scope, calibration_version) DO UPDATE
                    SET status = EXCLUDED.status,
                        evaluation_metrics_json = EXCLUDED.evaluation_metrics_json,
                        promoted_at = EXCLUDED.promoted_at,
                        rollback_reference = EXCLUDED.rollback_reference
                    """,
                    values,
                )
            conn.commit()
        else:
            conn.execute(
                """
                INSERT INTO model_calibration_versions (
                    sport, competition_scope, model_version, calibration_version,
                    training_cutoff, training_sample_count, evaluation_metrics_json,
                    status, promoted_at, rollback_reference, dataset_identity, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (sport, competition_scope, calibration_version) DO UPDATE
                SET status = excluded.status,
                    evaluation_metrics_json = excluded.evaluation_metrics_json,
                    promoted_at = excluded.promoted_at,
                    rollback_reference = excluded.rollback_reference
                """,
                values,
            )
            conn.commit()
    finally:
        conn.close()
    return get_calibration_version(sport, scope, version) or {"calibration_version": version}


def get_calibration_version(sport: str, scope: str, version: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    conn, db_type = storage._connect()
    ph = "%s" if db_type == "postgres" else "?"
    sql = (
        "SELECT sport, competition_scope, model_version, calibration_version, training_cutoff, "
        "training_sample_count, evaluation_metrics_json, status, promoted_at, rollback_reference, "
        "dataset_identity, created_at FROM model_calibration_versions "
        f"WHERE sport = {ph} AND competition_scope = {ph} AND calibration_version = {ph}"
    )
    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(sql, (sport, scope, version))
                row = cur.fetchone()
        else:
            row = conn.execute(sql, (sport, scope, version)).fetchone()
        if not row:
            return None
        return {
            "sport": row[0],
            "competition_scope": row[1],
            "model_version": row[2],
            "calibration_version": row[3],
            "training_cutoff": row[4],
            "training_sample_count": row[5],
            "evaluation_metrics": _loads(row[6]),
            "status": row[7],
            "promoted_at": row[8],
            "rollback_reference": row[9],
            "dataset_identity": row[10],
            "created_at": row[11],
        }
    finally:
        conn.close()


def get_active_calibration(sport: str, scope: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    conn, db_type = storage._connect()
    ph = "%s" if db_type == "postgres" else "?"
    sql = (
        "SELECT sport, competition_scope, model_version, calibration_version, training_cutoff, "
        "training_sample_count, evaluation_metrics_json, status, promoted_at, rollback_reference, "
        "dataset_identity, created_at FROM model_calibration_versions "
        f"WHERE sport = {ph} AND competition_scope = {ph} AND status = 'active' "
        "ORDER BY promoted_at DESC LIMIT 1"
    )
    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(sql, (sport, scope))
                row = cur.fetchone()
        else:
            row = conn.execute(sql, (sport, scope)).fetchone()
        if not row:
            return None
        return {
            "sport": row[0],
            "competition_scope": row[1],
            "model_version": row[2],
            "calibration_version": row[3],
            "training_cutoff": row[4],
            "training_sample_count": row[5],
            "evaluation_metrics": _loads(row[6]),
            "status": row[7],
            "promoted_at": row[8],
            "rollback_reference": row[9],
            "dataset_identity": row[10],
            "created_at": row[11],
        }
    finally:
        conn.close()
def get_graded_samples(sport, before=None, limit=5000):
    """
    Verified, graded snapshots in chronological order.

    Only ledger rows settled as WON/LOST are returned, so learning never trains
    on unverified or conflicting results.
    """
    ensure_schema()
    conn, db_type = storage._connect()
    ph = "%s" if db_type == "postgres" else "?"
    primary_market = "match_result" if str(sport).lower() == "football" else "moneyline"
    clauses = [
        "s.sport = " + ph,
        "g.settlement_status IN ('WON', 'LOST')",
        "g.market = " + ph,
    ]
    params = [str(sport).lower(), primary_market]
    if before:
        clauses.append("s.prediction_timestamp < " + ph)
        params.append(before)
    sql = (
        "SELECT s.id, s.prediction_timestamp, s.markets_json, g.actual_outcome_json "
        "FROM prediction_snapshots s JOIN prediction_grading_ledger g ON g.snapshot_id = s.id "
        f"WHERE {' AND '.join(clauses)} "
        f"ORDER BY s.prediction_timestamp ASC, s.id ASC LIMIT {ph}"
    )
    params.append(int(limit))
    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        else:
            rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    samples = []
    seen_snapshots = set()
    for row in rows:
        if row[0] in seen_snapshots:
            continue
        seen_snapshots.add(row[0])
        markets = _loads(row[2]) or {}
        actual = _loads(row[3]) or {}
        result = None
        if str(sport).lower() == "football":
            reg = actual.get("regulation_goals")
            if isinstance(reg, (list, tuple)) and len(reg) == 2 and None not in reg:
                home, away = int(reg[0]), int(reg[1])
                result = "home_win" if home > away else ("away_win" if away > home else "draw")
        else:
            pts = actual.get("match_goals")
            if isinstance(pts, (list, tuple)) and len(pts) == 2 and None not in pts:
                home, away = int(pts[0]), int(pts[1])
                result = "home_win" if home > away else "away_win"
        if result is None:
            continue
        samples.append(
            {
                "snapshot_id": row[0],
                "timestamp": row[1],
                "raw_probabilities": markets,
                "actual": result,
            }
        )
    return samples
