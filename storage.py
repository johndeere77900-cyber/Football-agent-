"""
Persistent storage for predictions, results, Elo ratings, and bot memory.

Supports PostgreSQL (Neon) as canonical production storage, with SQLite fallback
for offline testing when NEON_DATABASE_URL is not set.

Integrity rules:
- A prediction is immutable after its first successful save.
- Re-running prediction generation for an existing fixture/game does not
  rewrite the original prediction.
- Results are idempotent.
- A conflicting second result is rejected.
- Football result grading and Elo updates occur in one atomic transaction.
- Schema initialization is safe and idempotent.
- Numeric IDs and result values are validated before writes.
- JSON payloads are validated before storage.
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
    from psycopg.types.json import Jsonb
    HAS_PSYCOPG = True
except ImportError:
    HAS_PSYCOPG = False


# ----------------------------------------------------------------------
# Database Connection & Engine Helpers
# ----------------------------------------------------------------------


def get_database_url():
    """Return configured NEON_DATABASE_URL if available."""
    return os.environ.get(
        "NEON_DATABASE_URL",
        getattr(config, "NEON_DATABASE_URL", None),
    )


def is_postgres():
    """Return True if PostgreSQL (Neon) is configured and psycopg is installed."""
    url = get_database_url()
    return bool(url and HAS_PSYCOPG)


def _connect():
    """
    Open database connection.
    Returns (conn, is_pg_bool).
    """
    url = get_database_url()
    if url and HAS_PSYCOPG:
        conn = psycopg.connect(url, autocommit=False)
        return conn, True

    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn, False


def _q(sql, is_pg):
    """Adapt parameter placeholders: SQLite uses ?, Postgres uses %s."""
    if is_pg:
        return sql.replace("?", "%s")
    return sql


def _utc_now():
    """Return a timezone-aware UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


def _validate_positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _validate_non_negative_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return value


def _validate_optional_positive_int(value, name):
    if value is None:
        return None
    return _validate_positive_int(value, name)


def _validate_probability(value, name="probability"):
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
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string.")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} cannot be empty.")
    return value


def _json_dumps(value, name):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain JSON-serializable data.") from exc


