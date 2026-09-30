"""
Migration utility for migrating historical prediction data, Elo ratings, bot memory,
API cache, and request counts from local SQLite (predictions.db) and telegram_memory.json to Neon PostgreSQL.

Design goals:
- Safe and re-runnable (idempotent migration; re-running produces identical destination counts).
- Preserves original primary keys, fixture IDs, game IDs, Elo ratings, graded results,
  JSON payloads, API cache, and request count history.
- Migrates Telegram conversation memory to structured bot_memory table without duplication.
- Exact count matching (source_count == destination_count) AND key-field verification across all 6 persistent datasets:
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


def migrate(sqlite_path=None, target_url=None, dry_run=False, verify_only=False):
    """
    Execute migration from SQLite/JSON to Neon PostgreSQL with exact verification and idempotency.
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
        "key_field_verification": "PENDING",
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
        f_cols = {row[1] for row in source_cursor.execute("PRAGMA table_info(predictions)").fetchall()}
        has_f_context = "prediction_context" in f_cols

        if has_f_context:
            f_rows = source_cursor.execute(
                """
                SELECT
                    fixture_id, match_date, home_team, away_team, league,
                    markets_json, confidence_label, top_pick, top_probability,
                    odds_comparison_json, actual_home_goals, actual_away_goals,
                    top_pick_correct, home_team_id, away_team_id,
                    prediction_context, created_at
                FROM predictions
                ORDER BY id ASC
                """
            ).fetchall()
        else:
            f_rows = source_cursor.execute(
                """
                SELECT
                    fixture_id, match_date, home_team, away_team, league,
                    markets_json, confidence_label, top_pick, top_probability,
                    odds_comparison_json, actual_home_goals, actual_away_goals,
                    top_pick_correct, home_team_id, away_team_id,
                    'PRE_MATCH' AS prediction_context, created_at
                FROM predictions
                ORDER BY id ASC
                """
            ).fetchall()

        report["predictions"]["source"] = len(f_rows)

        if not dry_run and not verify_only and f_rows:
            with target_conn.transaction():
                with target_conn.cursor() as cur:
                    for row in f_rows:
                        (
                            fid, mdate, hteam, ateam, league,
                            mjson, clabel, tpick, tprob,
                            ojson, hgoals, agoals,
                            tcorrect, hid, aid, pcontext, cat
                        ) = row

                        pcontext = (pcontext or "PRE_MATCH").upper()
                        if pcontext not in ("PRE_MATCH", "LIVE"):
                            pcontext = "PRE_MATCH"

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
                                top_pick_correct = COALESCE(EXCLUDED.top_pick_correct, predictions.top_pick_correct),
                                prediction_context = EXCLUDED.prediction_context
                            """,
                            (
                                fid, mdate, hteam, ateam, league,
                                mjson_str, clabel, tpick, tprob,
                                ojson_str, hgoals, agoals,
                                tcorrect, hid, aid,
                                pcontext, cat or datetime.now(timezone.utc).isoformat(),
                            ),
                        )
                        if cur.rowcount >= 1:
                            report["predictions"]["migrated"] += 1

        # -------------------------------------------------------------
        # 2. Migrate Basketball Predictions
        # -------------------------------------------------------------
        try:
            b_cols = {row[1] for row in source_cursor.execute("PRAGMA table_info(basketball_predictions)").fetchall()}
            has_b_context = "prediction_context" in b_cols

            if has_b_context:
                b_rows = source_cursor.execute(
                    """
                    SELECT
                        game_id, game_date, home_team, away_team, league,
                        markets_json, confidence_label, top_pick, top_probability,
                        actual_home_points, actual_away_points, top_pick_correct,
                        prediction_context, created_at
                    FROM basketball_predictions
                    ORDER BY id ASC
                    """
                ).fetchall()
            else:
                b_rows = source_cursor.execute(
                    """
                    SELECT
                        game_id, game_date, home_team, away_team, league,
                        markets_json, confidence_label, top_pick, top_probability,
                        actual_home_points, actual_away_points, top_pick_correct,
                        'PRE_MATCH' AS prediction_context, created_at
                    FROM basketball_predictions
                    ORDER BY id ASC
                    """
                ).fetchall()
        except sqlite3.OperationalError:
            b_rows = []

        report["basketball_predictions"]["source"] = len(b_rows)

        if not dry_run and not verify_only and b_rows:
            with target_conn.transaction():
                with target_conn.cursor() as cur:
                    for row in b_rows:
                        (
                            gid, gdate, hteam, ateam, league,
                            mjson, clabel, tpick, tprob,
                            hpts, apts, tcorrect, bpcontext, cat
                        ) = row

                        bpcontext = (bpcontext or "PRE_MATCH").upper()
                        if bpcontext not in ("PRE_MATCH", "LIVE"):
                            bpcontext = "PRE_MATCH"

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
                                top_pick_correct = COALESCE(EXCLUDED.top_pick_correct, basketball_predictions.top_pick_correct),
                                prediction_context = EXCLUDED.prediction_context
                            """,
                            (
                                gid, gdate, hteam, ateam, league,
                                mjson_str, clabel, tpick, tprob,
                                hpts, apts, tcorrect,
                                bpcontext, cat or datetime.now(timezone.utc).isoformat(),
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

        if not dry_run and not verify_only and e_rows:
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
        tg_raw_entries = []

        if os.path.exists(json_memory_path):
            try:
                with open(json_memory_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("recent"), list):
                    tg_raw_entries = data["recent"]
            except Exception as exc:
                print(f"Warning: could not parse {json_memory_path}: {exc}")

        try:
            mem_db_rows = source_cursor.execute("SELECT chat_id, role, text, timestamp FROM bot_memory").fetchall()
            for r in mem_db_rows:
                tg_raw_entries.append({"role": r[1], "text": r[2], "timestamp": r[3], "chat_id": r[0]})
        except sqlite3.OperationalError:
            pass

        # Deterministic deduplication based on (chat_id, role, text, timestamp)
        seen_tg = set()
        tg_entries = []
        default_chat_id = getattr(config, "CHAT_ID", None) or os.environ.get("TELEGRAM_CHAT_ID", "default_chat")

        for entry in tg_raw_entries:
            if isinstance(entry, dict):
                cid = str(entry.get("chat_id", default_chat_id))
                role = entry.get("role", "user")
                text = entry.get("text", "")
                ts = entry.get("timestamp", "")
                key = (cid, role, text, ts)
                if text and key not in seen_tg:
                    seen_tg.add(key)
                    tg_entries.append((cid, role, text, ts))

        report["bot_memory"]["source"] = len(tg_entries)

        if not dry_run and not verify_only and tg_entries:
            with target_conn.transaction():
                with target_conn.cursor() as cur:
                    for cid, role, text, ts in tg_entries:
                        cur.execute(
                            """
                            INSERT INTO bot_memory (chat_id, role, text, timestamp, created_at)
                            SELECT %s, %s, %s, %s, %s
                            WHERE NOT EXISTS (
                                SELECT 1 FROM bot_memory
                                WHERE chat_id = %s AND role = %s AND text = %s AND timestamp = %s
                            )
                            """,
                            (cid, role, text, ts, datetime.now(timezone.utc).isoformat(), cid, role, text, ts),
                        )
                        if cur.rowcount >= 1:
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

        if not dry_run and not verify_only and cache_rows:
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
        # 6. Migrate Request Counts (Idempotent)
        # -------------------------------------------------------------
        try:
            rc_raw_rows = source_cursor.execute(
                "SELECT provider, request_timestamp, request_date, endpoint FROM api_request_counts"
            ).fetchall()
        except sqlite3.OperationalError:
            rc_raw_rows = []

        seen_rc = set()
        rc_rows = []
        for r in rc_raw_rows:
            key = (r[0], r[1], r[2], r[3])
            if key not in seen_rc:
                seen_rc.add(key)
                rc_rows.append(r)

        raw_rc_count = len(rc_raw_rows)
        dedup_rc_count = len(rc_rows)

        report["api_request_counts"] = {
            "raw_sqlite_rows": raw_rc_count,
            "deduplicated_source_rows": dedup_rc_count,
            "source": dedup_rc_count,
            "migrated": 0,
            "destination": 0,
            "deduplication_reason": (
                "Exact duplicate request log entries collapsed to establish deterministic request identity."
                if raw_rc_count != dedup_rc_count
                else "None (all source request logs distinct)"
            ),
            "status": "PENDING",
        }

        if not dry_run and not verify_only and rc_rows:
            with target_conn.transaction():
                with target_conn.cursor() as cur:
                    for row in rc_rows:
                        prov, rts, rdate, ep = row
                        cur.execute(
                            """
                            INSERT INTO api_request_counts (provider, request_timestamp, request_date, endpoint)
                            SELECT %s, %s, %s, %s
                            WHERE NOT EXISTS (
                                SELECT 1 FROM api_request_counts
                                WHERE provider = %s AND request_timestamp = %s AND endpoint = %s
                            )
                            """,
                            (prov, rts, rdate, ep, prov, rts, ep),
                        )
                        if cur.rowcount >= 1:
                            report["api_request_counts"]["migrated"] += 1

        # -------------------------------------------------------------
        # 7. Exact Source-vs-Destination Deterministic Verification
        # -------------------------------------------------------------
        if not dry_run:
            errors = []

            def norm_json(val):
                if val is None:
                    return None
                if isinstance(val, (dict, list)):
                    return val
                if isinstance(val, str):
                    try:
                        return json.loads(val)
                    except Exception:
                        return val
                return val

            def norm_val(val):
                if isinstance(val, float):
                    return round(val, 6)
                return val

            with target_conn.cursor() as cur:
                # -----------------------------------------------------
                # A. Predictions Verification
                # -----------------------------------------------------
                src_p = {}
                for r in f_rows:
                    fid = r[0]
                    pctx = (r[15] or "PRE_MATCH").upper()
                    if pctx not in ("PRE_MATCH", "LIVE"):
                        pctx = "PRE_MATCH"
                    src_p[fid] = {
                        "fixture_id": fid,
                        "home_team": r[2],
                        "away_team": r[3],
                        "league": r[4],
                        "top_pick": r[7],
                        "top_probability": norm_val(r[8]),
                        "prediction_context": pctx,
                        "actual_home_goals": r[10],
                        "actual_away_goals": r[11],
                        "top_pick_correct": r[12],
                        "created_at": r[16],
                    }

                cur.execute(
                    """
                    SELECT fixture_id, home_team, away_team, league, top_pick, top_probability,
                           prediction_context, actual_home_goals, actual_away_goals, top_pick_correct, created_at
                    FROM predictions
                    """
                )
                dest_p_rows = cur.fetchall()
                dest_p = {}
                for r in dest_p_rows:
                    fid = r[0]
                    dest_p[fid] = {
                        "fixture_id": fid,
                        "home_team": r[1],
                        "away_team": r[2],
                        "league": r[3],
                        "top_pick": r[4],
                        "top_probability": norm_val(r[5]),
                        "prediction_context": r[6],
                        "actual_home_goals": r[7],
                        "actual_away_goals": r[8],
                        "top_pick_correct": r[9],
                        "created_at": r[10],
                    }

                report["predictions"]["destination"] = len(dest_p)
                if len(src_p) != len(dest_p):
                    errors.append(f"predictions count mismatch: source={len(src_p)}, dest={len(dest_p)}")

                for fid, src_rec in src_p.items():
                    if fid not in dest_p:
                        errors.append(f"predictions missing destination record: fixture_id={fid}")
                        continue
                    dest_rec = dest_p[fid]
                    for k, src_v in src_rec.items():
                        dest_v = dest_rec.get(k)
                        if k == "created_at" and src_v is None and dest_v is not None:
                            continue
                        if src_v != dest_v:
                            errors.append(f"predictions mismatch fixture_id={fid} field '{k}': source={src_v!r}, dest={dest_v!r}")

                for fid in dest_p:
                    if fid not in src_p:
                        errors.append(f"predictions unexpected destination record: fixture_id={fid}")

                report["predictions"]["status"] = "EXACT_MATCH" if len(src_p) == len(dest_p) else f"COUNT_MISMATCH (source={len(src_p)}, dest={len(dest_p)})"

                # -----------------------------------------------------
                # B. Basketball Verification
                # -----------------------------------------------------
                src_bp = {}
                for r in b_rows:
                    gid = r[0]
                    bpctx = (r[12] or "PRE_MATCH").upper()
                    if bpctx not in ("PRE_MATCH", "LIVE"):
                        bpctx = "PRE_MATCH"
                    src_bp[gid] = {
                        "game_id": gid,
                        "home_team": r[2],
                        "away_team": r[3],
                        "league": r[4],
                        "top_pick": r[7],
                        "top_probability": norm_val(r[8]),
                        "prediction_context": bpctx,
                        "actual_home_points": r[9],
                        "actual_away_points": r[10],
                        "top_pick_correct": r[11],
                        "created_at": r[13],
                    }

                cur.execute(
                    """
                    SELECT game_id, home_team, away_team, league, top_pick, top_probability,
                           prediction_context, actual_home_points, actual_away_points, top_pick_correct, created_at
                    FROM basketball_predictions
                    """
                )
                dest_bp_rows = cur.fetchall()
                dest_bp = {}
                for r in dest_bp_rows:
                    gid = r[0]
                    dest_bp[gid] = {
                        "game_id": gid,
                        "home_team": r[1],
                        "away_team": r[2],
                        "league": r[3],
                        "top_pick": r[4],
                        "top_probability": norm_val(r[5]),
                        "prediction_context": r[6],
                        "actual_home_points": r[7],
                        "actual_away_points": r[8],
                        "top_pick_correct": r[9],
                        "created_at": r[10],
                    }

                report["basketball_predictions"]["destination"] = len(dest_bp)
                if len(src_bp) != len(dest_bp):
                    errors.append(f"basketball_predictions count mismatch: source={len(src_bp)}, dest={len(dest_bp)}")

                for gid, src_rec in src_bp.items():
                    if gid not in dest_bp:
                        errors.append(f"basketball_predictions missing destination record: game_id={gid}")
                        continue
                    dest_rec = dest_bp[gid]
                    for k, src_v in src_rec.items():
                        dest_v = dest_rec.get(k)
                        if k == "created_at" and src_v is None and dest_v is not None:
                            continue
                        if src_v != dest_v:
                            errors.append(f"basketball_predictions mismatch game_id={gid} field '{k}': source={src_v!r}, dest={dest_v!r}")

                for gid in dest_bp:
                    if gid not in src_bp:
                        errors.append(f"basketball_predictions unexpected destination record: game_id={gid}")

                report["basketball_predictions"]["status"] = "EXACT_MATCH" if len(src_bp) == len(dest_bp) else f"COUNT_MISMATCH (source={len(src_bp)}, dest={len(dest_bp)})"

                # -----------------------------------------------------
                # C. Elo Ratings Verification
                # -----------------------------------------------------
                src_e = {}
                for r in e_rows:
                    tid = r[0]
                    src_e[tid] = {
                        "team_id": tid,
                        "team_name": r[1],
                        "rating": norm_val(r[2]),
                        "updated_at": r[3],
                    }

                cur.execute("SELECT team_id, team_name, rating, updated_at FROM elo_ratings")
                dest_e_rows = cur.fetchall()
                dest_e = {}
                for r in dest_e_rows:
                    tid = r[0]
                    dest_e[tid] = {
                        "team_id": tid,
                        "team_name": r[1],
                        "rating": norm_val(r[2]),
                        "updated_at": r[3],
                    }

                report["elo_ratings"]["destination"] = len(dest_e)
                if len(src_e) != len(dest_e):
                    errors.append(f"elo_ratings count mismatch: source={len(src_e)}, dest={len(dest_e)}")

                for tid, src_rec in src_e.items():
                    if tid not in dest_e:
                        errors.append(f"elo_ratings missing destination record: team_id={tid}")
                        continue
                    dest_rec = dest_e[tid]
                    for k, src_v in src_rec.items():
                        dest_v = dest_rec.get(k)
                        if k == "updated_at" and src_v is None and dest_v is not None:
                            continue
                        if src_v != dest_v:
                            errors.append(f"elo_ratings mismatch team_id={tid} field '{k}': source={src_v!r}, dest={dest_v!r}")

                for tid in dest_e:
                    if tid not in src_e:
                        errors.append(f"elo_ratings unexpected destination record: team_id={tid}")

                report["elo_ratings"]["status"] = "EXACT_MATCH" if len(src_e) == len(dest_e) else f"COUNT_MISMATCH (source={len(src_e)}, dest={len(dest_e)})"

                # -----------------------------------------------------
                # D. Bot Memory Verification
                # -----------------------------------------------------
                src_tg = {}
                for cid, role, text, ts in tg_entries:
                    key = (cid, role, text, ts)
                    src_tg[key] = {"chat_id": cid, "role": role, "text": text, "timestamp": ts}

                cur.execute("SELECT chat_id, role, text, timestamp FROM bot_memory")
                dest_tg_rows = cur.fetchall()
                dest_tg = {}
                for cid, role, text, ts in dest_tg_rows:
                    key = (cid, role, text, ts)
                    dest_tg[key] = {"chat_id": cid, "role": role, "text": text, "timestamp": ts}

                report["bot_memory"]["destination"] = len(dest_tg)
                if len(src_tg) != len(dest_tg):
                    errors.append(f"bot_memory count mismatch: source={len(src_tg)}, dest={len(dest_tg)}")

                for key, src_rec in src_tg.items():
                    if key not in dest_tg:
                        errors.append(f"bot_memory missing destination record: {key}")
                        continue
                    dest_rec = dest_tg[key]
                    for k, src_v in src_rec.items():
                        dest_v = dest_rec.get(k)
                        if src_v != dest_v:
                            errors.append(f"bot_memory mismatch {key} field '{k}': source={src_v!r}, dest={dest_v!r}")

                for key in dest_tg:
                    if key not in src_tg:
                        errors.append(f"bot_memory unexpected destination record: {key}")

                report["bot_memory"]["status"] = "EXACT_MATCH" if len(src_tg) == len(dest_tg) else f"COUNT_MISMATCH (source={len(src_tg)}, dest={len(dest_tg)})"

                # -----------------------------------------------------
                # E. API Cache Verification
                # -----------------------------------------------------
                src_c = {}
                for r in cache_rows:
                    ckey = r[0]
                    src_c[ckey] = {
                        "cache_key": ckey,
                        "endpoint": r[1],
                        "request_params": norm_json(r[2]),
                        "response_payload": norm_json(r[3]),
                        "fetched_at": r[4],
                        "expires_at": r[5],
                    }

                cur.execute("SELECT cache_key, endpoint, request_params, response_payload, fetched_at, expires_at FROM api_cache")
                dest_c_rows = cur.fetchall()
                dest_c = {}
                for r in dest_c_rows:
                    ckey = r[0]
                    dest_c[ckey] = {
                        "cache_key": ckey,
                        "endpoint": r[1],
                        "request_params": norm_json(r[2]),
                        "response_payload": norm_json(r[3]),
                        "fetched_at": r[4],
                        "expires_at": r[5],
                    }

                report["api_cache"]["destination"] = len(dest_c)
                if len(src_c) != len(dest_c):
                    errors.append(f"api_cache count mismatch: source={len(src_c)}, dest={len(dest_c)}")

                for ckey, src_rec in src_c.items():
                    if ckey not in dest_c:
                        errors.append(f"api_cache missing destination record: cache_key={ckey}")
                        continue
                    dest_rec = dest_c[ckey]
                    for k, src_v in src_rec.items():
                        dest_v = dest_rec.get(k)
                        if src_v != dest_v:
                            errors.append(f"api_cache mismatch cache_key={ckey} field '{k}': source={src_v!r}, dest={dest_v!r}")

                for ckey in dest_c:
                    if ckey not in src_c:
                        errors.append(f"api_cache unexpected destination record: cache_key={ckey}")

                report["api_cache"]["status"] = "EXACT_MATCH" if len(src_c) == len(dest_c) else f"COUNT_MISMATCH (source={len(src_c)}, dest={len(dest_c)})"

                # -----------------------------------------------------
                # F. API Request Counts Verification
                # -----------------------------------------------------
                src_rc = {}
                for r in rc_rows:
                    key = (r[0], r[1], r[3]) # (provider, request_timestamp, endpoint)
                    src_rc[key] = {
                        "provider": r[0],
                        "request_timestamp": r[1],
                        "request_date": r[2],
                        "endpoint": r[3],
                    }

                cur.execute("SELECT provider, request_timestamp, request_date, endpoint FROM api_request_counts")
                dest_rc_rows = cur.fetchall()
                dest_rc = {}
                for r in dest_rc_rows:
                    key = (r[0], r[1], r[3])
                    dest_rc[key] = {
                        "provider": r[0],
                        "request_timestamp": r[1],
                        "request_date": r[2],
                        "endpoint": r[3],
                    }

                report["api_request_counts"]["destination"] = len(dest_rc)
                if len(src_rc) != len(dest_rc):
                    errors.append(f"api_request_counts count mismatch: source={len(src_rc)}, dest={len(dest_rc)}")

                for key, src_rec in src_rc.items():
                    if key not in dest_rc:
                        errors.append(f"api_request_counts missing destination record: {key}")
                        continue
                    dest_rec = dest_rc[key]
                    for k, src_v in src_rec.items():
                        dest_v = dest_rec.get(k)
                        if src_v != dest_v:
                            errors.append(f"api_request_counts mismatch {key} field '{k}': source={src_v!r}, dest={dest_v!r}")

                for key in dest_rc:
                    if key not in src_rc:
                        errors.append(f"api_request_counts unexpected destination record: {key}")

                report["api_request_counts"]["status"] = "EXACT_MATCH" if len(src_rc) == len(dest_rc) else f"COUNT_MISMATCH (source={len(src_rc)}, dest={len(dest_rc)})"

            if errors:
                report["key_field_verification"] = f"VERIFICATION_FAILED ({len(errors)} mismatches: {errors[:3]})"
                report["verification"] = "VERIFICATION_FAILED"
            else:
                report["key_field_verification"] = "VERIFIED_PASSED"
                report["verification"] = "VERIFIED_ALL_DATASETS"

        else:
            report["key_field_verification"] = "DRY_RUN_PASSED"
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
    parser.add_argument("--verify-only", action="store_true", help="Verify target database contents against source SQLite without writing.")

    args = parser.parse_args()

    try:
        res = migrate(
            sqlite_path=args.sqlite_db,
            target_url=args.target_url,
            dry_run=args.dry_run,
            verify_only=args.verify_only,
        )
        if res.get("verification") in ("VERIFIED_ALL_DATASETS", "DRY_RUN_PASSED"):
            sys.exit(0)
        else:
            print("Migration completed with verification warnings.", file=sys.stderr)
            sys.exit(1)
    except Exception as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        sys.exit(1)
