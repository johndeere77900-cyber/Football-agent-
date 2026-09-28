"""
Persistent storage for predictions and results.

SQLite-backed storage for football and basketball predictions, results,
and football Elo ratings.

Integrity rules:
- A prediction is immutable after its first successful save.
- Re-running prediction generation for an existing fixture/game does not
  rewrite the original prediction.
- Results are idempotent.
- A conflicting second result is rejected.
- Football result grading and Elo updates occur in one transaction.
- Database/schema initialization is safe for existing databases.
- Numeric IDs and result values are validated before writes.
- JSON payloads are validated before storage.
- Cleanup is explicit and remains a destructive operation only when
  explicitly requested.
"""

import json
import math
import sqlite3
from datetime import datetime, timezone

import config
import elo


# ----------------------------------------------------------------------
# Database helpers
# ----------------------------------------------------------------------


def _connect():
    """Open the configured SQLite database with foreign keys enabled."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _utc_now():
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _validate_positive_int(value, name):
    """Validate a strictly positive integer."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
    ):
        raise ValueError(
            f"{name} must be a positive integer."
        )

    return value


def _validate_non_negative_int(value, name):
    """Validate a non-negative integer."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise ValueError(
            f"{name} must be a non-negative integer."
        )

    return value


def _validate_optional_positive_int(value, name):
    """Validate an optional positive integer."""
    if value is None:
        return None

    return _validate_positive_int(
        value,
        name,
    )


def _validate_probability(value, name="probability"):
    """Validate a finite probability in the inclusive range [0, 1]."""
    if isinstance(value, bool):
        raise ValueError(
            f"{name} must be a number between 0 and 1."
        )

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a number between 0 and 1."
        ) from exc

    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError(
            f"{name} must be a number between 0 and 1."
        )

    return number


def _validate_text(value, name, allow_empty=False):
    """Validate a text field."""
    if not isinstance(value, str):
        raise ValueError(
            f"{name} must be a string."
        )

    if not allow_empty and not value.strip():
        raise ValueError(
            f"{name} cannot be empty."
        )

    return value


def _json_dumps(value, name):
    """
    Serialize a JSON value.

    Fail loudly before touching the database if the prediction payload
    cannot be serialized.
    """
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must contain JSON-serializable data."
        ) from exc


def _ensure_column(conn, table, column, coltype):
    """
    Add a missing column to an existing table.

    Table/column names are internal constants controlled by this module.
    """
    existing = {
        row[1]
        for row in conn.execute(
            f"PRAGMA table_info({table})"
        )
    }

    if column not in existing:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"
        )


# ----------------------------------------------------------------------
# Initialization
# ----------------------------------------------------------------------


def init_db():
    """
    Initialize football prediction storage.

    This function is safe to call repeatedly.
    """
    conn = _connect()

    try:
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

        # Migration support for databases created by older versions.
        _ensure_column(
            conn,
            "predictions",
            "home_team_id",
            "INTEGER",
        )

        _ensure_column(
            conn,
            "predictions",
            "away_team_id",
            "INTEGER",
        )

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

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def init_basketball_db():
    """
    Initialize basketball prediction storage.

    Kept as a public compatibility function because the CLI calls it.
    """
    conn = _connect()

    try:
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

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


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
):
    """
    Save a football prediction exactly once.

    IMPORTANT:
    The first saved prediction is authoritative.

    If the same fixture_id is saved again, the existing row is left
    completely unchanged. This prevents a later model/API run from
    rewriting historical prediction data.

    Returns:
        True  -> a new prediction was inserted
        False -> fixture already existed and was left unchanged
    """
    fixture_id = _validate_positive_int(
        fixture_id,
        "fixture_id",
    )

    _validate_text(
        match_date,
        "match_date",
    )

    _validate_text(
        home_team,
        "home_team",
    )

    _validate_text(
        away_team,
        "away_team",
    )

    _validate_text(
        league,
        "league",
    )

    if not isinstance(markets, dict):
        raise ValueError(
            "markets must be a dictionary."
        )

    if not isinstance(confidence, dict):
        raise ValueError(
            "confidence must be a dictionary."
        )

    required_confidence = (
        "label",
        "top_pick",
        "top_probability",
    )

    missing = [
        key
        for key in required_confidence
        if key not in confidence
    ]

    if missing:
        raise ValueError(
            "confidence is missing required fields: "
            + ", ".join(missing)
        )

    _validate_text(
        confidence["label"],
        "confidence.label",
    )

    _validate_text(
        confidence["top_pick"],
        "confidence.top_pick",
    )

    top_probability = _validate_probability(
        confidence["top_probability"],
        "confidence.top_probability",
    )

    home_team_id = _validate_optional_positive_int(
        home_team_id,
        "home_team_id",
    )

    away_team_id = _validate_optional_positive_int(
        away_team_id,
        "away_team_id",
    )

    markets_json = _json_dumps(
        markets,
        "markets",
    )

    odds_json = (
        _json_dumps(
            odds_comparison,
            "odds_comparison",
        )
        if odds_comparison is not None
        else None
    )

    conn = _connect()

    try:
        # INSERT OR IGNORE is deliberate.
        #
        # The fixture_id UNIQUE constraint makes the first prediction
        # authoritative. A later attempt to save the same fixture is
        # ignored rather than updating the prediction.
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO predictions (
                fixture_id,
                match_date,
                home_team,
                away_team,
                league,
                markets_json,
                confidence_label,
                top_pick,
                top_probability,
                odds_comparison_json,
                home_team_id,
                away_team_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fixture_id,
                match_date,
                home_team,
                away_team,
                league,
                markets_json,
                confidence["label"],
                confidence["top_pick"],
                top_probability,
                odds_json,
                home_team_id,
                away_team_id,
                _utc_now(),
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


def record_result(
    fixture_id,
    home_goals,
    away_goals,
):
    """
    Record the final football result.

    Result writes are idempotent:
    - same result again -> True, no second Elo update
    - different result -> ValueError
    - unknown fixture -> False

    Prediction fields are never modified here.
    """
    fixture_id = _validate_positive_int(
        fixture_id,
        "fixture_id",
    )

    home_goals = _validate_non_negative_int(
        home_goals,
        "home_goals",
    )

    away_goals = _validate_non_negative_int(
        away_goals,
        "away_goals",
    )

    conn = _connect()

    try:
        row = conn.execute(
            """
            SELECT
                top_pick,
                home_team_id,
                away_team_id,
                home_team,
                away_team,
                actual_home_goals,
                actual_away_goals
            FROM predictions
            WHERE fixture_id = ?
            """,
            (fixture_id,),
        ).fetchone()

        if row is None:
            conn.rollback()
            return False

        (
            top_pick,
            home_id,
            away_id,
            home_name,
            away_name,
            recorded_home_goals,
            recorded_away_goals,
        ) = row

        # A result has already been recorded.
        if (
            recorded_home_goals is not None
            or recorded_away_goals is not None
        ):
            if (
                recorded_home_goals == home_goals
                and recorded_away_goals == away_goals
            ):
                conn.rollback()
                return True

            raise ValueError(
                "A different result is already recorded "
                f"for fixture {fixture_id}."
            )

        if home_goals > away_goals:
            actual = "home_win"
        elif home_goals < away_goals:
            actual = "away_win"
        else:
            actual = "draw"

        correct = (
            1
            if top_pick == actual
            else 0
        )

        conn.execute(
            """
            UPDATE predictions
            SET
                actual_home_goals = ?,
                actual_away_goals = ?,
                top_pick_correct = ?
            WHERE fixture_id = ?
            """,
            (
                home_goals,
                away_goals,
                correct,
                fixture_id,
            ),
        )

        # Elo and result are committed atomically.
        if (
            home_id is not None
            and away_id is not None
        ):
            _update_elo_ratings_conn(
                conn,
                home_id,
                home_name,
                away_id,
                away_name,
                home_goals,
                away_goals,
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
    """Return a team's stored Elo rating or the configured default."""
    if team_id is None:
        return elo.DEFAULT_RATING

    team_id = _validate_positive_int(
        team_id,
        "team_id",
    )

    conn = _connect()

    try:
        row = conn.execute(
            """
            SELECT rating
            FROM elo_ratings
            WHERE team_id = ?
            """,
            (team_id,),
        ).fetchone()

        if row is None:
            return elo.DEFAULT_RATING

        return row[0]

    finally:
        conn.close()


