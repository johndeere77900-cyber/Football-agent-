"""
Thin wrapper around the API-Football v3 API, with simple file-based caching
so repeated calls for the same team on the same day don't burn through the
free-tier daily request limit.
"""

import json
import os
import time
import requests

import config


def _headers():
    return {
        "x-apisports-key": config.API_FOOTBALL_KEY,
    }


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
    """Make a GET request to API-Football, using the on-disk cache first."""
    cache_key = endpoint + "_" + json.dumps(params, sort_keys=True)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{config.API_FOOTBALL_BASE_URL}/{endpoint}"
    resp = requests.get(url, headers=_headers(), params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if data.get("response"):
        _cache_set(cache_key, data)

    return data


def get_fixtures_by_date(date_str, league_id=None):
    """date_str format: 'YYYY-MM-DD'. Returns list of fixtures for that date."""
    params = {"date": date_str}
    if league_id:
        params["league"] = league_id
    data = _get("fixtures", params)
    response = data.get("response", [])
    return response if isinstance(response, list) else []


def get_team_statistics(team_id, league_id, season):
    """
    Season-aggregate stats for a team in a given league/season - one call.
    API-Football sometimes returns an empty list [] instead of a stats
    object when data isn't available (common for smaller leagues on the
    free tier) - this safely returns an empty dict in that case so the
    caller can fall back to league-average numbers instead of crashing.
    """
    params = {"team": team_id, "league": league_id, "season": season}
    data = _get("teams/statistics", params)
    response = data.get("response", {})
    if not isinstance(response, dict):
        return {}
    return response


def get_head_to_head(team_a_id, team_b_id, last=10):
    """Recent head-to-head matches between two teams."""
    params = {"h2h": f"{team_a_id}-{team_b_id}", "last": last}
    data = _get("fixtures/headtohead", params)
    response = data.get("response", [])
    return response if isinstance(response, list) else []


def get_recent_form(team_id, last=8):
    """Most recent finished matches for a team, used for recent-form weighting."""
    params = {"team": team_id, "last": last, "status": "FT"}
    data = _get("fixtures", params)
    response = data.get("response", [])
    return response if isinstance(response, list) else []


def get_fixture_result(fixture_id):
    """Final score + stats for a single finished fixture (used for grading)."""
    params = {"id": fixture_id}
    data = _get("fixtures", params)
    response = data.get("response", [])
    if isinstance(response, list) and response:
        return response[0]
    return None
