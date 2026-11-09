"""Season-range statistics backfill for fixtures the date lookup cannot match.

Date-based matching fails for two reasons: a fixture may store a placeholder date
(the known Serie A 2019 defect), and a provider scoreboard may not list a match on
the exact UTC day. This job pulls one scoreboard for the whole competition season
and matches on team names plus the final score, so a wrong stored date no longer
prevents a correct match.

Identity rules (deliberately strict):
- the fixture's competition must map to an ESPN competition whose summary header
  reports that same competition,
- both team names must agree under alias-aware normalization,
- the final score must be identical,
- the candidate must be the only event in that season with those properties, or
  the closest by date when several agree (a tie is rejected).

Payloads are cached on disk; writes are batched. Dry-run by default.
"""

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage
import espn_football_stats as espn

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".api_cache", "espn_soccer", "season")


def season_range(fixtures):
    dates = sorted(f["kickoff_date"] for f in fixtures)
    start = dates[0]
    end = dates[-1]
    return start.replace("-", "") + "-" + end.replace("-", "")


def season_events(slug, date_range):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{slug}_{date_range}.json")
    url = f"{espn.SITE}/{slug}/scoreboard?dates={date_range}"
    try:
        payload = espn.cached_json(path, url)
    except Exception as exc:
        return [], f"{type(exc).__name__}"
    events = []
    for event in payload.get("events") or []:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competition = competitions[0]
        home, away = espn.competitor(competition, "home"), espn.competitor(competition, "away")
        try:
            home_score, away_score = int(home.get("score")), int(away.get("score"))
        except (TypeError, ValueError):
            continue
        events.append({
            "event_id": event.get("id"),
            "home_name": (home.get("team") or {}).get("displayName"),
            "away_name": (away.get("team") or {}).get("displayName"),
            "home_score": home_score,
            "away_score": away_score,
            "date": (event.get("date") or "")[:10],
        })
    return events, None


def match_in_season(fixture, events):
    candidates = []
    for event in events:
        if event["home_score"] != fixture["home_goals"] or event["away_score"] != fixture["away_goals"]:
            continue
        home = espn.name_score(fixture["home_team"], event["home_name"])
        away = espn.name_score(fixture["away_team"], event["away_name"])
        if home < espn.MIN_NAME_SCORE or away < espn.MIN_NAME_SCORE:
            continue
        candidates.append((home + away, event))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    best = candidates[0]
    if len(candidates) > 1 and candidates[1][0] == best[0]:
        same_date = [c for c in candidates if c[0] == best[0] and c[1]["date"] == fixture["kickoff_date"]]
        if len(same_date) == 1:
            return same_date[0][1]
        return None
    return best[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--league", type=int, default=None)
    parser.add_argument("--season", type=int, default=None)
    args = parser.parse_args(argv)

    conn, db_type = storage._connect()
    if db_type != "postgres":
        raise SystemExit(f"refusing to run against {db_type}")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.fixture_id, f.league_id, f.season, f.home_team, f.away_team,
                   f.home_goals, f.away_goals,
                   to_char(f.kickoff_at::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS kickoff_date
            FROM historical_fixtures f
            LEFT JOIN historical_fixture_enrichment e ON e.fixture_id = f.fixture_id
            WHERE e.fixture_id IS NULL
            ORDER BY f.league_id, f.season, f.kickoff_at
        """)
        keys = ["fixture_id", "league_id", "season", "home_team", "away_team",
                "home_goals", "away_goals", "kickoff_date"]
        pending = [dict(zip(keys, row)) for row in cur.fetchall()]
    pending = [f for f in pending if f["league_id"] in espn.ESPN_LEAGUES]
    if args.league is not None:
        pending = [f for f in pending if f["league_id"] == args.league]
    if args.season is not None:
        pending = [f for f in pending if f["season"] == args.season]

    groups = collections.defaultdict(list)
    for fixture in pending:
        groups[(fixture["league_id"], fixture["season"])].append(fixture)

    written = matched = skipped = 0
    for (league_id, season), fixtures in sorted(groups.items()):
        slug = espn.ESPN_LEAGUES[league_id]
        events, error = season_events(slug, season_range(fixtures))
        if error:
            print(json.dumps({"league": league_id, "season": season, "error": error}), flush=True)
            continue
        items = []
        for fixture in fixtures:
            event = match_in_season(fixture, events)
            if event is None:
                skipped += 1
                continue
            payload_path = os.path.join(os.path.dirname(espn.CACHE_DIR),
                                        f"{event['event_id']}.json")
            try:
                summary = espn.cached_json(
                    payload_path, f"{espn.SITE}/{slug}/summary?event={event['event_id']}")
            except Exception:
                skipped += 1
                continue
            header = ((summary.get("header") or {}).get("league") or {}).get("name") or ""
            wanted = espn.EXPECTED_COMPETITION.get(league_id, ())
            if "women" in header.lower() or not any(t in header.lower() for t in wanted):
                skipped += 1
                continue
            item = espn.build_item(fixture, event, espn.parse_summary(summary))
            if item is None:
                skipped += 1
                continue
            items.append(item)
        matched += len(items)
        if args.apply and items:
            storage.save_historical_enrichment(items, source=espn.SOURCE)
            written += len(items)
        print(json.dumps({"league": league_id, "season": season, "events": len(events),
                          "candidates": len(fixtures), "matched": len(items)}), flush=True)

    print(json.dumps({"pending": len(pending), "matched": matched,
                      "written": written, "skipped": skipped}, indent=2))
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM historical_fixtures f
                       LEFT JOIN historical_fixture_enrichment e ON e.fixture_id = f.fixture_id
                       WHERE e.fixture_id IS NULL""")
        print(json.dumps({"statless_remaining": cur.fetchone()[0]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
