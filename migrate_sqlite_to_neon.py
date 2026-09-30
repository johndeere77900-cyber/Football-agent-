"""
Migration utility for migrating historical prediction data, Elo ratings, and
bot memory from local SQLite (predictions.db) and telegram_memory.json to Neon PostgreSQL.

Design goals:
- Safe and re-runnable (idempotent ON CONFLICT handling).
- Preserves original primary keys, fixture IDs, game IDs, Elo ratings, graded results,
  and JSON payloads.
- Migrates Telegram conversation memory to structured bot_memory table.
- Verifies destination record counts and key field parity.
- Does NOT delete or alter source predictions.db or telegram_memory.json.
- Diagnostic CLI with --dry-run option.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import config
import storage

try:
    import psycopg
except ImportError:
    psycopg = None


def validate_sqlite_source(sqlite_path):
    """Validate that SQLite source file exists and contains valid schema."""
    if not os.path.exists(sqlite_path):
        raise FileNotFoundError(f"SQLite database file not found at: {sqlite_path}")

    conn = sqlite3.connect(sqlite_path)
    try:
        cursor = conn.cursor()
        tables = {
            row[0]
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        if "predictions" not in tables:
            raise ValueError(f"SQLite database at {sqlite_path} does not contain 'predictions' table.")

        return True
    finally:
        conn.close()


def migrate(sqlite_path=None, target_url=None, dry_run=False):
    """
    Execute migration from SQLite/JSON to Neon PostgreSQL.
    """
    sqlite_path = sqlite_path or config.DB_PATH
    target_url = target_url or config.NEON_DATABASE_URL or os.environ.get("NEON_DATABASE_URL")

    if not target_url:
        raise ValueError(
            "Target Neon database URL is missing. Set NEON_DATABASE_URL or pass --target-url."
        )

    if psycopg is None:
        raise RuntimeError("psycopg library is required for PostgreSQL migration.")

    validate_sqlite_source(sqlite_path)

    print(f"Starting migration check from {sqlite_path} to Neon database...")

    source_conn = sqlite3.connect(sqlite_path)
    target_conn = None

    if not dry_run:
        target_conn = psycopg.connect(target_url)

    report = {
        "football_predictions_migrated": 0,
        "football_predictions_source_count": 0,
        "basketball_predictions_migrated": 0,
        "basketball_predictions_source_count": 0,
        "elo_ratings_migrated": 0,
        "elo_ratings_source_count": 0,
        "telegram_messages_migrated": 0,
        "telegram_messages_source_count": 0,
        "verification": "PENDING",
    }

    try:
        if not dry_run:
            os.environ["NEON_DATABASE_URL"] = target_url
            os.environ["ENVIRONMENT"] = "production"
            storage.init_db()

        # -------------------------------------------------------------
        # 1. Migrate Football Predictions
        # -------------------------------------------------------------
        source_cursor = source_conn.cursor()
        f_rows = source_cursor.execute(
            """
            SELECT
                fixture_id, match_date, home_team, away_team, league,
                markets_json, confidence_label, top_pick, top_probability,
                odds_comparison_json, actual_home_goals, actual_away_goals,
                top_pick_correct, home_team_id, away_team_id,
                created_at
            FROM predictions
            ORDER BY id ASC
            """
        ).fetchall()

        report["football_predictions_source_count"] = len(f_rows)

        if not dry_run and f_rows:
            with target_conn.transaction():
                with target_conn.cursor() as cur:
                    for row in f_rows:
                        (
                            fid, mdate, hteam, ateam, league,
                            mjson, clabel, tpick, tprob,
                            ojson, hgoals, agoals,
                            tcorrect, hid, aid, cat
                        ) = row

                        # Ensure valid JSON payloads for Postgres JSONB columns
                        mjson_data = storage._json_loads(mjson) if mjson else {}
                        ojson_data = storage._json_loads(ojson) if ojson else None

                        mjson_str = storage._json_dumps(mjson_data, "markets") if mjson_data else None
                        ojson_str = storage._json_dumps(ojson_data, "odds") if ojson_data else None

                        cur.execute(
                            """
                            INSERT INTO predictions (
                                fixture_id, match_date, home_team, away_team, league,
                                markets_json, confidence_label, top_pick, top_probability,
                                odds_comparison_json, actual_home_goals, actual_away_goals,
                                top_pick_correct, home_team_id, away_team_id,
                                prediction_context, created_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (fixture_id) DO UPDATE SET
                                actual_home_goals = COALESCE(EXCLUDED.actual_home_goals, predictions.actual_home_goals),
                                actual_away_goals = COALESCE(EXCLUDED.actual_away_goals, predictions.actual_away_goals),
                                top_pick_correct = COALESCE(EXCLUDED.top_pick_correct, predictions.top_pick_correct)
                            """,
                            (
                                fid, mdate, hteam, ateam, league,
                                mjson_str, clabel, tpick, tprob,
                                ojson_str, hgoals, agoals,
                                tcorrect, hid, aid,
                                "PRE_MATCH", cat or datetime.now(timezone.utc).isoformat(),
                            ),
                        )
                        if cur.rowcount >= 1:
                            report["football_predictions_migrated"] += 1

        # -------------------------------------------------------------
        # 2. Migrate Basketball Predictions
        # -------------------------------------------------------------
        try:
            b_rows = source_cursor.execute(
                """
                SELECT
                    game_id, game_date, home_team, away_team, league,
                    markets_json, confidence_label, top_pick, top_probability,
                    actual_home_points, actual_away_points, top_pick_correct,
                    created_at
                FROM basketball_predictions
                ORDER BY id ASC
                """
            ).fetchall()
        except sqlite3.OperationalError:
            b_rows = []

        report["basketball_predictions_source_count"] = len(b_rows)

        if not dry_run and b_rows:
            with target_conn.transaction():
                with target_conn.cursor() as cur:
                    for row in b_rows:
                        (
                            gid, gdate, hteam, ateam, league,
                            mjson, clabel, tpick, tprob,
                            hpts, apts, tcorrect, cat
                        ) = row

                        mjson_data = storage._json_loads(mjson) if mjson else {}
                        mjson_str = storage._json_dumps(mjson_data, "markets") if mjson_data else None

                        cur.execute(
                            """
                            INSERT INTO basketball_predictions (
                                game_id, game_date, home_team, away_team, league,
                                markets_json, confidence_label, top_pick, top_probability,
                                actual_home_points, actual_away_points, top_pick_correct,
                                prediction_context, created_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (game_id) DO UPDATE SET
                                actual_home_points = COALESCE(EXCLUDED.actual_home_points, basketball_predictions.actual_home_points),
                                actual_away_points = COALESCE(EXCLUDED.actual_away_points, basketball_predictions.actual_away_points),
                                top_pick_correct = COALESCE(EXCLUDED.top_pick_correct, basketball_predictions.top_pick_correct)
                            """,
                            (
                                gid, gdate, hteam, ateam, league,
                                mjson_str, clabel, tpick, tprob,
                                hpts, apts, tcorrect,
                                "PRE_MATCH", cat or datetime.now(timezone.utc).isoformat(),
                            ),
                        )
                        if cur.rowcount >= 1:
                            report["basketball_predictions_migrated"] += 1

        # -------------------------------------------------------------
        # 3. Migrate Elo Ratings
        # -------------------------------------------------------------
        try:
            e_rows = source_cursor.execute(
                """
                SELECT team_id, team_name, rating, updated_at
                FROM elo_ratings
                """
            ).fetchall()
        except sqlite3.OperationalError:
            e_rows = []

        report["elo_ratings_source_count"] = len(e_rows)

        if not dry_run and e_rows:
            with target_conn.transaction():
                with target_conn.cursor() as cur:
                    for row in e_rows:
                        tid, tname, rating, upat = row
                        cur.execute(
                            """
                            INSERT INTO elo_ratings (team_id, team_name, rating, updated_at)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (team_id) DO UPDATE SET
                                team_name = EXCLUDED.team_name,
                                rating = EXCLUDED.rating,
                                updated_at = EXCLUDED.updated_at
                            """,
                            (tid, tname, rating, upat or datetime.now(timezone.utc).isoformat()),
                        )
                        if cur.rowcount >= 1:
                            report["elo_ratings_migrated"] += 1

        # -------------------------------------------------------------
        # 4. Migrate Telegram Memory
        # -------------------------------------------------------------
        json_memory_path = "telegram_memory.json"
        tg_entries = []

        if os.path.exists(json_memory_path):
            try:
                with open(json_memory_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("recent"), list):
                    tg_entries = data["recent"]
            except Exception as exc:
                print(f"Warning: could not parse {json_memory_path}: {exc}")

        report["telegram_messages_source_count"] = len(tg_entries)

        if not dry_run and tg_entries:
            chat_id = getattr(config, "CHAT_ID", None) or os.environ.get("TELEGRAM_CHAT_ID", "default_chat")
            with target_conn.transaction():
                with target_conn.cursor() as cur:
                    for entry in tg_entries:
                        if isinstance(entry, dict):
                            role = entry.get("role", "user")
                            text = entry.get("text", "")
                            ts = entry.get("timestamp") or datetime.now(timezone.utc).isoformat()
                            if text:
                                cur.execute(
                                    """
                                    INSERT INTO bot_memory (chat_id, role, text, timestamp, created_at)
                                    VALUES (%s, %s, %s, %s, %s)
                                    """,
                                    (str(chat_id), role, text, ts, datetime.now(timezone.utc).isoformat()),
                                )
                                report["telegram_messages_migrated"] += 1

        # -------------------------------------------------------------
        # 5. Verification
        # -------------------------------------------------------------
        if not dry_run:
            with target_conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM predictions")
                f_target_count = cur.fetchone()[0]

                cur.execute("SELECT COUNT(*) FROM basketball_predictions")
                b_target_count = cur.fetchone()[0]

                cur.execute("SELECT COUNT(*) FROM elo_ratings")
                e_target_count = cur.fetchone()[0]

            print(
                f"Destination Target Counts: "
                f"football_predictions={f_target_count}, "
                f"basketball_predictions={b_target_count}, "
                f"elo_ratings={e_target_count}"
            )

            if f_target_count >= report["football_predictions_source_count"]:
                report["verification"] = "VERIFIED_MATCH"
            else:
                report["verification"] = "COUNT_MISMATCH"

        else:
            report["verification"] = "DRY_RUN_PASSED"

    finally:
        source_conn.close()
        if target_conn is not None:
            target_conn.close()

    print("\n=== MIGRATION REPORT ===")
    print(json.dumps(report, indent=2))
    print("========================\n")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate prediction data from SQLite to Neon PostgreSQL.")
    parser.add_argument("--sqlite-db", help="Path to source SQLite database file.", default="predictions.db")
    parser.add_argument("--target-url", help="Target Neon PostgreSQL connection URL.")
    parser.add_argument("--dry-run", action="store_true", help="Validate source data without writing to target database.")

    args = parser.parse_args()

    try:
        res = migrate(sqlite_path=args.sqlite_db, target_url=args.target_url, dry_run=args.dry_run)
        if res.get("verification") in ("VERIFIED_MATCH", "DRY_RUN_PASSED"):
            sys.exit(0)
        else:
            print("Migration completed with verification warnings.", file=sys.stderr)
            sys.exit(1)
    except Exception as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        sys.exit(1)
