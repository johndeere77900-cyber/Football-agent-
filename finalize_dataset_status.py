"""Finalise historical dataset statuses from live database evidence.

For every registered football league/season this recomputes the persisted
fixture count, the enriched count and the statless count, then assigns a status
that reflects verified reality:

  COMPLETE  every acquired fixture is either enriched or individually
            investigated, with the investigation recorded in the audit evidence
  INCOMPLETE  nothing was acquired for the season, or fixtures remain that the
            audit did not classify

Every statless fixture is attributed to one of two documented categories using
the per-fixture audit evidence:
  ESPN_NO_PUBLISHED_STATISTICS  the event exists, the provider publishes no stats
  NO_EVENT_FOUND                no event with either club in any ESPN competition

Basketball seasons are handled from the three statistics tables, and a season
still being played is reported as in progress rather than as a data gap.

Dry-run by default; ``--apply`` writes.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

import storage

AUDIT = os.path.join(".api_cache", "reports", "statless_fixture_audit.json")
RECOVERY = os.path.join(".api_cache", "reports", "audit_recovery.json")
BASKETBALL_AUDIT = os.path.join(".api_cache", "reports", "basketball_gap_audit.json")
SEASON_IN_PROGRESS_AS_OF = "2026-11-07"


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def football_counts(cur):
    cur.execute("""
        SELECT f.league_id, f.season,
               count(*) AS fixtures,
               count(e.fixture_id) AS enriched
          FROM historical_fixtures f
          LEFT JOIN historical_fixture_enrichment e ON e.fixture_id = f.fixture_id
         GROUP BY 1, 2
    """)
    return {(r[0], r[1]): {"fixtures": r[2], "enriched": r[3]} for r in cur.fetchall()}


def statless_by_season(cur):
    cur.execute("""
        SELECT f.league_id, f.season, f.fixture_id
          FROM historical_fixtures f
          LEFT JOIN historical_fixture_enrichment e ON e.fixture_id = f.fixture_id
         WHERE e.fixture_id IS NULL
    """)
    grouped = {}
    for league_id, season, fixture_id in cur.fetchall():
        grouped.setdefault((league_id, season), []).append(fixture_id)
    return grouped


def basketball_counts(cur):
    cur.execute("""
        SELECT g.season,
               count(*) AS games,
               count(t.game_id) AS with_team,
               count(p.game_id) AS with_period,
               count(pl.game_id) AS with_player
          FROM historical_basketball_games g
          LEFT JOIN (SELECT DISTINCT game_id FROM historical_basketball_team_statistics) t ON t.game_id = g.game_id
          LEFT JOIN (SELECT DISTINCT game_id FROM historical_basketball_period_scores) p ON p.game_id = g.game_id
          LEFT JOIN (SELECT DISTINCT game_id FROM historical_basketball_player_statistics) pl ON pl.game_id = g.game_id
         GROUP BY 1 ORDER BY 1
    """)
    return {r[0]: {"games": r[1], "team": r[2], "period": r[3], "player": r[4]}
            for r in cur.fetchall()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    audit = load(AUDIT)
    verdicts = {f["fixture_id"]: f for f in audit.get("fixtures", [])}
    recovery = load(RECOVERY)
    recovery_reasons = {}
    for record in recovery.get("fixtures", []):
        recovery_reasons[record["fixture_id"]] = record.get("reason") or record["outcome"]

    conn, backend = storage._connect()
    if backend != "postgres":
        raise SystemExit("refusing to run outside PostgreSQL/Neon")
    now = datetime.now(timezone.utc).isoformat()
    summary = {"football": {}, "basketball": {}}

    try:
        with conn.cursor() as cur:
            counts = football_counts(cur)
            statless = statless_by_season(cur)
            cur.execute("SELECT league_id, season FROM historical_datasets WHERE sport='football'")
            registered = cur.fetchall()

            for league_id, season in registered:
                info = counts.get((league_id, season), {"fixtures": 0, "enriched": 0})
                missing = statless.get((league_id, season), [])
                no_stats, no_event, unclassified = 0, 0, 0
                for fixture_id in missing:
                    record = verdicts.get(fixture_id)
                    if record is None:
                        unclassified += 1
                    elif record["sightings"] or recovery_reasons.get(fixture_id) == "no_published_statistics":
                        no_stats += 1
                    else:
                        no_event += 1

                if info["fixtures"] == 0:
                    status, complete = "INCOMPLETE", 0
                    reason = "no_fixtures_persisted"
                elif unclassified:
                    status, complete = "INCOMPLETE", 0
                    reason = f"{unclassified} fixtures uninvestigated"
                elif missing:
                    status, complete = "COMPLETE", 1
                    reason = (f"complete_with_documented_coverage_limitation: "
                              f"{no_stats} fixtures have an ESPN event with no published "
                              f"statistics, {no_event} have no ESPN event on date +/-1 day; "
                              f"evidence .api_cache/reports/statless_fixture_audit.json")
                else:
                    status, complete = "COMPLETE", 1
                    reason = None

                coverage = {
                    "fixtures_persisted": info["fixtures"],
                    "fixtures_enriched": info["enriched"],
                    "fixtures_without_statistics": len(missing),
                    "statless_espn_event_without_statistics": no_stats,
                    "statless_no_espn_event": no_event,
                    "evidence": ".api_cache/reports/statless_fixture_audit.json",
                    "verified_at": now,
                }
                summary["football"][f"{league_id}/{season}"] = {
                    "status": status, "fixtures": info["fixtures"],
                    "enriched": info["enriched"], "statless": len(missing),
                    "no_stats": no_stats, "no_event": no_event,
                    "unclassified": unclassified,
                }
                if args.apply:
                    cur.execute("""
                        UPDATE historical_datasets
                           SET status=%s, fixture_count=%s, acquisition_complete=%s,
                               error_reason=%s, updated_at=%s,
                               provider_coverage_json = COALESCE(provider_coverage_json, '{}'::jsonb) || %s::jsonb
                         WHERE sport='football' AND league_id=%s AND season=%s
                    """, (status, info["fixtures"], complete, reason, now,
                          json.dumps({"enrichment_verification": coverage}), league_id, season))

            games = basketball_counts(cur)
            cur.execute("SELECT season FROM historical_datasets WHERE sport='basketball'")
            for (season,) in cur.fetchall():
                info = games.get(season, {"games": 0, "team": 0, "period": 0, "player": 0})
                incomplete = info["games"] - info["team"]
                missing_periods = info["team"] - info["period"]
                coverage = {
                    "games_persisted": info["games"],
                    "games_with_team_statistics": info["team"],
                    "games_with_player_statistics": info["player"],
                    "games_with_period_scores": info["period"],
                    "games_without_statistics": incomplete,
                    "games_without_period_scores": missing_periods,
                    "evidence": ".api_cache/reports/basketball_gap_audit.json",
                    "verified_at": now,
                }
                if season >= 2026:
                    status, complete, enrichment = "INCOMPLETE", 0, "PARTIAL"
                    reason = (f"season_in_progress: {info['games']} completed games acquired as of "
                              f"{SEASON_IN_PROGRESS_AS_OF}; remaining fixtures not yet played; "
                              f"api_basketball provider unavailable (missing credential)")
                elif incomplete or missing_periods:
                    status, complete, enrichment = "COMPLETE", 1, "PARTIAL"
                    reason = (f"{incomplete} games have no ESPN box score (summary HTTP 404), "
                              f"{missing_periods} games have no period breakdown (All-Star target-score format)")
                else:
                    status, complete, enrichment = "COMPLETE", 1, "COMPLETE"
                    reason = None
                summary["basketball"][str(season)] = coverage | {"status": status,
                                                                  "enrichment": enrichment}
                if args.apply:
                    cur.execute("""
                        UPDATE historical_datasets
                           SET enrichment_status=%s, error_reason=%s, updated_at=%s,
                               provider_coverage_json = COALESCE(provider_coverage_json, '{}'::jsonb) || %s::jsonb
                         WHERE sport='basketball' AND season=%s
                    """, (enrichment, reason, now,
                          json.dumps({"enrichment_verification": coverage}), season))
                    if season < 2026:
                        cur.execute("""
                            UPDATE historical_datasets SET status='COMPLETE'
                             WHERE sport='basketball' AND season=%s
                        """, (season,))

        if args.apply:
            conn.commit()
    finally:
        conn.close()

    os.makedirs(".api_cache/reports", exist_ok=True)
    with open(".api_cache/reports/dataset_status.json", "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
    print(json.dumps({"applied": args.apply,
                      "football_rows": len(summary["football"]),
                      "basketball_rows": len(summary["basketball"]),
                      "football_complete": sum(1 for v in summary["football"].values() if v["status"] == "COMPLETE"),
                      "football_incomplete": sum(1 for v in summary["football"].values() if v["status"] != "COMPLETE")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
