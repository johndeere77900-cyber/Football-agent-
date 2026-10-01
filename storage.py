"""
Persistent storage for predictions, results, football Elo ratings, Telegram memory,
and API request/cache state.

Supports PostgreSQL (Neon) via `psycopg` when `NEON_DATABASE_URL` is set,
with a local SQLite fallback when absent.

Integrity rules:
- Production environments MUST fail if NEON_DATABASE_URL is missing.
- A prediction is immutable after its first successful save.
- Unique fixture/game ID enforcement.
- Results are idempotent.
- A conflicting second result is rejected with a ValueError.
- Football result grading and Elo updates occur in one transaction.
- Database/schema initialization is safe for existing databases.
- Bounded Telegram memory (max 50 messages per chat).
- Persistent API response caching and atomic request-budget tracking.
"""

import json
import math
import os
import sqlite3
from datetime import datetime, timezone

import config
import elo

try:
    import psycopg
except ImportError:
    psycopg = None


# ----------------------------------------------------------------------
# Database helpers & Connection
# ----------------------------------------------------------------------


def is_neon():
    """Return True if Neon PostgreSQL configuration is active."""
    url = getattr(config, "NEON_DATABASE_URL", None) or os.environ.get("NEON_DATABASE_URL")
    return bool(url and url.strip())


def _check_production_env():
    """Raise RuntimeError if running in production without Neon database URL."""
    env = (getattr(config, "ENVIRONMENT", "") or os.environ.get("ENVIRONMENT", "")).lower()
    require_neon = getattr(config, "REQUIRE_NEON", False) or os.environ.get("REQUIRE_NEON", "").lower() in ("true", "1", "yes")

    if (env == "production" or require_neon) and not is_neon():
        raise RuntimeError(
            "NEON_DATABASE_URL is required in production environment. "
            "Silent SQLite fallback is disabled for production workflows."
        )


def _connect():
    """
    Open database connection.

    Uses PostgreSQL (Neon) via psycopg if configured, otherwise SQLite.
    Fails immediately in production if NEON_DATABASE_URL is missing.
    """
    _check_production_env()

    if is_neon():
        if psycopg is None:
            raise RuntimeError("psycopg library is required for Neon PostgreSQL storage.")
        url = getattr(config, "NEON_DATABASE_URL", None) or os.environ.get("NEON_DATABASE_URL")
        conn = psycopg.connect(url)
        return conn, "postgres"
    else:
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn, "sqlite"


def _utc_now():
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _validate_positive_int(value, name):
    """Validate a strictly positive integer."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _validate_non_negative_int(value, name):
    """Validate a non-negative integer."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return value


def _validate_optional_positive_int(value, name):
    """Validate an optional positive integer."""
    if value is None:
        return None
    return _validate_positive_int(value, name)


def _validate_probability(value, name="probability"):
    """Validate a finite probability in the inclusive range [0, 1]."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number between 0 and 1.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number between 0 and 1.") from exc

    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError(f"{name} must be a number between 0 and 1.")
    return number


def _validate_text(value, name, allow_empty=False):
    """Validate a text field."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string.")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} cannot be empty.")
    return value


def _json_dumps(value, name):
    """Serialize a JSON value."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain JSON-serializable data.") from exc


def _json_loads(value):
    """Safely parse JSON or return original dict if already parsed."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return None


def _ensure_column_sqlite(conn, table, column, coltype):
    """Add a missing column to an existing SQLite table."""
    existing = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def _normalise_result_pick(value):
    """Normalize a stored prediction label to the canonical result key."""
    if not isinstance(value, str):
        return value

    normalized = value.strip().lower()
    aliases = {
        "home_win": "home_win",
        "home win": "home_win",
        "home": "home_win",
        "h": "home_win",
        "draw": "draw",
        "tie": "draw",
        "x": "draw",
        "away_win": "away_win",
        "away win": "away_win",
        "away": "away_win",
        "a": "away_win",
    }
    return aliases.get(
        normalized,
        normalized.replace("-", "_").replace(" ", "_"),
    )


# ----------------------------------------------------------------------
# Initialization
# ----------------------------------------------------------------------