def _update_elo_ratings_conn(
    conn,
    home_id,
    home_name,
    away_id,
    away_name,
    home_goals,
    away_goals,
):
    """Update Elo ratings using an existing database transaction."""
    home_id = _validate_positive_int(
        home_id,
        "home_id",
    )

    away_id = _validate_positive_int(
        away_id,
        "away_id",
    )

    home_goals = _validate_non_negative_int(
        home_goals,
        "home_goals",
    )

    away_goals = _validate_non_negative_int(
        away_goals,
        "away_goals",
    )

    home_name = _validate_text(
        home_name,
        "home_name",
    )

    away_name = _validate_text(
        away_name,
        "away_name",
    )

    def get_rating(team_id):
        row = conn.execute(
            """
            SELECT rating
            FROM elo_ratings
            WHERE team_id = ?
            """,
            (team_id,),
        ).fetchone()

        if row is None:
            return elo.DEFAULT_RATING

        return row[0]

    home_rating = get_rating(home_id)
    away_rating = get_rating(away_id)

    new_home, new_away = elo.update_ratings(
        home_rating,
        away_rating,
        home_goals,
        away_goals,
    )

    now = _utc_now()

    conn.execute(
        """
        INSERT INTO elo_ratings (
            team_id,
            team_name,
            rating,
            updated_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(team_id) DO UPDATE SET
            team_name = excluded.team_name,
            rating = excluded.rating,
            updated_at = excluded.updated_at
        """,
        (
            home_id,
            home_name,
            new_home,
            now,
        ),
    )

    conn.execute(
        """
        INSERT INTO elo_ratings (
            team_id,
            team_name,
            rating,
            updated_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(team_id) DO UPDATE SET
            team_name = excluded.team_name,
            rating = excluded.rating,
            updated_at = excluded.updated_at
        """,
        (
            away_id,
            away_name,
            new_away,
            now,
        ),
    )


