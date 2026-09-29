"""
Thin, defensive wrapper around the API-Football v3 API.

Design goals:
- Prediction-first reliability.
- Aggressive reuse of the local cache to protect the API credit budget.
- No fabricated data.
- Validate identifiers before making requests.
- Cache successful responses, including valid empty responses.
- Retry only transient failures.
- Provide the live-fixtures endpoint used by Telegram.
- Keep all public functions used elsewhere in the repository.
"""

import json
import os
import time

import requests

import config


# ============================================================================
# API / CACHE CONFIGURATION
# ============================================================================

MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 3
REQUEST_TIMEOUT_SECONDS = 15
FIXTURE_BATCH_SIZE = 20


# ============================================================================
# VALIDATION
# ============================================================================

def _validate_positive_int(
    value,
    name,
):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
    ):
        raise ValueError(
            f"{name} must be a positive integer."
        )

    return value


def _validate_positive_int_like(
    value,
    name,
):
    try:
        value = int(value)
    except (
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError(
            f"{name} must be a positive integer."
        ) from exc

    if value <= 0:
        raise ValueError(
            f"{name} must be a positive integer."
        )

    return value


def _validate_date(
    date_str,
):
    if not isinstance(
        date_str,
        str,
    ):
        raise ValueError(
            "date_str must be a string in YYYY-MM-DD format."
        )

    try:
        parsed = time.strptime(
            date_str,
            "%Y-%m-%d",
        )
    except ValueError as exc:
        raise ValueError(
            "date_str must use YYYY-MM-DD format."
        ) from exc

    # strptime accepts some odd inputs on some platforms, so round-trip
    # validation keeps the API request deterministic.
    normalized = time.strftime(
        "%Y-%m-%d",
        parsed,
    )

    if normalized != date_str:
        raise ValueError(
            "date_str must use YYYY-MM-DD format."
        )

    return date_str


def _validate_nonempty_text(
    value,
    name,
):
    if not isinstance(
        value,
        str,
    ):
        raise ValueError(
            f"{name} must be a string."
        )

    value = value.strip()

    if not value:
        raise ValueError(
            f"{name} cannot be empty."
        )

    return value


# ============================================================================
# API HEADERS
# ============================================================================

def _headers():
    api_key = getattr(
        config,
        "API_FOOTBALL_KEY",
        None,
    )

    if not api_key:
        raise RuntimeError(
            "API_FOOTBALL_KEY is not configured."
        )

    return {
        "x-apisports-key": api_key
    }


# ============================================================================
# CACHE
# ============================================================================

def _cache_path(key):
    cache_dir = getattr(
        config,
        "CACHE_DIR",
        ".api_cache",
    )

    os.makedirs(
        cache_dir,
        exist_ok=True,
    )

    safe_key = (
        str(key)
        .replace("/", "_")
        .replace("\\", "_")
        .replace("?", "_")
        .replace("&", "_")
        .replace(":", "_")
        .replace(" ", "_")
    )

    return os.path.join(
        cache_dir,
        safe_key + ".json",
    )


def _cache_get(key):
    path = _cache_path(
        key
    )

    if not os.path.exists(path):
        return None

    try:
        modified = os.path.getmtime(
            path
        )

        age_hours = (
            time.time()
            - modified
        ) / 3600

        ttl_hours = float(
            getattr(
                config,
                "CACHE_TTL_HOURS",
                20,
            )
        )

        if age_hours > ttl_hours:
            return None

        with open(
            path,
            "r",
            encoding="utf-8",
        ) as handle:
            data = json.load(
                handle
            )

        if not isinstance(
            data,
            dict,
        ):
            return None

        return data

    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        # A damaged cache entry must never break the prediction engine.
        return None


def _cache_set(
    key,
    data,
):
    path = _cache_path(
        key
    )

    temp_path = (
        path
        + ".tmp"
    )

    try:
        with open(
            temp_path,
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                data,
                handle,
                ensure_ascii=False,
            )

        os.replace(
            temp_path,
            path,
        )

    except OSError:
        # Cache failure must never destroy an otherwise valid API response.
        try:
            if os.path.exists(
                temp_path
            ):
                os.remove(
                    temp_path
                )
        except OSError:
            pass


def _cache_key(
    endpoint,
    params,
):
    return (
        endpoint
        + "_"
        + json.dumps(
            params,
            sort_keys=True,
            separators=(
                ",",
                ":",
            ),
        )
    )


# ============================================================================
# CORE HTTP REQUEST
# ============================================================================

def _get(
    endpoint,
    params,
):
    """
    Perform a GET request with cache-first behavior.

    Important credit protection:
    - Cached responses never call the API.
    - Successful empty responses are cached too.
    - Only transient failures are retried.
    - Permanent HTTP/API errors are raised immediately.
    """

    endpoint = _validate_nonempty_text(
        endpoint,
        "endpoint",
    )

    if not isinstance(
        params,
        dict,
    ):
        raise ValueError(
            "params must be a dictionary."
        )

    cache_key = _cache_key(
        endpoint,
        params,
    )

    cached = _cache_get(
        cache_key
    )

    if cached is not None:
        return cached

    url = (
        f"{config.API_FOOTBALL_BASE_URL}"
        f"/{endpoint.lstrip('/')}"
    )

    backoff = RETRY_BACKOFF_SECONDS

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):
        try:
            response = requests.get(
                url,
                headers=_headers(),
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

        except requests.RequestException as exc:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    "API-Football request failed after "
                    f"{MAX_RETRIES} attempts: {exc}"
                ) from exc

            print(
                "API-Football network error; "
                f"retrying in {backoff}s "
                f"(attempt {attempt}/{MAX_RETRIES})",
                flush=True,
            )

            time.sleep(
                backoff
            )

            backoff *= 2

            continue

        status = response.status_code

        # Rate limiting is transient. Retry, but keep the retry count low
        # because every actual API request matters to the daily budget.
        if status == 429:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    "API-Football rate limit reached "
                    f"after {MAX_RETRIES} attempts."
                )

            retry_after = response.headers.get(
                "Retry-After"
            )

            try:
                wait_seconds = max(
                    1,
                    min(
                        int(retry_after),
                        60,
                    ),
                )
            except (
                TypeError,
                ValueError,
            ):
                wait_seconds = backoff

            print(
                "API-Football rate limited (429); "
                f"retrying in {wait_seconds}s "
                f"(attempt {attempt}/{MAX_RETRIES})",
                flush=True,
            )

            time.sleep(
                wait_seconds
            )

            backoff *= 2

            continue

        # Server-side failures can be transient.
        if status in {
            500,
            502,
            503,
            504,
        }:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    "API-Football server error after "
                    f"{MAX_RETRIES} attempts: HTTP {status}"
                )

            print(
                "API-Football server error "
                f"({status}); retrying in {backoff}s "
                f"(attempt {attempt}/{MAX_RETRIES})",
                flush=True,
            )

            time.sleep(
                backoff
            )

            backoff *= 2

            continue

        try:
            data = response.json()

        except ValueError as exc:
            raise RuntimeError(
                "API-Football returned invalid JSON "
                f"for /{endpoint} "
                f"(HTTP {status})."
            ) from exc

        if not isinstance(
            data,
            dict,
        ):
            raise RuntimeError(
                "API-Football returned a non-object JSON "
                f"response for /{endpoint}."
            )

        if not response.ok:
            errors = data.get(
                "errors"
            )

            raise RuntimeError(
                "API-Football HTTP error: "
                f"{status}; errors={errors!r}"
            )

        api_errors = data.get(
            "errors"
        )

        if api_errors:
            raise RuntimeError(
                "API-Football API error: "
                f"{api_errors!r}"
            )

        # Cache every successful API response, including:
        # {"response": []}
        #
        # This is important. Not caching empty results causes repeated
        # identical requests and unnecessary API-credit consumption.
        _cache_set(
            cache_key,
            data,
        )

        return data

    raise RuntimeError(
        f"API-Football request failed: /{endpoint}"
    )


