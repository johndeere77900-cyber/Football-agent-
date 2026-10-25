"""
ESPN URL-based provider for historical NBA basketball games.

Primary route for historical basketball acquisition when league_id == 12 (NBA).
Queries ESPN's URL-based scoreboard endpoint for months October (start year)
through June (following year).

Endpoint URL:
https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard
"""

import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ESPN_NBA_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
NBA_LEAGUE_ID = 12


def fetch_espn_nba_season_games(league_id: int, season: int, timeout: int = 15) -> tuple[list, dict]:
    """
    Fetch and normalize historical NBA games from ESPN scoreboard API for a given season.

    For NBA league_id (12) and season YYYY, queries months Oct YYYY through June YYYY+1.
    Deduplicates events by ESPN event ID.

    Returns:
        tuple (normalized_games, coverage_metadata)
    """
    coverage_meta = {
        "months_attempted": 0,
        "months_succeeded": 0,
        "months_failed": 0,
        "all_months_succeeded": False,
        "monthly_details": [],
    }

    try:
        lid = int(league_id)
        ssn = int(season)
    except (ValueError, TypeError):
        logger.warning(f"Invalid league_id ({league_id}) or season ({season}) provided to ESPN provider.")
        return [], coverage_meta

    # Confirm configured league ID represents the NBA (12)
    if lid != NBA_LEAGUE_ID:
        logger.info(f"League ID {lid} is not NBA ({NBA_LEAGUE_ID}). Skipping ESPN NBA provider.")
        return [], coverage_meta

    months = [
        (ssn, 10), (ssn, 11), (ssn, 12),
        (ssn + 1, 1), (ssn + 1, 2), (ssn + 1, 3),
        (ssn + 1, 4), (ssn + 1, 5), (ssn + 1, 6),
    ]

    seen_event_ids = set()
    normalized_games = []

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    for year, month in months:
        date_param = f"{year}{month:02d}"
        url = f"{ESPN_NBA_SCOREBOARD_URL}?dates={date_param}&limit=1000"
        coverage_meta["months_attempted"] += 1

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_bytes = resp.read()
                data = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            logger.warning(f"ESPN scoreboard query failed for date {date_param}: {exc}")
            coverage_meta["months_failed"] += 1
            coverage_meta["monthly_details"].append({"month": date_param, "status": "FAILED", "error": str(exc)})
            continue

        if not isinstance(data, dict):
            coverage_meta["months_failed"] += 1
            coverage_meta["monthly_details"].append({"month": date_param, "status": "FAILED", "error": "non_object_json"})
            continue

        events = data.get("events")
        if not isinstance(events, list):
            coverage_meta["months_failed"] += 1
            coverage_meta["monthly_details"].append({"month": date_param, "status": "FAILED", "error": "missing_events_list"})
            continue

        coverage_meta["months_succeeded"] += 1
        coverage_meta["monthly_details"].append({"month": date_param, "status": "SUCCESS", "event_count": len(events)})

        for ev in events:
            if not isinstance(ev, dict):
                continue

            eid_raw = ev.get("id")
            if not eid_raw:
                continue

            try:
                game_id = int(eid_raw)
                if game_id <= 0:
                    continue
            except (ValueError, TypeError):
                continue

            if game_id in seen_event_ids:
                continue

            iso_date = ev.get("date")
            if not iso_date or not isinstance(iso_date, str):
                continue

            competitions = ev.get("competitions")
            if not isinstance(competitions, list) or not competitions or not isinstance(competitions[0], dict):
                continue

            comp = competitions[0]
            status_obj = comp.get("status", {}) if isinstance(comp.get("status"), dict) else {}
            status_type = status_obj.get("type", {}) if isinstance(status_obj.get("type"), dict) else {}

            completed = status_type.get("completed", False)
            status_name = status_type.get("name", "")
            is_final = completed or status_name == "STATUS_FINAL"
            status_short = "FT" if is_final else "NS"

            competitors = comp.get("competitors")
            if not isinstance(competitors, list) or len(competitors) < 2:
                continue

            home_comp = None
            away_comp = None
            for c in competitors:
                if isinstance(c, dict):
                    if c.get("homeAway") == "home":
                        home_comp = c
                    elif c.get("homeAway") == "away":
                        away_comp = c

            if not home_comp or not away_comp:
                continue

            home_team_obj = home_comp.get("team", {}) if isinstance(home_comp.get("team"), dict) else {}
            away_team_obj = away_comp.get("team", {}) if isinstance(away_comp.get("team"), dict) else {}

            try:
                home_team_id = int(home_team_obj.get("id"))
                away_team_id = int(away_team_obj.get("id"))
                if home_team_id <= 0 or away_team_id <= 0 or home_team_id == away_team_id:
                    continue
            except (ValueError, TypeError):
                continue

            home_name = home_team_obj.get("displayName") or home_team_obj.get("name")
            away_name = away_team_obj.get("displayName") or away_team_obj.get("name")
            if not home_name or not isinstance(home_name, str) or not home_name.strip():
                continue
            if not away_name or not isinstance(away_name, str) or not away_name.strip():
                continue

            home_score_raw = home_comp.get("score")
            away_score_raw = away_comp.get("score")
            home_points = None
            away_points = None

            try:
                if home_score_raw is not None:
                    home_points = int(home_score_raw)
                if away_score_raw is not None:
                    away_points = int(away_score_raw)
            except (ValueError, TypeError):
                pass

            game_dict = {
                "id": game_id,
                "date": iso_date,
                "status": {
                    "short": status_short,
                    "long": status_type.get("description", "Final") if is_final else "Not Started",
                },
                "league": {
                    "id": lid,
                    "season": ssn,
                    "name": "NBA",
                },
                "teams": {
                    "home": {"id": home_team_id, "name": home_name.strip()},
                    "away": {"id": away_team_id, "name": away_name.strip()},
                },
                "scores": {
                    "home": {"total": home_points},
                    "away": {"total": away_points},
                },
                "provider_provenance": {
                    "provider": "espn_url",
                    "provider_type": "primary_url",
                    "provider_game_id": game_id,
                    "provider_team_ids": {"home": home_team_id, "away": away_team_id},
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                },
            }

            seen_event_ids.add(game_id)
            normalized_games.append(game_dict)

    coverage_meta["all_months_succeeded"] = (
        coverage_meta["months_attempted"] == len(months)
        and coverage_meta["months_failed"] == 0
        and coverage_meta["months_succeeded"] == len(months)
    )

    logger.info(f"ESPN NBA provider fetched {len(normalized_games)} games for season {ssn} (succeeded {coverage_meta['months_succeeded']}/{coverage_meta['months_attempted']} months).")
    return normalized_games, coverage_meta