def _ensure_column_sqlite(conn, table, column, coltype):
    existing = {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def _ensure_column_pg(conn, table, column, coltype):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
            """,
            (table, column),
        )
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def _normalise_result_pick(value):
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
    return aliases.get(normalized, normalized.replace("-", "_").replace(" ", "_"))


# ----------------------------------------------------------------------
# Initialization DDL
# ----------------------------------------------------------------------


def init_db():
    """Initialize storage tables (PostgreSQL or SQLite)."""
    conn, is_pg = _connect()
    try:
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS predictions (
                        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        fixture_id BIGINT UNIQUE NOT NULL,
                        match_date TEXT NOT NULL,
                        home_team TEXT NOT NULL,
                        away_team TEXT NOT NULL,
                        league TEXT NOT NULL,
                        markets_json JSONB NOT NULL,
                        confidence_label TEXT NOT NULL,
                        top_pick TEXT NOT NULL,
                        top_probability DOUBLE PRECISION NOT NULL,
                        odds_comparison_json JSONB,
                        actual_home_goals INTEGER,
                        actual_away_goals INTEGER,
                        top_pick_correct INTEGER,
                        created_at TEXT NOT NULL,
                        home_team_id BIGINT,
                        away_team_id BIGINT,
                        prediction_context TEXT NOT NULL DEFAULT 'PRE_MATCH'
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS elo_ratings (
                        team_id BIGINT PRIMARY KEY,
                        team_name TEXT NOT NULL,
                        rating DOUBLE PRECISION NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS basketball_predictions (
                        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        game_id BIGINT UNIQUE NOT NULL,
                        game_date TEXT NOT NULL,
                        home_team TEXT NOT NULL,
                        away_team TEXT NOT NULL,
                        league TEXT NOT NULL,
                        markets_json JSONB NOT NULL,
                        confidence_label TEXT NOT NULL,
                        top_pick TEXT NOT NULL,
                        top_probability DOUBLE PRECISION NOT NULL,
                        actual_home_points INTEGER,
                        actual_away_points INTEGER,
                        top_pick_correct INTEGER,
                        created_at TEXT NOT NULL
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bot_memory (
                        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        chat_id BIGINT NOT NULL,
                        role TEXT NOT NULL,
                        text TEXT NOT NULL,
                        timestamp TEXT NOT NULL
                    );
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_predictions_fixture ON predictions(fixture_id);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_bot_memory_chat ON bot_memory(chat_id);")
            _ensure_column_pg(conn, "predictions", "prediction_context", "TEXT NOT NULL DEFAULT 'PRE_MATCH'")
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
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
                """
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_basketball_db():
    init_db()


# ----------------------------------------------------------------------
# Football Prediction Storage
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
    missing = [k for k in required_confidence if k not in confidence]
    if missing:
        raise ValueError("confidence is missing required fields: " + ", ".join(missing))

    _validate_text(confidence["label"], "confidence.label")
    _validate_text(confidence["top_pick"], "confidence.top_pick")
    top_probability = _validate_probability(confidence["top_probability"], "confidence.top_probability")

    home_team_id = _validate_optional_positive_int(home_team_id, "home_team_id")
    away_team_id = _validate_optional_positive_int(away_team_id, "away_team_id")
    context_str = "LIVE" if str(prediction_context).upper() == "LIVE" else "PRE_MATCH"

    markets_str = _json_dumps(markets, "markets")
    odds_str = _json_dumps(odds_comparison, "odds_comparison") if odds_comparison is not None else None

    conn, is_pg = _connect()
    try:
        created_at = _utc_now()
        if is_pg:
            markets_val = Jsonb(markets)
            odds_val = Jsonb(odds_comparison) if odds_comparison is not None else None
            sql = """
                INSERT INTO predictions (
                    fixture_id, match_date, home_team, away_team, league,
                    markets_json, confidence_label, top_pick, top_probability,
                    odds_comparison_json, home_team_id, away_team_id, created_at, prediction_context
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fixture_id) DO NOTHING
            """
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        fixture_id, match_date, home_team, away_team, league,
                        markets_val, confidence["label"], confidence["top_pick"], top_probability,
                        odds_val, home_team_id, away_team_id, created_at, context_str,
                    ),
                )
                inserted = cur.rowcount == 1
        else:
            sql = """
                INSERT OR IGNORE INTO predictions (
                    fixture_id, match_date, home_team, away_team, league,
                    markets_json, confidence_label, top_pick, top_probability,
                    odds_comparison_json, home_team_id, away_team_id, created_at, prediction_context
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            cursor = conn.execute(
                sql,
                (
                    fixture_id, match_date, home_team, away_team, league,
                    markets_str, confidence["label"], confidence["top_pick"], top_probability,
                    odds_str, home_team_id, away_team_id, created_at, context_str,
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
# Football Result Grading (Atomic with Elo Updates)
# ----------------------------------------------------------------------


def record_result(fixture_id, home_goals, away_goals):
    """
    Record final football result and update Elo ratings in ONE atomic transaction.
    """
    fixture_id = _validate_positive_int(fixture_id, "fixture_id")
    home_goals = _validate_non_negative_int(home_goals, "home_goals")
    away_goals = _validate_non_negative_int(away_goals, "away_goals")

    conn, is_pg = _connect()
    try:
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT top_pick, home_team_id, away_team_id, home_team, away_team, actual_home_goals, actual_away_goals
                    FROM predictions
                    WHERE fixture_id = %s
                    """,
                    (fixture_id,),
                )
                row = cur.fetchone()
        else:
            row = conn.execute(
                """
                SELECT top_pick, home_team_id, away_team_id, home_team, away_team, actual_home_goals, actual_away_goals
                FROM predictions
                WHERE fixture_id = ?
                """,
                (fixture_id,),
            ).fetchone()

        if row is None:
            conn.rollback()
            return False

        (top_pick, home_id, away_id, home_name, away_name, rec_home, rec_away) = row

        if rec_home is not None or rec_away is not None:
            if rec_home == home_goals and rec_away == away_goals:
                conn.rollback()
                return True
            raise ValueError(f"A different result is already recorded for fixture {fixture_id}.")

        if home_goals > away_goals:
            actual = "home_win"
        elif home_goals < away_goals:
            actual = "away_win"
        else:
            actual = "draw"

        correct = 1 if _normalise_result_pick(top_pick) == actual else 0

        sql_update = _q(
            """
            UPDATE predictions
            SET actual_home_goals = ?, actual_away_goals = ?, top_pick_correct = ?
            WHERE fixture_id = ?
            """,
            is_pg,
        )

        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql_update, (home_goals, away_goals, correct, fixture_id))
        else:
            conn.execute(sql_update, (home_goals, away_goals, correct, fixture_id))

        if home_id is not None and away_id is not None:
            _update_elo_ratings_conn(conn, is_pg, home_id, home_name, away_id, away_name, home_goals, away_goals)

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
    if team_id is None:
        return elo.DEFAULT_RATING
    team_id = _validate_positive_int(team_id, "team_id")

    conn, is_pg = _connect()
    try:
        sql = _q("SELECT rating FROM elo_ratings WHERE team_id = ?", is_pg)
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql, (team_id,))
                row = cur.fetchone()
        else:
            row = conn.execute(sql, (team_id,)).fetchone()

        if row is None:
            return elo.DEFAULT_RATING
        return float(row[0])
    finally:
        conn.close()


