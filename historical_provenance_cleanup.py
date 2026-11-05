"""Retire redundant provenance-only rows from the canonical enrichment table.

The canonical table mixes two different things: records that carry verified match
statistics, and records that only assert which provider touched a fixture. The
latter add no statistics, and every one of them overlaps a fixture that already
holds genuine statistics in another row.

This job archives such rows into ``historical_enrichment_retired`` (source
identity, timestamp, mask, provider version, raw payload and the retirement
reason) and only then removes them from the canonical table, inside a single
transaction. Nothing is deleted before the archive copy is durable.

Refusals:
- A candidate whose own payload contains statistics is never treated as
  provenance-only; it is reported instead of deleted.
- A candidate that is the only row for its fixture is never selected.
- Historical fixtures are never touched.

Dry-run by default; pass --apply to commit.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage

PROVENANCE_SOURCES = ("api_football", "football_data_org")

STAT_COLUMNS = (
    "home_shots", "away_shots",
    "home_shots_on_target", "away_shots_on_target",
    "home_corners", "away_corners",
    "home_yellow_cards", "away_yellow_cards",
    "home_red_cards", "away_red_cards",
    "home_possession", "away_possession",
    "home_xg", "away_xg",
)

RETIRED_DDL = """
    CREATE TABLE IF NOT EXISTS historical_enrichment_retired (
        fixture_id BIGINT NOT NULL,
        source TEXT NOT NULL,
        fetched_at TEXT,
        field_mask TEXT,
        provider_version TEXT,
        raw_json JSONB,
        retired_at TEXT NOT NULL,
        retire_reason TEXT NOT NULL,
        PRIMARY KEY (fixture_id, source)
    )
"""

ALL_NULL = " AND ".join(f"e.{column} IS NULL" for column in STAT_COLUMNS)

CANDIDATE_PREDICATE = f"""
    e.source IN ({', '.join(repr(s) for s in PROVENANCE_SOURCES)})
    AND e.field_mask IS NULL
    AND {ALL_NULL}
    AND EXISTS (
        SELECT 1 FROM historical_fixture_enrichment s
         WHERE s.fixture_id = e.fixture_id
           AND s.home_shots IS NOT NULL)
"""

# A payload that itself carries statistics is not provenance-only.
PAYLOAD_HAS_STATS = """
    CASE WHEN e.raw_json ? 'statistics'
          AND e.raw_json->'statistics' <> 'null'::jsonb
          AND e.raw_json->'statistics' <> '{}'::jsonb
          AND e.raw_json->'statistics' <> '[]'::jsonb
         THEN TRUE ELSE FALSE END
"""


def _ensure_table(cur):
    cur.execute(RETIRED_DDL)


def survey(cur):
    """Count candidates and any exception that must not be deleted blindly."""
    _ensure_table(cur)
    cur.execute(f"SELECT count(*) FROM historical_fixture_enrichment e WHERE {CANDIDATE_PREDICATE}")
    candidates = cur.fetchone()[0]
    cur.execute(f"""
        SELECT count(*) FROM historical_fixture_enrichment e
         WHERE {CANDIDATE_PREDICATE} AND {PAYLOAD_HAS_STATS}
    """)
    payload_exceptions = cur.fetchone()[0]
    # Refusal guard: a candidate fixture must keep a statistics row after the
    # provenance-only row is removed. This must always be 0, because the
    # candidate predicate itself requires a statistics-bearing peer.
    cur.execute(f"""
        SELECT count(*) FROM (
            SELECT DISTINCT e.fixture_id FROM historical_fixture_enrichment e
             WHERE {CANDIDATE_PREDICATE}) c
         WHERE NOT EXISTS (
            SELECT 1 FROM historical_fixture_enrichment s
             WHERE s.fixture_id = c.fixture_id AND s.home_shots IS NOT NULL)
    """)
    orphan_risk = cur.fetchone()[0]
    cur.execute("""
        SELECT count(*) FROM historical_fixture_enrichment
         WHERE home_shots IS NOT NULL
    """)
    stats_rows_before = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM historical_fixture_enrichment")
    total_before = cur.fetchone()[0]
    return {
        "candidates": candidates,
        "payload_statistics_exceptions": payload_exceptions,
        "fixtures_without_retained_statistics": orphan_risk,
        "statistics_rows_before": stats_rows_before,
        "enrichment_rows_before": total_before,
    }


def apply_cleanup(cur):
    """Archive then delete candidates in the caller's transaction."""
    retired_at = datetime.now(timezone.utc).isoformat()
    reason = (
        "provenance_only_row_superseded_by_verified_statistics: no statistic "
        "value in the row or its payload; the same fixture retains a verified "
        "statistics row; archived to preserve source identity and audit evidence"
    )
    cur.execute(f"""
        INSERT INTO historical_enrichment_retired
            (fixture_id, source, fetched_at, field_mask, provider_version,
             raw_json, retired_at, retire_reason)
        SELECT e.fixture_id, e.source, e.fetched_at, e.field_mask,
               e.provider_version, e.raw_json, %s, %s
          FROM historical_fixture_enrichment e
         WHERE {CANDIDATE_PREDICATE}
           AND NOT ({PAYLOAD_HAS_STATS})
        ON CONFLICT (fixture_id, source) DO NOTHING
    """, (retired_at, reason))
    archived = cur.rowcount
    cur.execute(f"""
        DELETE FROM historical_fixture_enrichment e
         WHERE {CANDIDATE_PREDICATE}
           AND NOT ({PAYLOAD_HAS_STATS})
    """)
    deleted = cur.rowcount
    return archived, deleted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    conn, db_type = storage._connect()
    if db_type != "postgres":
        raise SystemExit("cleanup targets the PostgreSQL/Neon store only")
    try:
        with conn.cursor() as cur:
            before = survey(cur)
            print(json.dumps(before, indent=2))
            if not args.apply:
                conn.rollback()
                print("dry-run only (pass --apply to archive and delete)")
                return 0

            archived, deleted = apply_cleanup(cur)

            after = {}
            cur.execute("SELECT count(*) FROM historical_fixture_enrichment")
            after["enrichment_rows_after"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM historical_fixture_enrichment WHERE home_shots IS NOT NULL")
            after["statistics_rows_after"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM historical_enrichment_retired")
            after["retired_rows"] = cur.fetchone()[0]
            cur.execute("""
                SELECT count(*) FROM historical_fixture_enrichment e
                 WHERE e.home_shots IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM historical_fixture_enrichment s
                        WHERE s.fixture_id = e.fixture_id
                          AND s.home_shots IS NOT NULL)
            """)
            after["statistics_rows_without_peer"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM historical_fixtures")
            after["fixtures"] = cur.fetchone()[0]

            lost = before["statistics_rows_before"] - after["statistics_rows_after"]
            if lost != 0:
                conn.rollback()
                raise SystemExit(f"ABORT: {lost} statistics rows would be lost; rolled back")

            conn.commit()
            print(json.dumps({"archived": archived, "deleted": deleted, **after}, indent=2))
            return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