# ============================================================================
# FIXTURES
# ============================================================================

def get_fixtures_by_date(
    date_str,
    league_id=None,
):
    """
    Return fixtures for a calendar date.

    A league filter is included when supplied.
    """

    date_str = _validate_date(
        date_str
    )

    params = {
        "date": date_str
    }

    if league_id is not None:
        league_id = (
            _validate_positive_int_like(
                league_id,
                "league_id",
            )
        )

        params["league"] = league_id

    data = _get(
        "fixtures",
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
        return []

    return response


def get_live_fixtures():
    """
    Return currently live football fixtures.

    API-Football uses `live=all` for the live fixtures endpoint.
    """

    data = _get(
        "fixtures",
        {
            "live": "all"
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
        return []

    return response


def get_team_statistics(
    team_id,
    league_id,
    season,
):
    team_id = _validate_positive_int_like(
        team_id,
        "team_id",
    )

    league_id = _validate_positive_int_like(
        league_id,
        "league_id",
    )

    season = _validate_positive_int_like(
        season,
        "season",
    )

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

    if not isinstance(
        response,
        dict,
    ):
        return {}

    return response


def get_head_to_head(
    team_a_id,
    team_b_id,
    last=10,
):
    team_a_id = _validate_positive_int_like(
        team_a_id,
        "team_a_id",
    )

    team_b_id = _validate_positive_int_like(
        team_b_id,
        "team_b_id",
    )

    if (
        isinstance(last, bool)
        or not isinstance(last, int)
        or last <= 0
    ):
        raise ValueError(
            "last must be a positive integer."
        )

    params = {
        "h2h": (
            f"{team_a_id}-{team_b_id}"
        ),
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

    if not isinstance(
        response,
        list,
    ):
        return []

    return response


def get_recent_form(
    team_id,
    last=8,
):
    team_id = _validate_positive_int_like(
        team_id,
        "team_id",
    )

    if (
        isinstance(last, bool)
        or not isinstance(last, int)
        or last <= 0
    ):
        raise ValueError(
            "last must be a positive integer."
        )

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

    if not isinstance(
        response,
        list,
    ):
        return []

    return response

# ============================================================================
# SINGLE FIXTURE / SEASON DATA
# ============================================================================

def get_fixture_result(
    fixture_id,
):
    fixture_id = _validate_positive_int_like(
        fixture_id,
        "fixture_id",
    )

    data = _get(
        "fixtures",
        {
            "id": fixture_id
        },
    )

    response = data.get(
        "response",
        [],
    )

    if (
        isinstance(response, list)
        and response
        and isinstance(
            response[0],
            dict,
        )
    ):
        return response[0]

    return None


def get_league_fixtures(
    league_id,
    season,
):
    """
    Retrieve all fixtures for a league season.

    The normal cache layer prevents identical repeated requests.
    """

    league_id = _validate_positive_int_like(
        league_id,
        "league_id",
    )

    season = _validate_positive_int_like(
        season,
        "season",
    )

    data = _get(
        "fixtures",
        {
            "league": league_id,
            "season": season,
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
        return []

    return response


def get_enriched_fixtures(
    fixture_ids,
    batch_size=FIXTURE_BATCH_SIZE,
):
    """
    Retrieve enriched fixture records in batches.

    Multiple fixture IDs are sent in one request where supported.
    The result is keyed by fixture ID.
    """

    if not isinstance(
        fixture_ids,
        (list, tuple, set),
    ):
        raise ValueError(
            "fixture_ids must be a list, tuple, or set."
        )

    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
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
            numeric_id = int(
                fixture_id
            )
        except (
            TypeError,
            ValueError,
        ):
            continue

        if numeric_id > 0:
            clean_ids.append(
                numeric_id
            )

    clean_ids = sorted(
        set(clean_ids)
    )

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

        ids = "-".join(
            str(value)
            for value in batch
        )

        data = _get(
            "fixtures",
            {
                "ids": ids
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

            try:
                fixture_id = int(
                    fixture_id
                )
            except (
                TypeError,
                ValueError,
            ):
                continue

            if fixture_id > 0:
                enriched[
                    fixture_id
                ] = fixture

    return enriched


# ============================================================================
# STANDINGS / LEAGUES
# ============================================================================

def get_league_standings(
    league_id,
    season,
):
    """
    Return flattened league standings.
    """

    league_id = _validate_positive_int_like(
        league_id,
        "league_id",
    )

    season = _validate_positive_int_like(
        season,
        "season",
    )

    data = _get(
        "standings",
        {
            "league": league_id,
            "season": season,
        },
    )

    response = data.get(
        "response",
        [],
    )

    if not isinstance(
        response,
        list,
    ) or not response:
        return []

    try:
        groups = (
            response[0]
            .get("league", {})
            .get("standings", [])
        )

    except (
        AttributeError,
        IndexError,
        TypeError,
    ):
        return []

    if not isinstance(
        groups,
        list,
    ):
        return []

    flattened = []

    for group in groups:
        if not isinstance(
            group,
            list,
        ):
            continue

        for team in group:
            if isinstance(
                team,
                dict,
            ):
                flattened.append(
                    team
                )

    return flattened


def search_leagues(
    name,
):
    name = _validate_nonempty_text(
        name,
        "name",
    )

    data = _get(
        "leagues",
        {
            "search": name
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
        return []

    return response


def get_league_coverage(
    league_id,
):
    league_id = _validate_positive_int_like(
        league_id,
        "league_id",
    )

    data = _get(
        "leagues",
        {
            "id": league_id
        },
    )

    response = data.get(
        "response",
        [],
    )

    if not isinstance(
        response,
        list,
    ) or not response:
        return []

    first = response[0]

    if not isinstance(
        first,
        dict,
    ):
        return []

    seasons = first.get(
        "seasons",
        [],
    )

    return (
        seasons
        if isinstance(
            seasons,
            list,
        )
        else []
    )


# ============================================================================
# RAW DEBUG
# ============================================================================

def raw_debug_call(
    endpoint,
    params,
):
    """
    Diagnostic-only API call.

    This intentionally bypasses normal caching.

    It should not be used by the production prediction path because
    bypassing the cache can consume additional API credits.
    """

    endpoint = _validate_nonempty_text(
        endpoint,
        "endpoint",
    )

    if not isinstance(
        params,
        dict,
    ):
        raise ValueError(
            "params must be a dictionary."
        )

    url = (
        f"{config.API_FOOTBALL_BASE_URL}"
        f"/{endpoint.lstrip('/')}"
    )

    response = requests.get(
        url,
        headers=_headers(),
        params=params,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "API-Football debug request returned invalid JSON."
        ) from exc

    if not response.ok:
        raise RuntimeError(
            "API-Football debug request failed: "
            f"HTTP {response.status_code}; "
            f"errors={data.get('errors')!r}"
        )

    return data
