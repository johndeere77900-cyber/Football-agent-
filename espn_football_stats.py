"""Acquire match statistics for fixtures that have no enrichment row at all.

The remaining stateless fixtures are not duplicates: they are competitions and
seasons for which no statistics source was ever run (for example the 2024/25
Champions League and Europa League seasons, which were stored from a fixtures-only
feed). This job reads ESPN's public soccer scoreboard and match summary endpoints,
matches a summary to a stored fixture on independent evidence, and writes the
verified statistics through ``storage.save_historical_enrichment``.

Identity rule for accepting a match:
- the fixture's competition maps to a known ESPN league,
- both team names agree after alias-aware normalization, and
- the ESPN final score equals the stored final score.

A fixture that does not meet all three is reported as unmatched and receives
nothing. Statistics are never attached to an uncertain identity, and a value the
provider does not publish stays null.

Fixtures are fetched and parsed concurrently; database writes stay on one thread.
Payloads are cached on disk, so a restart only costs the in-flight requests.
Dry-run by default; ``--apply`` writes.
"""

import argparse
import difflib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage
import team_identity

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".api_cache", "espn_soccer")
SITE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TIMEOUT = 30
SOURCE = "espn_soccer_summary"

ESPN_LEAGUES = {
    1: "fifa.world", 2: "uefa.champions", 3: "uefa.europa", 4: "uefa.euro",
    5: "uefa.nations", 39: "eng.1", 61: "fra.1", 78: "ger.1", 88: "ned.1",
    94: "por.1", 135: "ita.1", 140: "esp.1",
}

# ESPN statistic name -> stored field, as home/away pairs.
FIELD_MAP = {
    "possessionPct": "possession",
    "totalShots": "shots",
    "shotsOnTarget": "shots_on_target",
    "wonCorners": "corners",
    "yellowCards": "yellow_cards",
    "redCards": "red_cards",
}
# Qualifying rounds are published as separate ESPN competitions, so they are
# searched as additional candidates when the main competition has no match.
EXTRA_SLUGS = {2: ("uefa.champions_qual",), 3: ("uefa.europa_qual",), 4: ("uefa.euroq",)}
# Competition names the summary header must report before statistics are accepted.
# The "all" scoreboard mixes men's, women's and youth competitions that can repeat a
# team pairing and scoreline on the same date.
EXPECTED_COMPETITION = {
    1: ("world cup",), 2: ("champions league",), 3: ("europa league",),
    4: ("european championship",), 5: ("nations league",),
    39: ("premier league",), 140: ("laliga", "la liga"), 135: ("serie a",),
    78: ("bundesliga",), 61: ("ligue 1",), 88: ("eredivisie",), 94: ("liga portugal",),
}
MIN_NAME_SCORE = 0.80


def _get(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def cached_json(path, url):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            pass
    data = _get(url)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)
    return data


def normalize(name):
    return team_identity.normalize_team_name(name or "")


def name_score(left, right):
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long = sorted((a.replace(" ", ""), b.replace(" ", "")), key=len)
    if len(short) >= 3 and short in long:
        return 0.95
    # Provider feeds shorten club names ("Athletic Bilbao" vs "Athletic Club"), so a
    # distinctive shared word counts as agreement. Four-letter words are excluded
    # because they are common filler ("real", "club") that would pair different clubs.
    tokens_a = {t for t in re.split(r"[^a-z0-9]+", a) if len(t) >= 4}
    tokens_b = {t for t in re.split(r"[^a-z0-9]+", b) if len(t) >= 4}
    if tokens_a & tokens_b:
        return 0.85
    # Feeds abbreviate the same club ("Barça" / "Barcelona", "Bara" after accent
    # stripping), so a shared prefix of at least four characters counts when the two
    # words differ substantially in length (the prefix is a real stem, not a shared
    # filler word).
    for left in tokens_a:
        for right in tokens_b:
            shared = os.path.commonprefix([left, right])
            if len(shared) >= 5 or (len(shared) >= 4 and abs(len(left) - len(right)) >= 3):
                return 0.85
    return difflib.SequenceMatcher(None, a, b).ratio()


