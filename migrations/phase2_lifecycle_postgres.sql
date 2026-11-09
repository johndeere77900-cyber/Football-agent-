-- Phase 2 durable lifecycle schema (PostgreSQL / Neon)
--
-- REPOSITORY-ONLY PROPOSAL. Freebuff must never apply this file or connect to
-- Neon. ChatGPT reviews and executes any authorised production migration.
--
-- All statements are idempotent so a reviewed run can be replayed safely.

-- 1. Durable match lifecycle ------------------------------------------------
CREATE TABLE IF NOT EXISTS live_match_lifecycle (
    lifecycle_id          TEXT PRIMARY KEY,
    sport                 TEXT NOT NULL,
    canonical_fixture_key TEXT NOT NULL,
    provider              TEXT,
    provider_fixture_id   TEXT,
    league_id             BIGINT,
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
    CONSTRAINT uq_lifecycle_sport_fixture UNIQUE (sport, canonical_fixture_key)
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_stage ON live_match_lifecycle (sport, stage);
CREATE INDEX IF NOT EXISTS idx_lifecycle_kickoff ON live_match_lifecycle (kickoff_at);

-- 2. Append-only provider observations --------------------------------------
CREATE TABLE IF NOT EXISTS live_match_observations (
    id                   BIGSERIAL PRIMARY KEY,
    lifecycle_id         TEXT NOT NULL,
    provider             TEXT NOT NULL,
    provider_fixture_id  TEXT,
    observation_timestamp TEXT NOT NULL,
    lifecycle_context    TEXT NOT NULL,
    provider_status      TEXT,
    observed_fields_json JSONB,
    payload_json         JSONB,
    parser_version       TEXT,
    provenance_json      JSONB,
    observation_key      TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    CONSTRAINT uq_observation_key UNIQUE (observation_key)
);

CREATE INDEX IF NOT EXISTS idx_observations_lifecycle
    ON live_match_observations (lifecycle_id, observation_timestamp);

-- 3. Snapshot idempotency and lifecycle linkage -----------------------------
ALTER TABLE prediction_snapshots ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE prediction_snapshots ADD COLUMN IF NOT EXISTS lifecycle_id TEXT;
ALTER TABLE prediction_snapshots ADD COLUMN IF NOT EXISTS model_inputs_json JSONB;

CREATE UNIQUE INDEX IF NOT EXISTS idx_pred_snapshots_idem
    ON prediction_snapshots (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_pred_snapshots_lifecycle
    ON prediction_snapshots (lifecycle_id);

-- 4. Append-only grading ledger ---------------------------------------------
CREATE TABLE IF NOT EXISTS prediction_grading_ledger (
    id                        BIGSERIAL PRIMARY KEY,
    snapshot_id               BIGINT,
    lifecycle_id              TEXT,
    sport                     TEXT NOT NULL,
    market                    TEXT NOT NULL,
    predicted_output_json     JSONB,
    actual_outcome_json       JSONB,
    settlement_status         TEXT NOT NULL,
    settlement_rules_version  TEXT,
    result_provenance_json    JSONB,
    graded_at                 TEXT NOT NULL,
    grading_key               TEXT NOT NULL,
    CONSTRAINT uq_grading_key UNIQUE (grading_key)
);

CREATE INDEX IF NOT EXISTS idx_grading_snapshot ON prediction_grading_ledger (snapshot_id);

-- 5. Controlled calibration / model versions --------------------------------
CREATE TABLE IF NOT EXISTS model_calibration_versions (
    id                      BIGSERIAL PRIMARY KEY,
    sport                   TEXT NOT NULL,
    competition_scope       TEXT NOT NULL,
    model_version           TEXT,
    calibration_version     TEXT NOT NULL,
    training_cutoff         TEXT,
    training_sample_count   INTEGER,
    evaluation_metrics_json JSONB,
    status                  TEXT NOT NULL,
    promoted_at             TEXT,
    rollback_reference      TEXT,
    dataset_identity        TEXT,
    created_at              TEXT NOT NULL,
    CONSTRAINT uq_calibration_version UNIQUE (sport, competition_scope, calibration_version)
);

CREATE INDEX IF NOT EXISTS idx_calibration_active
    ON model_calibration_versions (sport, competition_scope, status, promoted_at DESC);
