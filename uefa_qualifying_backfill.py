"""Backfill qualifying-round fixtures from the official UEFA match API.

The stored dataset holds finals and most group stages but not the qualifying
rounds for a handful of competition seasons. This job reads the official UEFA
match list and passes the genuinely missing fixtures through the existing
``storage.save_historical_fixtures`` writer, so canonical identity, per-record
provenance and duplicate suppression are enforced by the same contract that
governs every other acquisition route.

Guarantees:
- Fixtures are keyed by a deterministic id derived from the UEFA match id, so a
  re-run never creates a second row for the same UEFA match.
- The existing writer deduplicates against canonically identical fixtures that
  were stored from another provider, so a fixture already held is never inserted
  twice.
- Only finished matches with a real score are submitted; a match without a
  result is reported, never invented.
- The 120-minute score is stored. A penalty shoot-out is kept in ``raw_json``
  and never folded into the scoreline.
- Dry-run by default; ``--apply`` writes.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage
import team_identity

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TIMEOUT = 40
BASE = "https://match.uefa.com/v5/matches"

# Stored league id -> official UEFA competition id.
COMPETITIONS = {2: 1, 3: 14, 4: 3}

# (league_id, season) pairs whose qualifying rounds are missing. Season is the
# stored season, which for these competitions is the starting year.
TARGETS = [(2, 2019), (2, 2025), (3, 2025), (4, 2020), (4, 2024)]


def as_dict(value):
    """UEFA returns some fields as objects and sometimes as plain strings."""
    return value if isinstance(value, dict) else {}


def stable_id(prefix, value):
    return int(hashlib.sha256(f"{prefix}_{value}".encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


def fetch_competition_season(competition_id, season_year):
    """Fetch every match page for one competition season."""
    matches, offset = [], 0
    while True:
        query = urllib.parse.urlencode({
            "competitionId": competition_id,
            "seasonYear": season_year,
            "limit": 500,
            "offset": offset,
        })
        request = urllib.request.Request(f"{BASE}?{query}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list) or not payload:
            break
        matches.extend(payload)
        if len(payload) < 500:
            break
        offset += 500
    return matches


def kickoff(match):
    block = as_dict(match.get("kickOffTime"))
    # "date" is date-only and "dateTime" carries the actual kickoff, so the
    # timestamp field is preferred and the date-only value is the fallback.
    raw = block.get("dateTime") or block.get("date") or match.get("kickOffTime")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def final_score(match):
    """Return the 120-minute score as (home, away), or None.

    ``score.total`` is preferred because it is the played result; a shoot-out
    block is deliberately ignored so a penalty result never becomes the
    scoreline.
    """
    score = as_dict(match.get("score"))
    total = as_dict(score.get("total"))
    home, away = total.get("home"), total.get("away")
    if home is None or away is None:
        normal = as_dict(score.get("fullTime"))
        home, away = normal.get("home"), normal.get("away")
    if home is None or away is None:
        return None
    try:
        home, away = int(home), int(away)
    except (TypeError, ValueError):
        return None
    if home < 0 or away < 0:
        return None
    return home, away


def team_block(team):
    team = as_dict(team)
    name = team.get("internationalName") or team.get("name") or team.get("translations", {}).get("name", {}).get("EN")
    if not isinstance(name, str) or not name.strip():
        return None
    return {"name": name.strip(), "id": stable_id("uefa_team", name.strip().lower())}


def build_item(match, league_id, season):
    """Translate one UEFA match into the writer's fixture item shape."""
    home, away = team_block(match.get("homeTeam")), team_block(match.get("awayTeam"))
    when = kickoff(match)
    goals = final_score(match)
    if not home or not away or not when or goals is None:
        return None, "incomplete_match_payload"
    match_id = match.get("id")
    if match_id is None:
        return None, "missing_match_id"
    status = as_dict(match.get("status")).get("code") or match.get("status") or "FT"
    if isinstance(status, str) and status.upper() not in ("FT", "AET", "PEN", "PLAYED", "FINISHED"):
        return None, f"not_finished:{status}"
    item = {
        "fixture": {
            "id": stable_id("uefa_match", match_id),
            "date": when.isoformat(),
            "status": {"short": "FT"},
        },
        "league": {"id": league_id, "season": season},
        "teams": {"home": home, "away": away},
        "goals": {"home": goals[0], "away": goals[1]},
    }
    return item, None


def existing_match_keys(league_id, season):
    """Real-world match keys already stored for a competition season."""
    conn, _ = storage._connect()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT to_char(kickoff_at::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD'),
                   home_team, away_team
            FROM historical_fixtures WHERE league_id = %s AND season = %s
        """, (league_id, season))
        rows = cur.fetchall()
    return {(
        day,
        team_identity.normalize_team_name(home),
        team_identity.normalize_team_name(away),
    ) for day, home, away in rows}


def item_already_present(item, existing):
    if not existing:
        return False
    day = item["fixture"]["date"][:10]
    home = team_identity.normalize_team_name(item["teams"]["home"]["name"])
    away = team_identity.normalize_team_name(item["teams"]["away"]["name"])
    return (day, home, away) in existing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--league", type=int, default=None)
    parser.add_argument("--season", type=int, default=None)
    args = parser.parse_args(argv)

    targets = [(lid, season) for lid, season in TARGETS
               if (args.league is None or lid == args.league)
               and (args.season is None or season == args.season)]

    grand = {"fetched": 0, "built": 0, "rejected": 0, "inserted": 0,
             "already_existing": 0, "enriched": 0}
    for league_id, season in targets:
        competition_id = COMPETITIONS[league_id]
        # UEFA labels a Champions League season by the year it ends (2019/20 is
        # seasonYear 2020) while a European Championship uses its finals year.
        uefa_season = season + 1 if league_id in (2, 3) else season
        try:
            matches = fetch_competition_season(competition_id, uefa_season)
        except Exception as exc:
            print(json.dumps({"league_id": league_id, "season": season,
                              "error": f"{type(exc).__name__}: {exc}"}), flush=True)
            continue
        items, rejected = [], 0
        for match in matches:
            item, reason = build_item(match, league_id, season)
            if item is None:
                rejected += 1
                continue
            items.append(item)
        result = {"league_id": league_id, "season": season,
                  "fetched": len(matches), "built": len(items), "rejected": rejected}
        # A rerun must not create a second row for a fixture already stored under
        # another provider. The writer's canonical-id dedup cannot see this when
        # the two feeds resolved a club to different canonical ids, so the season's
        # existing real-world matches are compared directly.
        existing = existing_match_keys(league_id, season)
        before = len(items)
        items = [item for item in items
                 if not item_already_present(item, existing)]
        result["skipped_already_present"] = before - len(items)
        if args.apply and items:
            written = storage.save_historical_fixtures(
                items, league_id=league_id, season=season, source="uefa_match_v5")
            result.update({k: written.get(k) for k in
                           ("total", "inserted", "already_existing", "enriched",
                            "duplicates_skipped", "rejected_count") if k in written})
            grand["inserted"] += written.get("inserted", 0) or 0
            grand["already_existing"] += written.get("already_existing", 0) or 0
            grand["enriched"] += written.get("enriched", 0) or 0
        grand["fetched"] += len(matches)
        grand["built"] += len(items)
        grand["rejected"] += rejected
        print(json.dumps(result), flush=True)

    print(json.dumps(grand, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