def summary_date(fixture):
    return fixture["kickoff_date"].replace("-", "")


def load_pending(cur, limit=None):
    cur.execute("""
        SELECT f.fixture_id, f.league_id, f.season, f.home_team, f.away_team,
               f.home_goals, f.away_goals,
               to_char(f.kickoff_at::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS kickoff_date
        FROM historical_fixtures f
        LEFT JOIN historical_fixture_enrichment e ON e.fixture_id = f.fixture_id
        WHERE e.fixture_id IS NULL
        ORDER BY f.league_id, f.kickoff_at
    """)
    keys = ["fixture_id", "league_id", "season", "home_team", "away_team",
            "home_goals", "away_goals", "kickoff_date"]
    rows = [dict(zip(keys, row)) for row in cur.fetchall()]
    rows = [row for row in rows if row["league_id"] in ESPN_LEAGUES]
    return rows[:limit] if limit else rows


def scoreboard(slug, date):
    url = f"{SITE}/{slug}/scoreboard?dates={date}"
    path = os.path.join(CACHE_DIR, "scoreboard", f"{slug}_{date}.json")
    try:
        return cached_json(path, url)
    except Exception:
        return {}


def competitor(competition, side):
    for team in competition.get("competitors") or []:
        if team.get("homeAway") == side:
            return team
    return {}


def candidate_events(payload):
    events = []
    for event in payload.get("events") or []:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competition = competitions[0]
        home, away = competitor(competition, "home"), competitor(competition, "away")
        try:
            home_score = int(home.get("score"))
            away_score = int(away.get("score"))
        except (TypeError, ValueError):
            home_score = away_score = None
        events.append({
            "event_id": event.get("id"),
            "home_name": (home.get("team") or {}).get("displayName"),
            "away_name": (away.get("team") or {}).get("displayName"),
            "home_score": home_score,
            "away_score": away_score,
            "date": event.get("date"),
        })
    return events


def match_event(fixture, events):
    ranked = []
    for event in events:
        if event["home_score"] != fixture["home_goals"] or event["away_score"] != fixture["away_goals"]:
            continue
        home = name_score(fixture["home_team"], event["home_name"])
        away = name_score(fixture["away_team"], event["away_name"])
        if home < MIN_NAME_SCORE or away < MIN_NAME_SCORE:
            continue
        ranked.append((home + away, event))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def parse_summary(payload):
    """Extract the published team statistics as home/away pairs."""
    stats = {"home": {}, "away": {}}
    boxscore = payload.get("boxscore") or {}
    for team in boxscore.get("teams") or []:
        side = team.get("homeAway")
        if side not in ("home", "away"):
            continue
        for entry in team.get("statistics") or []:
            name = entry.get("name")
            field = FIELD_MAP.get(name)
            if not field:
                continue
            value = entry.get("value", entry.get("displayValue"))
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            stats[side][field] = value
    return stats


def build_item(fixture, event, stats):
    if not any(stats["home"].values()) and not any(stats["away"].values()):
        return None
    pairs = {}
    for field in set(stats["home"]) | set(stats["away"]):
        home, away = stats["home"].get(field), stats["away"].get(field)
        if home is None and away is None:
            continue
        pairs[field] = {"home": home, "away": away}
    item = {
        "fixture": {"id": fixture["fixture_id"]},
        "statistics": pairs,
        "provider_provenance": {
            "provider": "espn",
            "provider_version": "espn_soccer_summary_v1",
            "espn_event_id": event["event_id"],
        },
        "teams": {
            "home": {"id": fixture["fixture_id"] * 2, "name": fixture["home_team"]},
            "away": {"id": fixture["fixture_id"] * 2 + 1, "name": fixture["away_team"]},
        },
        "espn_match": {
            "event_id": event["event_id"],
            "date": event["date"],
            "home": event["home_name"],
            "away": event["away_name"],
            "home_score": event["home_score"],
            "away_score": event["away_score"],
        },
    }
    return item


