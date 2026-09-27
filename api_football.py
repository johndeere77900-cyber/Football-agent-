"""
Thin wrapper around the API-Football v3 API, with simple file-based caching
so repeated calls for the same team on the same day don't burn through the
free-tier daily request limit. Includes automatic retry with backoff for
rate-limit (429) errors.
"""

import json
import os
import time
import requests

import config

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5


def _headers():
    return {"x-apisports-key": config.API_FOOTBALL_KEY}


def _cache_path(key):
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    safe_key = key.replace("/", "_").replace("?", "_").replace("&", "_")
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
    cache_key = endpoint + "_" + json.dumps(params, sort_keys=True)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{config.API_FOOTBALL_BASE_URL}/{endpoint}"
    backoff = RETRY_BACKOFF_SECONDS

    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(url, headers=_headers(), params=params, timeout=15)

        if resp.status_code == 429 and attempt < MAX_RETRIES:
            print(f"  Rate limited (429), retrying in {backoff}s (attempt {attempt}/{MAX_RETRIES})...")
            time.sleep(backoff)
            backoff *= 2
            continue

        resp.raise_for_status()
        data = resp.json()

        if data.get("response"):
            _cache_set(cache_key, data)

        return data

    resp.raise_for_status()


def get_fixtures_by_date(date_str, league_id=None):
    params = {"date": date_str}
    if league_id:
        params["league"] = league_id
    data = _get("fixtures", params)
    response = data.get("response", [])
    return response if isinstance(response, list) else []


def get_team_statistics(team_id, league_id, season):
    params = {"team": team_id, "league": league_id, "season": season}
    data = _get("teams/statistics", params)
    response = data.get("response", {})
    if not isinstance(response, dict):
        return {}
    return response


def get_head_to_head(team_a_id, team_b_id, last=10):
    params = {"h2h": f"{team_a_id}-{team_b_id}", "last": last}
    data = _get("fixtures/headtohead", params)
    response = data.get("response", [])
    return response if isinstance(response, list) else []


def get_recent_form(team_id, last=8):
    params = {"team": team_id, "last": last, "status": "FT"}
    data = _get("fixtures", params)
    response = data.get("response", [])
    return response if isinstance(response, list) else []


def get_fixture_result(fixture_id):
    params = {"id": fixture_id}
    data = _get("fixtures", params)
    response = data.get("response", [])
    if isinstance(response, list) and response:
        return response[0]
    return None


def get_league_fixtures(league_id, season):
    """Every fixture for an entire league season in one call - used for backtesting."""
    params = {"league": league_id, "season": season}
    data = _get("fixtures", params)
    response = data.get("response", [])
    return response if isinstance(response, list) else []


def get_league_standings(league_id, season):
    """
    Current live standings for a league/season - used to calculate a real,
    up-to-date league-average-goals figure instead of a guessed constant.
    """
    params = {"league": league_id, "season": season}
    data = _get("standings", params)
    response = data.get("response", [])
    if not response:
        return []
    try:
        groups = response[0]["league"]["standings"]
        return [team for group in groups for team in group]
    except (KeyError, IndexError, TypeError):
        return []


def search_leagues(name):
    """
    Searches API-Football's own league list by name - the safe way to find
    a correct league ID rather than guessing one, since numbering has
    changed between API versions in the past.
    """
    params = {"search": name}
    data = _get("leagues", params)
    response = data.get("response", [])
    return response if isinstance(response, list) else []


def get_league_coverage(league_id):
    """
    Asks API-Football what seasons exist for this league and what data is
    actually covered for each on the current plan - the definitive way to
    find out why a season might return nothing, rather than guessing.
    """
    params = {"id": league_id}
    data = _get("leagues", params)
    response = data.get("response", [])
    if not response:
        return []
    return response[0].get("seasons", [])
