"""Fill possession, xG and match statistics from FotMob match data.

ESPN publishes possession but no xG at all, and the CSV imports that supply most
of the historical fixtures carry no possession. FotMob's public match endpoint
reports both for the competitions this database holds, so this job matches each
played fixture to its FotMob match and fills only the fields that are still
missing.

Matching is deliberately strict and mirrors the ESPN job:
- the match must be on the fixture's UTC date (or the adjacent day for late
  kickoffs), under a competition alias for the fixture's league,
- both club names must agree under alias-aware normalisation,
- the final score must be identical,
- a tie between two candidates is rejected rather than guessed.

Writes never overwrite a non-null statistic: existing valid values keep their
original source and the provenance of every filled field is merged into
``raw_json.field_sources``. A fixture with no enrichment row at all gains a
``fotmob`` row; one that already has a row is updated in place so the table
keeps a single record per fixture.

Resumable and idempotent: cached responses are reused, and a fixture whose
possession and xG are both present is skipped. Dry-run by default.
"""

import argparse
import collections
import datetime
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage
import espn_football_stats as espn

BASE = "https://www.fotmob.com/api/data"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".api_cache", "fotmob")
SOURCE = "fotmob"
PROVIDER_VERSION = "fotmob-web-2026-11"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TIMEOUT = 30

# League ids this database holds, mapped to the competition names FotMob uses.
LEAGUE_ALIASES = {
    1: ("world cup", "fifa world cup"),
    2: ("champions league", "uefa champions league"),
    3: ("europa league", "uefa europa league"),
    4: ("euro", "european championship", "uefa euro"),
    5: ("nations league", "uefa nations league"),
    39: ("premier league",),
    61: ("ligue 1", "ligue 1 uber eats"),
    78: ("bundesliga",),
    88: ("eredivisie",),
    94: ("primeira liga", "liga portugal"),
    135: ("serie a",),
    140: ("laliga", "la liga", "primera division"),
}

# FotMob team names that differ from the database spelling.
TEAM_ALIASES = {
    "manchester united": "manutd",
    "manchester city": "mancity",
    "newcastle united": "newcastle",
    "nottingham forest": "nottmforest",
    "brightonandhovealbion": "brighton",
    "wolverhamptonwanderers": "wolves",
    "tottenhamhotspur": "tottenham",
    "westhamunited": "westham",
    "sheffieldunited": "sheffieldutd",
    "paris saint germain": "psg",
    "bayern munich": "bayernmunich",
    "borussia dortmund": "dortmund",
    "borussia monchengladbach": "monchengladbach",
}

_lock = threading.Lock()
_stats = collections.Counter()


def norm(name):
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9 ]", "", text.lower())
    compact = text.replace(" ", "")
    if compact in TEAM_ALIASES:
        return TEAM_ALIASES[compact]
    for full, short in TEAM_ALIASES.items():
        if compact == full:
            return short
    return compact


def fetch(url, path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            pass
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, path)
            return data
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as exc:
            last = exc
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"{type(last).__name__}: {last}")


def day_matches(day):
    """All matches FotMob lists for one UTC day, keyed by match id."""
    compact = day.replace("-", "")
    path = os.path.join(CACHE, "day", f"{compact}.json")
    try:
        payload = fetch(f"{BASE}/matches?date={compact}", path)
    except RuntimeError:
        with _lock:
            _stats["day_error"] += 1
        return {}
    out = {}
    for league in payload.get("leagues") or []:
        league_name = (league.get("name") or "").lower()
        for match in league.get("matches") or []:
            status = match.get("status") or {}
            finished = bool(status.get("finished")) or match.get("statusId") == 6
            out[str(match.get("id"))] = {
                "id": str(match.get("id")),
                "league": league_name,
                "utc": (match.get("status") or {}).get("utcTime"),
                "home": ((match.get("home") or {}).get("name")),
                "away": ((match.get("away") or {}).get("name")),
                "home_score": ((match.get("home") or {}).get("score")),
                "away_score": ((match.get("away") or {}).get("score")),
                "finished": finished,
            }
    return out