def _update_elo_ratings_conn(conn, is_pg, home_id, home_name, away_id, away_name, home_goals, away_goals):
    home_id = _validate_positive_int(home_id, "home_id")
    away_id = _validate_positive_int(away_id, "away_id")
    home_goals = _validate_non_negative_int(home_goals, "home_goals")
    away_goals = _validate_non_negative_int(away_goals, "away_goals")
    home_name = _validate_text(home_name, "home_name")
    away_name = _validate_text(away_name, "away_name")

    def get_rating(t_id):
        sql = _q("SELECT rating FROM elo_ratings WHERE team_id = ?", is_pg)
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql, (t_id,))
                r = cur.fetchone()
        else:
            r = conn.execute(sql, (t_id,)).fetchone()
        return float(r[0]) if r else elo.DEFAULT_RATING

    home_rating = get_rating(home_id)
    away_rating = get_rating(away_id)
    new_home, new_away = elo.update_ratings(home_rating, away_rating, home_goals, away_goals)
    now = _utc_now()

    if is_pg:
        sql_elo = """
            INSERT INTO elo_ratings (team_id, team_name, rating, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (team_id) DO UPDATE SET
                team_name = EXCLUDED.team_name,
                rating = EXCLUDED.rating,
                updated_at = EXCLUDED.updated_at
        """
        with conn.cursor() as cur:
            cur.execute(sql_elo, (home_id, home_name, new_home, now))
            cur.execute(sql_elo, (away_id, away_name, new_away, now))
    else:
        sql_elo = """
            INSERT INTO elo_ratings (team_id, team_name, rating, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(team_id) DO UPDATE SET
                team_name = excluded.team_name,
                rating = excluded.rating,
                updated_at = excluded.updated_at
        """
        conn.execute(sql_elo, (home_id, home_name, new_home, now))
        conn.execute(sql_elo, (away_id, away_name, new_away, now))


def update_elo_ratings(home_id, home_name, away_id, away_name, home_goals, away_goals):
    conn, is_pg = _connect()
    try:
        _update_elo_ratings_conn(conn, is_pg, home_id, home_name, away_id, away_name, home_goals, away_goals)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Football Reporting
# ----------------------------------------------------------------------


def accuracy_summary():
    conn, is_pg = _connect()
    try:
        sql = _q("SELECT confidence_label, top_pick_correct FROM predictions WHERE top_pick_correct IS NOT NULL", is_pg)
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        else:
            rows = conn.execute(sql).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"total_graded": 0, "overall_accuracy": 0.0, "by_confidence": {}}

    correct_total = sum(int(r[1]) for r in rows)
    summary = {
        "total_graded": len(rows),
        "overall_accuracy": correct_total / len(rows),
        "by_confidence": {},
    }

    for label in ("High", "Moderate", "Toss-up"):
        subset = [int(r[1]) for r in rows if r[0] == label]
        if subset:
            summary["by_confidence"][label] = {
                "count": len(subset),
                "accuracy": sum(subset) / len(subset),
            }

    return summary


def get_pending_fixtures():
    conn, is_pg = _connect()
    try:
        sql = _q(
            """
            SELECT fixture_id, match_date, home_team, away_team
            FROM predictions
            WHERE actual_home_goals IS NULL
            ORDER BY match_date ASC, fixture_id ASC
            """,
            is_pg,
        )
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def get_recent_football_predictions(limit=20):
    conn, is_pg = _connect()
    try:
        sql = _q(
            """
            SELECT home_team, away_team, league, top_pick, top_probability,
                   confidence_label, match_date
            FROM predictions
            ORDER BY created_at DESC
            LIMIT ?
            """,
            is_pg,
        )
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql, (limit,))
                return cur.fetchall()
        return conn.execute(sql, (limit,)).fetchall()
    finally:
        conn.close()