def work(fixture):
    """Fetch the scoreboard and summary for one fixture; parse off the main thread."""
    slugs = [ESPN_LEAGUES[fixture["league_id"]]]
    slugs.extend(EXTRA_SLUGS.get(fixture["league_id"], ()))
    # The "all" scoreboard lists every competition on a date, which covers
    # qualifying rounds published under competition ids the other slugs miss.
    slugs.append("all")
    base = datetime.fromisoformat(fixture["kickoff_date"])
    # A late kick-off can land on the previous or next date in the provider's
    # scoreboard calendar, so the neighbouring days are checked too. The match is
    # still only accepted when both team names and the final score agree.
    event = None
    for slug in slugs:
        for shift in (0, -1, 1):
            date = (base + timedelta(days=shift)).strftime("%Y%m%d")
            event = match_event(fixture, candidate_events(scoreboard(slug, date)))
            if event:
                break
        if event:
            break
    if event is None:
        return fixture, None, "no_matching_espn_event"
    url = f"{SITE}/{slug}/summary?event={event['event_id']}"
    path = os.path.join(CACHE_DIR, f"{event['event_id']}.json")
    try:
        payload = cached_json(path, url)
    except Exception as exc:
        return fixture, None, f"summary_error:{type(exc).__name__}"
    header = ((payload.get("header") or {}).get("league") or {}).get("name") or ""
    competition = header.lower()
    wanted = EXPECTED_COMPETITION.get(fixture["league_id"], ())
    if "women" in competition or not any(token in competition for token in wanted):
        return fixture, None, "competition_mismatch"
    stats = parse_summary(payload)
    item = build_item(fixture, event, stats)
    return fixture, item, (None if item else "no_published_statistics")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--league", type=int, default=None)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)

    conn, db_type = storage._connect()
    if db_type != "postgres":
        raise SystemExit(f"refusing to run against {db_type}")

    with conn.cursor() as cur:
        pending = load_pending(cur, args.limit)
    if args.league:
        pending = [fixture for fixture in pending if fixture["league_id"] == args.league]

    print(json.dumps({"pending": len(pending), "source": SOURCE}), flush=True)

    counters = {"matched": 0, "written": 0, "no_matching_espn_event": 0,
                "no_published_statistics": 0, "summary_error": 0,
                "competition_mismatch": 0}
    buffer = []
    started = time.time()

    def flush():
        """Write as a batch: one connection and one transaction per batch."""
        if not buffer:
            return
        batch = list(buffer)
        buffer.clear()
        try:
            storage.save_historical_enrichment(batch, source=SOURCE)
            counters["written"] += len(batch)
        except Exception as exc:
            counters["write_error"] = counters.get("write_error", 0) + 1
            print(json.dumps({"write_error": f"{type(exc).__name__}: {exc}"}), flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for index, (fixture, item, reason) in enumerate(pool.map(work, pending), 1):
            if item is None:
                counters[reason.split(":")[0] if reason else "no_matching_espn_event"] += 1
            else:
                counters["matched"] += 1
                if args.apply:
                    buffer.append(item)
                    if len(buffer) >= 25:
                        flush()
            if index % 100 == 0:
                counters["elapsed_s"] = round(time.time() - started, 1)
                print(json.dumps(counters), flush=True)

    if args.apply:
        flush()

    counters["elapsed_s"] = round(time.time() - started, 1)
    print(json.dumps(counters, indent=2))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM historical_fixture_enrichment WHERE source = %s", (SOURCE,))
        print(json.dumps({"stored_rows_for_source": cur.fetchone()[0]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
