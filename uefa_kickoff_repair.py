"""Repair the kickoff time of fixtures imported from the UEFA match API.

The first import preferred UEFA's date-only ``kickOffTime.date`` field, so all
1,093 rows store a correct date but 00:00 UTC. The parser is fixed; this job
repairs the stored rows from the same authoritative provider response.

Safety rules:
- only rows with ``source = 'uefa_match_v5'`` and a deterministic UEFA fixture id
  are touched,
- a row is updated only when the provider's UTC date equals the stored date, so a
  match id mismatch cannot move a fixture to another day,
- a missing or unparsable ``dateTime`` leaves the row untouched; no time is
  guessed or interpolated,
- the update is idempotent: re-running it writes the same values.

Dry-run by default; ``--apply`` writes.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage

BASE = "https://match.uefa.com/v5/matches"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
COMPETITIONS = {2: 1, 4: 3}
# (league_id, stored season, UEFA seasonYear), matching the backfill mapping.
TARGETS = [(2, 2019, 2020), (2, 2025, 2026), (4, 2020, 2020), (4, 2024, 2024)]


def stable_id(prefix, value):
    return int(hashlib.sha256(f"{prefix}_{value}".encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


def fetch(competition_id, season_year):
    matches, offset = [], 0
    while True:
        query = urllib.parse.urlencode({"competitionId": competition_id,
                                        "seasonYear": season_year,
                                        "limit": 500, "offset": offset})
        request = urllib.request.Request(f"{BASE}?{query}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=40) as response:
            page = json.loads(response.read().decode("utf-8"))
        if not isinstance(page, list) or not page:
            break
        matches.extend(page)
        if len(page) < 500:
            break
        offset += 500
    return matches


def kickoff_utc(match):
    block = match.get("kickOffTime")
    if not isinstance(block, dict):
        return None
    raw = block.get("dateTime")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    conn, db_type = storage._connect()
    if db_type != "postgres":
        raise SystemExit(f"refusing to run against {db_type}")

    totals = {"fetched": 0, "candidates": 0, "updated": 0,
              "no_datetime": 0, "date_mismatch": 0, "not_stored": 0}
    with conn.cursor() as cur:
        for league_id, season, season_year in TARGETS:
            try:
                matches = fetch(COMPETITIONS[league_id], season_year)
            except Exception as exc:
                print(json.dumps({"league_id": league_id, "season": season,
                                  "error": f"{type(exc).__name__}: {exc}"}), flush=True)
                continue
            pending = []
            for match in matches:
                totals["fetched"] += 1
                when = kickoff_utc(match)
                if when is None:
                    totals["no_datetime"] += 1
                    continue
                fid = stable_id("uefa_match", match.get("id"))
                cur.execute("""
                    SELECT to_char(kickoff_at::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD')
                    FROM historical_fixtures
                    WHERE fixture_id = %s AND source = 'uefa_match_v5'
                """, (fid,))
                row = cur.fetchone()
                if not row:
                    totals["not_stored"] += 1
                    continue
                totals["candidates"] += 1
                if row[0] != when.strftime("%Y-%m-%d"):
                    totals["date_mismatch"] += 1
                    continue
                pending.append((when.isoformat(), fid))
            if args.apply and pending:
                cur.executemany("""
                    UPDATE historical_fixtures SET kickoff_at = %s
                    WHERE fixture_id = %s AND source = 'uefa_match_v5'
                """, pending)
                totals["updated"] += cur.rowcount
            print(json.dumps({"league_id": league_id, "season": season,
                              "matches": len(matches), "candidates": len(pending)}), flush=True)
        if args.apply:
            conn.commit()

    with conn.cursor() as cur:
        cur.execute("""SELECT count(*),
                              count(*) FILTER (WHERE to_char(kickoff_at::timestamptz AT TIME ZONE 'UTC','HH24:MI')='00:00')
                       FROM historical_fixtures WHERE source='uefa_match_v5'""")
        total_rows, still_midnight = cur.fetchone()
    totals["stored_rows"] = total_rows
    totals["still_midnight"] = still_midnight
    print(json.dumps(totals, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
