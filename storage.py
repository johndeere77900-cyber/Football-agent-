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
- Persistent API response caching and request-budget tracking.
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
            conn.commit()

    except Exception:
        if db_type == "postgres":
            conn.rollback()
        else:
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