def get_recent_basketball_predictions(limit=20):
    conn, is_pg = _connect()
    try:
        sql = _q(
            """
            SELECT home_team, away_team, league, top_pick, top_probability,
                   confidence_label, game_date
            FROM basketball_predictions
            ORDER BY created_at DESC
            LIMIT ?
            """,
            is_pg,
        )
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql, (limit,))
                return cur.fetchall()
        return conn.execute(sql, (limit,)).fetchall()
    except Exception:
        return []
    finally:
        conn.close()


def get_graded_football_rows():
    conn, is_pg = _connect()
    try:
        sql = _q(
            "SELECT confidence_label, top_pick_correct FROM predictions WHERE top_pick_correct IS NOT NULL",
            is_pg,
        )
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def get_graded_basketball_rows():
    conn, is_pg = _connect()
    try:
        sql = _q(
            "SELECT confidence_label, top_pick_correct FROM basketball_predictions WHERE top_pick_correct IS NOT NULL",
            is_pg,
        )
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()
        return conn.execute(sql).fetchall()
    except Exception:
        return []
    finally:
        conn.close()


def cleanup_non_target_leagues(keep_keywords, dry_run=False):
    if not isinstance(keep_keywords, (list, tuple, set)):
        raise ValueError("keep_keywords must be a list, tuple, or set.")

    normalized_keywords = [k.strip().lower() for k in keep_keywords if isinstance(k, str) and k.strip()]
    if not normalized_keywords:
        raise ValueError("At least one non-empty cleanup keyword is required.")

    conn, is_pg = _connect()
    try:
        sql_select = _q("SELECT id, league FROM predictions", is_pg)
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql_select)
                rows = cur.fetchall()
        else:
            rows = conn.execute(sql_select).fetchall()

        to_delete = []
        for row_id, league in rows:
            league_lower = (league or "").lower()
            if not any(k in league_lower for k in normalized_keywords):
                to_delete.append(row_id)

        if to_delete and not dry_run:
            sql_del = _q("DELETE FROM predictions WHERE id = ?", is_pg)
            if is_pg:
                with conn.cursor() as cur:
                    for r_id in to_delete:
                        cur.execute(sql_del, (r_id,))
            else:
                conn.executemany(sql_del, [(r_id,) for r_id in to_delete])

        conn.commit()
        return len(to_delete), len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Basketball Prediction Storage
# ----------------------------------------------------------------------


