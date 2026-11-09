"""Fill missing football statistics from the approved Football-Data.co.uk CSV source.

The acquisition layer already stores fixtures. For seasons where the stored
payload carries no statistics, the approved ``football_data_csv`` source still
publishes the full match statistics block. This job re-reads that approved
source and attaches its statistics to fixtures that *already exist* in the
database.

Guarantees:
- No fixture is created, invented or deleted. Matching is by league, season,
  kickoff date and normalised team names, and only an unambiguous single match
  is accepted.
- A statistic is written only when the CSV row supplies it. Empty cells stay
  NULL and are never coerced to zero.
- Existing verified values are retained: the writer merges additively.
- The job is idempotent and resumable; already-enriched fixtures are skipped.

Dry-run by default; pass ``--apply`` to write.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import data_resolver
import storage
import team_identity

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TIMEOUT = 25

# CSV column -> statistics field, for the ratio-free match statistics block.
COLUMN_FIELDS = {
    "HS": "shots",
    "AS": "shots",
    "HST": "shots_on_target",
    "AST": "shots_on_target",
    "HC": "corners",
    "AC": "corners",
    "HY": "yellow_cards",
    "AY": "yellow_cards",
    "HR": "red_cards",
    "AR": "red_cards",
}


def _int(value):
    """Return a non-negative integer from a CSV cell, or None when absent."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = int(float(text))
    except ValueError:
        return None
    return number if number >= 0 else None


def _parse_date(text):
    """Parse the CSV date formats into an ISO date string, or None."""
    text = str(text or "").strip()
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _norm(name):
    return team_identity.normalize_team_name(name)


def fetch_csv_rows(league_id, season):
    """Download and parse one approved CSV league-season, returning raw rows."""
    code = data_resolver.LEAGUE_TO_FD_CSV_CODE.get(league_id)
    season_code = data_resolver._format_fd_csv_season_code(season)
    if not code or not season_code:
        return None, "unsupported_league_or_season"
    url = data_resolver.FOOTBALL_DATA_CSV_URL_PATTERN.format(
        season_code=season_code, division_code=code)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            content = response.read().decode("utf-8-sig", errors="ignore")
    except Exception as exc:
        return None, f"fetch_failed:{type(exc).__name__}"
    if not content.strip() or "<html" in content[:200].lower():
        return None, "no_csv_published"
    reader = csv.DictReader(io.StringIO(content))
    return [row for row in reader if row], "ok"


def statistics_from_row(row):
    """Build the additive statistics payload a CSV row genuinely supplies."""
    statistics = {}
    for column, field in COLUMN_FIELDS.items():
        value = _int(row.get(column))
        if value is None:
            continue
        side = "home" if column.startswith("H") else "away"
        statistics.setdefault(field, {})[side] = value
    # A field is usable only when both sides are present and unambiguous.
    usable = {
        field: values for field, values in statistics.items()
        if values.get("home") is not None and values.get("away") is not None
    }
    return usable


def load_fixtures(conn, league_id, season):
    """Stored fixtures for one league-season, keyed by ISO date."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fixture_id, kickoff_at, home_team, away_team
              FROM historical_fixtures
             WHERE league_id = %s AND season = %s
            """,
            (league_id, season))
        rows = cur.fetchall()
    by_date = {}
    for fixture_id, kickoff_at, home, away in rows:
        iso = str(kickoff_at or "")[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", iso):
            continue
        by_date.setdefault(iso, []).append(
            (int(fixture_id), _norm(home), _norm(away)))
    return by_date, len(rows)


def load_enriched(conn, fixture_ids):
    """Fixture ids that already hold at least one usable statistic."""
    if not fixture_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT fixture_id FROM historical_fixture_enrichment
             WHERE home_shots IS NOT NULL
               AND fixture_id = ANY(%s)
            """,
            (list(fixture_ids),))
        return {int(row[0]) for row in cur.fetchall()}


def plan_season(conn, league_id, season):
    """Match CSV rows to stored fixtures without writing anything."""
    rows, status = fetch_csv_rows(league_id, season)
    result = {
        "league_id": league_id,
        "season": season,
        "source_status": status,
        "csv_rows": len(rows or []),
        "stored_fixtures": 0,
        "matched": 0,
        "already_enriched": 0,
        "unmatched_csv_rows": 0,
        "ambiguous": 0,
    }
    if not rows:
        return result, []

    by_date, stored = load_fixtures(conn, league_id, season)
    result["stored_fixtures"] = stored
    if not by_date:
        return result, []

    matched_ids = set()
    plan = []
    for row in rows:
        iso = _parse_date(row.get("Date"))
        home = _norm(row.get("HomeTeam"))
        away = _norm(row.get("AwayTeam"))
        if not iso or not home or not away:
            result["unmatched_csv_rows"] += 1
            continue
        candidates = []
        # The CSV date is authoritative, but a fixture stored from a
        # date-only source can sit one day either side of it.
        for offset in (0, -1, 1):
            day = (date.fromisoformat(iso) + timedelta(days=offset)).isoformat()
            candidates.extend(by_date.get(day, []))
        exact = [c for c in candidates if c[1] == home and c[2] == away]
        if not exact:
            result["unmatched_csv_rows"] += 1
            continue
        if len(exact) > 1:
            result["ambiguous"] += 1
            continue
        fixture_id = exact[0][0]
        if fixture_id in matched_ids:
            result["ambiguous"] += 1
            continue
        matched_ids.add(fixture_id)
        statistics = statistics_from_row(row)
        if not statistics:
            continue
        plan.append({"fixture_id": fixture_id, "statistics": statistics,
                     "date": iso, "home": home, "away": away})

    enriched = load_enriched(conn, matched_ids)
    result["matched"] = len(matched_ids)
    result["already_enriched"] = sum(1 for item in plan if item["fixture_id"] in enriched)
    return result, [item for item in plan if item["fixture_id"] not in enriched]


def apply_plan(plan):
    """Persist the planned statistics through the existing writer contract."""
    if not plan:
        return 0
    items = [{
        "fixture": {"id": item["fixture_id"]},
        "statistics": item["statistics"],
    } for item in plan]
    return storage.save_historical_enrichment(items, source="football_data_csv")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leagues", type=str, default=None,
                        help="comma separated league ids; default all supported")
    parser.add_argument("--seasons", type=int, nargs="+", default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    leagues = ([int(x) for x in args.leagues.split(",")] if args.leagues
               else sorted(data_resolver.LEAGUE_TO_FD_CSV_CODE))
    seasons = args.seasons or list(range(2019, 2027))

    conn, db_type = storage._connect()
    if db_type != "postgres":
        raise SystemExit("this job targets the PostgreSQL/Neon store only")
    summaries = []
    try:
        for league_id in leagues:
            for season in seasons:
                result, plan = plan_season(conn, league_id, season)
                if args.apply and plan:
                    written = apply_plan(plan)
                    result["written"] = written
                result["planned"] = len(plan)
                summaries.append(result)
                if result["source_status"] != "ok" and result["csv_rows"] == 0:
                    continue
                print(json.dumps(result), flush=True)
    finally:
        conn.close()

    print(json.dumps({
        "league_seasons_with_csv": sum(1 for s in summaries if s["csv_rows"]),
        "planned_total": sum(s.get("planned", 0) for s in summaries),
        "written_total": sum(s.get("written", 0) for s in summaries),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