def init_db():
    """
    Initialize prediction, Elo, bot memory, and API cache storage.

    Safe to call repeatedly.
    """
    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS predictions (
                        id SERIAL PRIMARY KEY,
                        fixture_id BIGINT UNIQUE,
                        match_date TEXT,
                        home_team TEXT,
                        away_team TEXT,
                        league TEXT,
                        markets_json JSONB,
                        confidence_label TEXT,
                        top_pick TEXT,
                        top_probability DOUBLE PRECISION,
                        odds_comparison_json JSONB,
                        actual_home_goals INTEGER,
                        actual_away_goals INTEGER,
                        top_pick_correct INTEGER,
                        home_team_id BIGINT,
                        away_team_id BIGINT,
                        prediction_context TEXT DEFAULT 'PRE_MATCH',
                        created_at TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS elo_ratings (
                        team_id BIGINT PRIMARY KEY,
                        team_name TEXT,
                        rating DOUBLE PRECISION,
                        updated_at TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS basketball_predictions (
                        id SERIAL PRIMARY KEY,
                        game_id BIGINT UNIQUE,
                        game_date TEXT,
                        home_team TEXT,
                        away_team TEXT,
                        league TEXT,
                        markets_json JSONB,
                        confidence_label TEXT,
                        top_pick TEXT,
                        top_probability DOUBLE PRECISION,
                        actual_home_points INTEGER,
                        actual_away_points INTEGER,
                        top_pick_correct INTEGER,
                        prediction_context TEXT DEFAULT 'PRE_MATCH',
                        created_at TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bot_memory (
                        id SERIAL PRIMARY KEY,
                        chat_id TEXT,
                        role TEXT,
                        text TEXT,
                        timestamp TEXT,
                        created_at TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS api_cache (
                        cache_key TEXT PRIMARY KEY,
                        endpoint TEXT,
                        request_params JSONB,
                        response_payload JSONB,
                        fetched_at TEXT,
                        expires_at TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS api_request_counts (
                        id SERIAL PRIMARY KEY,
                        provider TEXT,
                        request_timestamp TEXT,
                        request_date TEXT,
                        endpoint TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS historical_fixtures (
                        fixture_id BIGINT PRIMARY KEY,
                        league_id BIGINT NOT NULL,
                        season INTEGER NOT NULL,
                        kickoff_at TEXT NOT NULL,
                        status_short TEXT,
                        home_team_id BIGINT,
                        away_team_id BIGINT,
                        home_team TEXT,
                        away_team TEXT,
                        home_goals INTEGER,
                        away_goals INTEGER,
                        raw_json JSONB NOT NULL,
                        source TEXT NOT NULL DEFAULT 'api_football',
                        fetched_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_hist_fixtures_league_season ON historical_fixtures (league_id, season)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_hist_fixtures_league_season_kickoff ON historical_fixtures (league_id, season, kickoff_at)"
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS historical_fixture_enrichment (
                        fixture_id BIGINT PRIMARY KEY,
                        raw_json JSONB NOT NULL,
                        fetched_at TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'api_football'
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS historical_datasets (
                        sport TEXT NOT NULL DEFAULT 'football',
                        league_id BIGINT NOT NULL,
                        season INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'INCOMPLETE',
                        fixture_count INTEGER NOT NULL DEFAULT 0,
                        expected_pages INTEGER NOT NULL DEFAULT 0,
                        pages_completed INTEGER NOT NULL DEFAULT 0,
                        acquisition_complete INTEGER NOT NULL DEFAULT 0,
                        enrichment_status TEXT NOT NULL DEFAULT 'NONE',
                        completed_at TEXT,
                        updated_at TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'api_football',
                        error_reason TEXT,
                        PRIMARY KEY (sport, league_id, season)
                    )
                    """
                )
                cur.execute("ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS sport TEXT NOT NULL DEFAULT 'football'")
                cur.execute("ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS expected_pages INTEGER NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS pages_completed INTEGER NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS acquisition_complete INTEGER NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS enrichment_status TEXT NOT NULL DEFAULT 'NONE'")
                cur.execute("ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS error_reason TEXT")
                cur.execute(
                    """
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM pg_constraint WHERE conname = 'historical_datasets_pkey'
                        ) AND NOT EXISTS (
                            SELECT 1 FROM pg_constraint c
                            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
                            WHERE c.conname = 'historical_datasets_pkey' AND a.attname = 'sport'
                        ) THEN
                            ALTER TABLE historical_datasets DROP CONSTRAINT historical_datasets_pkey;
                            ALTER TABLE historical_datasets ADD PRIMARY KEY (sport, league_id, season);
                        END IF;
                    END $$;
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS historical_basketball_games (
                        game_id BIGINT PRIMARY KEY,
                        league_id BIGINT NOT NULL,
                        season INTEGER NOT NULL,
                        game_date TEXT NOT NULL,
                        status_short TEXT,
                        home_team_id BIGINT,
                        away_team_id BIGINT,
                        home_team TEXT,
                        away_team TEXT,
                        home_points INTEGER,
                        away_points INTEGER,
                        raw_json JSONB NOT NULL,
                        source TEXT NOT NULL DEFAULT 'api_basketball',
                        fetched_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_hist_basketball_league_season ON historical_basketball_games (league_id, season)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_hist_basketball_league_season_date ON historical_basketball_games (league_id, season, game_date)"
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS backtest_runs (
                        run_id TEXT PRIMARY KEY,
                        sport TEXT NOT NULL,
                        league_id BIGINT NOT NULL,
                        season INTEGER NOT NULL,
                        dataset_identity TEXT,
                        dataset_fixture_count INTEGER NOT NULL,
                        sample_size INTEGER NOT NULL,
                        min_prior_matches INTEGER NOT NULL,
                        sample_seed INTEGER,
                        selected_count INTEGER NOT NULL,
                        graded_count INTEGER NOT NULL,
                        accuracy DOUBLE PRECISION,
                        brier_score DOUBLE PRECISION,
                        log_loss DOUBLE PRECISION,
                        ece DOUBLE PRECISION,
                        enrichment_status TEXT DEFAULT 'NONE',
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        evaluation_json JSONB,
                        code_version TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS backtest_market_metrics (
                        id SERIAL PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
                        sport TEXT NOT NULL,
                        market_key TEXT NOT NULL,
                        sample_count INTEGER NOT NULL,
                        accuracy DOUBLE PRECISION,
                        brier_score DOUBLE PRECISION,
                        log_loss DOUBLE PRECISION,
                        ece DOUBLE PRECISION,
                        metrics_json JSONB
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_backtest_market_metrics_run_id ON backtest_market_metrics (run_id)"
                )
            conn.commit()

        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fixture_id INTEGER UNIQUE,
                    match_date TEXT,
                    home_team TEXT,
                    away_team TEXT,
                    league TEXT,
                    markets_json TEXT,
                    confidence_label TEXT,
                    top_pick TEXT,
                    top_probability REAL,
                    odds_comparison_json TEXT,
                    actual_home_goals INTEGER,
                    actual_away_goals INTEGER,
                    top_pick_correct INTEGER,
                    home_team_id INTEGER,
                    away_team_id INTEGER,
                    prediction_context TEXT DEFAULT 'PRE_MATCH',
                    created_at TEXT
                )
                """
            )
            _ensure_column_sqlite(conn, "predictions", "home_team_id", "INTEGER")
            _ensure_column_sqlite(conn, "predictions", "away_team_id", "INTEGER")
            _ensure_column_sqlite(conn, "predictions", "prediction_context", "TEXT DEFAULT 'PRE_MATCH'")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS elo_ratings (
                    team_id INTEGER PRIMARY KEY,
                    team_name TEXT,
                    rating REAL,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS basketball_predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id INTEGER UNIQUE,
                    game_date TEXT,
                    home_team TEXT,
                    away_team TEXT,
                    league TEXT,
                    markets_json TEXT,
                    confidence_label TEXT,
                    top_pick TEXT,
                    top_probability REAL,
                    actual_home_points INTEGER,
                    actual_away_points INTEGER,
                    top_pick_correct INTEGER,
                    prediction_context TEXT DEFAULT 'PRE_MATCH',
                    created_at TEXT
                )
                """
            )
            _ensure_column_sqlite(conn, "basketball_predictions", "prediction_context", "TEXT DEFAULT 'PRE_MATCH'")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT,
                    role TEXT,
                    text TEXT,
                    timestamp TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_cache (
                    cache_key TEXT PRIMARY KEY,
                    endpoint TEXT,
                    request_params TEXT,
                    response_payload TEXT,
                    fetched_at TEXT,
                    expires_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_request_counts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT,
                    request_timestamp TEXT,
                    request_date TEXT,
                    endpoint TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS historical_fixtures (
                    fixture_id INTEGER PRIMARY KEY,
                    league_id INTEGER NOT NULL,
                    season INTEGER NOT NULL,
                    kickoff_at TEXT NOT NULL,
                    status_short TEXT,
                    home_team_id INTEGER,
                    away_team_id INTEGER,
                    home_team TEXT,
                    away_team TEXT,
                    home_goals INTEGER,
                    away_goals INTEGER,
                    raw_json TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'api_football',
                    fetched_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hist_fixtures_league_season ON historical_fixtures (league_id, season)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hist_fixtures_league_season_kickoff ON historical_fixtures (league_id, season, kickoff_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS historical_fixture_enrichment (
                    fixture_id INTEGER PRIMARY KEY,
                    raw_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'api_football'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS historical_datasets (
                    sport TEXT NOT NULL DEFAULT 'football',
                    league_id INTEGER NOT NULL,
                    season INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'INCOMPLETE',
                    fixture_count INTEGER NOT NULL DEFAULT 0,
                    expected_pages INTEGER NOT NULL DEFAULT 0,
                    pages_completed INTEGER NOT NULL DEFAULT 0,
                    acquisition_complete INTEGER NOT NULL DEFAULT 0,
                    enrichment_status TEXT NOT NULL DEFAULT 'NONE',
                    rejected_count INTEGER NOT NULL DEFAULT 0,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'api_football',
                    error_reason TEXT,
                    PRIMARY KEY (sport, league_id, season)
                )
                """
            )
            _ensure_column_sqlite(conn, "historical_datasets", "sport", "TEXT NOT NULL DEFAULT 'football'")
            _ensure_column_sqlite(conn, "historical_datasets", "expected_pages", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column_sqlite(conn, "historical_datasets", "pages_completed", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column_sqlite(conn, "historical_datasets", "acquisition_complete", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column_sqlite(conn, "historical_datasets", "enrichment_status", "TEXT NOT NULL DEFAULT 'NONE'")
            _ensure_column_sqlite(conn, "historical_datasets", "rejected_count", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column_sqlite(conn, "historical_datasets", "error_reason", "TEXT")

            tbl_info = conn.execute("PRAGMA table_info(historical_datasets)").fetchall()
            col_names = [row[1] for row in tbl_info]
            pk_cols = [row[1] for row in tbl_info if row[5] > 0]
            if pk_cols and ("sport" not in pk_cols or "rejected_count" not in col_names):
                conn.execute("BEGIN TRANSACTION")
                try:
                    conn.execute("ALTER TABLE historical_datasets RENAME TO historical_datasets_old")
                    conn.execute(
                        """
                        CREATE TABLE historical_datasets (
                            sport TEXT NOT NULL DEFAULT 'football',
                            league_id INTEGER NOT NULL,
                            season INTEGER NOT NULL,
                            status TEXT NOT NULL DEFAULT 'INCOMPLETE',
                            fixture_count INTEGER NOT NULL DEFAULT 0,
                            expected_pages INTEGER NOT NULL DEFAULT 0,
                            pages_completed INTEGER NOT NULL DEFAULT 0,
                            acquisition_complete INTEGER NOT NULL DEFAULT 0,
                            enrichment_status TEXT NOT NULL DEFAULT 'NONE',
                            rejected_count INTEGER NOT NULL DEFAULT 0,
                            completed_at TEXT,
                            updated_at TEXT NOT NULL,
                            source TEXT NOT NULL DEFAULT 'api_football',
                            error_reason TEXT,
                            PRIMARY KEY (sport, league_id, season)
                        )
                        """
                    )
                    old_cols = {row[1] for row in conn.execute("PRAGMA table_info(historical_datasets_old)").fetchall()}
                    sport_expr = "COALESCE(sport, 'football')" if "sport" in old_cols else "'football'"
                    exp_expr = "expected_pages" if "expected_pages" in old_cols else "0"
                    pc_expr = "pages_completed" if "pages_completed" in old_cols else "0"
                    ac_expr = "acquisition_complete" if "acquisition_complete" in old_cols else "0"
                    es_expr = "COALESCE(enrichment_status, 'NONE')" if "enrichment_status" in old_cols else "'NONE'"
                    rej_expr = "COALESCE(rejected_count, 0)" if "rejected_count" in old_cols else "0"
                    err_expr = "error_reason" if "error_reason" in old_cols else "NULL"
                    comp_expr = "completed_at" if "completed_at" in old_cols else "NULL"
                    upd_expr = "updated_at" if "updated_at" in old_cols else "datetime('now')"
                    src_expr = "COALESCE(source, 'api_football')" if "source" in old_cols else "'api_football'"

                    conn.execute(
                        f"""
                        INSERT OR IGNORE INTO historical_datasets (
                            sport, league_id, season, status, fixture_count, expected_pages,
                            pages_completed, acquisition_complete, enrichment_status, rejected_count,
                            completed_at, updated_at, source, error_reason
                        )
                        SELECT {sport_expr}, league_id, season, status, fixture_count, {exp_expr},
                               {pc_expr}, {ac_expr}, {es_expr}, {rej_expr},
                               {comp_expr}, {upd_expr}, {src_expr}, {err_expr}
                        FROM historical_datasets_old
                        """
                    )
                    conn.execute("DROP TABLE historical_datasets_old")
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS historical_basketball_games (
                    game_id INTEGER PRIMARY KEY,
                    league_id INTEGER NOT NULL,
                    season INTEGER NOT NULL,
                    game_date TEXT NOT NULL,
                    status_short TEXT,
                    home_team_id INTEGER,
                    away_team_id INTEGER,
                    home_team TEXT,
                    away_team TEXT,
                    home_points INTEGER,
                    away_points INTEGER,
                    raw_json TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'api_basketball',
                    fetched_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hist_basketball_league_season ON historical_basketball_games (league_id, season)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hist_basketball_league_season_date ON historical_basketball_games (league_id, season, game_date)"
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS backtest_runs (
                    run_id TEXT PRIMARY KEY,
                    sport TEXT NOT NULL,
                    league_id INTEGER NOT NULL,
                    season INTEGER NOT NULL,
                    dataset_identity TEXT,
                    dataset_fixture_count INTEGER NOT NULL,
                    sample_size INTEGER NOT NULL,
                    min_prior_matches INTEGER NOT NULL,
                    sample_seed INTEGER,
                    selected_count INTEGER NOT NULL,
                    graded_count INTEGER NOT NULL,
                    accuracy REAL,
                    brier_score REAL,
                    log_loss REAL,
                    ece REAL,
                    enrichment_status TEXT DEFAULT 'NONE',
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    evaluation_json TEXT,
                    code_version TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS backtest_market_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
                    sport TEXT NOT NULL,
                    market_key TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    accuracy REAL,
                    brier_score REAL,
                    log_loss REAL,
                    ece REAL,
                    metrics_json TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_backtest_market_metrics_run_id ON backtest_market_metrics (run_id)"
            )
            conn.commit()

    except Exception:
        if db_type == "postgres":
            conn.rollback()
        else:
            conn.rollback()
        raise

    finally:
        conn.close()


# ----------------------------------------------------------------------
# Historical Basketball Storage Functions
# ----------------------------------------------------------------------


def validate_historical_basketball_game(item, target_league_id=None, target_season=None):
    """
    Validate a raw historical basketball game item against ingestion rules.
    """
    if not isinstance(item, dict):
        return False, "game_item_not_dict", None

    gid = _parse_strict_int(item.get("id"))
    if gid is None or gid <= 0:
        return False, "missing_invalid_or_non_positive_game_id", None

    date_str = item.get("date")
    if not date_str or not isinstance(date_str, str):
        return False, "missing_game_date", None

    teams_obj = item.get("teams")
    if not isinstance(teams_obj, dict):
        return False, "missing_or_invalid_teams_object", None

    home_obj = teams_obj.get("home")
    away_obj = teams_obj.get("away")
    if not isinstance(home_obj, dict) or not isinstance(away_obj, dict):
        return False, "missing_home_or_away_team_object", None

    h_id = _parse_strict_int(home_obj.get("id"))
    a_id = _parse_strict_int(away_obj.get("id"))

    if h_id is None or h_id <= 0:
        return False, "missing_invalid_or_non_positive_home_team_id", None
    if a_id is None or a_id <= 0:
        return False, "missing_invalid_or_non_positive_away_team_id", None

    if h_id == a_id:
        return False, "identical_home_and_away_team_ids", None

    h_name = home_obj.get("name")
    away_name = away_obj.get("name")
    if not isinstance(h_name, str) or not h_name.strip():
        return False, "missing_or_empty_home_team_name", None
    if not isinstance(away_name, str) or not away_name.strip():
        return False, "missing_or_empty_away_team_name", None

    league_obj = item.get("league")
    if isinstance(league_obj, dict):
        raw_p_league_id = league_obj.get("id")
        if raw_p_league_id is not None:
            p_league_id = _parse_strict_int(raw_p_league_id)
            if p_league_id is None:
                return False, "invalid_payload_league_id", None
            if target_league_id is not None and p_league_id != target_league_id:
                return False, f"conflicting_league_id ({p_league_id} != {target_league_id})", None

        raw_p_season = league_obj.get("season")
        if raw_p_season is not None:
            p_season = _parse_strict_int(raw_p_season)
            if p_season is None:
                return False, "invalid_payload_season", None
            if target_season is not None and str(p_season) != str(target_season):
                return False, f"conflicting_season ({p_season} != {target_season})", None

    status_short = item.get("status", {}).get("short") if isinstance(item.get("status"), dict) else None
    scores_obj = item.get("scores")

    h_pts = None
    a_pts = None
    if isinstance(scores_obj, dict):
        home_score = scores_obj.get("home", {})
        away_score = scores_obj.get("away", {})
        raw_h_pts = home_score.get("total") if isinstance(home_score, dict) else None
        raw_a_pts = away_score.get("total") if isinstance(away_score, dict) else None

        if raw_h_pts is not None:
            h_pts = _parse_strict_int(raw_h_pts)
            if h_pts is None or h_pts < 0:
                return False, "invalid_or_negative_home_points", None

        if raw_a_pts is not None:
            a_pts = _parse_strict_int(raw_a_pts)
            if a_pts is None or a_pts < 0:
                return False, "invalid_or_negative_away_points", None

    is_completed = status_short in ("FT", "AOT")
    if is_completed:
        if h_pts is None or a_pts is None:
            return False, f"completed_game_missing_points (status={status_short})", None

    normalized = dict(item)
    normalized["id"] = gid
    return True, "valid", normalized


def save_historical_basketball_games(games, league_id, season, source="api_basketball"):
    """
    Save historical API-Basketball games into persistent storage.
    """
    league_id = _validate_positive_int(league_id, "league_id")
    season = _validate_positive_int(season, "season")

    if not isinstance(games, (list, tuple)):
        raise ValueError("games must be a list or tuple.")

    valid_games = []
    seen_ids = set()
    dup_count = 0
    rejected_count = 0
    rejection_reasons = {}

    for item in games:
        is_valid, reason, norm_item = validate_historical_basketball_game(
            item, target_league_id=league_id, target_season=season
        )
        if not is_valid:
            rejected_count += 1
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
            continue

        gid = norm_item["id"]
        if gid in seen_ids:
            dup_count += 1
            continue

        seen_ids.add(gid)
        valid_games.append((gid, norm_item))

    if not valid_games:
        return {
            "total": len(games),
            "valid": 0,
            "inserted": 0,
            "duplicates_skipped": dup_count,
            "rejected_count": rejected_count,
            "rejection_reasons": rejection_reasons,
        }

    conn, db_type = _connect()
    now_str = _utc_now()
    inserted_count = 0

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                for gid, item in valid_games:
                    gdate = str(item.get("date", ""))
                    status = item.get("status", {}).get("short", "") if isinstance(item.get("status"), dict) else ""
                    teams = item.get("teams", {})
                    h_id = teams.get("home", {}).get("id")
                    a_id = teams.get("away", {}).get("id")
                    h_name = teams.get("home", {}).get("name", "")
                    a_name = teams.get("away", {}).get("name", "")
                    scores = item.get("scores", {})
                    h_pts = scores.get("home", {}).get("total") if isinstance(scores.get("home"), dict) else None
                    a_pts = scores.get("away", {}).get("total") if isinstance(scores.get("away"), dict) else None

                    raw_json_str = _json_dumps(item, "raw_json")

                    cur.execute(
                        """
                        INSERT INTO historical_basketball_games (
                            game_id, league_id, season, game_date, status_short,
                            home_team_id, away_team_id, home_team, away_team,
                            home_points, away_points, raw_json, source, fetched_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (game_id) DO NOTHING
                        """,
                        (
                            gid, league_id, season, gdate, status,
                            h_id, a_id, h_name, a_name,
                            h_pts, a_pts, raw_json_str, source, now_str,
                        ),
                    )
                    if cur.rowcount == 1:
                        inserted_count += 1
            conn.commit()

        else:
            for gid, item in valid_games:
                gdate = str(item.get("date", ""))
                status = item.get("status", {}).get("short", "") if isinstance(item.get("status"), dict) else ""
                teams = item.get("teams", {})
                h_id = teams.get("home", {}).get("id")
                a_id = teams.get("away", {}).get("id")
                h_name = teams.get("home", {}).get("name", "")
                a_name = teams.get("away", {}).get("name", "")
                scores = item.get("scores", {})
                h_pts = scores.get("home", {}).get("total") if isinstance(scores.get("home"), dict) else None
                a_pts = scores.get("away", {}).get("total") if isinstance(scores.get("away"), dict) else None

                raw_json_str = _json_dumps(item, "raw_json")

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO historical_basketball_games (
                        game_id, league_id, season, game_date, status_short,
                        home_team_id, away_team_id, home_team, away_team,
                        home_points, away_points, raw_json, source, fetched_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        gid, league_id, season, gdate, status,
                        h_id, a_id, h_name, a_name,
                        h_pts, a_pts, raw_json_str, source, now_str,
                    ),
                )
                if cursor.rowcount == 1:
                    inserted_count += 1
            conn.commit()

        return {
            "total": len(games),
            "valid": len(valid_games),
            "inserted": inserted_count,
            "duplicates_skipped": dup_count,
            "rejected_count": rejected_count,
            "rejection_reasons": rejection_reasons,
        }

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_historical_basketball_games(league_id, season):
    """
    Retrieve stored historical basketball games for a league and season.
    """
    league_id = _validate_positive_int(league_id, "league_id")
    season = _validate_positive_int(season, "season")

    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT raw_json
                    FROM historical_basketball_games
                    WHERE league_id = %s AND season = %s
                    ORDER BY game_date ASC, game_id ASC
                    """,
                    (league_id, season),
                )
                rows = cur.fetchall()
        else:
            try:
                rows = conn.execute(
                    """
                    SELECT raw_json
                    FROM historical_basketball_games
                    WHERE league_id = ? AND season = ?
                    ORDER BY game_date ASC, game_id ASC
                    """,
                    (league_id, season),
                ).fetchall()
            except sqlite3.OperationalError:
                conn.close()
                init_db()
                conn, _ = _connect()
                rows = conn.execute(
                    """
                    SELECT raw_json
                    FROM historical_basketball_games
                    WHERE league_id = ? AND season = ?
                    ORDER BY game_date ASC, game_id ASC
                    """,
                    (league_id, season),
                ).fetchall()

        games = []
        for row in rows:
            payload = _json_loads(row[0])
            if isinstance(payload, dict):
                games.append(payload)

        return games

    finally:
        conn.close()


def get_historical_basketball_game_count(league_id, season):
    """Get count of stored historical basketball games for a league and season."""
    league_id = _validate_positive_int(league_id, "league_id")
    season = _validate_positive_int(season, "season")

    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM historical_basketball_games WHERE league_id = %s AND season = %s",
                    (league_id, season),
                )
                row = cur.fetchone()
                return row[0] if row else 0
        else:
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM historical_basketball_games WHERE league_id = ? AND season = ?",
                    (league_id, season),
                ).fetchone()
                return row[0] if row else 0
            except sqlite3.OperationalError:
                conn.close()
                init_db()
                conn, _ = _connect()
                row = conn.execute(
                    "SELECT COUNT(*) FROM historical_basketball_games WHERE league_id = ? AND season = ?",
                    (league_id, season),
                ).fetchone()
                return row[0] if row else 0
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Backtest Persistence Storage Functions
# ----------------------------------------------------------------------


def save_backtest_run(run_data, market_metrics=None):
    """
    Save a completed backtest experiment run and its structured market metrics.
    """
    if not isinstance(run_data, dict):
        raise ValueError("run_data must be a dictionary.")

    run_id = _validate_text(run_data.get("run_id"), "run_id")
    sport = _validate_text(run_data.get("sport", "football"), "sport").lower()
    league_id = _validate_positive_int(run_data.get("league_id"), "league_id")
    season = _validate_positive_int(run_data.get("season"), "season")
    dataset_identity = run_data.get("dataset_identity") or f"{sport}_{league_id}_{season}"
    dataset_fixture_count = _validate_non_negative_int(run_data.get("dataset_fixture_count", 0), "dataset_fixture_count")
    sample_size = _validate_positive_int(run_data.get("sample_size", 1), "sample_size")
    min_prior_matches = _validate_non_negative_int(run_data.get("min_prior_matches", 0), "min_prior_matches")
    sample_seed = run_data.get("sample_seed")
    selected_count = _validate_non_negative_int(run_data.get("selected_count", 0), "selected_count")
    graded_count = _validate_non_negative_int(run_data.get("graded_count", 0), "graded_count")
    accuracy = run_data.get("accuracy")
    brier_score = run_data.get("brier_score")
    log_loss = run_data.get("log_loss")
    ece = run_data.get("ece")
    enrichment_status = run_data.get("enrichment_status", "NONE")
    started_at = run_data.get("started_at") or _utc_now()
    completed_at = run_data.get("completed_at") or _utc_now()
    eval_json_str = _json_dumps(run_data.get("evaluation_json"), "evaluation_json") if run_data.get("evaluation_json") else None
    code_version = run_data.get("code_version")

    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO backtest_runs (
                            run_id, sport, league_id, season, dataset_identity, dataset_fixture_count,
                            sample_size, min_prior_matches, sample_seed, selected_count, graded_count,
                            accuracy, brier_score, log_loss, ece, enrichment_status,
                            started_at, completed_at, evaluation_json, code_version
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (run_id) DO UPDATE SET
                            accuracy = EXCLUDED.accuracy,
                            brier_score = EXCLUDED.brier_score,
                            log_loss = EXCLUDED.log_loss,
                            ece = EXCLUDED.ece,
                            evaluation_json = EXCLUDED.evaluation_json,
                            completed_at = EXCLUDED.completed_at
                        """,
                        (
                            run_id, sport, league_id, season, dataset_identity, dataset_fixture_count,
                            sample_size, min_prior_matches, sample_seed, selected_count, graded_count,
                            accuracy, brier_score, log_loss, ece, enrichment_status,
                            started_at, completed_at, eval_json_str, code_version,
                        ),
                    )

                    if isinstance(market_metrics, (list, tuple)):
                        for mm in market_metrics:
                            if not isinstance(mm, dict):
                                continue
                            m_key = mm.get("market_key", "unknown")
                            s_count = mm.get("sample_count", 0)
                            acc = mm.get("accuracy")
                            brier = mm.get("brier_score")
                            lloss = mm.get("log_loss")
                            m_ece = mm.get("ece")
                            m_json_str = _json_dumps(mm.get("metrics_json"), "metrics_json") if mm.get("metrics_json") else None

                            cur.execute(
                                """
                                INSERT INTO backtest_market_metrics (
                                    run_id, sport, market_key, sample_count, accuracy, brier_score, log_loss, ece, metrics_json
                                )
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                (run_id, sport, m_key, s_count, acc, brier, lloss, m_ece, m_json_str),
                            )

        else:
            conn.execute(
                """
                INSERT INTO backtest_runs (
                    run_id, sport, league_id, season, dataset_identity, dataset_fixture_count,
                    sample_size, min_prior_matches, sample_seed, selected_count, graded_count,
                    accuracy, brier_score, log_loss, ece, enrichment_status,
                    started_at, completed_at, evaluation_json, code_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id) DO UPDATE SET
                    accuracy = excluded.accuracy,
                    brier_score = excluded.brier_score,
                    log_loss = excluded.log_loss,
                    ece = excluded.ece,
                    evaluation_json = excluded.evaluation_json,
                    completed_at = excluded.completed_at
                """,
                (
                    run_id, sport, league_id, season, dataset_identity, dataset_fixture_count,
                    sample_size, min_prior_matches, sample_seed, selected_count, graded_count,
                    accuracy, brier_score, log_loss, ece, enrichment_status,
                    started_at, completed_at, eval_json_str, code_version,
                ),
            )

            if isinstance(market_metrics, (list, tuple)):
                for mm in market_metrics:
                    if not isinstance(mm, dict):
                        continue
                    m_key = mm.get("market_key", "unknown")
                    s_count = mm.get("sample_count", 0)
                    acc = mm.get("accuracy")
                    brier = mm.get("brier_score")
                    lloss = mm.get("log_loss")
                    m_ece = mm.get("ece")
                    m_json_str = _json_dumps(mm.get("metrics_json"), "metrics_json") if mm.get("metrics_json") else None

                    conn.execute(
                        """
                        INSERT INTO backtest_market_metrics (
                            run_id, sport, market_key, sample_count, accuracy, brier_score, log_loss, ece, metrics_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (run_id, sport, m_key, s_count, acc, brier, lloss, m_ece, m_json_str),
                    )

            conn.commit()

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_latest_backtest_runs(sport=None, limit=10):
    """
    Fetch recent backtest experiment runs for summary dashboard/reporting.
    """
    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                if sport:
                    cur.execute(
                        """
                        SELECT run_id, sport, league_id, season, dataset_fixture_count, sample_size,
                               graded_count, accuracy, brier_score, log_loss, completed_at
                        FROM backtest_runs
                        WHERE sport = %s
                        ORDER BY completed_at DESC
                        LIMIT %s
                        """,
                        (sport.lower(), limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT run_id, sport, league_id, season, dataset_fixture_count, sample_size,
                               graded_count, accuracy, brier_score, log_loss, completed_at
                        FROM backtest_runs
                        ORDER BY completed_at DESC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                return cur.fetchall()
        else:
            if sport:
                return conn.execute(
                    """
                    SELECT run_id, sport, league_id, season, dataset_fixture_count, sample_size,
                           graded_count, accuracy, brier_score, log_loss, completed_at
                    FROM backtest_runs
                    WHERE sport = ?
                    ORDER BY completed_at DESC
                    LIMIT ?
                    """,
                    (sport.lower(), limit),
                ).fetchall()
            else:
                return conn.execute(
                    """
                    SELECT run_id, sport, league_id, season, dataset_fixture_count, sample_size,
                           graded_count, accuracy, brier_score, log_loss, completed_at
                    FROM backtest_runs
                    ORDER BY completed_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()


def get_historical_dataset_status(league_id, season, sport="football"):
    """
    Get the dataset manifest status for a sport, league, and season.
    """
    league_id = _validate_positive_int(league_id, "league_id")
    season = _validate_positive_int(season, "season")
    sport = _validate_text(sport, "sport").lower()

    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, fixture_count, expected_pages, pages_completed,
                           acquisition_complete, enrichment_status, rejected_count, completed_at, updated_at, error_reason
                    FROM historical_datasets
                    WHERE sport = %s AND league_id = %s AND season = %s
                    """,
                    (sport, league_id, season),
                )
                row = cur.fetchone()
        else:
            try:
                row = conn.execute(
                    """
                    SELECT status, fixture_count, expected_pages, pages_completed,
                           acquisition_complete, enrichment_status, rejected_count, completed_at, updated_at, error_reason
                    FROM historical_datasets
                    WHERE sport = ? AND league_id = ? AND season = ?
                    """,
                    (sport, league_id, season),
                ).fetchone()
            except sqlite3.OperationalError:
                conn.close()
                init_db()
                conn, _ = _connect()
                row = conn.execute(
                    """
                    SELECT status, fixture_count, expected_pages, pages_completed,
                           acquisition_complete, enrichment_status, rejected_count, completed_at, updated_at, error_reason
                    FROM historical_datasets
                    WHERE sport = ? AND league_id = ? AND season = ?
                    """,
                    (sport, league_id, season),
                ).fetchone()

        if row:
            return {
                "sport": sport,
                "league_id": league_id,
                "season": season,
                "status": row[0],
                "fixture_count": row[1],
                "expected_pages": row[2] or 0,
                "pages_completed": row[3] or 0,
                "acquisition_complete": bool(row[4]),
                "enrichment_status": row[5] or "NONE",
                "rejected_count": row[6] or 0,
                "completed_at": row[7],
                "updated_at": row[8],
                "error_reason": row[9],
            }

        return {
            "sport": sport,
            "league_id": league_id,
            "season": season,
            "status": "INCOMPLETE",
            "fixture_count": 0,
            "expected_pages": 0,
            "pages_completed": 0,
            "acquisition_complete": False,
            "enrichment_status": "NONE",
            "rejected_count": 0,
            "completed_at": None,
            "updated_at": None,
            "error_reason": None,
        }

    finally:
        conn.close()


def mark_historical_dataset_complete(
    league_id,
    season,
    fixture_count,
    source=None,
    sport="football",
    expected_pages=0,
    pages_completed=0,
    acquisition_complete=True,
    enrichment_status="NONE",
    rejected_count=0,
):
    """
    Mark a historical dataset as COMPLETE.
    """
    league_id = _validate_positive_int(league_id, "league_id")
    season = _validate_positive_int(season, "season")
    fixture_count = _validate_non_negative_int(fixture_count, "fixture_count")
    rejected_count = _validate_non_negative_int(rejected_count, "rejected_count")
    sport = _validate_text(sport, "sport").lower()
    if source is None:
        source = "api_basketball" if sport == "basketball" else "api_football"

    acq_int = 1 if acquisition_complete else 0
    now_str = _utc_now()
    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO historical_datasets (
                        sport, league_id, season, status, fixture_count, expected_pages, pages_completed,
                        acquisition_complete, enrichment_status, rejected_count, completed_at, updated_at, source, error_reason
                    )
                    VALUES (%s, %s, %s, 'COMPLETE', %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                    ON CONFLICT (sport, league_id, season) DO UPDATE SET
                        status = 'COMPLETE',
                        fixture_count = EXCLUDED.fixture_count,
                        expected_pages = EXCLUDED.expected_pages,
                        pages_completed = EXCLUDED.pages_completed,
                        acquisition_complete = EXCLUDED.acquisition_complete,
                        enrichment_status = EXCLUDED.enrichment_status,
                        rejected_count = EXCLUDED.rejected_count,
                        completed_at = EXCLUDED.completed_at,
                        updated_at = EXCLUDED.updated_at,
                        source = EXCLUDED.source,
                        error_reason = NULL
                    """,
                    (
                        sport, league_id, season, fixture_count, expected_pages, pages_completed,
                        acq_int, enrichment_status, rejected_count, now_str, now_str, source,
                    ),
                )
            conn.commit()
        else:
            conn.execute(
                """
                INSERT INTO historical_datasets (
                    sport, league_id, season, status, fixture_count, expected_pages, pages_completed,
                    acquisition_complete, enrichment_status, rejected_count, completed_at, updated_at, source, error_reason
                )
                VALUES (?, ?, ?, 'COMPLETE', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT (sport, league_id, season) DO UPDATE SET
                    status = 'COMPLETE',
                    fixture_count = excluded.fixture_count,
                    expected_pages = excluded.expected_pages,
                    pages_completed = excluded.pages_completed,
                    acquisition_complete = excluded.acquisition_complete,
                    enrichment_status = excluded.enrichment_status,
                    rejected_count = excluded.rejected_count,
                    completed_at = excluded.completed_at,
                    updated_at = excluded.updated_at,
                    source = excluded.source,
                    error_reason = NULL
                """,
                (
                    sport, league_id, season, fixture_count, expected_pages, pages_completed,
                    acq_int, enrichment_status, rejected_count, now_str, now_str, source,
                ),
            )
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_historical_dataset_incomplete(
    league_id,
    season,
    fixture_count=None,
    source=None,
    sport="football",
    expected_pages=0,
    pages_completed=0,
    acquisition_complete=False,
    enrichment_status="NONE",
    rejected_count=0,
    error_reason=None,
):
    """
    Mark or keep a historical dataset status as INCOMPLETE.
    """
    league_id = _validate_positive_int(league_id, "league_id")
    season = _validate_positive_int(season, "season")
    rejected_count = _validate_non_negative_int(rejected_count, "rejected_count")
    sport = _validate_text(sport, "sport").lower()
    if source is None:
        source = "api_basketball" if sport == "basketball" else "api_football"

    if fixture_count is None:
        if sport == "basketball":
            fixture_count = get_historical_basketball_game_count(league_id, season)
        else:
            fixture_count = get_historical_fixture_count(league_id, season)
    else:
        fixture_count = _validate_non_negative_int(fixture_count, "fixture_count")

    acq_int = 1 if acquisition_complete else 0
    now_str = _utc_now()
    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO historical_datasets (
                        sport, league_id, season, status, fixture_count, expected_pages, pages_completed,
                        acquisition_complete, enrichment_status, rejected_count, completed_at, updated_at, source, error_reason
                    )
                    VALUES (%s, %s, %s, 'INCOMPLETE', %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s)
                    ON CONFLICT (sport, league_id, season) DO UPDATE SET
                        status = 'INCOMPLETE',
                        fixture_count = EXCLUDED.fixture_count,
                        expected_pages = EXCLUDED.expected_pages,
                        pages_completed = EXCLUDED.pages_completed,
                        acquisition_complete = EXCLUDED.acquisition_complete,
                        enrichment_status = EXCLUDED.enrichment_status,
                        rejected_count = EXCLUDED.rejected_count,
                        updated_at = EXCLUDED.updated_at,
                        source = EXCLUDED.source,
                        error_reason = EXCLUDED.error_reason
                    """,
                    (
                        sport, league_id, season, fixture_count, expected_pages, pages_completed,
                        acq_int, enrichment_status, rejected_count, now_str, source, error_reason,
                    ),
                )
            conn.commit()
        else:
            conn.execute(
                """
                INSERT INTO historical_datasets (
                    sport, league_id, season, status, fixture_count, expected_pages, pages_completed,
                    acquisition_complete, enrichment_status, rejected_count, completed_at, updated_at, source, error_reason
                )
                VALUES (?, ?, ?, 'INCOMPLETE', ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT (sport, league_id, season) DO UPDATE SET
                    status = 'INCOMPLETE',
                    fixture_count = excluded.fixture_count,
                    expected_pages = excluded.expected_pages,
                    pages_completed = excluded.pages_completed,
                    acquisition_complete = excluded.acquisition_complete,
                    enrichment_status = excluded.enrichment_status,
                    rejected_count = excluded.rejected_count,
                    updated_at = excluded.updated_at,
                    source = excluded.source,
                    error_reason = excluded.error_reason
                """,
                (
                    sport, league_id, season, fixture_count, expected_pages, pages_completed,
                    acq_int, enrichment_status, rejected_count, now_str, source, error_reason,
                ),
            )
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_basketball_db():
    """Initialize database tables (compatibility wrapper)."""
    init_db()


# ----------------------------------------------------------------------
# Football prediction storage
# ----------------------------------------------------------------------


def save_prediction(
    fixture_id,
    match_date,
    home_team,
    away_team,
    league,
    markets,
    confidence,
    home_team_id=None,
    away_team_id=None,
    odds_comparison=None,
    prediction_context="PRE_MATCH",
):
    """
    Save a football prediction exactly once.

    The first saved prediction is authoritative.
    Returns True if inserted, False if fixture already existed.
    """
    fixture_id = _validate_positive_int(fixture_id, "fixture_id")
    _validate_text(match_date, "match_date")
    _validate_text(home_team, "home_team")
    _validate_text(away_team, "away_team")
    _validate_text(league, "league")

    if not isinstance(markets, dict):
        raise ValueError("markets must be a dictionary.")
    if not isinstance(confidence, dict):
        raise ValueError("confidence must be a dictionary.")

    required_confidence = ("label", "top_pick", "top_probability")
    missing = [key for key in required_confidence if key not in confidence]
    if missing:
        raise ValueError("confidence is missing required fields: " + ", ".join(missing))

    _validate_text(confidence["label"], "confidence.label")
    _validate_text(confidence["top_pick"], "confidence.top_pick")
    top_probability = _validate_probability(confidence["top_probability"], "confidence.top_probability")

    home_team_id = _validate_optional_positive_int(home_team_id, "home_team_id")
    away_team_id = _validate_optional_positive_int(away_team_id, "away_team_id")
    prediction_context = _validate_text(prediction_context, "prediction_context").upper()
    if prediction_context not in ("PRE_MATCH", "LIVE"):
        prediction_context = "PRE_MATCH"

    markets_json_str = _json_dumps(markets, "markets")
    odds_json_str = _json_dumps(odds_comparison, "odds_comparison") if odds_comparison is not None else None

    conn, db_type = _connect()

    try:
        now_str = _utc_now()
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO predictions (
                        fixture_id, match_date, home_team, away_team, league,
                        markets_json, confidence_label, top_pick, top_probability,
                        odds_comparison_json, home_team_id, away_team_id,
                        prediction_context, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (fixture_id) DO NOTHING
                    """,
                    (
                        fixture_id, match_date, home_team, away_team, league,
                        markets_json_str, confidence["label"], confidence["top_pick"], top_probability,
                        odds_json_str, home_team_id, away_team_id,
                        prediction_context, now_str,
                    ),
                )
                inserted = cur.rowcount == 1
            conn.commit()
        else:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO predictions (
                    fixture_id, match_date, home_team, away_team, league,
                    markets_json, confidence_label, top_pick, top_probability,
                    odds_comparison_json, home_team_id, away_team_id,
                    prediction_context, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fixture_id, match_date, home_team, away_team, league,
                    markets_json_str, confidence["label"], confidence["top_pick"], top_probability,
                    odds_json_str, home_team_id, away_team_id,
                    prediction_context, now_str,
                ),
            )
            inserted = cursor.rowcount == 1
            conn.commit()

        return inserted

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Football result grading
# ----------------------------------------------------------------------


def record_result(fixture_id, home_goals, away_goals):
    """
    Record the final football result.

    Result writes are idempotent:
    - same result again -> True, no second Elo update
    - different result -> ValueError
    - unknown fixture -> False
    """
    fixture_id = _validate_positive_int(fixture_id, "fixture_id")
    home_goals = _validate_non_negative_int(home_goals, "home_goals")
    away_goals = _validate_non_negative_int(away_goals, "away_goals")

    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            top_pick, home_team_id, away_team_id, home_team, away_team,
                            actual_home_goals, actual_away_goals
                        FROM predictions
                        WHERE fixture_id = %s
                        FOR UPDATE
                        """,
                        (fixture_id,),
                    )
                    row = cur.fetchone()

                    if row is None:
                        return False

                    (
                        top_pick, home_id, away_id, home_name, away_name,
                        recorded_home_goals, recorded_away_goals,
                    ) = row

                    if recorded_home_goals is not None or recorded_away_goals is not None:
                        if recorded_home_goals == home_goals and recorded_away_goals == away_goals:
                            return True
                        raise ValueError(
                            f"A different result is already recorded for fixture {fixture_id}."
                        )

                    if home_goals > away_goals:
                        actual = "home_win"
                    elif home_goals < away_goals:
                        actual = "away_win"
                    else:
                        actual = "draw"

                    correct = 1 if _normalise_result_pick(top_pick) == actual else 0

                    cur.execute(
                        """
                        UPDATE predictions
                        SET actual_home_goals = %s, actual_away_goals = %s, top_pick_correct = %s
                        WHERE fixture_id = %s
                        """,
                        (home_goals, away_goals, correct, fixture_id),
                    )

                    if home_id is not None and away_id is not None:
                        _update_elo_ratings_postgres(
                            cur, home_id, home_name, away_id, away_name, home_goals, away_goals
                        )

            return True

        else:
            row = conn.execute(
                """
                SELECT
                    top_pick, home_team_id, away_team_id, home_team, away_team,
                    actual_home_goals, actual_away_goals
                FROM predictions
                WHERE fixture_id = ?
                """,
                (fixture_id,),
            ).fetchone()

            if row is None:
                conn.rollback()
                return False

            (
                top_pick, home_id, away_id, home_name, away_name,
                recorded_home_goals, recorded_away_goals,
            ) = row

            if recorded_home_goals is not None or recorded_away_goals is not None:
                if recorded_home_goals == home_goals and recorded_away_goals == away_goals:
                    conn.rollback()
                    return True
                raise ValueError(
                    f"A different result is already recorded for fixture {fixture_id}."
                )

            if home_goals > away_goals:
                actual = "home_win"
            elif home_goals < away_goals:
                actual = "away_win"
            else:
                actual = "draw"

            correct = 1 if _normalise_result_pick(top_pick) == actual else 0

            conn.execute(
                """
                UPDATE predictions
                SET actual_home_goals = ?, actual_away_goals = ?, top_pick_correct = ?
                WHERE fixture_id = ?
                """,
                (home_goals, away_goals, correct, fixture_id),
            )

            if home_id is not None and away_id is not None:
                _update_elo_ratings_sqlite_conn(
                    conn, home_id, home_name, away_id, away_name, home_goals, away_goals
                )

            conn.commit()
            return True

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Football Elo
# ----------------------------------------------------------------------


def get_team_rating(team_id):
    """Return a team's stored Elo rating or default."""
    if team_id is None:
        return elo.DEFAULT_RATING

    team_id = _validate_positive_int(team_id, "team_id")
    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute("SELECT rating FROM elo_ratings WHERE team_id = %s", (team_id,))
                row = cur.fetchone()
                return row[0] if row else elo.DEFAULT_RATING
        else:
            row = conn.execute("SELECT rating FROM elo_ratings WHERE team_id = ?", (team_id,)).fetchone()
            return row[0] if row else elo.DEFAULT_RATING
    finally:
        conn.close()


def _update_elo_ratings_postgres(cur, home_id, home_name, away_id, away_name, home_goals, away_goals):
    cur.execute("SELECT rating FROM elo_ratings WHERE team_id = %s", (home_id,))
    home_row = cur.fetchone()
    home_rating = home_row[0] if home_row else elo.DEFAULT_RATING

    cur.execute("SELECT rating FROM elo_ratings WHERE team_id = %s", (away_id,))
    away_row = cur.fetchone()
    away_rating = away_row[0] if away_row else elo.DEFAULT_RATING

    new_home, new_away = elo.update_ratings(home_rating, away_rating, home_goals, away_goals)
    now = _utc_now()

    cur.execute(
        """
        INSERT INTO elo_ratings (team_id, team_name, rating, updated_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (team_id) DO UPDATE SET
            team_name = EXCLUDED.team_name,
            rating = EXCLUDED.rating,
            updated_at = EXCLUDED.updated_at
        """,
        (home_id, home_name, new_home, now),
    )
    cur.execute(
        """
        INSERT INTO elo_ratings (team_id, team_name, rating, updated_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (team_id) DO UPDATE SET
            team_name = EXCLUDED.team_name,
            rating = EXCLUDED.rating,
            updated_at = EXCLUDED.updated_at
        """,
        (away_id, away_name, new_away, now),
    )


def _update_elo_ratings_sqlite_conn(conn, home_id, home_name, away_id, away_name, home_goals, away_goals):
    row_home = conn.execute("SELECT rating FROM elo_ratings WHERE team_id = ?", (home_id,)).fetchone()
    home_rating = row_home[0] if row_home else elo.DEFAULT_RATING

    row_away = conn.execute("SELECT rating FROM elo_ratings WHERE team_id = ?", (away_id,)).fetchone()
    away_rating = row_away[0] if row_away else elo.DEFAULT_RATING

    new_home, new_away = elo.update_ratings(home_rating, away_rating, home_goals, away_goals)
    now = _utc_now()

    conn.execute(
        """
        INSERT INTO elo_ratings (team_id, team_name, rating, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(team_id) DO UPDATE SET
            team_name = excluded.team_name,
            rating = excluded.rating,
            updated_at = excluded.updated_at
        """,
        (home_id, home_name, new_home, now),
    )
    conn.execute(
        """
        INSERT INTO elo_ratings (team_id, team_name, rating, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(team_id) DO UPDATE SET
            team_name = excluded.team_name,
            rating = excluded.rating,
            updated_at = excluded.updated_at
        """,
        (away_id, away_name, new_away, now),
    )


def update_elo_ratings(home_id, home_name, away_id, away_name, home_goals, away_goals):
    """Public wrapper for an atomic Elo update."""
    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.transaction():
                with conn.cursor() as cur:
                    _update_elo_ratings_postgres(cur, home_id, home_name, away_id, away_name, home_goals, away_goals)
        else:
            _update_elo_ratings_sqlite_conn(conn, home_id, home_name, away_id, away_name, home_goals, away_goals)
            conn.commit()

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------


def accuracy_summary():
    """Return graded football prediction accuracy."""
    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT confidence_label, top_pick_correct
                    FROM predictions
                    WHERE top_pick_correct IS NOT NULL
                    """
                )
                rows = cur.fetchall()
        else:
            rows = conn.execute(
                """
                SELECT confidence_label, top_pick_correct
                FROM predictions
                WHERE top_pick_correct IS NOT NULL
                """
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"total_graded": 0, "overall_accuracy": 0.0, "by_confidence": {}}

    correct_total = sum(int(row[1]) for row in rows)
    summary = {
        "total_graded": len(rows),
        "overall_accuracy": correct_total / len(rows),
        "by_confidence": {},
    }

    for label in ("High", "Moderate", "Toss-up"):
        subset = [int(row[1]) for row in rows if row[0] == label]
        if subset:
            summary["by_confidence"][label] = {
                "count": len(subset),
                "accuracy": sum(subset) / len(subset),
            }

    return summary


def get_pending_fixtures():
    """Return football predictions that have not yet been graded."""
    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT fixture_id, match_date, home_team, away_team
                    FROM predictions
                    WHERE actual_home_goals IS NULL
                    ORDER BY match_date ASC, fixture_id ASC
                    """
                )
                return cur.fetchall()
        else:
            return conn.execute(
                """
                SELECT fixture_id, match_date, home_team, away_team
                FROM predictions
                WHERE actual_home_goals IS NULL
                ORDER BY match_date ASC, fixture_id ASC
                """
            ).fetchall()
    finally:
        conn.close()


def cleanup_non_target_leagues(keep_keywords, dry_run=False):
    """Remove football predictions whose league does not contain keep_keywords."""
    if not isinstance(keep_keywords, (list, tuple, set)):
        raise ValueError("keep_keywords must be a list, tuple, or set.")

    normalized_keywords = [
        kw.strip().lower() for kw in keep_keywords if isinstance(kw, str) and kw.strip()
    ]
    if not normalized_keywords:
        raise ValueError("At least one non-empty cleanup keyword is required.")

    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute("SELECT id, league FROM predictions")
                rows = cur.fetchall()
                to_delete = [
                    row_id for row_id, league in rows
                    if not any(kw in (league or "").lower() for kw in normalized_keywords)
                ]
                if to_delete and not dry_run:
                    cur.execute("DELETE FROM predictions WHERE id = ANY(%s)", (to_delete,))
            conn.commit()
            return len(to_delete), len(rows)

        else:
            rows = conn.execute("SELECT id, league FROM predictions").fetchall()
            to_delete = [
                row_id for row_id, league in rows
                if not any(kw in (league or "").lower() for kw in normalized_keywords)
            ]
            if to_delete and not dry_run:
                conn.executemany("DELETE FROM predictions WHERE id = ?", [(row_id,) for row_id in to_delete])
            conn.commit()
            return len(to_delete), len(rows)

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Basketball prediction storage
# ----------------------------------------------------------------------


def save_basketball_prediction(
    game_id,
    game_date,
    home_team,
    away_team,
    league,
    markets,
    confidence,
    prediction_context="PRE_MATCH",
):
    """Save a basketball prediction exactly once."""
    game_id = _validate_positive_int(game_id, "game_id")
    _validate_text(game_date, "game_date")
    _validate_text(home_team, "home_team")
    _validate_text(away_team, "away_team")
    _validate_text(league, "league")

    if not isinstance(markets, dict):
        raise ValueError("markets must be a dictionary.")
    if not isinstance(confidence, dict):
        raise ValueError("confidence must be a dictionary.")

    required_confidence = ("label", "top_pick", "top_probability")
    missing = [key for key in required_confidence if key not in confidence]
    if missing:
        raise ValueError("confidence is missing required fields: " + ", ".join(missing))

    _validate_text(confidence["label"], "confidence.label")
    _validate_text(confidence["top_pick"], "confidence.top_pick")
    top_probability = _validate_probability(confidence["top_probability"], "confidence.top_probability")

    prediction_context = _validate_text(prediction_context, "prediction_context").upper()
    if prediction_context not in ("PRE_MATCH", "LIVE"):
        prediction_context = "PRE_MATCH"

    markets_json_str = _json_dumps(markets, "markets")
    conn, db_type = _connect()

    try:
        now_str = _utc_now()
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO basketball_predictions (
                        game_id, game_date, home_team, away_team, league,
                        markets_json, confidence_label, top_pick, top_probability,
                        prediction_context, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (game_id) DO NOTHING
                    """,
                    (
                        game_id, game_date, home_team, away_team, league,
                        markets_json_str, confidence["label"], confidence["top_pick"], top_probability,
                        prediction_context, now_str,
                    ),
                )
                inserted = cur.rowcount == 1
            conn.commit()
        else:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO basketball_predictions (
                    game_id, game_date, home_team, away_team, league,
                    markets_json, confidence_label, top_pick, top_probability,
                    prediction_context, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    game_id, game_date, home_team, away_team, league,
                    markets_json_str, confidence["label"], confidence["top_pick"], top_probability,
                    prediction_context, now_str,
                ),
            )
            inserted = cursor.rowcount == 1
            conn.commit()

        return inserted

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_basketball_result(game_id, home_points, away_points):
    """Record a final basketball result."""
    game_id = _validate_positive_int(game_id, "game_id")
    home_points = _validate_non_negative_int(home_points, "home_points")
    away_points = _validate_non_negative_int(away_points, "away_points")

    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT top_pick, actual_home_points, actual_away_points
                    FROM basketball_predictions
                    WHERE game_id = %s
                    FOR UPDATE
                    """,
                    (game_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return False

                top_pick, recorded_home, recorded_away = row
                if recorded_home is not None or recorded_away is not None:
                    if recorded_home == home_points and recorded_away == away_points:
                        return True
                    raise ValueError(f"A different result is already recorded for game {game_id}.")

                actual = "home_win" if home_points > away_points else ("away_win" if away_points > home_points else "draw")
                correct = 1 if _normalise_result_pick(top_pick) == actual else 0

                cur.execute(
                    """
                    UPDATE basketball_predictions
                    SET actual_home_points = %s, actual_away_points = %s, top_pick_correct = %s
                    WHERE game_id = %s
                    """,
                    (home_points, away_points, correct, game_id),
                )
            conn.commit()
            return True

        else:
            row = conn.execute(
                """
                SELECT top_pick, actual_home_points, actual_away_points
                FROM basketball_predictions
                WHERE game_id = ?
                """,
                (game_id,),
            ).fetchone()

            if row is None:
                conn.rollback()
                return False

            top_pick, recorded_home, recorded_away = row
            if recorded_home is not None or recorded_away is not None:
                if recorded_home == home_points and recorded_away == away_points:
                    conn.rollback()
                    return True
                raise ValueError(f"A different result is already recorded for game {game_id}.")

            actual = "home_win" if home_points > away_points else ("away_win" if away_points > home_points else "draw")
            correct = 1 if _normalise_result_pick(top_pick) == actual else 0

            conn.execute(
                """
                UPDATE basketball_predictions
                SET actual_home_points = ?, actual_away_points = ?, top_pick_correct = ?
                WHERE game_id = ?
                """,
                (home_points, away_points, correct, game_id),
            )
            conn.commit()
            return True

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def basketball_accuracy_summary():
    """Return graded basketball prediction accuracy."""
    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT confidence_label, top_pick_correct
                    FROM basketball_predictions
                    WHERE top_pick_correct IS NOT NULL
                    """
                )
                rows = cur.fetchall()
        else:
            rows = conn.execute(
                """
                SELECT confidence_label, top_pick_correct
                FROM basketball_predictions
                WHERE top_pick_correct IS NOT NULL
                """
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"total_graded": 0, "overall_accuracy": 0.0, "by_confidence": {}}

    correct_total = sum(int(row[1]) for row in rows)
    summary = {
        "total_graded": len(rows),
        "overall_accuracy": correct_total / len(rows),
        "by_confidence": {},
    }

    for label in ("High", "Moderate", "Toss-up"):
        subset = [int(row[1]) for row in rows if row[0] == label]
        if subset:
            summary["by_confidence"][label] = {
                "count": len(subset),
                "accuracy": sum(subset) / len(subset),
            }

    return summary


def get_pending_basketball_games():
    """Return basketball predictions that have not yet been graded."""
    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT game_id, game_date, home_team, away_team
                    FROM basketball_predictions
                    WHERE actual_home_points IS NULL
                    ORDER BY game_date ASC, game_id ASC
                    """
                )
                return cur.fetchall()
        else:
            return conn.execute(
                """
                SELECT game_id, game_date, home_team, away_team
                FROM basketball_predictions
                WHERE actual_home_points IS NULL
                ORDER BY game_date ASC, game_id ASC
                """
            ).fetchall()
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Dashboard & Recent Predictions Helper Abstraction
# ----------------------------------------------------------------------


def get_recent_predictions(sport="football", limit=20):
    """
    Fetch recent predictions for dashboard rendering.

    Returns list of tuples: (home_team, away_team, league, top_pick, top_probability, confidence_label, date)
    """
    table = "predictions" if sport == "football" else "basketball_predictions"
    date_col = "match_date" if sport == "football" else "game_date"

    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT home_team, away_team, league, top_pick, top_probability,
                           confidence_label, {date_col}
                    FROM {table}
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                return cur.fetchall()
        else:
            return conn.execute(
                f"""
                SELECT home_team, away_team, league, top_pick, top_probability,
                       confidence_label, {date_col}
                FROM {table}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Telegram Bot Memory (Bounded at max 50 messages per chat)
# ----------------------------------------------------------------------


def save_telegram_message(chat_id, role, text, timestamp=None):
    """Save a Telegram message into structured database memory."""
    chat_id = str(chat_id)
    _validate_text(role, "role")
    _validate_text(text, "text")
    if timestamp is None:
        timestamp = _utc_now()

    conn, db_type = _connect()

    try:
        now_str = _utc_now()
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bot_memory (chat_id, role, text, timestamp, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (chat_id, role, text, timestamp, now_str),
                )
                # Enforce max 50 message bound per chat
                cur.execute(
                    """
                    DELETE FROM bot_memory
                    WHERE chat_id = %s AND id NOT IN (
                        SELECT id FROM bot_memory
                        WHERE chat_id = %s
                        ORDER BY id DESC
                        LIMIT 50
                    )
                    """,
                    (chat_id, chat_id),
                )
            conn.commit()
        else:
            conn.execute(
                """
                INSERT INTO bot_memory (chat_id, role, text, timestamp, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, role, text, timestamp, now_str),
            )
            conn.execute(
                """
                DELETE FROM bot_memory
                WHERE chat_id = ? AND id NOT IN (
                    SELECT id FROM bot_memory
                    WHERE chat_id = ?
                    ORDER BY id DESC
                    LIMIT 50
                )
                """,
                (chat_id, chat_id),
            )
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_recent_telegram_messages(chat_id, limit=50):
    """Fetch recent Telegram messages for a chat ID (chronological order)."""
    chat_id = str(chat_id)
    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT role, text, timestamp
                    FROM (
                        SELECT role, text, timestamp, id
                        FROM bot_memory
                        WHERE chat_id = %s
                        ORDER BY id DESC
                        LIMIT %s
                    ) sub
                    ORDER BY id ASC
                    """,
                    (chat_id, limit),
                )
                rows = cur.fetchall()
        else:
            rows = conn.execute(
                """
                SELECT role, text, timestamp
                FROM (
                    SELECT role, text, timestamp, id
                    FROM bot_memory
                    WHERE chat_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (chat_id, limit),
            ).fetchall()

        return [
            {"role": row[0], "text": row[1], "timestamp": row[2]}
            for row in rows
        ]
    except Exception:
        return []
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Persistent API Cache & Request Budget Storage
# ----------------------------------------------------------------------


def get_api_cache(cache_key):
    """Retrieve unexpired cached response from persistent API cache."""
    conn, db_type = _connect()
    now_str = _utc_now()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT response_payload, expires_at
                    FROM api_cache
                    WHERE cache_key = %s
                    """,
                    (cache_key,),
                )
                row = cur.fetchone()
        else:
            row = conn.execute(
                """
                SELECT response_payload, expires_at
                FROM api_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()

        if row is None:
            return None

        payload, expires_at = row
        if expires_at and expires_at < now_str:
            return None

        if isinstance(payload, (dict, list)):
            return payload

        return _json_loads(payload)

    except Exception:
        return None
    finally:
        conn.close()


def set_api_cache(cache_key, endpoint, request_params, response_payload, ttl_seconds):
    """Store response in persistent API cache."""
    conn, db_type = _connect()
    now_dt = datetime.now(timezone.utc)
    fetched_at = now_dt.isoformat()
    expires_dt = datetime.fromtimestamp(now_dt.timestamp() + ttl_seconds, tz=timezone.utc)
    expires_at = expires_dt.isoformat()

    params_json = _json_dumps(request_params, "request_params")
    payload_json = _json_dumps(response_payload, "response_payload")

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO api_cache (cache_key, endpoint, request_params, response_payload, fetched_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        endpoint = EXCLUDED.endpoint,
                        request_params = EXCLUDED.request_params,
                        response_payload = EXCLUDED.response_payload,
                        fetched_at = EXCLUDED.fetched_at,
                        expires_at = EXCLUDED.expires_at
                    """,
                    (cache_key, endpoint, params_json, payload_json, fetched_at, expires_at),
                )
            conn.commit()
        else:
            conn.execute(
                """
                INSERT INTO api_cache (cache_key, endpoint, request_params, response_payload, fetched_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (cache_key) DO UPDATE SET
                    endpoint = excluded.endpoint,
                    request_params = excluded.request_params,
                    response_payload = excluded.response_payload,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at
                """,
                (cache_key, endpoint, params_json, payload_json, fetched_at, expires_at),
            )
            conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def record_api_request(provider, endpoint, request_date=None):
    """Record an API request to track credit consumption."""
    if request_date is None:
        request_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    timestamp = _utc_now()
    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO api_request_counts (provider, request_timestamp, request_date, endpoint)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (provider, timestamp, request_date, endpoint),
                )
            conn.commit()
        else:
            conn.execute(
                """
                INSERT INTO api_request_counts (provider, request_timestamp, request_date, endpoint)
                VALUES (?, ?, ?, ?)
                """,
                (provider, timestamp, request_date, endpoint),
            )
            conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def get_api_request_count(provider, date_pattern):
    """
    Get request count for a provider over a date pattern.

    e.g., date_pattern="2026-09-30" (daily) or date_pattern="2026-09%" (monthly).
    """
    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                if "%" in date_pattern:
                    cur.execute(
                        "SELECT COUNT(*) FROM api_request_counts WHERE provider = %s AND request_date LIKE %s",
                        (provider, date_pattern),
                    )
                else:
                    cur.execute(
                        "SELECT COUNT(*) FROM api_request_counts WHERE provider = %s AND request_date = %s",
                        (provider, date_pattern),
                    )
                row = cur.fetchone()
                return row[0] if row else 0
        else:
            try:
                if "%" in date_pattern:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM api_request_counts WHERE provider = ? AND request_date LIKE ?",
                        (provider, date_pattern),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM api_request_counts WHERE provider = ? AND request_date = ?",
                        (provider, date_pattern),
                    ).fetchone()
                return row[0] if row else 0
            except sqlite3.OperationalError:
                # Table missing -> init db and retry
                conn.close()
                init_db()
                conn, _ = _connect()
                if "%" in date_pattern:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM api_request_counts WHERE provider = ? AND request_date LIKE ?",
                        (provider, date_pattern),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM api_request_counts WHERE provider = ? AND request_date = ?",
                        (provider, date_pattern),
                    ).fetchone()
                return row[0] if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


def reserve_api_request(provider, date_pattern, request_date, endpoint, limit):
    """
    Atomically reserve one API request credit if current usage is below limit.

    Returns True if reserved successfully, False if quota exhausted.
    Fails closed by raising RuntimeError on database connection/query failures.
    """
    timestamp = _utc_now()
    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            lock_key = f"{provider}:{date_pattern}"
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
                    if "%" in date_pattern:
                        cur.execute(
                            """
                            INSERT INTO api_request_counts (provider, request_timestamp, request_date, endpoint)
                            SELECT %s, %s, %s, %s
                            WHERE (
                                SELECT COUNT(*) FROM api_request_counts
                                WHERE provider = %s AND request_date LIKE %s
                            ) < %s
                            RETURNING id
                            """,
                            (provider, timestamp, request_date, endpoint, provider, date_pattern, limit),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO api_request_counts (provider, request_timestamp, request_date, endpoint)
                            SELECT %s, %s, %s, %s
                            WHERE (
                                SELECT COUNT(*) FROM api_request_counts
                                WHERE provider = %s AND request_date = %s
                            ) < %s
                            RETURNING id
                            """,
                            (provider, timestamp, request_date, endpoint, provider, date_pattern, limit),
                        )
                    row = cur.fetchone()
                    return row is not None

        else:
            try:
                with conn:
                    if "%" in date_pattern:
                        row = conn.execute(
                            "SELECT COUNT(*) FROM api_request_counts WHERE provider = ? AND request_date LIKE ?",
                            (provider, date_pattern),
                        ).fetchone()
                    else:
                        row = conn.execute(
                            "SELECT COUNT(*) FROM api_request_counts WHERE provider = ? AND request_date = ?",
                            (provider, date_pattern),
                        ).fetchone()

                    count = row[0] if row else 0
                    if count >= limit:
                        return False

                    conn.execute(
                        """
                        INSERT INTO api_request_counts (provider, request_timestamp, request_date, endpoint)
                        VALUES (?, ?, ?, ?)
                        """,
                        (provider, timestamp, request_date, endpoint),
                    )
                    return True
            except sqlite3.OperationalError:
                # Table missing -> initialize schema and retry
                conn.close()
                init_db()
                conn, _ = _connect()
                with conn:
                    if "%" in date_pattern:
                        row = conn.execute(
                            "SELECT COUNT(*) FROM api_request_counts WHERE provider = ? AND request_date LIKE ?",
                            (provider, date_pattern),
                        ).fetchone()
                    else:
                        row = conn.execute(
                            "SELECT COUNT(*) FROM api_request_counts WHERE provider = ? AND request_date = ?",
                            (provider, date_pattern),
                        ).fetchone()

                    count = row[0] if row else 0
                    if count >= limit:
                        return False

                    conn.execute(
                        """
                        INSERT INTO api_request_counts (provider, request_timestamp, request_date, endpoint)
                        VALUES (?, ?, ?, ?)
                        """,
                        (provider, timestamp, request_date, endpoint),
                    )
                    return True

    except Exception as exc:
        raise RuntimeError(f"Database quota reservation error; failing closed: {exc}") from exc
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Historical Fixture Storage Functions
# ----------------------------------------------------------------------


def _parse_strict_int(val):
    """
    Strictly parse an integer value.

    Rejects:
    - bool (True, False)
    - float or string float representations (1.9, "1.9", 1.0, "1.0", NaN, Infinity)
    - non-canonical integer strings, empty strings, dicts, lists, objects
    Accepts:
    - genuine int
    - canonical integer strings (e.g. "123", "-5", "0")
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        val_str = val.strip()
        if not val_str:
            return None
        if val_str.startswith("-") or val_str.startswith("+"):
            digits = val_str[1:]
        else:
            digits = val_str
        if digits.isdigit():
            try:
                return int(val_str)
            except ValueError:
                return None
    return None


def validate_historical_fixture(item, target_league_id=None, target_season=None):
    """
    Validate a raw historical fixture item against ingestion integrity rules.

    Checks:
    - Fixture ID: present, positive int, non-boolean, strict integer.
    - Kickoff Timestamp: present, valid ISO-8601 string, must be timezone-aware (rejects naive), normalized to UTC ISO-8601.
    - Team Structure: home & away team IDs present, positive strict int, home_id != away_id, names non-empty strings.
    - Dataset Identity: if payload contains league.id or league.season, matches target_league_id / target_season.
      (Note: Missing league metadata in payload is accepted as non-conflicting, as top-level params identify target).
    - Results: non-negative strict integer goals (no floats/booleans/negatives/missing goals for completed FT/AET/PEN fixtures).

    Returns:
        (is_valid: bool, reason: str, normalized_item: dict or None)
    """
    if not isinstance(item, dict):
        return False, "fixture_item_not_dict", None

    fixture_obj = item.get("fixture")
    if not isinstance(fixture_obj, dict):
        return False, "missing_or_invalid_fixture_object", None

    # A. FIXTURE ID
    fid = _parse_strict_int(fixture_obj.get("id"))
    if fid is None or fid <= 0:
        return False, "missing_invalid_or_non_positive_fixture_id", None

    # B. KICKOFF TIMESTAMP
    date_str = fixture_obj.get("date")
    if not date_str or not isinstance(date_str, str):
        return False, "missing_kickoff_timestamp", None

    try:
        iso_str = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            return False, "timezone_naive_kickoff_timestamp", None
        dt_utc = dt.astimezone(timezone.utc)
        norm_kickoff = dt_utc.isoformat()
    except (ValueError, TypeError):
        return False, "malformed_kickoff_timestamp", None

    # C. TEAM STRUCTURE
    teams_obj = item.get("teams")
    if not isinstance(teams_obj, dict):
        return False, "missing_or_invalid_teams_object", None

    home_obj = teams_obj.get("home")
    away_obj = teams_obj.get("away")
    if not isinstance(home_obj, dict) or not isinstance(away_obj, dict):
        return False, "missing_home_or_away_team_object", None

    h_id = _parse_strict_int(home_obj.get("id"))
    a_id = _parse_strict_int(away_obj.get("id"))

    if h_id is None or h_id <= 0:
        return False, "missing_invalid_or_non_positive_home_team_id", None
    if a_id is None or a_id <= 0:
        return False, "missing_invalid_or_non_positive_away_team_id", None

    if h_id == a_id:
        return False, "identical_home_and_away_team_ids", None

    h_name = home_obj.get("name")
    away_name = away_obj.get("name")
    if not isinstance(h_name, str) or not h_name.strip():
        return False, "missing_or_empty_home_team_name", None
    if not isinstance(away_name, str) or not away_name.strip():
        return False, "missing_or_empty_away_team_name", None

    # D. DATASET IDENTITY
    league_obj = item.get("league")
    if isinstance(league_obj, dict):
        raw_p_league_id = league_obj.get("id")
        if raw_p_league_id is not None:
            p_league_id = _parse_strict_int(raw_p_league_id)
            if p_league_id is None:
                return False, "invalid_payload_league_id", None

            if target_league_id is not None and p_league_id != target_league_id:
                return False, f"conflicting_league_id ({p_league_id} != {target_league_id})", None

        raw_p_season = league_obj.get("season")
        if raw_p_season is not None:
            p_season = _parse_strict_int(raw_p_season)
            if p_season is None:
                return False, "invalid_payload_season", None

            if target_season is not None and p_season != target_season:
                return False, f"conflicting_season ({p_season} != {target_season})", None

    # E. VALIDATE RESULTS
    status_short = fixture_obj.get("status", {}).get("short") if isinstance(fixture_obj.get("status"), dict) else None
    goals_obj = item.get("goals")

    h_goals = None
    a_goals = None
    if isinstance(goals_obj, dict):
        raw_h_goals = goals_obj.get("home")
        raw_a_goals = goals_obj.get("away")

        if raw_h_goals is not None:
            h_goals = _parse_strict_int(raw_h_goals)
            if h_goals is None or h_goals < 0:
                return False, "invalid_or_negative_home_goals", None

        if raw_a_goals is not None:
            a_goals = _parse_strict_int(raw_a_goals)
            if a_goals is None or a_goals < 0:
                return False, "invalid_or_negative_away_goals", None

    is_completed = status_short in ("FT", "AET", "PEN")
    if is_completed:
        if h_goals is None or a_goals is None:
            return False, f"completed_fixture_missing_goals (status={status_short})", None

    # Build normalized fixture copy
    normalized = dict(item)

    norm_fixture = dict(fixture_obj)
    norm_fixture["id"] = fid
    norm_fixture["date"] = norm_kickoff
    normalized["fixture"] = norm_fixture

    norm_teams = dict(teams_obj)
    norm_home = dict(home_obj)
    norm_home["id"] = h_id
    norm_home["name"] = h_name.strip()
    norm_away = dict(away_obj)
    norm_away["id"] = a_id
    norm_away["name"] = away_name.strip()
    norm_teams["home"] = norm_home
    norm_teams["away"] = norm_away
    normalized["teams"] = norm_teams

    if isinstance(goals_obj, dict):
        norm_goals = dict(goals_obj)
        norm_goals["home"] = h_goals
        norm_goals["away"] = a_goals
        normalized["goals"] = norm_goals

    return True, "valid", normalized


def save_historical_fixtures(fixtures, league_id, season, source="api_football"):
    """
    Save historical API-Football fixtures into persistent storage.

    Idempotent: skips fixtures that already exist in the database (ON CONFLICT DO NOTHING).
    Returns dict: {
        "total": total_input,
        "valid": valid_count,
        "inserted": inserted_count,
        "duplicates_skipped": dup_count,
        "rejected_count": rejected_count,
        "rejection_reasons": rejection_reasons_dict,
    }
    """
    league_id = _validate_positive_int(league_id, "league_id")
    season = _validate_positive_int(season, "season")

    if not isinstance(fixtures, (list, tuple)):
        raise ValueError("fixtures must be a list or tuple.")

    valid_fixtures = []
    seen_ids = set()
    dup_count = 0
    rejected_count = 0
    rejection_reasons = {}

    for item in fixtures:
        is_valid, reason, norm_item = validate_historical_fixture(
            item, target_league_id=league_id, target_season=season
        )
        if not is_valid:
            rejected_count += 1
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
            continue

        fid = norm_item["fixture"]["id"]
        if fid in seen_ids:
            dup_count += 1
            continue

        seen_ids.add(fid)
        valid_fixtures.append((fid, norm_item))

    if not valid_fixtures:
        return {
            "total": len(fixtures),
            "valid": 0,
            "inserted": 0,
            "duplicates_skipped": dup_count,
            "rejected_count": rejected_count,
            "rejection_reasons": rejection_reasons,
        }

    conn, db_type = _connect()
    now_str = _utc_now()
    inserted_count = 0

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                for fid, item in valid_fixtures:
                    kickoff = str(item.get("fixture", {}).get("date", ""))
                    status = item.get("fixture", {}).get("status", {}).get("short", "")
                    h_id = item.get("teams", {}).get("home", {}).get("id")
                    a_id = item.get("teams", {}).get("away", {}).get("id")
                    h_name = item.get("teams", {}).get("home", {}).get("name", "")
                    a_name = item.get("teams", {}).get("away", {}).get("name", "")
                    h_goals = item.get("goals", {}).get("home")
                    a_goals = item.get("goals", {}).get("away")

                    raw_json_str = _json_dumps(item, "raw_json")

                    cur.execute(
                        """
                        INSERT INTO historical_fixtures (
                            fixture_id, league_id, season, kickoff_at, status_short,
                            home_team_id, away_team_id, home_team, away_team,
                            home_goals, away_goals, raw_json, source, fetched_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (fixture_id) DO NOTHING
                        """,
                        (
                            fid, league_id, season, kickoff, status,
                            h_id, a_id, h_name, a_name,
                            h_goals, a_goals, raw_json_str, source, now_str,
                        ),
                    )
                    if cur.rowcount == 1:
                        inserted_count += 1
            conn.commit()

        else:
            for fid, item in valid_fixtures:
                kickoff = str(item.get("fixture", {}).get("date", ""))
                status = item.get("fixture", {}).get("status", {}).get("short", "")
                h_id = item.get("teams", {}).get("home", {}).get("id")
                a_id = item.get("teams", {}).get("away", {}).get("id")
                h_name = item.get("teams", {}).get("home", {}).get("name", "")
                a_name = item.get("teams", {}).get("away", {}).get("name", "")
                h_goals = item.get("goals", {}).get("home")
                a_goals = item.get("goals", {}).get("away")

                raw_json_str = _json_dumps(item, "raw_json")

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO historical_fixtures (
                        fixture_id, league_id, season, kickoff_at, status_short,
                        home_team_id, away_team_id, home_team, away_team,
                        home_goals, away_goals, raw_json, source, fetched_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fid, league_id, season, kickoff, status,
                        h_id, a_id, h_name, a_name,
                        h_goals, a_goals, raw_json_str, source, now_str,
                    ),
                )
                if cursor.rowcount == 1:
                    inserted_count += 1
            conn.commit()

        return {
            "total": len(fixtures),
            "valid": len(valid_fixtures),
            "inserted": inserted_count,
            "duplicates_skipped": dup_count,
            "rejected_count": rejected_count,
            "rejection_reasons": rejection_reasons,
        }

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_historical_fixtures(league_id, season):
    """
    Retrieve stored historical fixtures for a league and season.

    Deterministically ordered by kickoff_at ASC, fixture_id ASC.
    Reconstructs original API fixture structure from raw_json.
    """
    league_id = _validate_positive_int(league_id, "league_id")
    season = _validate_positive_int(season, "season")

    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT raw_json
                    FROM historical_fixtures
                    WHERE league_id = %s AND season = %s
                    ORDER BY kickoff_at ASC, fixture_id ASC
                    """,
                    (league_id, season),
                )
                rows = cur.fetchall()
        else:
            try:
                rows = conn.execute(
                    """
                    SELECT raw_json
                    FROM historical_fixtures
                    WHERE league_id = ? AND season = ?
                    ORDER BY kickoff_at ASC, fixture_id ASC
                    """,
                    (league_id, season),
                ).fetchall()
            except sqlite3.OperationalError:
                conn.close()
                init_db()
                conn, _ = _connect()
                rows = conn.execute(
                    """
                    SELECT raw_json
                    FROM historical_fixtures
                    WHERE league_id = ? AND season = ?
                    ORDER BY kickoff_at ASC, fixture_id ASC
                    """,
                    (league_id, season),
                ).fetchall()

        fixtures = []
        for row in rows:
            payload = _json_loads(row[0])
            if isinstance(payload, dict):
                fixtures.append(payload)

        return fixtures

    finally:
        conn.close()


def get_historical_fixture_count(league_id, season):
    """Get the count of stored historical fixtures for a league and season."""
    league_id = _validate_positive_int(league_id, "league_id")
    season = _validate_positive_int(season, "season")

    conn, db_type = _connect()

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM historical_fixtures WHERE league_id = %s AND season = %s",
                    (league_id, season),
                )
                row = cur.fetchone()
                return row[0] if row else 0
        else:
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM historical_fixtures WHERE league_id = ? AND season = ?",
                    (league_id, season),
                ).fetchone()
                return row[0] if row else 0
            except sqlite3.OperationalError:
                conn.close()
                init_db()
                conn, _ = _connect()
                row = conn.execute(
                    "SELECT COUNT(*) FROM historical_fixtures WHERE league_id = ? AND season = ?",
                    (league_id, season),
                ).fetchone()
                return row[0] if row else 0
    finally:
        conn.close()


def save_historical_enrichment(enriched_fixtures, source="api_football"):
    """
    Save historical fixture enrichment into persistent storage.

    Accepts dict (fixture_id -> fixture) or list of enriched fixtures.
    Idempotent: ON CONFLICT DO NOTHING.
    Returns count of newly inserted enrichment records.
    """
    if isinstance(enriched_fixtures, dict):
        items = list(enriched_fixtures.values())
    elif isinstance(enriched_fixtures, (list, tuple)):
        items = list(enriched_fixtures)
    else:
        raise ValueError("enriched_fixtures must be a dict or list.")

    valid_items = []
    seen_ids = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        fid = item.get("fixture", {}).get("id")
        if fid is None:
            continue

        try:
            fid = int(fid)
        except (TypeError, ValueError):
            continue

        if fid in seen_ids:
            continue

        seen_ids.add(fid)
        valid_items.append((fid, item))

    if not valid_items:
        return 0

    conn, db_type = _connect()
    now_str = _utc_now()
    inserted_count = 0

    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                for fid, item in valid_items:
                    raw_json_str = _json_dumps(item, "raw_json")
                    cur.execute(
                        """
                        INSERT INTO historical_fixture_enrichment (fixture_id, raw_json, fetched_at, source)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (fixture_id) DO NOTHING
                        """,
                        (fid, raw_json_str, now_str, source),
                    )
                    if cur.rowcount == 1:
                        inserted_count += 1
            conn.commit()
        else:
            for fid, item in valid_items:
                raw_json_str = _json_dumps(item, "raw_json")
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO historical_fixture_enrichment (fixture_id, raw_json, fetched_at, source)
                    VALUES (?, ?, ?, ?)
                    """,
                    (fid, raw_json_str, now_str, source),
                )
                if cursor.rowcount == 1:
                    inserted_count += 1
            conn.commit()

        return inserted_count

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_historical_enrichment(fixture_ids):
    """
    Retrieve stored historical fixture enrichment records for given fixture IDs.

    Returns dict mapping fixture_id (int) -> enriched fixture dict.
    """
    if not isinstance(fixture_ids, (list, tuple, set)):
        raise ValueError("fixture_ids must be a list, tuple, or set.")

    clean_ids = []
    for fid in fixture_ids:
        if fid in (None, ""):
            continue
        try:
            num = int(fid)
            if num > 0:
                clean_ids.append(num)
        except (TypeError, ValueError):
            continue

    clean_ids = list(set(clean_ids))
    if not clean_ids:
        return {}

    conn, db_type = _connect()

    try:
        enriched_map = {}
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT fixture_id, raw_json
                    FROM historical_fixture_enrichment
                    WHERE fixture_id = ANY(%s)
                    """,
                    (clean_ids,),
                )
                rows = cur.fetchall()
        else:
            try:
                placeholders = ",".join(["?"] * len(clean_ids))
                rows = conn.execute(
                    f"""
                    SELECT fixture_id, raw_json
                    FROM historical_fixture_enrichment
                    WHERE fixture_id IN ({placeholders})
                    """,
                    clean_ids,
                ).fetchall()
            except sqlite3.OperationalError:
                conn.close()
                init_db()
                conn, _ = _connect()
                placeholders = ",".join(["?"] * len(clean_ids))
                rows = conn.execute(
                    f"""
                    SELECT fixture_id, raw_json
                    FROM historical_fixture_enrichment
                    WHERE fixture_id IN ({placeholders})
                    """,
                    clean_ids,
                ).fetchall()

        for fid, raw in rows:
            payload = _json_loads(raw)
            if isinstance(payload, dict):
                enriched_map[int(fid)] = payload

        return enriched_map

    finally:
        conn.close()
