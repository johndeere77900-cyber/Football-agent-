-- Phase 2 durable lifecycle schema (SQLite / local development)
--
-- Mirrors migrations/phase2_lifecycle_postgres.sql. SQLite has no
-- "ADD COLUMN IF NOT EXISTS"; storage._ensure_column_sqlite handles the
-- prediction_snapshots additions, and lifecycle_storage.ensure_schema()
-- applies this DDL lazily for local runs.

CREATE TABLE IF NOT EXISTS live_match_lifecycle (
    lifecycle_id          TEXT PRIMARY KEY,
    sport                 TEXT NOT NULL,
    canonical_fixture_key TEXT NOT NULL,
    provider              TEXT,
    provider_fixture_id   TEXT,
    league_id             INTEGER,
    season                INTEGER,
    home_team_id          TEXT,
    away_team_id          TEXT,
    kickoff_at            TEXT,
    stage                 TEXT NOT NULL,
    provider_status       TEXT,
    first_discovered_at   TEXT NOT NULL,
    last_processed_at     TEXT,
    retry_count           INTEGER NOT NULL DEFAULT 0,
    next_retry_at         TEXT,
    error_state           TEXT,
    error_class           TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (sport, canonical_fixture_key)
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_stage ON live_match_lifecycle (sport, stage);
CREATE INDEX IF NOT EXISTS idx_lifecycle_kickoff ON live_match_lifecycle (kickoff_at);

CREATE TABLE IF NOT EXISTS live_match_observations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    lifecycle_id          TEXT NOT NULL,
    provider              TEXT NOT NULL,
    provider_fixture_id   TEXT,
    observation_timestamp TEXT NOT NULL,
    lifecycle_context     TEXT NOT NULL,
    provider_status       TEXT,
    observed_fields_json  TEXT,
    payload_json          TEXT,
    parser_version        TEXT,
    provenance_json       TEXT,
    observation_key       TEXT NOT NULL UNIQUE,
    created_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_observations_lifecycle
    ON live_match_observations (lifecycle_id, observation_timestamp);

CREATE TABLE IF NOT EXISTS prediction_grading_ledger (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id              INTEGER,
    lifecycle_id             TEXT,
    sport                    TEXT NOT NULL,
    market                   TEXT NOT NULL,
    predicted_output_json    TEXT,
    actual_outcome_json      TEXT,
    settlement_status        TEXT NOT NULL,
    settlement_rules_version TEXT,
    result_provenance_json   TEXT,
    graded_at                TEXT NOT NULL,
    grading_key              TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_grading_snapshot ON prediction_grading_ledger (snapshot_id);

CREATE TABLE IF NOT EXISTS model_calibration_versions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    sport                   TEXT NOT NULL,
    competition_scope       TEXT NOT NULL,
    model_version           TEXT,
    calibration_version     TEXT NOT NULL,
    training_cutoff         TEXT,
    training_sample_count   INTEGER,
    evaluation_metrics_json TEXT,
    status                  TEXT NOT NULL,
    promoted_at             TEXT,
    rollback_reference      TEXT,
    dataset_identity        TEXT,
    created_at              TEXT NOT NULL,
    UNIQUE (sport, competition_scope, calibration_version)
);
