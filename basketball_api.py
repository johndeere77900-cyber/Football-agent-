"""
Thin wrapper around the API-Basketball v1 API (api-sports.io). Uses the
SAME API key as api_football.py - one account covers all api-sports.io
products, including basketball, at no extra signup. Tracks its own
separate 100/day quota, entirely independent of the football one.

Mirrors api_football.py's structure: file-based caching, and automatic
retry with backoff on rate-limit (429) errors.
"""

import json
import os
import time
import requests

import config

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5


def _headers():
    return {
        "x-apisports-key": config.API_FOOTBALL_KEY,
    }


def _cache_path(key):
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    safe_key = "basketball_" + key.replace("/", "_").replace("?", "_").replace("&", "_")
    return os.path.join(config.CACHE_DIR, safe_key + ".json")


def _cache_get(key):
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    age_hours = (time.time() - os.path.getmtime(path)) / 3600
    if age_hours > config.CACHE_TTL_HOURS:
        return None
    with open(path, "r") as f:
        return json.load(f)


def _cache_set(key, data):
    path = _cache_path(key)
    with open(path, "w") as f:
        json.dump(data, f)


def _get(endpoint, params):
    """
    Make a GET request to API-Basketball, using the on-disk cache first.
    Retries automatically on a 429 (rate limit) with increasing backoff.
    """
    cache_key = endpoint + "_" + json.dumps(params, sort_keys=True)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{config.API_BASKETBALL_BASE_URL}/{endpoint}"
    backoff = RETRY_BACKOFF_SECONDS

    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(url, headers=_headers(), params=params, timeout=15)

        if resp.status_code == 429 and attempt < MAX_RETRIES:
            print(f"  Rate limited (429), retrying in {backoff}s "
                  f"(attempt {attempt}/{MAX_RETRIES})...")
            time.sleep(backoff)
            backoff *= 2
            continue

        resp.raise_for_status()
        data = resp.json()

        if data.get("response"):
            _cache_set(cache_key, data)

        return data

    resp.raise_for_status()


def get_games_by_date(date_str, league_id):
    """
    date_str format: 'YYYY-MM-DD'. Returns list of games for that date, in
    the given league (e.g. 12 for NBA). API-Basketball requires a season
    parameter - derived automatically from the date (NBA seasons span two
    calendar years, e.g. games in early 2027 belong to the 2026 season).
    """
    year = int(date_str.split("-")[0])
    month = int(date_str.split("-")[1])
    # NBA season "2026" runs roughly Oct 2026 - Jun 2027
    season = year if month >= 8 else year - 1

    params = {"date": date_str, "league": league_id, "season": season}
    data = _get("games", params)
    response = data.get("response", [])
    return response if isinstance(response, list) else []


def get_team_statistics(team_id, league_id, season):
    """Season-aggregate stats for a basketball team - one call."""
    params = {"team": team_id, "league": league_id, "season": season}
    data = _get("statistics", params)
    response = data.get("response", {})
    if not isinstance(response, dict):
        return {}
    return response


def get_game_result(game_id):
    """Final score for a single finished game (used for grading)."""
    params = {"id": game_id}
    data = _get("games", params)
    response = data.get("response", [])
    if isinstance(response, list) and response:
        return response[0]
    return None
