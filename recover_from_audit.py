"""Persist statistics for fixtures whose ESPN event was already identified.

The statless audit matched every remaining fixture against ESPN's all-competition
scoreboard on independent evidence (both club names and the final score). This
takes those sightings, fetches the summary for the exact event id, re-validates
the competition named in the summary header, and writes the published statistics
through ``storage.save_historical_enrichment``.

Nothing is invented: a fixture is written only when ESPN publishes team
statistics for the matched event and the header names the expected competition.
Every acceptance and rejection is recorded in an evidence file.

Usage:
    python recover_from_audit.py --dry-run --limit 20
    python recover_from_audit.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor

import espn_football_stats as efs
import storage

EVIDENCE = os.path.join(".api_cache", "reports", "audit_recovery.json")
SOURCE = "espn_soccer_summary"


def load_sightings():
    with open(os.path.join(".api_cache", "reports", "statless_fixture_audit.json"),
              "r", encoding="utf-8") as handle:
        evidence = json.load(handle)
    wanted = {}
    for record in evidence["fixtures"]:
        if not record.get("sightings"):
            continue
        wanted[record["fixture_id"]] = record
    return wanted


def load_fixtures(fixture_ids):
    conn, backend = storage._connect()
    if backend != "postgres":
        raise SystemExit("refusing to run outside PostgreSQL/Neon")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.fixture_id, f.league_id, f.season, f.home_team, f.away_team,
                       f.home_goals, f.away_goals,
                       to_char(f.kickoff_at::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD')
                  FROM historical_fixtures f
                 WHERE NOT EXISTS (SELECT 1 FROM historical_fixture_enrichment e
                                    WHERE e.fixture_id = f.fixture_id)
                   AND f.fixture_id = ANY(%s)
                """,
                (list(fixture_ids),),
            )
            keys = ["fixture_id", "league_id", "season", "home_team", "away_team",
                    "home_goals", "away_goals", "kickoff_date"]
            return [dict(zip(keys, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def recover(job):
    fixture, sightings = job
    league_id = fixture["league_id"]
    slug = efs.ESPN_LEAGUES.get(league_id)
    if not slug:
        return fixture, None, "league_not_supported"
    wanted = efs.EXPECTED_COMPETITION.get(league_id, ())
    reasons = []
    for sighting in sightings:
        event_id = sighting.get("event_id")
        if not event_id:
            continue
        event = {
            "event_id": event_id,
            "date": sighting.get("date"),
            "home_name": sighting.get("home"),
            "away_name": sighting.get("away"),
            "home_score": int(float(sighting["home_score"])) if sighting.get("home_score") is not None else None,
            "away_score": int(float(sighting["away_score"])) if sighting.get("away_score") is not None else None,
        }
        url = f"{efs.SITE}/all/summary?event={event_id}"
        path = os.path.join(efs.CACHE_DIR, f"{event_id}.json")
        try:
            payload = efs.cached_json(path, url)
        except Exception as exc:
            reasons.append(f"summary_error:{type(exc).__name__}")
            continue
        header = ((payload.get("header") or {}).get("league") or {}).get("name") or ""
        competition = header.lower()
        if "women" in competition or not any(token in competition for token in wanted):
            reasons.append(f"competition_mismatch:{header}")
            continue
        stats = efs.parse_summary(payload)
        item = efs.build_item(fixture, event, stats)
        if item is None:
            reasons.append(f"no_published_statistics:{header}")
            continue
        item["espn_match"]["competition"] = header
        return fixture, item, None
    return fixture, None, (reasons[0] if reasons else "no_usable_sighting")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    wanted = load_sightings()
    fixtures = load_fixtures(wanted)
    if args.limit:
        fixtures = fixtures[: args.limit]
    jobs = [(fixture, wanted[fixture["fixture_id"]]["sightings"]) for fixture in fixtures]
    print(json.dumps({"candidates": len(jobs), "apply": args.apply}), flush=True)

    counters = {"written": 0, "skipped": 0}
    reasons = {}
    buffer = []
    evidence = []

    def flush():
        if not buffer:
            return
        batch = list(buffer)
        buffer.clear()
        storage.save_historical_enrichment(batch, source=SOURCE)
        counters["written"] += len(batch)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for index, (fixture, item, reason) in enumerate(pool.map(recover, jobs), 1):
            if item is None:
                counters["skipped"] += 1
                key = (reason or "unknown").split(":")[0]
                reasons[key] = reasons.get(key, 0) + 1
                evidence.append({"fixture_id": fixture["fixture_id"],
                                 "league_id": fixture["league_id"],
                                 "matchup": f"{fixture['home_team']} v {fixture['away_team']}",
                                 "outcome": "skipped", "reason": reason})
            else:
                evidence.append({"fixture_id": fixture["fixture_id"],
                                 "league_id": fixture["league_id"],
                                 "matchup": f"{fixture['home_team']} v {fixture['away_team']}",
                                 "outcome": "accepted",
                                 "event_id": item["espn_match"]["event_id"],
                                 "competition": item["espn_match"]["competition"]})
                if args.apply:
                    buffer.append(item)
                    if len(buffer) >= 25:
                        flush()
            if index % 100 == 0:
                print(json.dumps(counters), flush=True)

    if args.apply:
        flush()

    os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
    with open(EVIDENCE, "w", encoding="utf-8") as handle:
        json.dump({"counters": counters, "reasons": reasons, "fixtures": evidence},
                  handle, indent=2)
    print(json.dumps({"counters": counters, "reasons": reasons}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
