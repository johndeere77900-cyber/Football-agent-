"""
Persistent storage for predictions and results.

SQLite-backed storage for football and basketball predictions, results,
and football Elo ratings.

Important integrity rules:
- Prediction saves are upserts that preserve already-recorded results.
- Results are idempotent: recording the same fixture result twice does not
  update Elo twice.
- Football result grading and Elo updates occur in one transaction.
- IDs and numeric result values are validated before database writes.
- Cleanup is explicit and uses stable league identifiers when available.
"""

import json
import sqlite3
from datetime import datetime

import config
import elo


def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _utc_now():
    return datetime.utcnow().isoformat()


def _validate_positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a positive integer.")
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _validate_non_negative_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a non-negative integer.")
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return value


def _ensure_column(conn, table, column, coltype):
    """Add a column to an existing table if it does not already exist."""
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


def init_db():
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
    fixture_id = _validate_positive_int(
        fixture_id,
        "fixture_id",
    )

    if not isinstance(markets, dict):
        raise ValueError("markets must be a dictionary.")

    if not isinstance(confidence, dict):
        raise ValueError(
            "confidence must be a dictionary."
        )

    required_confidence = (
        "label",
        "top_pick",
        "top_probability",
    )

    if not all(
        key in confidence
        for key in required_confidence
    ):
        raise ValueError(
            "confidence is missing required fields."
        )

    if home_team_id is not None:
        home_team_id = _validate_positive_int(
            home_team_id,
            "home_team_id",
        )

    if away_team_id is not None:
        away_team_id = _validate_positive_int(
            away_team_id,
            "away_team_id",
        )

    top_probability = confidence[
        "top_probability"
    ]

    if (
        isinstance(top_probability, bool)
        or not isinstance(
            top_probability,
            (int, float),
        )
        or not 0 <= top_probability <= 1
    ):
        raise ValueError(
            "top_probability must be between 0 and 1."
        )

    conn = _connect()

    try:
        # Do NOT use INSERT OR REPLACE here.
        #
        # SQLite REPLACE deletes the existing row and inserts a new one.
        # That can erase an already-recorded match result.
        conn.execute(
            """
            INSERT INTO predictions (
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
            ON CONFLICT(fixture_id) DO UPDATE SET
                match_date = excluded.match_date,
                home_team = excluded.home_team,
                away_team = excluded.away_team,
                league = excluded.league,
                markets_json = excluded.markets_json,
                confidence_label = excluded.confidence_label,
                top_pick = excluded.top_pick,
                top_probability = excluded.top_probability,
                odds_comparison_json = excluded.odds_comparison_json,
                home_team_id = excluded.home_team_id,
                away_team_id = excluded.away_team_id
            """,
            (
                fixture_id,
                match_date,
                home_team,
                away_team,
                league,
                json.dumps(markets),
                confidence["label"],
                confidence["top_pick"],
                top_probability,
                (
                    json.dumps(odds_comparison)
                    if odds_comparison is not None
                    else None
                ),
                home_team_id,
                away_team_id,
                _utc_now(),
            ),
        )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_result(
    fixture_id,
    home_goals,
    away_goals,
):
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

        # Idempotency protection:
        # If this fixture already has a recorded result, do not grade it
        # again and do not update Elo again.
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

        # Elo update must be part of the same transaction.
        if home_id is not None and away_id is not None:
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


def get_team_rating(team_id):
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

        return (
            row[0]
            if row is not None
            else elo.DEFAULT_RATING
        )
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

    def get_rating(team_id):
        row = conn.execute(
            """
            SELECT rating
            FROM elo_ratings
            WHERE team_id = ?
            """,
            (team_id,),
        ).fetchone()

        return (
            row[0]
            if row is not None
            else elo.DEFAULT_RATING
        )

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


def accuracy_summary():
    conn = _connect()

    try:
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
        return {
            "total_graded": 0
        }

    summary = {
        "total_graded": len(rows),
        "overall_accuracy": 0.0,
        "by_confidence": {},
    }

    correct_total = sum(
        int(row[1])
        for row in rows
    )

    summary["overall_accuracy"] = (
        correct_total / len(rows)
    )

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
            """
        ).fetchall()
    finally:
        conn.close()


def cleanup_non_target_leagues(
    keep_keywords,
    dry_run=False,
):
    """
    Remove football predictions whose league does not match one of the
    supplied keywords.

    This remains a destructive operation when dry_run=False.

    Returns:
        (would_delete, total_rows)

    The function intentionally does not silently delete anything when
    dry_run=True.
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
# Basketball
# ----------------------------------------------------------------------

def init_basketball_db():
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


def save_basketball_prediction(
    game_id,
    game_date,
    home_team,
    away_team,
    league,
    markets,
    confidence,
):
    game_id = _validate_positive_int(
        game_id,
        "game_id",
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

    if not all(
        key in confidence
        for key in required_confidence
    ):
        raise ValueError(
            "confidence is missing required fields."
        )

    top_probability = confidence[
        "top_probability"
    ]

    if (
        isinstance(top_probability, bool)
        or not isinstance(
            top_probability,
            (int, float),
        )
        or not 0 <= top_probability <= 1
    ):
        raise ValueError(
            "top_probability must be between 0 and 1."
        )

    conn = _connect()

    try:
        # Avoid INSERT OR REPLACE so an existing graded basketball result
        # cannot be deleted and recreated as an ungraded prediction.
        conn.execute(
            """
            INSERT INTO basketball_predictions (
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
            ON CONFLICT(game_id) DO UPDATE SET
                game_date = excluded.game_date,
                home_team = excluded.home_team,
                away_team = excluded.away_team,
                league = excluded.league,
                markets_json = excluded.markets_json,
                confidence_label = excluded.confidence_label,
                top_pick = excluded.top_pick,
                top_probability = excluded.top_probability
            """,
            (
                game_id,
                game_date,
                home_team,
                away_team,
                league,
                json.dumps(markets),
                confidence["label"],
                confidence["top_pick"],
                top_probability,
                _utc_now(),
            ),
        )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_basketball_result(
    game_id,
    home_points,
    away_points,
):
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
                recorded_home_points
                == home_points
                and recorded_away_points
                == away_points
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


def basketball_accuracy_summary():
    conn = _connect()

    try:
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
        return {
            "total_graded": 0
        }

    summary = {
        "total_graded": len(rows),
        "overall_accuracy": 0.0,
        "by_confidence": {},
    }

    correct_total = sum(
        int(row[1])
        for row in rows
    )

    summary["overall_accuracy"] = (
        correct_total / len(rows)
    )

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
            """
        ).fetchall()
    finally:
        conn.close()
