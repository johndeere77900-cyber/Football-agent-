"""
Thin wrapper around the API-Football v3 API.

Features:
- file-based caching;
- retry/backoff for rate limits;
- league fixture retrieval;
- historical fixture enrichment in batches;
- no fabricated API data.
"""

import json
import os
import time

import requests

import config


MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
FIXTURE_BATCH_SIZE = 20


def _headers():
    return {"x-apisports-key": config.API_FOOTBALL_KEY}


def _cache_path(key):
    os.makedirs(config.CACHE_DIR, exist_ok=True)

    safe_key = (
        key
        .replace("/", "_")
        .replace("?", "_")
        .replace("&", "_")
    )

    return os.path.join(
        config.CACHE_DIR,
        safe_key + ".json",
    )


def _cache_get(key):
    path = _cache_path(key)

    if not os.path.exists(path):
        return None

    age_hours = (
        time.time() - os.path.getmtime(path)
    ) / 3600

    if age_hours > config.CACHE_TTL_HOURS:
        return None

    with open(path, "r") as f:
        return json.load(f)


def _cache_set(key, data):
    path = _cache_path(key)

    with open(path, "w") as f:
        json.dump(data, f)


def _get(endpoint, params):
    cache_key = (
        endpoint
        + "_"
        + json.dumps(
            params,
            sort_keys=True,
        )
    )

    cached = _cache_get(cache_key)

    if cached is not None:
        return cached

    url = (
        f"{config.API_FOOTBALL_BASE_URL}"
        f"/{endpoint}"
    )

    backoff = RETRY_BACKOFF_SECONDS

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):
        resp = requests.get(
            url,
            headers=_headers(),
            params=params,
            timeout=15,
        )

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

        data = resp.json()

        if data.get("response"):
            _cache_set(
                cache_key,
                data,
            )

        return data

    resp.raise_for_status()


def get_fixtures_by_date(
    date_str,
    league_id=None,
):
    params = {"date": date_str}

    if league_id:
        params["league"] = league_id

    data = _get(
        "fixtures",
        params,
    )

    response = data.get(
        "response",
        [],
    )

    return (
        response
        if isinstance(response, list)
        else []
    )


def get_team_statistics(
    team_id,
    league_id,
    season,
):
    params = {
        "team": team_id,
        "league": league_id,
        "season": season,
    }

    data = _get(
        "teams/statistics",
        params,
    )

    response = data.get(
        "response",
        {},
    )

    return (
        response
        if isinstance(response, dict)
        else {}
    )


def get_head_to_head(
    team_a_id,
    team_b_id,
    last=10,
):
    params = {
        "h2h": f"{team_a_id}-{team_b_id}",
        "last": last,
    }

    data = _get(
        "fixtures/headtohead",
        params,
    )

    response = data.get(
        "response",
        [],
    )

    return (
        response
        if isinstance(response, list)
        else []
    )


def get_recent_form(
    team_id,
    last=8,
):
    params = {
        "team": team_id,
        "last": last,
        "status": "FT",
    }

    data = _get(
        "fixtures",
        params,
    )

    response = data.get(
        "response",
        [],
    )

    return (
        response
        if isinstance(response, list)
        else []
    )


def get_fixture_result(
    fixture_id,
):
    params = {"id": fixture_id}

    data = _get(
        "fixtures",
        params,
    )

    response = data.get(
        "response",
        [],
    )

    if (
        isinstance(response, list)
        and response
    ):
        return response[0]

    return None


def get_league_fixtures(
    league_id,
    season,
):
    """
    Retrieve every fixture for a league season.

    The result is cached by the normal API wrapper.
    """
    params = {
        "league": league_id,
        "season": season,
    }

    data = _get(
        "fixtures",
        params,
    )

    response = data.get(
        "response",
        [],
    )

    return (
        response
        if isinstance(response, list)
        else []
    )


def get_enriched_fixtures(
    fixture_ids,
    batch_size=FIXTURE_BATCH_SIZE,
):
    """
    Retrieve enriched fixture records in batches.

    API-Football supports multiple fixture IDs in one request.
    This avoids making one API request per fixture.

    The returned dictionary is keyed by fixture ID.
    """
    if not isinstance(
        fixture_ids,
        (list, tuple, set),
    ):
        raise ValueError(
            "fixture_ids must be a list, tuple, or set."
        )

    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or batch_size <= 0
    ):
        raise ValueError(
            "batch_size must be a positive integer."
        )

    clean_ids = []

    for fixture_id in fixture_ids:
        if fixture_id in (
            None,
            "",
        ):
            continue

        try:
            numeric_id = int(fixture_id)
        except (TypeError, ValueError):
            continue

        if numeric_id > 0:
            clean_ids.append(numeric_id)

    clean_ids = sorted(set(clean_ids))

    if not clean_ids:
        return {}

    enriched = {}

    for start in range(
        0,
        len(clean_ids),
        batch_size,
    ):
        batch = clean_ids[
            start:start + batch_size
        ]

        data = _get(
            "fixtures",
            {
                "ids": "-".join(
                    str(value)
                    for value in batch
                )
            },
        )

        response = data.get(
            "response",
            [],
        )

        if not isinstance(
            response,
            list,
        ):
            continue

        for fixture in response:
            if not isinstance(
                fixture,
                dict,
            ):
                continue

            fixture_id = (
                fixture
                .get("fixture", {})
                .get("id")
            )

            if fixture_id is None:
                continue

            enriched[int(fixture_id)] = fixture

    return enriched


def get_league_standings(
    league_id,
    season,
):
    """
    Current live standings for a league/season.
    """
    params = {
        "league": league_id,
        "season": season,
    }

    data = _get(
        "standings",
        params,
    )

    response = data.get(
        "response",
        [],
    )

    if not response:
        return []

    try:
        groups = response[0][
            "league"
        ]["standings"]

        return [
            team
            for group in groups
            for team in group
        ]

    except (
        KeyError,
        IndexError,
        TypeError,
    ):
        return []


def search_leagues(name):
    params = {"search": name}

    data = _get(
        "leagues",
        params,
    )

    response = data.get(
        "response",
        [],
    )

    return (
        response
        if isinstance(response, list)
        else []
    )


def get_league_coverage(
    league_id,
):
    params = {"id": league_id}

    data = _get(
        "leagues",
        params,
    )

    response = data.get(
        "response",
        [],
    )

    if not response:
        return []

    return response[0].get(
        "seasons",
        [],
    )


def raw_debug_call(
    endpoint,
    params,
):
    """
    Bypass normal parsing and caching.

    Used only for diagnosis.
    """
    url = (
        f"{config.API_FOOTBALL_BASE_URL}"
        f"/{endpoint}"
    )

    resp = requests.get(
        url,
        headers=_headers(),
        params=params,
        timeout=15,
    )

    resp.raise_for_status()

    return resp.json()
