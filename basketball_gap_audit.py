"""Audit basketball games whose statistics are missing, one game at a time.

For every game with no team statistics, and every game with team statistics but
no period scores, this fetches the ESPN summary and records exactly what the
provider publishes: boxscore presence, period linescores and player lines.

Nothing is fabricated: a game is only written when ESPN returns a parsable
boxscore, and a game with no period breakdown (for example an All-Star game
played to a target score) is recorded as a source limitation, not filled with
invented quarters.

Usage:
    python basketball_gap_audit.py --dry-run
    python basketball_gap_audit.py --apply
"""

from __future__ import annotations

import argparse
import json
import os

import basketball_enrichment as be
import storage

REPORT_DIR = os.path.join(".api_cache", "reports")


def gap_rows():
    conn, backend = storage._connect()
    if backend != "postgres":
        raise SystemExit("refusing to run outside PostgreSQL/Neon")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT g.game_id, g.league_id, g.season, g.game_date,
                       g.home_team, g.away_team, g.source,
                       EXISTS (SELECT 1 FROM historical_basketball_team_statistics t
                                WHERE t.game_id = g.game_id) AS has_team,
                       EXISTS (SELECT 1 FROM historical_basketball_period_scores p
                                WHERE p.game_id = g.game_id) AS has_period
                  FROM historical_basketball_games g
                 WHERE NOT EXISTS (SELECT 1 FROM historical_basketball_team_statistics t
                                    WHERE t.game_id = g.game_id)
                    OR NOT EXISTS (SELECT 1 FROM historical_basketball_period_scores p
                                    WHERE p.game_id = g.game_id)
                 ORDER BY g.game_date, g.game_id
                """
            )
            return cur.fetchall()
    finally:
        conn.close()


def describe(data):
    """Compact, evidence-only description of one ESPN payload."""
    if data is None:
        return {"summary": "absent", "detail": "no payload returned"}
    boxscore = data.get("boxscore") or {}
    header = data.get("header") or {}
    competition = (header.get("competitions") or [{}])[0] or {}
    status = (competition.get("status") or {}).get("type") or {}
    teams = boxscore.get("teams") or []
    players = boxscore.get("players") or []
    competitor_lines = 0
    for competitor in competition.get("competitors") or []:
        competitor_lines += len(competitor.get("linescores") or [])
    return {
        "summary": "present",
        "status": status.get("name"),
        "completed": status.get("completed"),
        "boxscore_teams": len(teams),
        "boxscore_player_groups": len(players),
        "header_linescores": competitor_lines,
        "has_scoring_plays": bool(data.get("scoringPlays")),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    os.makedirs(REPORT_DIR, exist_ok=True)
    rows = gap_rows()
    if args.limit:
        rows = rows[: args.limit]
    print(f"games needing attention: {len(rows)}")

    evidence = []
    saved = {"teams": 0, "periods": 0, "players": 0}
    for game_id, league_id, season, game_date, home, away, source, has_team, has_period in rows:
        data = be.fetch_summary(game_id, league_id=league_id or 12)
        parsed = be.parse_summary(data) if data else None
        info = describe(data)
        record = {
            "game_id": game_id,
            "league_id": league_id,
            "season": season,
            "game_date": str(game_date),
            "matchup": f"{home} v {away}",
            "had_team_stats": has_team,
            "had_period_scores": has_period,
            **info,
            "parsable_boxscore": bool(parsed),
            "parsed_periods": len(parsed["periods"]) if parsed else 0,
            "parsed_players": len(parsed["players"]) if parsed else 0,
        }
        if parsed and args.apply:
            game = {"game_id": game_id, "league_id": league_id, "season": season,
                    "source": source}
            counts = be.save_parsed(game, parsed)
            record["written"] = counts
            for key in saved:
                saved[key] += counts.get(key, 0)
        evidence.append(record)
        summary = record["summary"]
        print(
            f"  {game_id} {record['matchup'][:38]:38s} {record['game_date'][:10]} "
            f"summary={summary} status={record.get('status')} teams={record.get('boxscore_teams')} "
            f"players={record.get('boxscore_player_groups')} lines={record.get('header_linescores')} "
            f"parsed={record['parsable_boxscore']}"
        )

    path = os.path.join(REPORT_DIR, "basketball_gap_audit.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"games": evidence, "written": saved}, handle, indent=2)
    print(f"evidence: {path}")
    if args.apply:
        print("rows written:", saved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