def number(value):
    """FotMob reports some statistics as strings ("2.43"), others as numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace("%", "")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def stat_value(groups, keys):
    """Team-level [home, away] numbers for the first matching statistic key."""
    for group in groups:
        for item in group.get("stats") or []:
            if (item.get("key") or "").lower() not in keys:
                continue
            values = item.get("stats")
            if not (isinstance(values, list) and len(values) == 2):
                continue
            pair = [number(values[0]), number(values[1])]
            if any(v is not None for v in pair):
                return pair
    return [None, None]


def parse_detail(match_id):
    path = os.path.join(CACHE, "match", f"{match_id}.json")
    try:
        payload = fetch(f"{BASE}/matchDetails?matchId={match_id}", path)
    except RuntimeError:
        with _lock:
            _stats["detail_error"] += 1
        return None
    periods = ((payload.get("content") or {}).get("stats") or {}).get("Periods") or {}
    groups = ((periods.get("All") or {}).get("stats")) or []
    if not groups:
        with _lock:
            _stats["detail_no_stats"] += 1
        return None
    possession = stat_value(groups, {"ballpossesion", "ballpossession"})
    xg = stat_value(groups, {"expected_goals"})
    shots = stat_value(groups, {"total_shots"})
    on_target = stat_value(groups, {"shotsontarget"})
    corners = stat_value(groups, {"corners"})
    yellow = stat_value(groups, {"yellow_cards"})
    red = stat_value(groups, {"red_cards"})
    return {
        "home_possession": possession[0], "away_possession": possession[1],
        "home_xg": xg[0], "away_xg": xg[1],
        "home_shots": shots[0], "away_shots": shots[1],
        "home_shots_on_target": on_target[0], "away_shots_on_target": on_target[1],
        "home_corners": corners[0], "away_corners": corners[1],
        "home_yellow_cards": yellow[0], "away_yellow_cards": yellow[1],
        "home_red_cards": red[0], "away_red_cards": red[1],
    }


def same_club(left, right):
    """Provider spellings differ, so reuse the project's alias-aware comparison."""
    return espn.name_score(left, right) >= 0.72 or norm(left) == norm(right)


