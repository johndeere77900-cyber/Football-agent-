"""Statistics backfill through each club's season schedule.

The competition scoreboard only answers "what happened on date D", so a fixture
whose stored date is a placeholder, or whose competition slug does not list the
round, never matches. A club's own season schedule lists every match that club
played with its event id, opponent and score, which removes the date dependency.

Identity rules:
- the club name must resolve to exactly one ESPN team id for that competition,
- the schedule event must carry the same opponent and the same final score,
- the summary header must report the fixture's competition and must not be a
  women's competition.

Both clubs are tried independently; a candidate found from either side is used
only when it is unique. Writes are batched and idempotent. Dry-run by default.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage
import espn_football_stats as espn

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".api_cache", "espn_soccer", "teams")
MIN_TEAM_SCORE = 0.84


def teams_for(slug):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{slug}_teams.json")
    payload = espn.cached_json(
        path, f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/teams")
    squads = ((payload.get("sports") or [{}])[0].get("leagues") or [{}])[0].get("teams") or []
    if not squads:
        squads = payload.get("teams") or []
    result = []
    for entry in squads:
        team = entry.get("team") if isinstance(entry, dict) and "team" in entry else entry
        if not isinstance(team, dict):
            continue
        name = team.get("displayName") or team.get("name")
        if team.get("id") and name:
            result.append({"id": str(team["id"]), "name": name})
    return result


def resolve_team(name, teams):
    scored = sorted(((espn.name_score(name, team["name"]), team) for team in teams),
                    key=lambda item: item[0], reverse=True)
    if not scored or scored[0][0] < MIN_TEAM_SCORE:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None
    return scored[0][1]


def score_value(raw):
    """Team schedules express a score as {"value": 2.0} rather than a string."""
    if isinstance(raw, dict):
        raw = raw.get("value", raw.get("displayValue"))
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def schedule(slug, team_id, season):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{slug}_{team_id}_{season}.json")
    try:
        payload = espn.cached_json(
            path,
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/teams/{team_id}/schedule?season={season}")
    except Exception:
        return []
    events = []
    for event in payload.get("events") or []:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competition = competitions[0]
        home, away = espn.competitor(competition, "home"), espn.competitor(competition, "away")
        home_score, away_score = score_value(home.get("score")), score_value(away.get("score"))
        if home_score is None or away_score is None:
            continue
        events.append({
            "event_id": event.get("id"),
            "home_name": (home.get("team") or {}).get("displayName"),
            "away_name": (away.get("team") or {}).get("displayName"),
            "home_score": home_score,
            "away_score": away_score,
            "date": (event.get("date") or "")[:10],
        })
    return events


def match_fixture(fixture, events):
    candidates = []
    for event in events:
        if event["home_score"] != fixture["home_goals"] or event["away_score"] != fixture["away_goals"]:
            continue
        if event["event_id"] is None:
            continue
        home = espn.name_score(fixture["home_team"], event["home_name"])
        away = espn.name_score(fixture["away_team"], event["away_name"])
        if home < espn.MIN_NAME_SCORE or away < espn.MIN_NAME_SCORE:
            continue
        candidates.append(event)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        same_day = [c for c in candidates if c["date"] == fixture["kickoff_date"]]
        if len(same_day) == 1:
            return same_day[0]
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--league", type=int, default=None)
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
    if not pending:
        print(json.dumps({"pending": 0}))
        return 0

    matched = written = attempted = 0
    items = []
    by_league = {}
    for fixture in pending:
        by_league.setdefault((fixture["league_id"], fixture["season"]), []).append(fixture)

    for (league_id, season), fixtures in sorted(by_league.items()):
        slug = espn.ESPN_LEAGUES[league_id]
        try:
            teams = teams_for(slug)
        except Exception as exc:
            print(json.dumps({"league": league_id, "season": season,
                              "error": f"{type(exc).__name__}"}), flush=True)
            continue
        found = 0
        for fixture in fixtures:
            attempted += 1
            event = None
            for side in ("home_team", "away_team"):
                team = resolve_team(fixture[side], teams)
                if not team:
                    continue
                event = match_fixture(fixture, schedule(slug, team["id"], season))
                if event:
                    break
            if not event:
                continue
            try:
                summary = espn.cached_json(
                    os.path.join(espn.CACHE_DIR, f"{event['event_id']}.json"),
                    f"{espn.SITE}/{slug}/summary?event={event['event_id']}")
            except Exception:
                continue
            header = ((summary.get("header") or {}).get("league") or {}).get("name") or ""
            wanted = espn.EXPECTED_COMPETITION.get(league_id, ())
            if "women" in header.lower() or not any(t in header.lower() for t in wanted):
                continue
            item = espn.build_item(fixture, event, espn.parse_summary(summary))
            if item is None:
                continue
            items.append(item)
            found += 1
        matched += found
        if args.apply and items:
            storage.save_historical_enrichment(items, source=espn.SOURCE)
            written += len(items)
            items = []
        print(json.dumps({"league": league_id, "season": season,
                          "candidates": len(fixtures), "matched": found}), flush=True)

    print(json.dumps({"attempted": attempted, "matched": matched, "written": written}, indent=2))
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM historical_fixtures f
                       LEFT JOIN historical_fixture_enrichment e ON e.fixture_id = f.fixture_id
                       WHERE e.fixture_id IS NULL""")
        print(json.dumps({"statless_remaining": cur.fetchone()[0]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