def save_basketball_prediction(game_id, game_date, home_team, away_team, league, markets, confidence):
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
    missing = [k for k in required_confidence if k not in confidence]
    if missing:
        raise ValueError("confidence is missing required fields: " + ", ".join(missing))

    _validate_text(confidence["label"], "confidence.label")
    _validate_text(confidence["top_pick"], "confidence.top_pick")
    top_probability = _validate_probability(confidence["top_probability"], "confidence.top_probability")

    markets_str = _json_dumps(markets, "markets")
    conn, is_pg = _connect()
    try:
        created_at = _utc_now()
        if is_pg:
            sql = """
                INSERT INTO basketball_predictions (
                    game_id, game_date, home_team, away_team, league,
                    markets_json, confidence_label, top_pick, top_probability, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (game_id) DO NOTHING
            """
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        game_id, game_date, home_team, away_team, league,
                        Jsonb(markets), confidence["label"], confidence["top_pick"], top_probability, created_at,
                    ),
                )
                inserted = cur.rowcount == 1
        else:
            sql = """
                INSERT OR IGNORE INTO basketball_predictions (
                    game_id, game_date, home_team, away_team, league,
                    markets_json, confidence_label, top_pick, top_probability, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            cursor = conn.execute(
                sql,
                (
                    game_id, game_date, home_team, away_team, league,
                    markets_str, confidence["label"], confidence["top_pick"], top_probability, created_at,
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
    game_id = _validate_positive_int(game_id, "game_id")
    home_points = _validate_non_negative_int(home_points, "home_points")
    away_points = _validate_non_negative_int(away_points, "away_points")

    conn, is_pg = _connect()
    try:
        sql_sel = _q(
            "SELECT top_pick, actual_home_points, actual_away_points FROM basketball_predictions WHERE game_id = ?",
            is_pg,
        )
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql_sel, (game_id,))
                row = cur.fetchone()
        else:
            row = conn.execute(sql_sel, (game_id,)).fetchone()

        if row is None:
            conn.rollback()
            return False

        (top_pick, rec_home, rec_away) = row

        if rec_home is not None or rec_away is not None:
            if rec_home == home_points and rec_away == away_points:
                conn.rollback()
                return True
            raise ValueError(f"A different result is already recorded for game {game_id}.")

        if home_points > away_points:
            actual = "home_win"
        elif home_points < away_points:
            actual = "away_win"
        else:
            actual = "draw"

        correct = 1 if _normalise_result_pick(top_pick) == actual else 0

        sql_upd = _q(
            """
            UPDATE basketball_predictions
            SET actual_home_points = ?, actual_away_points = ?, top_pick_correct = ?
            WHERE game_id = ?
            """,
            is_pg,
        )
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql_upd, (home_points, away_points, correct, game_id))
        else:
            conn.execute(sql_upd, (home_points, away_points, correct, game_id))

        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def basketball_accuracy_summary():
    conn, is_pg = _connect()
    try:
        sql = _q("SELECT confidence_label, top_pick_correct FROM basketball_predictions WHERE top_pick_correct IS NOT NULL", is_pg)
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        else:
            rows = conn.execute(sql).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"total_graded": 0, "overall_accuracy": 0.0, "by_confidence": {}}

    correct_total = sum(int(r[1]) for r in rows)
    summary = {
        "total_graded": len(rows),
        "overall_accuracy": correct_total / len(rows),
        "by_confidence": {},
    }

    for label in ("High", "Moderate", "Toss-up"):
        subset = [int(r[1]) for r in rows if r[0] == label]
        if subset:
            summary["by_confidence"][label] = {
                "count": len(subset),
                "accuracy": sum(subset) / len(subset),
            }

    return summary


def get_pending_basketball_games():
    conn, is_pg = _connect()
    try:
        sql = _q(
            """
            SELECT game_id, game_date, home_team, away_team
            FROM basketball_predictions
            WHERE actual_home_points IS NULL
            ORDER BY game_date ASC, game_id ASC
            """,
            is_pg,
        )
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Bot Memory Persistence (Structured Database Table)
# ----------------------------------------------------------------------


def append_bot_message(chat_id, role, text, timestamp=None):
    """Store one structured Telegram bot message entry and enforce a max 50 recent message limit per chat."""
    chat_id = int(chat_id)
    _validate_text(role, "role")
    _validate_text(text, "text")
    if not timestamp:
        timestamp = _utc_now()

    conn, is_pg = _connect()
    try:
        sql_ins = _q(
            "INSERT INTO bot_memory (chat_id, role, text, timestamp) VALUES (?, ?, ?, ?)",
            is_pg,
        )
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql_ins, (chat_id, role, text, timestamp))
        else:
            conn.execute(sql_ins, (chat_id, role, text, timestamp))

        # Enforce max 50 entries per chat_id
        sql_count = _q("SELECT id FROM bot_memory WHERE chat_id = ? ORDER BY id DESC", is_pg)
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql_count, (chat_id,))
                rows = cur.fetchall()
        else:
            rows = conn.execute(sql_count, (chat_id,)).fetchall()

        if len(rows) > 50:
            excess_ids = [r[0] for r in rows[50:]]
            if is_pg:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM bot_memory WHERE id = ANY(%s)", (excess_ids,))
            else:
                conn.executemany("DELETE FROM bot_memory WHERE id = ?", [(x,) for x in excess_ids])

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_bot_messages(chat_id, limit=50):
    """Retrieve recent bot memory messages for a specific chat_id, chronologically sorted."""
    chat_id = int(chat_id)
    conn, is_pg = _connect()
    try:
        sql = _q(
            "SELECT role, text, timestamp FROM bot_memory WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            is_pg,
        )
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql, (chat_id, limit))
                rows = cur.fetchall()
        else:
            rows = conn.execute(sql, (chat_id, limit)).fetchall()

        messages = [
            {"role": r[0], "text": r[1], "timestamp": r[2]}
            for r in reversed(rows)
        ]
        return messages
    finally:
        conn.close()