def candidate_score(fixture, match):
    """0 for a clean match, 1 for a day-shifted one, 2 for no match at all."""
    if not same_club(fixture["home_team"], match["home"]):
        return 2
    if not same_club(fixture["away_team"], match["away"]):
        return 2
    try:
        if int(fixture["home_goals"]) != int(match["home_score"]):
            return 2
        if int(fixture["away_goals"]) != int(match["away_score"]):
            return 2
    except (TypeError, ValueError):
        return 2
    return 0 if (match["utc"] or "")[:10] == fixture["day"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--league", type=int, default=None)
    parser.add_argument("--season", type=int, default=None)
    parser.add_argument("--newest-first", action="store_true")
    args = parser.parse_args(argv)

    conn, _ = storage._connect()
    conn.autocommit = False
    cur = conn.cursor()

    where = ["home_goals is not null", "away_goals is not null"]
    params = []
    if args.league:
        where.append("league_id=%s")
        params.append(args.league)
    if args.season:
        where.append("season=%s")
        params.append(args.season)
    order = "DESC" if args.newest_first else "ASC"
    cur.execute(f"""
        SELECT f.fixture_id, f.league_id, f.season,
               to_char(f.kickoff_at::timestamptz at time zone 'utc','YYYY-MM-DD') AS day,
               f.home_team, f.away_team, f.home_goals, f.away_goals,
               e.home_possession, e.home_xg
          FROM historical_fixtures f
          LEFT JOIN historical_fixture_enrichment e ON e.fixture_id = f.fixture_id
         WHERE {' AND '.join(where)}
         ORDER BY f.kickoff_at {order}
    """, params)
    fixtures = [dict(zip(("fixture_id", "league_id", "season", "day", "home_team", "away_team",
                          "home_goals", "away_goals", "have_possession", "have_xg"), row))
                for row in cur.fetchall()]
    if args.limit:
        fixtures = fixtures[:args.limit]

    by_day = collections.defaultdict(list)
    for fixture in fixtures:
        by_day[fixture["day"]].append(fixture)

    days = sorted(by_day)
    print(json.dumps({"fixtures": len(fixtures), "days": len(days)}), flush=True)

    def day_index(day):
        index = day_matches(day)
        return day, index

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        indexes = dict(pool.map(day_index, days))

    plans = []
    for fixture in fixtures:
        aliases = LEAGUE_ALIASES.get(fixture["league_id"], ())
        best = None
        best_score = 3
        day = datetime.date.fromisoformat(fixture["day"])
        for delta in (0, 1, -1):
            probe = (day + datetime.timedelta(days=delta)).isoformat()
            for match in indexes.get(probe, {}).values():
                if not match["finished"]:
                    continue
                if aliases and not any(alias in (match["league"] or "") for alias in aliases):
                    continue
                score = candidate_score(fixture, match)
                if score >= 2:
                    continue  # clubs, score or competition disagree: never a candidate
                if score < best_score:
                    best, best_score = match, score
                elif score == best_score:
                    best, best_score = None, 2  # ambiguous pairing: refuse
        if best is None:
            _stats["no_match"] += 1
            continue
        if fixture["have_possession"] is not None and fixture["have_xg"] is not None:
            _stats["already_complete"] += 1
            continue
        # FotMob publishes xG only from the 2020/21 season onwards, so a fixture from
        # before that is complete once possession is present; without this it would be
        # re-fetched on every pass.
        if fixture["have_possession"] is not None and fixture["day"] < "2020-01-01":
            _stats["pre_xg_era"] += 1
            continue
        plans.append((fixture, best))

    def detail_for(item):
        fixture, match = item
        return fixture, match, parse_detail(match["id"])

    written = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for fixture, match, parsed in pool.map(detail_for, plans):
            if not parsed:
                _stats["no_detail"] += 1
                continue
            fields = {k: v for k, v in parsed.items() if v is not None}
            if not fields:
                _stats["no_values"] += 1
                continue
            if args.apply:
                cur.execute("""
                    INSERT INTO historical_fixture_enrichment
                        (fixture_id, raw_json, fetched_at, source, field_mask, provider_version,
                         home_possession, away_possession, home_xg, away_xg,
                         home_shots, away_shots, home_shots_on_target, away_shots_on_target,
                         home_corners, away_corners, home_yellow_cards, away_yellow_cards,
                         home_red_cards, away_red_cards)
                    VALUES (%(fixture_id)s, %(raw_json)s::jsonb, %(fetched_at)s, %(source)s,
                            %(field_mask)s, %(provider_version)s,
                            %(home_possession)s, %(away_possession)s, %(home_xg)s, %(away_xg)s,
                            %(home_shots)s, %(away_shots)s, %(home_shots_on_target)s,
                            %(away_shots_on_target)s, %(home_corners)s, %(away_corners)s,
                            %(home_yellow_cards)s, %(away_yellow_cards)s,
                            %(home_red_cards)s, %(away_red_cards)s)
                    ON CONFLICT (source, fixture_id) DO UPDATE SET
                        home_possession = COALESCE(historical_fixture_enrichment.home_possession,
                                                   EXCLUDED.home_possession),
                        away_possession = COALESCE(historical_fixture_enrichment.away_possession,
                                                   EXCLUDED.away_possession),
                        home_xg = COALESCE(historical_fixture_enrichment.home_xg, EXCLUDED.home_xg),
                        away_xg = COALESCE(historical_fixture_enrichment.away_xg, EXCLUDED.away_xg),
                        home_shots = COALESCE(historical_fixture_enrichment.home_shots,
                                              EXCLUDED.home_shots),
                        away_shots = COALESCE(historical_fixture_enrichment.away_shots,
                                              EXCLUDED.away_shots),
                        home_shots_on_target = COALESCE(
                            historical_fixture_enrichment.home_shots_on_target,
                            EXCLUDED.home_shots_on_target),
                        away_shots_on_target = COALESCE(
                            historical_fixture_enrichment.away_shots_on_target,
                            EXCLUDED.away_shots_on_target),
                        home_corners = COALESCE(historical_fixture_enrichment.home_corners,
                                                EXCLUDED.home_corners),
                        away_corners = COALESCE(historical_fixture_enrichment.away_corners,
                                                EXCLUDED.away_corners),
                        home_yellow_cards = COALESCE(historical_fixture_enrichment.home_yellow_cards,
                                                     EXCLUDED.home_yellow_cards),
                        away_yellow_cards = COALESCE(historical_fixture_enrichment.away_yellow_cards,
                                                     EXCLUDED.away_yellow_cards),
                        home_red_cards = COALESCE(historical_fixture_enrichment.home_red_cards,
                                                  EXCLUDED.home_red_cards),
                        away_red_cards = COALESCE(historical_fixture_enrichment.away_red_cards,
                                                  EXCLUDED.away_red_cards),
                        raw_json = historical_fixture_enrichment.raw_json || EXCLUDED.raw_json,
                        fetched_at = EXCLUDED.fetched_at
                """, {
                    "fixture_id": fixture["fixture_id"],
                    "raw_json": json.dumps({"field_sources": {field: SOURCE for field in fields},
                                            "fotmob_match_id": match["id"]}),
                    "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "source": SOURCE,
                    "field_mask": ",".join(sorted(fields)),
                    "provider_version": PROVIDER_VERSION,
                    **{field: fields.get(field) for field in (
                        "home_possession", "away_possession", "home_xg", "away_xg",
                        "home_shots", "away_shots", "home_shots_on_target", "away_shots_on_target",
                        "home_corners", "away_corners", "home_yellow_cards", "away_yellow_cards",
                        "home_red_cards", "away_red_cards")},
                })
                written += 1
                if written % 200 == 0:
                    conn.commit()
                    print(json.dumps({"written": written}), flush=True)
            else:
                written += 1

    if args.apply:
        conn.commit()

    report = {"fixtures": len(fixtures), "days": len(days), "matched": len(plans),
              "written": written, "applied": bool(args.apply), **dict(_stats)}
    os.makedirs(os.path.join(os.path.dirname(CACHE), "reports"), exist_ok=True)
    with open(os.path.join(os.path.dirname(CACHE), "reports", "fotmob_enrichment.json"), "w",
              encoding="utf-8") as handle:
        json.dump(report, handle, indent=1)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