def update_elo_ratings(
    home_id,
    home_name,
    away_id,
    away_name,
    home_goals,
    away_goals,
):
    """Public wrapper for an atomic Elo update."""
    conn = _connect()

    try:
        _update_elo_ratings_conn(
            conn,
            home_id,
            home_name,
            away_id,
            away_name,
            home_goals,
            away_goals,
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ----------------------------------------------------------------------
# Football reporting
# ----------------------------------------------------------------------


def accuracy_summary():
    """Return graded football prediction accuracy."""
    conn = _connect()

    try:
        rows = conn.execute(
            """
            SELECT
                confidence_label,
                top_pick_correct
            FROM predictions
            WHERE top_pick_correct IS NOT NULL
            """
        ).fetchall()

    finally:
        conn.close()

    if not rows:
        return {
            "total_graded": 0,
            "overall_accuracy": 0.0,
            "by_confidence": {},
        }

    correct_total = sum(
        int(row[1])
        for row in rows
    )

    summary = {
        "total_graded": len(rows),
        "overall_accuracy": (
            correct_total / len(rows)
        ),
        "by_confidence": {},
    }

    for label in (
        "High",
        "Moderate",
        "Toss-up",
    ):
        subset = [
            int(row[1])
            for row in rows
            if row[0] == label
        ]

        if subset:
            summary["by_confidence"][label] = {
                "count": len(subset),
                "accuracy": (
                    sum(subset) / len(subset)
                ),
            }

    return summary


def get_pending_fixtures():
    """Return football predictions that have not yet been graded."""
    conn = _connect()

    try:
        return conn.execute(
            """
            SELECT
                fixture_id,
                match_date,
                home_team,
                away_team
            FROM predictions
            WHERE actual_home_goals IS NULL
            ORDER BY match_date ASC, fixture_id ASC
            """
        ).fetchall()

    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Football cleanup
# ----------------------------------------------------------------------


def cleanup_non_target_leagues(
    keep_keywords,
    dry_run=False,
):
    """
    Remove football predictions whose league does not contain one of the
    supplied keywords.

    Returns:
        (would_delete, total_rows)

    When dry_run=True, no deletion occurs.
    """
    if not isinstance(
        keep_keywords,
        (list, tuple, set),
    ):
        raise ValueError(
            "keep_keywords must be a list, tuple, or set."
        )

    normalized_keywords = []

    for keyword in keep_keywords:
        if not isinstance(keyword, str):
            raise ValueError(
                "Every cleanup keyword must be a string."
            )

        keyword = keyword.strip().lower()

        if keyword:
            normalized_keywords.append(
                keyword
            )

    if not normalized_keywords:
        raise ValueError(
            "At least one non-empty cleanup keyword is required."
        )

    conn = _connect()

    try:
        rows = conn.execute(
            """
            SELECT id, league
            FROM predictions
            """
        ).fetchall()

        to_delete = []

        for row_id, league in rows:
            league_lower = (
                league or ""
            ).lower()

            if not any(
                keyword in league_lower
                for keyword in normalized_keywords
            ):
                to_delete.append(row_id)

        if (
            to_delete
            and not dry_run
        ):
            conn.executemany(
                """
                DELETE FROM predictions
                WHERE id = ?
                """,
                [
                    (row_id,)
                    for row_id in to_delete
                ],
            )

        conn.commit()

        return (
            len(to_delete),
            len(rows),
        )

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
):
    """
    Save a basketball prediction exactly once.

    The first prediction for a game_id is authoritative.
    A later duplicate save is ignored.
    """
    game_id = _validate_positive_int(
        game_id,
        "game_id",
    )

    _validate_text(
        game_date,
        "game_date",
    )

    _validate_text(
        home_team,
        "home_team",
    )

    _validate_text(
        away_team,
        "away_team",
    )

    _validate_text(
        league,
        "league",
    )

    if not isinstance(markets, dict):
        raise ValueError(
            "markets must be a dictionary."
        )

    if not isinstance(confidence, dict):
        raise ValueError(
            "confidence must be a dictionary."
        )

    required_confidence = (
        "label",
        "top_pick",
        "top_probability",
    )

    missing = [
        key
        for key in required_confidence
        if key not in confidence
    ]

    if missing:
        raise ValueError(
            "confidence is missing required fields: "
            + ", ".join(missing)
        )

    _validate_text(
        confidence["label"],
        "confidence.label",
    )

    _validate_text(
        confidence["top_pick"],
        "confidence.top_pick",
    )

    top_probability = _validate_probability(
        confidence["top_probability"],
        "confidence.top_probability",
    )

    markets_json = _json_dumps(
        markets,
        "markets",
    )

    conn = _connect()

    try:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO basketball_predictions (
                game_id,
                game_date,
                home_team,
                away_team,
                league,
                markets_json,
                confidence_label,
                top_pick,
                top_probability,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                game_id,
                game_date,
                home_team,
                away_team,
                league,
                markets_json,
                confidence["label"],
                confidence["top_pick"],
                top_probability,
                _utc_now(),
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
# Basketball result grading
# ----------------------------------------------------------------------


def record_basketball_result(
    game_id,
    home_points,
    away_points,
):
    """
    Record a final basketball result.

    Result writes are idempotent and cannot alter the original prediction.
    """
    game_id = _validate_positive_int(
        game_id,
        "game_id",
    )

    home_points = _validate_non_negative_int(
        home_points,
        "home_points",
    )

    away_points = _validate_non_negative_int(
        away_points,
        "away_points",
    )

    conn = _connect()

    try:
        row = conn.execute(
            """
            SELECT
                top_pick,
                actual_home_points,
                actual_away_points
            FROM basketball_predictions
            WHERE game_id = ?
            """,
            (game_id,),
        ).fetchone()

        if row is None:
            conn.rollback()
            return False

        (
            top_pick,
            recorded_home_points,
            recorded_away_points,
        ) = row

        if (
            recorded_home_points is not None
            or recorded_away_points is not None
        ):
            if (
                recorded_home_points == home_points
                and recorded_away_points == away_points
            ):
                conn.rollback()
                return True

            raise ValueError(
                "A different result is already recorded "
                f"for game {game_id}."
            )

        if home_points > away_points:
            actual = "home_win"
        elif home_points < away_points:
            actual = "away_win"
        else:
            actual = "draw"

        correct = (
            1
            if top_pick == actual
            else 0
        )

        conn.execute(
            """
            UPDATE basketball_predictions
            SET
                actual_home_points = ?,
                actual_away_points = ?,
                top_pick_correct = ?
            WHERE game_id = ?
            """,
            (
                home_points,
                away_points,
                correct,
                game_id,
            ),
        )

        conn.commit()
        return True

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

# ----------------------------------------------------------------------
# Basketball reporting
# ----------------------------------------------------------------------


def basketball_accuracy_summary():
    """Return graded basketball prediction accuracy."""
    conn = _connect()

    try:
        rows = conn.execute(
            """
            SELECT
                confidence_label,
                top_pick_correct
            FROM basketball_predictions
            WHERE top_pick_correct IS NOT NULL
            """
        ).fetchall()

    finally:
        conn.close()

    if not rows:
        return {
            "total_graded": 0,
            "overall_accuracy": 0.0,
            "by_confidence": {},
        }

    correct_total = sum(
        int(row[1])
        for row in rows
    )

    summary = {
        "total_graded": len(rows),
        "overall_accuracy": (
            correct_total / len(rows)
        ),
        "by_confidence": {},
    }

    for label in (
        "High",
        "Moderate",
        "Toss-up",
    ):
        subset = [
            int(row[1])
            for row in rows
            if row[0] == label
        ]

        if subset:
            summary["by_confidence"][label] = {
                "count": len(subset),
                "accuracy": (
                    sum(subset) / len(subset)
                ),
            }

    return summary


def get_pending_basketball_games():
    """Return basketball predictions that have not yet been graded."""
    conn = _connect()

    try:
        return conn.execute(
            """
            SELECT
                game_id,
                game_date,
                home_team,
                away_team
            FROM basketball_predictions
            WHERE actual_home_points IS NULL
            ORDER BY game_date ASC, game_id ASC
            """
        ).fetchall()

    finally:
        conn.close()
