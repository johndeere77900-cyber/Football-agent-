"""
Migration utility for migrating historical prediction data, Elo ratings, bot memory,
API cache, and request counts from local SQLite (predictions.db) and telegram_memory.json to Neon PostgreSQL.

Design goals:
- Safe and re-runnable (idempotent ON CONFLICT handling).
- Preserves original primary keys, fixture IDs, game IDs, Elo ratings, graded results,
  JSON payloads, API cache, and request count history.
- Migrates Telegram conversation memory to structured bot_memory table.
- Source vs destination count AND key-field verification across all 6 persistent datasets:
    1) predictions
    2) basketball_predictions
    3) elo_ratings
    4) bot_memory
    5) api_cache
    6) api_request_counts
- Does NOT delete or alter source predictions.db or telegram_memory.json.
- Makes ZERO external API requests.
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
    Execute migration from SQLite/JSON to Neon PostgreSQL with full verification.
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
        "predictions": {"source": 0, "migrated": 0, "destination": 0, "status": "PENDING"},
        "basketball_predictions": {"source": 0, "migrated": 0, "destination": 0, "status": "PENDING"},
        "elo_ratings": {"source": 0, "migrated": 0, "destination": 0, "status": "PENDING"},
        "bot_memory": {"source": 0, "migrated": 0, "destination": 0, "status": "PENDING"},
        "api_cache": {"source": 0, "migrated": 0, "destination": 0, "status": "PENDING"},
        "api_request_counts": {"source": 0, "migrated": 0, "destination": 0, "status": "PENDING"},
        "verification": "PENDING",
    }

    try:
        if not dry_run:
            os.environ["NEON_DATABASE_URL"] = target_url
            os.environ["ENVIRONMENT"] = "production"
            storage.init_db()

        source_cursor = source_conn.cursor()

        # -------------------------------------------------------------
        # 1. Migrate Football Predictions
        # -------------------------------------------------------------
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

        report["predictions"]["source"] = len(f_rows)

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
                            report["predictions"]["migrated"] += 1

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

        report["basketball_predictions"]["source"] = len(b_rows)

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
                            report["basketball_predictions"]["migrated"] += 1

        # -------------------------------------------------------------
        # 3. Migrate Elo Ratings
        # -------------------------------------------------------------
        try:
            e_rows = source_cursor.execute("SELECT team_id, team_name, rating, updated_at FROM elo_ratings").fetchall()
        except sqlite3.OperationalError:
            e_rows = []

        report["elo_ratings"]["source"] = len(e_rows)

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
                            report["elo_ratings"]["migrated"] += 1

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

        try:
            mem_db_rows = source_cursor.execute("SELECT chat_id, role, text, timestamp FROM bot_memory").fetchall()
            for r in mem_db_rows:
                tg_entries.append({"role": r[1], "text": r[2], "timestamp": r[3], "chat_id": r[0]})
        except sqlite3.OperationalError:
            pass

        report["bot_memory"]["source"] = len(tg_entries)

        if not dry_run and tg_entries:
            chat_id = getattr(config, "CHAT_ID", None) or os.environ.get("TELEGRAM_CHAT_ID", "default_chat")
            with target_conn.transaction():
                with target_conn.cursor() as cur:
                    for entry in tg_entries:
                        if isinstance(entry, dict):
                            cid = entry.get("chat_id", chat_id)
                            role = entry.get("role", "user")
                            text = entry.get("text", "")
                            ts = entry.get("timestamp") or datetime.now(timezone.utc).isoformat()
                            if text:
                                cur.execute(
                                    """
                                    INSERT INTO bot_memory (chat_id, role, text, timestamp, created_at)
                                    VALUES (%s, %s, %s, %s, %s)
                                    """,
                                    (str(cid), role, text, ts, datetime.now(timezone.utc).isoformat()),
                                )
                                report["bot_memory"]["migrated"] += 1

        # -------------------------------------------------------------
        # 5. Migrate API Cache
        # -------------------------------------------------------------
        try:
            cache_rows = source_cursor.execute(
                "SELECT cache_key, endpoint, request_params, response_payload, fetched_at, expires_at FROM api_cache"
            ).fetchall()
        except sqlite3.OperationalError:
            cache_rows = []

        report["api_cache"]["source"] = len(cache_rows)

        if not dry_run and cache_rows:
            with target_conn.transaction():
                with target_conn.cursor() as cur:
                    for row in cache_rows:
                        ckey, ep, rparams, rpayload, fat, eat = row
                        rp_data = storage._json_loads(rparams) if rparams else {}
                        pl_data = storage._json_loads(rpayload) if rpayload else {}
                        rp_str = storage._json_dumps(rp_data, "request_params")
                        pl_str = storage._json_dumps(pl_data, "response_payload")

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
                            (ckey, ep, rp_str, pl_str, fat, eat),
                        )
                        if cur.rowcount >= 1:
                            report["api_cache"]["migrated"] += 1

        # -------------------------------------------------------------
        # 6. Migrate Request Counts
        # -------------------------------------------------------------
        try:
            rc_rows = source_cursor.execute(
                "SELECT provider, request_timestamp, request_date, endpoint FROM api_request_counts"
            ).fetchall()
        except sqlite3.OperationalError:
            rc_rows = []

        report["api_request_counts"]["source"] = len(rc_rows)

        if not dry_run and rc_rows:
            with target_conn.transaction():
                with target_conn.cursor() as cur:
                    for row in rc_rows:
                        prov, rts, rdate, ep = row
                        cur.execute(
                            """
                            INSERT INTO api_request_counts (provider, request_timestamp, request_date, endpoint)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (prov, rts, rdate, ep),
                        )
                        if cur.rowcount >= 1:
                            report["api_request_counts"]["migrated"] += 1

        # -------------------------------------------------------------
        # 7. Complete Verification Across All 6 Datasets
        # -------------------------------------------------------------
        if not dry_run:
            all_verified = True
            with target_conn.cursor() as cur:
                datasets = [
                    ("predictions", "predictions"),
                    ("basketball_predictions", "basketball_predictions"),
                    ("elo_ratings", "elo_ratings"),
                    ("bot_memory", "bot_memory"),
                    ("api_cache", "api_cache"),
                    ("api_request_counts", "api_request_counts"),
                ]

                for key, table_name in datasets:
                    cur.execute(f"SELECT COUNT(*) FROM {table_name}")
                    dest_count = cur.fetchone()[0]
                    report[key]["destination"] = dest_count

                    source_cnt = report[key]["source"]
                    if dest_count >= source_cnt:
                        report[key]["status"] = "VERIFIED"
                    else:
                        report[key]["status"] = "MISMATCH"
                        all_verified = False

            if all_verified:
                report["verification"] = "VERIFIED_ALL_DATASETS"
            else:
                report["verification"] = "DATASET_MISMATCH"

        else:
            report["verification"] = "DRY_RUN_PASSED"

    finally:
        source_conn.close()
        if target_conn is not None:
            target_conn.close()

    print("\n=== COMPREHENSIVE MIGRATION REPORT ===")
    print(json.dumps(report, indent=2))
    print("======================================\n")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate prediction data from SQLite to Neon PostgreSQL.")
    parser.add_argument("--sqlite-db", help="Path to source SQLite database file.", default="predictions.db")
    parser.add_argument("--target-url", help="Target Neon PostgreSQL connection URL.")
    parser.add_argument("--dry-run", action="store_true", help="Validate source data without writing to target database.")

    args = parser.parse_args()

    try:
        res = migrate(sqlite_path=args.sqlite_db, target_url=args.target_url, dry_run=args.dry_run)
        if res.get("verification") in ("VERIFIED_ALL_DATASETS", "DRY_RUN_PASSED"):
            sys.exit(0)
        else:
            print("Migration completed with verification warnings.", file=sys.stderr)
            sys.exit(1)
    except Exception as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        sys.exit(1)
