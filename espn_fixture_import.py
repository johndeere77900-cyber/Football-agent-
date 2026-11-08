"""Fixture acquisition from ESPN scoreboards for competitions with no other source.

The UEFA API cannot attribute the FIFA World Cup or the Nations League (id
discovery returns empty competition names), so these seasons are built from
ESPN's own competition scoreboard. The job is deliberately conservative:

- every candidate must come from the competition's own scoreboard, whose
  ``leagues[0].name`` must state that competition,
- an event already stored for the same league, season, kickoff date and club
  pairing is a duplicate and is skipped, whatever its provider id,
- unplayed events are skipped unless ``--include-scheduled`` is given,
- fixture ids are derived from the ESPN event id so a rerun is idempotent.

Dry-run by default: it reports what it would insert and records the evidence
under ``.api_cache/reports``. ``--apply`` performs the inserts.
"""

import argparse
import collections
import datetime
import json
import os
import re
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import espn_football_stats as espn
import storage

# Competition name the scoreboard must report for each supported league.
EXPECTED_NAME = {
    1: "FIFA World Cup",
    5: "UEFA Nations League",
}
SOURCE = "espn_scoreboard"
ID_BASE = 9_000_000_000_000_000
REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".api_cache", "reports", "espn_fixture_import.json")


def norm(name):
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", text.lower())


def dates(start, end):
    day = datetime.date.fromisoformat(start)
    last = datetime.date.fromisoformat(end)
    while day <= last:
        yield day.strftime("%Y%m%d")
        day += datetime.timedelta(days=1)


def event_payload(event, payload):
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    competition = competitions[0]
    home, away = espn.competitor(competition, "home"), espn.competitor(competition, "away")
    if not home or not away:
        return None
    status = (event.get("status") or {}).get("type") or {}
    return {
        "event_id": str(event.get("id") or ""),
        "kickoff": event.get("date"),
        "home": (home.get("team") or {}).get("displayName"),
        "away": (away.get("team") or {}).get("displayName"),
        "home_goals": home.get("score"),
        "away_goals": away.get("score"),
        "completed": bool(status.get("completed")),
        "status_short": status.get("shortDetail") or status.get("name"),
        "competition": ((payload.get("leagues") or [{}])[0] or {}).get("name"),
        "season_slug": ((event.get("season") or {}).get("slug")),
        "raw": event,
    }


def collect(slug, windows):
    found, rejected, per_window, failed = {}, collections.Counter(), [], []
    for start, end in windows:
        before = len(found)
        for day in dates(start, end):
            payload = {}
            for attempt in range(4):
                payload = espn.scoreboard(slug, day)
                if payload:
                    break
                time.sleep(1.5 * (attempt + 1))
            if not payload:
                # An empty body means the request failed, not that the day had no
                # matches: a scoreboard always answers with its competition block.
                failed.append(day)
                continue
            for event in payload.get("events") or []:
                parsed = event_payload(event, payload)
                if not parsed:
                    rejected["no_competitors"] += 1
                    continue
                found.setdefault(parsed["event_id"], parsed)
        per_window.append({"window": start + ".." + end, "events": len(found) - before})
    return found, rejected, per_window, failed


def existing_index(cur, league_id, season):
    cur.execute(
        """select fixture_id, lower(coalesce(home_team,'')), lower(coalesce(away_team,'')),
                  to_char(kickoff_at::timestamptz at time zone 'utc','YYYY-MM-DD')
           from historical_fixtures where league_id=%s and season=%s""",
        (league_id, season),
    )
    index = {}
    for fixture_id, home, away, day in cur.fetchall():
        index[fixture_id] = True
        index[(norm(home), norm(away), day)] = fixture_id
    return index


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--league", type=int, required=True)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--window", action="append", required=True,
                        help="YYYY-MM-DD:YYYY-MM-DD, repeatable")
    parser.add_argument("--include-scheduled", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if args.league not in EXPECTED_NAME:
        raise SystemExit("unsupported league: no competition name is pinned for " + str(args.league))
    expected = EXPECTED_NAME[args.league]
    windows = []
    for window in args.window:
        start, _, end = window.partition(":")
        windows.append((start, end))

    conn, _ = storage._connect()
    cur = conn.cursor()
    index = existing_index(cur, args.league, args.season)

    events, rejected, per_window, failed = collect(args.slug, windows)
    if failed:
        raise SystemExit("incomplete fetch, refusing to report coverage: failed dates " + ", ".join(failed[:20]))
    counters = collections.Counter()
    rows = []
    for event in events.values():
        counters["events"] += 1
        name = event.get("competition") or ""
        if expected and expected.lower() not in name.lower():
            counters["competition_mismatch"] += 1
            continue
        if not event["completed"] and not args.include_scheduled:
            counters["not_played"] += 1
            continue
        if not event["kickoff"]:
            counters["no_kickoff"] += 1
            continue
        day = event["kickoff"][:10]
        key = (norm(event["home"]), norm(event["away"]), day)
        fixture_id = ID_BASE + int(event["event_id"]) if event["event_id"].isdigit() else None
        if not fixture_id:
            counters["no_event_id"] += 1
            continue
        if key in index or fixture_id in index:
            counters["duplicate"] += 1
            continue
        index[key] = fixture_id
        index[fixture_id] = True
        try:
            home_goals, away_goals = int(event["home_goals"]), int(event["away_goals"])
        except (TypeError, ValueError):
            home_goals = away_goals = None
        rows.append({
            "fixture_id": fixture_id,
            "league_id": args.league,
            "season": args.season,
            "kickoff_at": event["kickoff"],
            "status_short": event["status_short"],
            "home_team": event["home"],
            "away_team": event["away"],
            "home_goals": home_goals,
            "away_goals": away_goals,
            "raw_json": json.dumps({"event_id": event["event_id"], "competition": name,
                                    "season_slug": event["season_slug"]}),
            "source": SOURCE,
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })

    if args.apply and rows:
        cur.executemany(
            """insert into historical_fixtures
               (fixture_id, league_id, season, kickoff_at, status_short, home_team, away_team,
                home_goals, away_goals, raw_json, source, fetched_at)
               values (%(fixture_id)s, %(league_id)s, %(season)s, %(kickoff_at)s, %(status_short)s,
                       %(home_team)s, %(away_team)s, %(home_goals)s, %(away_goals)s,
                       %(raw_json)s::jsonb, %(source)s, %(fetched_at)s)""",
            rows,
        )
        conn.commit()

    cur.execute("select count(*) from historical_fixtures where league_id=%s and season=%s",
                (args.league, args.season))
    stored = cur.fetchone()[0]

    report = {
        "slug": args.slug, "league_id": args.league, "season": args.season,
        "windows": per_window, "expected_competition": expected,
        "counters": dict(counters), "rejected": dict(rejected),
        "would_insert": len(rows), "applied": bool(args.apply and rows),
        "stored_fixtures_after": stored,
    }
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1)
    os.replace(REPORT + ".tmp", REPORT)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
