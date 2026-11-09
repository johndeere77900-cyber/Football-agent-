"""Diagnose why fixtures were not matched: re-run the match search with the
competition filter disabled and report which FotMob competition the provider
actually files each fixture under.
"""

import collections
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage
import espn_football_stats as espn
import fotmob_enrichment as fm

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".api_cache", "fotmob", "day")


def load_day(day):
    path = os.path.join(CACHE, day.replace("-", "") + ".json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def candidates(payload, home, away):
    out = []
    for league in payload.get("leagues") or []:
        for match in league.get("matches") or []:
            h = (match.get("home") or {}).get("name")
            a = (match.get("away") or {}).get("name")
            if espn.name_score(home, h) >= 0.72 and espn.name_score(away, a) >= 0.72:
                out.append({
                    "league_id": league.get("id"),
                    "league": league.get("name"),
                    "home": h, "away": a,
                    "score": ((match.get("home") or {}).get("score"),
                              (match.get("away") or {}).get("score")),
                    "utc": ((match.get("status") or {}).get("utcTime") or "")[:10],
                })
    return out


def main():
    conn, _ = storage._connect()
    cur = conn.cursor()
    cur.execute("""
        select f.fixture_id, f.league_id, f.status_short,
               to_char(f.kickoff_at::timestamptz at time zone 'utc','YYYY-MM-DD') as d,
               f.home_team, f.away_team, f.home_goals, f.away_goals
          from historical_fixtures f
         where f.status_short <> 'Scheduled'
           and not exists (select 1 from historical_fixture_enrichment e
                            where e.fixture_id = f.fixture_id
                              and e.home_possession is not null and e.away_possession is not null)
         order by f.kickoff_at
    """)
    rows = [dict(zip(("fixture_id", "league_id", "status", "d", "home", "away",
                      "home_goals", "away_goals"), r)) for r in cur.fetchall()]
    summary = collections.Counter()
    leagues = collections.Counter()
    examples = collections.defaultdict(list)
    by_league = collections.Counter()
    for row in rows:
        by_league[row["league_id"]] += 1
        found = None
        for delta in (0, 1, -1):
            import datetime
            probe = (datetime.date.fromisoformat(row["d"]) + datetime.timedelta(days=delta)).isoformat()
            payload = load_day(probe)
            if not payload:
                continue
            hits = candidates(payload, row["home"], row["away"])
            if hits:
                found = hits[0]
                break
        if not found:
            summary["not_in_provider_day_lists"] += 1
            if len(examples[(row["league_id"], "absent")]) < 3:
                examples[(row["league_id"], "absent")].append(
                    (row["home"], row["away"], row["d"], row["home_goals"], row["away_goals"]))
            continue
        summary["present_but_rejected"] += 1
        leagues[(row["league_id"], found["league_id"], found["league"])] += 1
        if len(examples[(row["league_id"], "present")]) < 3:
            examples[(row["league_id"], "present")].append(
                (row["home"], row["away"], row["d"], row["home_goals"], row["away_goals"],
                 found["league"], found["score"], found["utc"]))
    out = {
        "rows": len(rows),
        "by_league": dict(by_league),
        "summary": dict(summary),
        "provider_leagues": [{"stored": k[0], "fotmob_id": k[1], "fotmob": k[2], "n": v}
                             for k, v in leagues.most_common(40)],
        "examples": {str(k): v for k, v in examples.items()},
    }
    path = os.path.join(os.path.dirname(CACHE), "reports", "fotmob_unmatched_diag.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=1)
    print(json.dumps({k: out[k] for k in ("rows", "by_league", "summary")}, indent=1))
    print(json.dumps(out["provider_leagues"][:15], indent=1))


if __name__ == "__main__":
    main()
