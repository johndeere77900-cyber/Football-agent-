"""
Persistent storage for predictions, using SQLite (a single local file - no
server needed). Football and basketball each have their own table, so
their track records stay separate.
"""

import json
import sqlite3
from datetime import datetime

import config


def _connect():
    return sqlite3.connect(config.DB_PATH)


def init_db():
    conn = _connect()
    conn.execute("""
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
    """)
    conn.commit()
    conn.close()


def save_prediction(fixture_id, match_date, home_team, away_team, league,
                     markets, confidence, odds_comparison=None):
    conn = _connect()
    conn.execute("""
        INSERT OR REPLACE INTO predictions
        (fixture_id, match_date, home_team, away_team, league, markets_json,
         confidence_label, top_pick, top_probability, odds_comparison_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        fixture_id, match_date, home_team, away_team, league,
        json.dumps(markets), confidence["label"], confidence["top_pick"],
        confidence["top_probability"],
        json.dumps(odds_comparison) if odds_comparison else None,
        datetime.utcnow().isoformat(),
    ))
    conn.commit()
    conn.close()


def record_result(fixture_id, home_goals, away_goals):
    conn = _connect()
    row = conn.execute(
        "SELECT top_pick FROM predictions WHERE fixture_id = ?", (fixture_id,)
    ).fetchone()

    if row is None:
        conn.close()
        return False

    top_pick = row[0]
    if home_goals > away_goals:
        actual = "home_win"
    elif home_goals < away_goals:
        actual = "away_win"
    else:
        actual = "draw"

    correct = 1 if top_pick == actual else 0

    conn.execute("""
        UPDATE predictions
        SET actual_home_goals = ?, actual_away_goals = ?, top_pick_correct = ?
        WHERE fixture_id = ?
    """, (home_goals, away_goals, correct, fixture_id))
    conn.commit()
    conn.close()
    return True


def accuracy_summary():
    conn = _connect()
    rows = conn.execute("""
        SELECT confidence_label, top_pick_correct
        FROM predictions
        WHERE top_pick_correct IS NOT NULL
    """).fetchall()
    conn.close()

    if not rows:
        return {"total_graded": 0}

    summary = {"total_graded": len(rows), "overall_accuracy": 0.0, "by_confidence": {}}
    correct_total = sum(r[1] for r in rows)
    summary["overall_accuracy"] = correct_total / len(rows)

    for label in ("High", "Moderate", "Toss-up"):
        subset = [r[1] for r in rows if r[0] == label]
        if subset:
            summary["by_confidence"][label] = {
                "count": len(subset),
                "accuracy": sum(subset) / len(subset),
            }

    return summary


def get_pending_fixtures():
    conn = _connect()
    rows = conn.execute("""
        SELECT fixture_id, match_date, home_team, away_team
        FROM predictions
        WHERE actual_home_goals IS NULL
    """).fetchall()
    conn.close()
    return rows


def cleanup_non_target_leagues(keep_keywords):
    """
    One-time cleanup: removes predictions for leagues that don't match any
    of the given keywords (e.g. leftover test data from before the league
    restriction was added), while keeping everything that's actually in
    your current tracked leagues.
    """
    conn = _connect()
    rows = conn.execute("SELECT id, league FROM predictions").fetchall()

    to_delete = []
    for row_id, league in rows:
        league_lower = (league or "").lower()
        if not any(keyword.lower() in league_lower for keyword in keep_keywords):
            to_delete.append(row_id)

    if to_delete:
        conn.executemany("DELETE FROM predictions WHERE id = ?", [(i,) for i in to_delete])
        conn.commit()

    conn.close()
    return len(to_delete), len(rows)


# --- Basketball (new) -------------------------------------------------------

def init_basketball_db():
    conn = _connect()
    conn.execute("""
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
    """)
    conn.commit()
    conn.close()


def save_basketball_prediction(game_id, game_date, home_team, away_team, league,
                                markets, confidence):
    conn = _connect()
    conn.execute("""
        INSERT OR REPLACE INTO basketball_predictions
        (game_id, game_date, home_team, away_team, league, markets_json,
         confidence_label, top_pick, top_probability, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        game_id, game_date, home_team, away_team, league,
        json.dumps(markets), confidence["label"], confidence["top_pick"],
        confidence["top_probability"],
        datetime.utcnow().isoformat(),
    ))
    conn.commit()
    conn.close()


def record_basketball_result(game_id, home_points, away_points):
    conn = _connect()
    row = conn.execute(
        "SELECT top_pick FROM basketball_predictions WHERE game_id = ?", (game_id,)
    ).fetchone()

    if row is None:
        conn.close()
        return False

    top_pick = row[0]
    actual = "home_win" if home_points > away_points else "away_win"
    correct = 1 if top_pick == actual else 0

    conn.execute("""
        UPDATE basketball_predictions
        SET actual_home_points = ?, actual_away_points = ?, top_pick_correct = ?
        WHERE game_id = ?
    """, (home_points, away_points, correct, game_id))
    conn.commit()
    conn.close()
    return True


def basketball_accuracy_summary():
    conn = _connect()
    rows = conn.execute("""
        SELECT confidence_label, top_pick_correct
        FROM basketball_predictions
        WHERE top_pick_correct IS NOT NULL
    """).fetchall()
    conn.close()

    if not rows:
        return {"total_graded": 0}

    summary = {"total_graded": len(rows), "overall_accuracy": 0.0, "by_confidence": {}}
    correct_total = sum(r[1] for r in rows)
    summary["overall_accuracy"] = correct_total / len(rows)

    for label in ("High", "Moderate", "Toss-up"):
        subset = [r[1] for r in rows if r[0] == label]
        if subset:
            summary["by_confidence"][label] = {
                "count": len(subset),
                "accuracy": sum(subset) / len(subset),
            }

    return summary


def get_pending_basketball_games():
    conn = _connect()
    rows = conn.execute("""
        SELECT game_id, game_date, home_team, away_team
        FROM basketball_predictions
        WHERE actual_home_points IS NULL
    """).fetchall()
    conn.close()
    return rows
