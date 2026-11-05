"""Historical basketball statistics enrichment (ESPN boxscores).

The existing acquisition route stores basketball *games* only: scores, teams,
league, status and date. This module adds the statistics layer the database was
missing, in dedicated tables so football enrichment is never mixed with
basketball data:

    historical_basketball_team_statistics   (one row per game per side)
    historical_basketball_period_scores     (one row per game per side per period)
    historical_basketball_player_statistics (one row per game per player)

Rules enforced here:
- A statistic is persisted only when the provider actually supplies it. Missing
  values stay NULL and are never written as zero.
- ``field_mask`` records exactly which fields are genuinely populated.
- Writes are idempotent on the table identity, and an existing verified value is
  never replaced by NULL or a lower-fidelity value.
- Every row keeps its provider, retrieval time and provider version.

Run with ``--limit`` for a bounded batch; the job is resumable because games
that already have team statistics are skipped.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

import storage

ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
SPORT_PATHS = {
    12: "nba",
}
REQUEST_TIMEOUT = 25
MAX_RETRIES = 3
CACHE_DIR = os.path.join(getattr(__import__("config"), "CACHE_DIR", ".api_cache"), "espn_boxscores")

SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS historical_basketball_team_statistics (
        source TEXT NOT NULL,
        game_id BIGINT NOT NULL,
        league_id BIGINT,
        season INTEGER,
        team_id BIGINT,
        team_name TEXT,
        home_away TEXT NOT NULL,
        points INTEGER,
        field_goals_made INTEGER,
        field_goals_attempted INTEGER,
        field_goal_pct NUMERIC,
        three_point_made INTEGER,
        three_point_attempted INTEGER,
        three_point_pct NUMERIC,
        free_throws_made INTEGER,
        free_throws_attempted INTEGER,
        free_throw_pct NUMERIC,
        offensive_rebounds INTEGER,
        defensive_rebounds INTEGER,
        total_rebounds INTEGER,
        assists INTEGER,
        steals INTEGER,
        blocks INTEGER,
        turnovers INTEGER,
        personal_fouls INTEGER,
        field_mask TEXT,
        provider_version TEXT,
        raw_json JSONB,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (source, game_id, home_away)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hist_bball_team_stats_game "
    "ON historical_basketball_team_statistics (game_id)",
    "CREATE INDEX IF NOT EXISTS idx_hist_bball_team_stats_ls "
    "ON historical_basketball_team_statistics (league_id, season)",
    """
    CREATE TABLE IF NOT EXISTS historical_basketball_period_scores (
        source TEXT NOT NULL,
        game_id BIGINT NOT NULL,
        league_id BIGINT,
        season INTEGER,
        home_away TEXT NOT NULL,
        period INTEGER NOT NULL,
        points INTEGER NOT NULL,
        is_overtime BOOLEAN NOT NULL DEFAULT FALSE,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (source, game_id, home_away, period)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hist_bball_period_game "
    "ON historical_basketball_period_scores (game_id)",
    """
    CREATE TABLE IF NOT EXISTS historical_basketball_player_statistics (
        source TEXT NOT NULL,
        game_id BIGINT NOT NULL,
        league_id BIGINT,
        season INTEGER,
        team_id BIGINT,
        home_away TEXT,
        player_id BIGINT NOT NULL,
        player_name TEXT,
        starter BOOLEAN,
        did_not_play BOOLEAN,
        minutes NUMERIC,
        points INTEGER,
        field_goals_made INTEGER,
        field_goals_attempted INTEGER,
        three_point_made INTEGER,
        three_point_attempted INTEGER,
        free_throws_made INTEGER,
        free_throws_attempted INTEGER,
        total_rebounds INTEGER,
        assists INTEGER,
        steals INTEGER,
        blocks INTEGER,
        turnovers INTEGER,
        personal_fouls INTEGER,
        plus_minus NUMERIC,
        field_mask TEXT,
        provider_version TEXT,
        raw_json JSONB,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (source, game_id, player_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hist_bball_player_game "
    "ON historical_basketball_player_statistics (game_id)",
    "CREATE INDEX IF NOT EXISTS idx_hist_bball_player_team "
    "ON historical_basketball_player_statistics (team_id, season)",
]

TEAM_COLUMNS = (
    "points", "field_goals_made", "field_goals_attempted", "field_goal_pct",
    "three_point_made", "three_point_attempted", "three_point_pct",
    "free_throws_made", "free_throws_attempted", "free_throw_pct",
    "offensive_rebounds", "defensive_rebounds", "total_rebounds",
    "assists", "steals", "blocks", "turnovers", "personal_fouls",
)

PLAYER_COLUMNS = (
    "minutes", "points", "field_goals_made", "field_goals_attempted",
    "three_point_made", "three_point_attempted", "free_throws_made",
    "free_throws_attempted", "total_rebounds", "assists", "steals", "blocks",
    "turnovers", "personal_fouls", "plus_minus",
)

# ESPN statistic names -> (made column, attempted column) for combined values.
MADE_ATTEMPTED = {
    "fieldGoalsMade-fieldGoalsAttempted": ("field_goals_made", "field_goals_attempted"),
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": (
        "three_point_made", "three_point_attempted"),
    "freeThrowsMade-freeThrowsAttempted": ("free_throws_made", "free_throws_attempted"),
}

# ESPN statistic names -> single column.
SINGLE_STATS = {
    "points": "points",
    "fieldGoalPct": "field_goal_pct",
    "threePointFieldGoalPct": "three_point_pct",
    "freeThrowPct": "free_throw_pct",
    "offensiveRebounds": "offensive_rebounds",
    "defensiveRebounds": "defensive_rebounds",
    "totalRebounds": "total_rebounds",
    "assists": "assists",
    "steals": "steals",
    "blocks": "blocks",
    "turnovers": "turnovers",
}

# ESPN player keys/labels -> column, for both the keys and labels vocabularies.
PLAYER_KEY_MAP = {
    "minutes": "minutes",
    "points": "points",
    "fieldGoalsMade-fieldGoalsAttempted": ("field_goals_made", "field_goals_attempted"),
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": (
        "three_point_made", "three_point_attempted"),
    "freeThrowsMade-freeThrowsAttempted": ("free_throws_made", "free_throws_attempted"),
    "rebounds": "total_rebounds",
    "assists": "assists",
    "turnovers": "turnovers",
    "steals": "steals",
    "blocks": "blocks",
    "fouls": "personal_fouls",
    "plusMinus": "plus_minus",
}


def _number(value):
    """Return a numeric value, or None when the provider value is not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip().replace("%", "")
    if not text or text in {"-", "--", "N/A", "null", "None"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number == int(number) else number


def _pair(value):
    """Split a provider 'made-attempted' value, refusing malformed pairs."""
    if value is None:
        return None, None
    text = str(value).strip()
    if "-" not in text:
        return None, None
    made, _, attempted = text.partition("-")
    return _number(made), _number(attempted)


def ensure_schema():
    """Create the basketball statistics tables when they are absent."""
    conn, db_type = storage._connect()
    if db_type != "postgres":
        raise RuntimeError("basketball enrichment requires the PostgreSQL/Neon store")
    try:
        with conn.cursor() as cur:
            for statement in SCHEMA_SQL:
                cur.execute(statement)
        conn.commit()
    finally:
        conn.close()


def fetch_summary(event_id, league_id=12):
    """Fetch one ESPN event summary, caching the raw payload on disk.

    Returns the parsed JSON payload, or None when ESPN has no summary for the
    event (a genuine absence, not an error).
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{event_id}.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            pass

    league_path = SPORT_PATHS.get(league_id, "nba")
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/{league_path}/summary"
    backoff = 1.5
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, params={"event": event_id}, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            if attempt >= MAX_RETRIES:
                raise
            time.sleep(backoff)
            backoff *= 2
            continue

        if response.status_code == 404:
            return None
        if response.status_code == 429:
            if attempt >= MAX_RETRIES:
                return None
            time.sleep(backoff * 2)
            backoff *= 2
            continue
        if response.status_code >= 500:
            if attempt >= MAX_RETRIES:
                return None
            time.sleep(backoff)
            backoff *= 2
            continue
        if not response.ok:
            return None

        try:
            data = response.json()
        except ValueError:
            return None

        temp = cache_path + f".tmp.{os.getpid()}"
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(temp, cache_path)
        except OSError:
            if os.path.exists(temp):
                try:
                    os.remove(temp)
                except OSError:
                    pass
        return data
    return None


def _team_statistics(block):
    """Translate one ESPN team statistics block into typed column values."""
    values = {column: None for column in TEAM_COLUMNS}
    for entry in block.get("statistics") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        raw = entry.get("displayValue")
        if raw is None:
            raw = entry.get("value")
        if name in MADE_ATTEMPTED:
            made_col, att_col = MADE_ATTEMPTED[name]
            made, attempted = _pair(raw)
            if made is not None and attempted is not None:
                values[made_col] = made
                values[att_col] = attempted
            continue
        if name == "fouls":
            values["personal_fouls"] = _number(raw)
            continue
        column = SINGLE_STATS.get(name)
        if column:
            value = _number(raw)
            if value is not None:
                values[column] = value
    return values


def _period_scores(competition):
    """Extract quarter/period scores from the header competitors."""
    rows = []
    for competitor in competition.get("competitors") or []:
        if not isinstance(competitor, dict):
            continue
        home_away = competitor.get("homeAway")
        if home_away not in ("home", "away"):
            continue
        for index, line in enumerate(competitor.get("linescores") or [], start=1):
            if not isinstance(line, dict):
                continue
            points = _number(line.get("displayValue", line.get("value")))
            if points is None:
                continue
            # A standard game has four periods; anything beyond is overtime.
            rows.append((home_away, index, int(points), index > 4))
    return rows


def _player_rows(group):
    """Translate one ESPN player group into per-player typed values."""
    team = group.get("team") or {}
    rows = []
    for stat_block in group.get("statistics") or []:
        if not isinstance(stat_block, dict):
            continue
        keys = stat_block.get("keys") or []
        athletes = stat_block.get("athletes") or []
        for athlete_entry in athletes:
            if not isinstance(athlete_entry, dict):
                continue
            athlete = athlete_entry.get("athlete") or {}
            player_id = athlete.get("id") or athlete_entry.get("id")
            if player_id is None:
                continue
            try:
                player_id = int(player_id)
            except (TypeError, ValueError):
                continue
            values = {column: None for column in PLAYER_COLUMNS}
            stats = athlete_entry.get("stats") or []
            for position, raw in enumerate(stats):
                if position >= len(keys):
                    break
                mapped = PLAYER_KEY_MAP.get(str(keys[position]))
                if mapped is None:
                    continue
                if isinstance(mapped, tuple):
                    made_col, att_col = mapped
                    made, attempted = _pair(raw)
                    if made is not None and attempted is not None:
                        values[made_col] = made
                        values[att_col] = attempted
                else:
                    value = _number(raw)
                    if value is not None:
                        values[mapped] = value
            rows.append({
                "team_id": _number(team.get("id")),
                "home_away": None,
                "player_id": player_id,
                "player_name": athlete.get("displayName"),
                "starter": athlete_entry.get("starter"),
                "did_not_play": athlete_entry.get("didNotPlay"),
                "values": values,
                "raw_json": athlete_entry,
            })
    return rows


def parse_summary(data):
    """Parse an ESPN summary into team statistics, period scores and player lines.

    Returns None when the payload carries no usable boxscore, so an empty or
    summary-less response is never mistaken for enriched statistics.
    """
    if not isinstance(data, dict):
        return None
    boxscore = data.get("boxscore") or {}
    teams = boxscore.get("teams") or []
    if not teams:
        return None

    competition = ((data.get("header") or {}).get("competitions") or [{}])[0]
    if not isinstance(competition, dict):
        competition = {}

    # ESPN reports the game total in the header competitor score, not in the
    # boxscore statistics block, so the authoritative value is taken from there.
    final_scores = {}
    for competitor in competition.get("competitors") or []:
        if not isinstance(competitor, dict):
            continue
        home_away = competitor.get("homeAway")
        if home_away not in ("home", "away"):
            continue
        score = competitor.get("score")
        if isinstance(score, dict):
            score = score.get("value", score.get("displayValue"))
        points = _number(score)
        if points is not None:
            final_scores[home_away] = int(points)

    team_rows = []
    for block in teams:
        if not isinstance(block, dict):
            continue
        home_away = block.get("homeAway")
        team = block.get("team") or {}
        if home_away not in ("home", "away"):
            continue
        values = _team_statistics(block)
        if values["points"] is None and home_away in final_scores:
            values["points"] = final_scores[home_away]
        populated = [column for column in TEAM_COLUMNS if values[column] is not None]
        if not populated:
            continue
        team_rows.append({
            "home_away": home_away,
            "team_id": _number(team.get("id")),
            "team_name": team.get("displayName"),
            "values": values,
            "field_mask": ",".join(sorted(populated)),
        })
    if not team_rows:
        return None

    periods = _period_scores(competition)

    players = []
    for group in boxscore.get("players") or []:
        if isinstance(group, dict):
            players.extend(_player_rows(group))

    return {"teams": team_rows, "periods": periods, "players": players}


def _upsert(cur, table, columns, conflict, rows):
    """Idempotent upsert that never replaces a verified value with NULL."""
    if not rows:
        return 0
    update_cols = [c for c in columns if c not in conflict]
    assignments = ", ".join(
        f"{c} = COALESCE(EXCLUDED.{c}, {table}.{c})" for c in update_cols)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(conflict)}) DO UPDATE SET {assignments}"
    )
    cur.executemany(sql, rows)
    return len(rows)


def save_parsed(game, parsed, source="espn_boxscore", provider_version="espn_summary_v2"):
    """Persist a parsed summary for one game. Returns rows written per table."""
    now = datetime.now(timezone.utc).isoformat()
    count = {"teams": 0, "periods": 0, "players": 0}
    conn, _ = storage._connect()
    try:
        with conn.cursor() as cur:
            team_rows = []
            for row in parsed["teams"]:
                values = row["values"]
                team_rows.append(tuple(
                    [source, game["game_id"], game["league_id"], game["season"],
                     row["team_id"], row["team_name"], row["home_away"]]
                    + [values[c] for c in TEAM_COLUMNS]
                    + [row["field_mask"], provider_version,
                       json.dumps(row["values"]), now]))
            count["teams"] = _upsert(
                cur, "historical_basketball_team_statistics",
                ["source", "game_id", "league_id", "season", "team_id", "team_name",
                 "home_away"] + list(TEAM_COLUMNS) +
                ["field_mask", "provider_version", "raw_json", "fetched_at"],
                ["source", "game_id", "home_away"], team_rows)

            period_rows = [
                (source, game["game_id"], game["league_id"], game["season"],
                 home_away, period, points, overtime, now)
                for home_away, period, points, overtime in parsed["periods"]
            ]
            count["periods"] = _upsert(
                cur, "historical_basketball_period_scores",
                ["source", "game_id", "league_id", "season", "home_away", "period",
                 "points", "is_overtime", "fetched_at"],
                ["source", "game_id", "home_away", "period"], period_rows)

            side_by_team = {
                row["team_id"]: row["home_away"] for row in parsed["teams"]
                if row["team_id"] is not None
            }
            player_rows = []
            for row in parsed["players"]:
                values = row["values"]
                populated = [c for c in PLAYER_COLUMNS if values[c] is not None]
                if not populated and not row.get("did_not_play"):
                    continue
                player_rows.append(tuple(
                    [source, game["game_id"], game["league_id"], game["season"],
                     row["team_id"], side_by_team.get(row["team_id"]),
                     row["player_id"], row["player_name"], row["starter"],
                     row["did_not_play"]]
                    + [values[c] for c in PLAYER_COLUMNS]
                    + [",".join(sorted(populated)), provider_version,
                       json.dumps(row["raw_json"]), now]))
            count["players"] = _upsert(
                cur, "historical_basketball_player_statistics",
                ["source", "game_id", "league_id", "season", "team_id", "home_away",
                 "player_id", "player_name", "starter", "did_not_play"]
                + list(PLAYER_COLUMNS) +
                ["field_mask", "provider_version", "raw_json", "fetched_at"],
                ["source", "game_id", "player_id"], player_rows)
        conn.commit()
    finally:
        conn.close()
    return count


def pending_games(league_id=None, season=None, limit=None):
    """Games that have no team statistics yet, oldest first."""
    conn, _ = storage._connect()
    try:
        with conn.cursor() as cur:
            clauses = []
            params = []
            if league_id is not None:
                clauses.append("g.league_id = %s")
                params.append(league_id)
            if season is not None:
                clauses.append("g.season = %s")
                params.append(season)
            filters = " AND ".join(clauses) if clauses else "TRUE"
            sql = f"""
                SELECT g.game_id, g.league_id, g.season, g.source
                  FROM historical_basketball_games g
                 WHERE NOT EXISTS (
                       SELECT 1 FROM historical_basketball_team_statistics s
                        WHERE s.game_id = g.game_id AND s.source = 'espn_boxscore'
                          AND s.points IS NOT NULL)
                   AND {filters}
                 ORDER BY g.game_date, g.game_id
            """
            if limit:
                sql += " LIMIT %s"
                params.append(int(limit))
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _fetch_and_parse(game):
    """Fetch and parse one game (thread-safe); persistence stays serial."""
    try:
        data = fetch_summary(game["game_id"], game["league_id"] or 12)
    except Exception:
        return game, None, "error"
    parsed = parse_summary(data) if data else None
    return game, parsed, ("ok" if parsed else "empty")


def run(limit=None, league_id=None, season=None, delay=0.0, verbose=True, workers=1):
    """Enrich pending games from ESPN summaries and persist verified statistics.

    Network retrieval is parallel; every database write stays on the calling
    thread so the shared connection is never touched concurrently.
    """
    from concurrent.futures import ThreadPoolExecutor

    ensure_schema()
    games = pending_games(league_id=league_id, season=season, limit=limit)
    totals = {"games": 0, "empty": 0, "teams": 0, "periods": 0, "players": 0, "errors": 0}
    batch = [{"game_id": int(g[0]), "league_id": g[1], "season": g[2]} for g in games]

    def handle(game, parsed, status):
        if status != "ok":
            totals["errors" if status == "error" else "empty"] += 1
            return
        counts = save_parsed(game, parsed)
        totals["games"] += 1
        totals["teams"] += counts["teams"]
        totals["periods"] += counts["periods"]
        totals["players"] += counts["players"]
        if verbose and totals["games"] % 25 == 0:
            print(f"  ...{totals['games']} games enriched", flush=True)

    if workers and workers > 1:
        with ThreadPoolExecutor(max_workers=int(workers)) as pool:
            for game, parsed, status in pool.map(_fetch_and_parse, batch):
                handle(game, parsed, status)
    else:
        for game in batch:
            handle(*_fetch_and_parse(game))
            if delay:
                time.sleep(delay)
    return totals


def main(argv=None):
    parser = argparse.ArgumentParser(description="Enrich basketball games with ESPN boxscores")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--league", type=int, default=None)
    parser.add_argument("--season", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--ensure-schema", action="store_true")
    args = parser.parse_args(argv)

    if args.ensure_schema:
        ensure_schema()
        print("basketball statistics schema ensured")
        return 0

    totals = run(limit=args.limit, league_id=args.league, season=args.season,
                 delay=args.delay, workers=args.workers)
    print(json.dumps(totals, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
