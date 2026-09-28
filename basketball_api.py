"""
Thin wrapper around the API-Basketball v1 API (api-sports.io).

Uses the same API key as api_football.py. Provides file-based caching and
automatic retry with backoff for rate-limit responses.

The module validates identifiers and dates, treats malformed API responses
as errors, and avoids allowing a corrupted cache file to break API access.
"""

import datetime
import json
import os
import time

import requests

import config


MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5


def _validate_positive_int(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
    ):
        raise ValueError(
            f"{name} must be a positive integer."
        )

    return value


def _validate_date(date_str):
    if not isinstance(date_str, str):
        raise ValueError(
            "date_str must be a string in YYYY-MM-DD format."
        )

    try:
        parsed = datetime.date.fromisoformat(
            date_str
        )
    except ValueError as exc:
        raise ValueError(
            "date_str must be a valid YYYY-MM-DD date."
        ) from exc

    if parsed.isoformat() != date_str:
        raise ValueError(
            "date_str must be in YYYY-MM-DD format."
        )

    return parsed


def _headers():
    key = getattr(
        config,
        "API_FOOTBALL_KEY",
        None,
    )

    if not isinstance(key, str) or not key.strip():
        raise ValueError(
            "API_FOOTBALL_KEY is not configured."
        )

    return {
        "x-apisports-key": key,
    }


def _cache_path(key):
    if not isinstance(key, str) or not key:
        raise ValueError(
            "Cache key must be a non-empty string."
        )

    os.makedirs(
        config.CACHE_DIR,
        exist_ok=True,
    )

    safe_key = (
        "basketball_"
        + key.replace("/", "_")
        .replace("?", "_")
        .replace("&", "_")
        .replace("\\", "_")
    )

    return os.path.join(
        config.CACHE_DIR,
        safe_key + ".json",
    )


def _cache_get(key):
    path = _cache_path(key)

    if not os.path.exists(path):
        return None

    try:
        age_hours = (
            time.time()
            - os.path.getmtime(path)
        ) / 3600

        if (
            age_hours < 0
            or age_hours > config.CACHE_TTL_HOURS
        ):
            return None

        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return None

        return data

    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
    ):
        # A broken cache must never prevent a fresh API request.
        return None


def _cache_set(key, data):
    if not isinstance(data, dict):
        raise ValueError(
            "Cached API data must be a dictionary."
        )

    path = _cache_path(key)
    temp_path = path + ".tmp"

    try:
        with open(
            temp_path,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
            )

        os.replace(
            temp_path,
            path,
        )

    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _validate_api_response(data):
    if not isinstance(data, dict):
        raise ValueError(
            "API response must be a JSON object."
        )

    errors = data.get("errors")

    if errors:
        raise RuntimeError(
            f"API-Basketball returned errors: {errors}"
        )

    response = data.get("response")

    if response is None:
        raise ValueError(
            "API response is missing the response field."
        )

    return data


def _get(endpoint, params):
    """
    Make a GET request to API-Basketball, using the on-disk cache first.

    Retries automatically on HTTP 429 responses with increasing backoff.
    """
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError(
            "endpoint must be a non-empty string."
        )

    if not isinstance(params, dict):
        raise ValueError(
            "params must be a dictionary."
        )

    cache_key = (
        endpoint
        + "_"
        + json.dumps(
            params,
            sort_keys=True,
            separators=(",", ":"),
        )
    )

    cached = _cache_get(cache_key)

    if cached is not None:
        return cached

    url = (
        f"{config.API_BASKETBALL_BASE_URL}"
        f"/{endpoint}"
    )

    backoff = RETRY_BACKOFF_SECONDS
    last_response = None

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):
        try:
            resp = requests.get(
                url,
                headers=_headers(),
                params=params,
                timeout=15,
            )
        except requests.RequestException:
            if attempt >= MAX_RETRIES:
                raise

            time.sleep(backoff)
            backoff *= 2
            continue

        last_response = resp

        if (
            resp.status_code == 429
            and attempt < MAX_RETRIES
        ):
            print(
                "  Rate limited (429), "
                f"retrying in {backoff}s "
                f"(attempt {attempt}/{MAX_RETRIES})..."
            )
            time.sleep(backoff)
            backoff *= 2
            continue

        resp.raise_for_status()

        try:
            data = resp.json()
        except ValueError as exc:
            raise ValueError(
                "API-Basketball returned invalid JSON."
            ) from exc

        data = _validate_api_response(
            data
        )

        if data.get("response"):
            _cache_set(
                cache_key,
                data,
            )

        return data

    if last_response is not None:
        last_response.raise_for_status()

    raise RuntimeError(
        "API-Basketball request failed without a response."
)
    
def get_games_by_date(
    date_str,
    league_id,
):
    """
    Return games for a specific date and league.

    NBA seasons span two calendar years. Games from August onward are
    assigned to that calendar year's season; earlier games are assigned
    to the previous season.
    """
    parsed_date = _validate_date(
        date_str
    )

    league_id = _validate_positive_int(
        league_id,
        "league_id",
    )

    year = parsed_date.year
    month = parsed_date.month

    season = (
        year
        if month >= 8
        else year - 1
    )

    if season <= 0:
        raise ValueError(
            "Unable to derive a valid season."
        )

    params = {
        "date": date_str,
        "league": league_id,
        "season": season,
    }

    data = _get(
        "games",
        params,
    )

    response = data.get(
        "response",
        [],
    )

    if not isinstance(
        response,
        list,
    ):
        raise ValueError(
            "API games response must be a list."
        )

    return response


def get_team_statistics(
    team_id,
    league_id,
    season,
):
    """Return season-aggregate statistics for one basketball team."""
    team_id = _validate_positive_int(
        team_id,
        "team_id",
    )

    league_id = _validate_positive_int(
        league_id,
        "league_id",
    )

    season = _validate_positive_int(
        season,
        "season",
    )

    params = {
        "team": team_id,
        "league": league_id,
        "season": season,
    }

    data = _get(
        "statistics",
        params,
    )

    response = data.get(
        "response",
        {},
    )

    if not isinstance(
        response,
        dict,
    ):
        raise ValueError(
            "API statistics response must be a dictionary."
        )

    return response


def get_game_result(
    game_id,
):
    """Return the result record for a single basketball game."""
    game_id = _validate_positive_int(
        game_id,
        "game_id",
    )

    params = {
        "id": game_id,
    }

    data = _get(
        "games",
        params,
    )

    response = data.get(
        "response",
        [],
    )

    if not isinstance(
        response,
        list,
    ):
        raise ValueError(
            "API game-result response must be a list."
        )

    if not response:
        return None

    return response[0]
