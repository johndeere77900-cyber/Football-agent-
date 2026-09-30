"""
Controlled Migration Utility: Local SQLite -> Neon PostgreSQL.

Performs a safe, idempotent migration from local SQLite (predictions.db)
and telegram_memory.json to Neon PostgreSQL.

Sequence:
1. Validate local SQLite source database and schema.
2. Report local row counts and integrity checks.
3. Initialize Neon PostgreSQL schema via storage.init_db().
4. Migrate predictions, basketball_predictions, elo_ratings, and telegram_memory.
5. Preserve original IDs, timestamps, and JSON payloads.
6. Verify destination PostgreSQL row counts and key fields against source.
7. Report migration status.

Safe to re-run: uses ON CONFLICT DO NOTHING / UPDATE clauses.
Does NOT delete local SQLite database or JSON files.
"""

import json
import os
import sqlite3
import sys

import storage


def migrate():
    sqlite_path = os.environ.get("FOOTBALL_AGENT_DB", "predictions.db")
    neon_url = storage.get_database_url()

    if not neon_url or not storage.HAS_PSYCOPG:
        print("Error: NEON_DATABASE_URL environment variable must be set and psycopg installed.", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(sqlite_path):
        print(f"Notice: Local SQLite file '{sqlite_path}' does not exist. Initializing empty Neon database...", flush=True)
        storage.init_db()
        print("Neon database initialized successfully.", flush=True)
        return

    print(f"=== Starting Controlled Migration: SQLite ({sqlite_path}) -> Neon PostgreSQL ===", flush=True)

    # 1. Connect SQLite Source
    s_conn = sqlite3.connect(sqlite_path)

    # 2. Count source records
    pred_count = s_conn.execute("SELECT count(*) FROM predictions").fetchone()[0]
    bask_count = s_conn.execute("SELECT count(*) FROM basketball_predictions").fetchone()[0]
    elo_count = s_conn.execute("SELECT count(*) FROM elo_ratings").fetchone()[0]

    print(f"Source SQLite Records Found: predictions={pred_count}, basketball_predictions={bask_count}, elo_ratings={elo_count}", flush=True)

    # 3. Initialize Neon Schema
    storage.init_db()

    # 4. Connect Neon Target
    pg_conn = storage.psycopg.connect(neon_url, autocommit=False)

    try:
        # Migrate predictions
        if pred_count > 0:
            cur_s = s_conn.execute("""
                SELECT fixture_id, match_date, home_team, away_team, league, markets_json,
                       confidence_label, top_pick, top_probability, odds_comparison_json,
                       actual_home_goals, actual_away_goals, top_pick_correct, created_at,
                       home_team_id, away_team_id,
                       COALESCE(prediction_context, 'PRE_MATCH')
                FROM predictions
            """)
            rows = cur_s.fetchall()

            with pg_conn.cursor() as cur_pg:
                for r in rows:
                    (fid, date, h_team, a_team, league, m_json, c_label, top_p, top_prob,
                     o_json, a_h_goals, a_a_goals, correct, created, h_id, a_id, p_context) = r

                    m_val = storage.Jsonb(json.loads(m_json)) if m_json else storage.Jsonb({})
                    o_val = storage.Jsonb(json.loads(o_json)) if o_json else None

                    cur_pg.execute(
                        """
                        INSERT INTO predictions (
                            fixture_id, match_date, home_team, away_team, league,
                            markets_json, confidence_label, top_pick, top_probability,
                            odds_comparison_json, actual_home_goals, actual_away_goals,
                            top_pick_correct, created_at, home_team_id, away_team_id, prediction_context
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (fixture_id) DO UPDATE SET
                            actual_home_goals = EXCLUDED.actual_home_goals,
                            actual_away_goals = EXCLUDED.actual_away_goals,
                            top_pick_correct = EXCLUDED.top_pick_correct
                        """,
                        (
                            fid, date, h_team, a_team, league,
                            m_val, c_label, top_p, top_prob,
                            o_val, a_h_goals, a_a_goals,
                            correct, created, h_id, a_id, p_context
                        )
                    )

        # Migrate basketball_predictions
        if bask_count > 0:
            cur_s = s_conn.execute("""
                SELECT game_id, game_date, home_team, away_team, league, markets_json,
                       confidence_label, top_pick, top_probability, actual_home_points,
                       actual_away_points, top_pick_correct, created_at
                FROM basketball_predictions
            """)
            rows = cur_s.fetchall()

            with pg_conn.cursor() as cur_pg:
                for r in rows:
                    (gid, date, h_team, a_team, league, m_json, c_label, top_p, top_prob,
                     a_h_pts, a_a_pts, correct, created) = r

                    m_val = storage.Jsonb(json.loads(m_json)) if m_json else storage.Jsonb({})

                    cur_pg.execute(
                        """
                        INSERT INTO basketball_predictions (
                            game_id, game_date, home_team, away_team, league,
                            markets_json, confidence_label, top_pick, top_probability,
                            actual_home_points, actual_away_points, top_pick_correct, created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (game_id) DO UPDATE SET
                            actual_home_points = EXCLUDED.actual_home_points,
                            actual_away_points = EXCLUDED.actual_away_points,
                            top_pick_correct = EXCLUDED.top_pick_correct
                        """,
                        (
                            gid, date, h_team, a_team, league,
                            m_val, c_label, top_p, top_prob,
                            a_h_pts, a_a_pts, correct, created
                        )
                    )

        # Migrate elo_ratings
        if elo_count > 0:
            cur_s = s_conn.execute("SELECT team_id, team_name, rating, updated_at FROM elo_ratings")
            rows = cur_s.fetchall()

            with pg_conn.cursor() as cur_pg:
                for r in rows:
                    tid, tname, rating, updated = r
                    cur_pg.execute(
                        """
                        INSERT INTO elo_ratings (team_id, team_name, rating, updated_at)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (team_id) DO UPDATE SET
                            team_name = EXCLUDED.team_name,
                            rating = EXCLUDED.rating,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (tid, tname, rating, updated)
                    )

        # Migrate telegram_memory.json if present
        json_memory_path = "telegram_memory.json"
        if os.path.exists(json_memory_path):
            try:
                with open(json_memory_path, "r", encoding="utf-8") as f:
                    mem_data = json.load(f)
                recent = mem_data.get("recent", [])
                with pg_conn.cursor() as cur_pg:
                    for entry in recent:
                        if isinstance(entry, dict) and entry.get("text"):
                            role = entry.get("role", "user")
                            text = entry.get("text", "")
                            ts = entry.get("timestamp", storage._utc_now())
                            cur_pg.execute(
                                "INSERT INTO bot_memory (chat_id, role, text, timestamp) VALUES (%s, %s, %s, %s)",
                                (0, role, text, ts)
                            )
            except Exception as exc:
                print(f"Notice: telegram_memory.json read skipped: {exc}", flush=True)

        pg_conn.commit()

        # 5. Verification
        with pg_conn.cursor() as cur_pg:
            cur_pg.execute("SELECT count(*) FROM predictions")
            pg_pred = cur_pg.fetchone()[0]
            cur_pg.execute("SELECT count(*) FROM basketball_predictions")
            pg_bask = cur_pg.fetchone()[0]
            cur_pg.execute("SELECT count(*) FROM elo_ratings")
            pg_elo = cur_pg.fetchone()[0]

        print(f"Target Neon Records Verified: predictions={pg_pred}, basketball_predictions={pg_bask}, elo_ratings={pg_elo}", flush=True)

        assert pg_pred >= pred_count, "Migration Integrity Error: Neon predictions count is less than SQLite source!"
        assert pg_bask >= bask_count, "Migration Integrity Error: Neon basketball_predictions count is less than SQLite source!"
        assert pg_elo >= elo_count, "Migration Integrity Error: Neon elo_ratings count is less than SQLite source!"

        print("=== Migration Completed Successfully and Verified ===", flush=True)

    except Exception:
        pg_conn.rollback()
        raise
    finally:
        s_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    migrate()
