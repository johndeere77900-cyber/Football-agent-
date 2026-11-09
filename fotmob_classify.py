"""Give every played fixture that still lacks possession or xG one terminal,
evidence-backed classification, aggregated by competition.

Inputs are the per-fixture outcomes written by the enrichment run plus the live
state of production Neon. Output is a JSONL file with one line per fixture and a
summary printed to stdout, so the residual is auditable rather than a bare count.
"""

import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, ".api_cache", "fotmob")
REPORTS = os.path.join(BASE, ".api_cache", "reports")
OUTCOMES = os.path.join(REPORTS, "fotmob_outcomes.jsonl")
OUT = os.path.join(REPORTS, "fotmob_terminal_classification.jsonl")


def main():
    seen = {}
    if os.path.exists(OUTCOMES):
        with open(OUTCOMES, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                # Later runs supersede earlier attempts for the same fixture.
                seen[str(row["fixture_id"])] = row

    conn, _ = storage._connect()
    cur = conn.cursor()
    cur.execute("""
        select f.fixture_id, f.league_id, f.status_short,
               to_char(f.kickoff_at::timestamptz at time zone 'utc','YYYY-MM-DD') as d,
               f.home_team, f.away_team,
               exists (select 1 from historical_fixture_enrichment e
                        where e.fixture_id = f.fixture_id
                          and e.home_possession is not null and e.away_possession is not null) has_poss,
               exists (select 1 from historical_fixture_enrichment e
                        where e.fixture_id = f.fixture_id
                          and e.home_xg is not null and e.away_xg is not null) has_xg
          from historical_fixtures f
         where f.status_short <> 'Scheduled'
           and (not exists (select 1 from historical_fixture_enrichment e
                             where e.fixture_id = f.fixture_id
                               and e.home_possession is not null and e.away_possession is not null)
                or not exists (select 1 from historical_fixture_enrichment e
                                where e.fixture_id = f.fixture_id
                                  and e.home_xg is not null and e.away_xg is not null))
         order by f.league_id, f.kickoff_at
    """)
    rows = cur.fetchall()

    summary = collections.Counter()
    by_league = collections.Counter()
    out = []
    for fixture_id, league_id, status, day, home, away, has_poss, has_xg in rows:
        outcome = seen.get(str(fixture_id), {})
        state = outcome.get("outcome")
        if not has_poss:
            if state == "matched_no_statistics_block":
                reason = "provider_match_has_no_statistics_block"
            elif state in ("matched_empty_values",):
                reason = "provider_match_has_empty_values"
            elif state == "no_match":
                reason = "no_provider_match_on_competition_clubs_date_score"
            else:
                reason = "not_processed_missing_possession"
        else:
            # Possession is present, so the gap is xG only.
            if day < "2020-01-01":
                reason = "provider_publishes_no_xg_before_2020_21"
            elif state == "matched_no_statistics_block":
                reason = "provider_match_has_no_statistics_block"
            elif state == "no_match":
                reason = "no_provider_match_on_competition_clubs_date_score"
            else:
                reason = "provider_has_no_xg_for_this_fixture"
        summary[reason] += 1
        by_league[(league_id, reason)] += 1
        out.append({"fixture_id": fixture_id, "league_id": league_id, "status": status,
                    "day": day, "home": home, "away": away,
                    "has_possession": has_poss, "has_xg": has_xg, "reason": reason})

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as handle:
        for row in out:
            handle.write(json.dumps(row) + "\n")

    print(json.dumps({"fixtures_classified": len(out),
                      "reasons": dict(summary.most_common())}, indent=1))
    table = collections.defaultdict(dict)
    for (league_id, reason), n in by_league.items():
        table[league_id][reason] = n
    print(json.dumps({str(k): v for k, v in sorted(table.items())}, indent=1))


if __name__ == "__main__":
    main()
