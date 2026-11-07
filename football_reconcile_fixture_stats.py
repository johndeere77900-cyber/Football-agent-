"""Reconcile statless fixture rows that duplicate an already-enriched match.

A real-world match can be stored twice under different providers, with the two
rows carrying different canonical team ids ("football_team_m_gladbach" from one
feed, the CSV-specific id from another). Only one row then has statistics, which
makes an enriched match look like a statistics gap.

This job copies the verified statistics of the enriched row onto its duplicate
row under ``source='reconciled'``. Because the canonical ids disagree, pairing is
established from independent evidence instead:

- same competition and season,
- same UTC kickoff date,
- same final score,
- alias-aware agreement on both team names.

A pair is only used when exactly one enriched candidate is the best match in its
bucket; a tie is reported as ambiguous and left untouched. Nothing is deleted and
an existing enrichment row is never overwritten.

Dry-run by default; ``--apply`` writes.
"""

import argparse
import collections
import difflib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage
import team_identity

STAT_COLUMNS = list(storage._ENRICHMENT_STAT_COLUMNS)
MIN_NAME_SCORE = 0.80


def load_fixtures(cur):
    cur.execute("""
        SELECT fixture_id, league_id, season, home_team, away_team, home_goals, away_goals,
               to_char(kickoff_at::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS kickoff_date,
               source
        FROM historical_fixtures
    """)
    keys = ["fixture_id", "league_id", "season", "home_team", "away_team",
            "home_goals", "away_goals", "kickoff_date", "source"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def load_enrichment(cur):
    cur.execute(f"""
        SELECT fixture_id, source, raw_json, provider_version, field_mask, {', '.join(STAT_COLUMNS)}
        FROM historical_fixture_enrichment
        WHERE field_mask IS NOT NULL
    """)
    keys = ["fixture_id", "source", "raw_json", "provider_version", "field_mask"] + STAT_COLUMNS
    return {row[0]: dict(zip(keys, row)) for row in cur.fetchall()}


def name_score(left, right):
    """Alias-aware agreement between two club names.

    Short names are common in CSV feeds ("M'gladbach", "NAC"), so containment of a
    meaningful fragment counts as agreement; otherwise a high similarity ratio is
    required. Nothing here rewrites or guesses a name, it only compares two
    strings that already describe the same slot of the same fixture.
    """
    a = team_identity.normalize_team_name(left or "")
    b = team_identity.normalize_team_name(right or "")
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long = sorted((a.replace(" ", ""), b.replace(" ", "")), key=len)
    if len(short) >= 3 and short in long:
        return 0.95
    return difflib.SequenceMatcher(None, a, b).ratio()


def pair_score(statless, enriched):
    home = name_score(statless["home_team"], enriched["home_team"])
    away = name_score(statless["away_team"], enriched["away_team"])
    if home < MIN_NAME_SCORE or away < MIN_NAME_SCORE:
        return None
    return home + away


def bucket_key(fixture):
    if not fixture["kickoff_date"] or fixture["home_goals"] is None or fixture["away_goals"] is None:
        return None
    return (fixture["league_id"], fixture["season"], fixture["kickoff_date"],
            fixture["home_goals"], fixture["away_goals"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sample", type=int, default=5)
    args = parser.parse_args(argv)

    conn, db_type = storage._connect()
    if db_type != "postgres":
        raise SystemExit(f"refusing to run against {db_type}")

    with conn.cursor() as cur:
        fixtures = load_fixtures(cur)
        enrichment = load_enrichment(cur)

    buckets = collections.defaultdict(lambda: {"enriched": [], "statless": []})
    for fixture in fixtures:
        record = enrichment.get(fixture["fixture_id"])
        if record is None:
            key = bucket_key(fixture)
            if key is None:
                continue
            buckets[key]["statless"].append(fixture)
        else:
            # An enriched row never needs a donor, but it is a candidate donor.
            key = bucket_key(fixture)
            if key is not None:
                buckets[key]["enriched"].append((fixture, record))

    source_preference = {"football_data_csv": 0, "api_football": 1, "football_data_org": 2,
                         "soccerdata": 3, "openfootball_json": 4, "openfootball_internationals": 5}

    planned, ambiguous, unmatched = [], 0, 0
    for bucket in buckets.values():
        if not bucket["statless"]:
            continue
        # One real match can be enriched under several rows (a provider row and its
        # own reconciled copies). Collapsing them by their own name pair leaves one
        # donor per real match, so a tie then means two different matches and is
        # correctly refused.
        unique = {}
        for donor_fixture, donor in bucket["enriched"]:
            name_pair = (team_identity.normalize_team_name(donor_fixture["home_team"]),
                         team_identity.normalize_team_name(donor_fixture["away_team"]))
            rank = (source_preference.get(donor["source"], 9),
                    -len([c for c in STAT_COLUMNS if donor.get(c) is not None]))
            if name_pair not in unique or rank < unique[name_pair][0]:
                unique[name_pair] = (rank, donor_fixture, donor)
        candidates = [(item[1], item[2]) for item in unique.values()]
        used = set()
        for fixture in bucket["statless"]:
            scored = []
            for donor_fixture, donor in candidates:
                score = pair_score(fixture, donor_fixture)
                if score is not None:
                    # A donor is never used twice within one bucket.
                    scored.append((score, -donor_fixture["fixture_id"], donor_fixture, donor))
            scored.sort(reverse=True)
            if not scored:
                unmatched += 1
                continue
            if len(scored) > 1 and scored[0][0] == scored[1][0]:
                ambiguous += 1
                continue
            score, _, donor_fixture, donor = scored[0]
            if donor_fixture["fixture_id"] in used:
                ambiguous += 1
                continue
            used.add(donor_fixture["fixture_id"])
            planned.append((fixture, donor))

    statless_total = sum(1 for f in fixtures if f["fixture_id"] not in enrichment)
    print(json.dumps({
        "fixtures_total": len(fixtures),
        "enrichment_rows": len(enrichment),
        "statless_total": statless_total,
        "reconcilable_duplicates": len(planned),
        "ambiguous_identity": ambiguous,
        "no_matching_enriched_row": unmatched,
        "genuine_remaining_gap": statless_total - len(planned) - ambiguous,
    }, indent=2))

    for fixture, donor in planned[:args.sample]:
        print(f"  fixture {fixture['fixture_id']} ({fixture['source']}) "
              f"{fixture['home_team']} v {fixture['away_team']} {fixture['home_goals']}-{fixture['away_goals']} "
              f"{fixture['kickoff_date']} <- {donor['fixture_id']} ({donor['source']})")

    if not args.apply:
        print("dry-run: no rows written")
        return 0

    insert_sql = f"""
        INSERT INTO historical_fixture_enrichment
            (fixture_id, raw_json, fetched_at, source, {', '.join(STAT_COLUMNS)}, field_mask, provider_version)
        VALUES (%s, %s, %s, 'reconciled', {', '.join(['%s'] * len(STAT_COLUMNS))}, %s, %s)
        ON CONFLICT (source, fixture_id) DO NOTHING
    """
    now = storage._utc_now()

    def _raw(value):
        # The payload column may surface as text or as parsed json depending on
        # the column type; store it as text in either case.
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    payload = [
        (fixture["fixture_id"], _raw(donor["raw_json"]), now)
        + tuple(donor.get(column) for column in STAT_COLUMNS)
        + (donor["field_mask"], f"reconciled_from_{donor['fixture_id']}")
        for fixture, donor in planned
    ]

    written = 0
    with conn.cursor() as cur:
        for start in range(0, len(payload), 500):
            cur.executemany(insert_sql, payload[start:start + 500])
            written += cur.rowcount
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM historical_fixture_enrichment WHERE source='reconciled'")
        reconciled_total = cur.fetchone()[0]
        cur.execute("""
            SELECT count(*) FROM historical_fixtures f
            LEFT JOIN historical_fixture_enrichment e ON e.fixture_id=f.fixture_id
            WHERE e.fixture_id IS NULL
        """)
        still_statless = cur.fetchone()[0]

    print(json.dumps({"requested": len(payload), "inserted": written,
                      "reconciled_rows_total": reconciled_total,
                      "statless_remaining": still_statless}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
