"""
Basketball API wrapper for API-Sports.

This module is deliberately thin.

Responsibilities:
- validate API inputs;
- call API-Sports Basketball;
- cache successful responses;
- cache valid empty responses;
- retry only transient failures;
- expose the public functions used by the prediction agent;
- fail closed when the provider returns malformed data.

Prediction integrity rule:
This module never fabricates basketball statistics or results.
If the provider cannot supply valid data, the caller receives an error
or an empty result and must decide whether a prediction is possible.
"""

import datetime
import hashlib
import json
import os
import time

import requests

import config
import storage


class APIBasketballError(RuntimeError):
    """Expected API-Basketball/network-service failure."""


class APIBasketballQuotaExhaustedError(APIBasketballError):
    """Raised when API-Basketball daily request quota is exhausted."""


# ---------------------------------------------------------------------------
# Request / retry configuration
# ---------------------------------------------------------------------------

MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 3
REQUEST_TIMEOUT_SECONDS = 15

RETRYABLE_STATUS_CODES = {
    429,
    500,
    502,
    503,
    504,
}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_positive_int(value, name):
    """Require a real positive integer."""
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
    """Require an exact YYYY-MM-DD date."""
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


def _validate_endpoint(endpoint):
    """Require a safe non-empty API endpoint."""
    if not isinstance(endpoint, str):
        raise ValueError(
            "endpoint must be a string."
        )

    endpoint = endpoint.strip().strip("/")

    if not endpoint:
        raise ValueError(
            "endpoint must be a non-empty string."
        )

    if "/" in endpoint:
        raise ValueError(
            "endpoint must contain a single API endpoint name."
        )

    return endpoint


def _validate_params(params):
    """Require a dictionary suitable for a GET query."""
    if not isinstance(params, dict):
        raise ValueError(
            "params must be a dictionary."
        )

    return params


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _api_key():
    """
    Return the configured API-Sports key.

    The project intentionally uses the same API-Sports key for the
    football and basketball APIs.
    """
    key = getattr(
        config,
        "API_FOOTBALL_KEY",
        None,
    )

    if not isinstance(key, str) or not key.strip():
        raise ValueError(
            "API_FOOTBALL_KEY is not configured."
        )

    return key.strip()


def _base_url():
    """Return the configured Basketball API base URL."""
    base_url = getattr(
        config,
        "API_BASKETBALL_BASE_URL",
        None,
    )

    if not isinstance(base_url, str):
        raise ValueError(
            "API_BASKETBALL_BASE_URL is not configured."
        )

    base_url = base_url.strip().rstrip("/")

    if not base_url:
        raise ValueError(
            "API_BASKETBALL_BASE_URL is empty."
        )

    return base_url


def _headers():
    """Build API-Sports authentication headers."""
    return {
        "x-apisports-key": _api_key(),
    }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _cache_key(endpoint, params):
    """
    Build a deterministic cache key.

    Hashing the complete endpoint + parameter set avoids path-length and
    unsafe-filename problems while ensuring different API queries do not
    collide.
    """
    payload = {
        "endpoint": endpoint,
        "params": params,
    }

    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    digest = hashlib.sha256(
        serialized.encode("utf-8")
    ).hexdigest()

    return f"basketball_{digest}"


def _cache_path(endpoint, params):
    """Return the cache file path for an API request."""
    cache_dir = getattr(
        config,
        "CACHE_DIR",
        ".api_cache",
    )

    if not isinstance(cache_dir, str) or not cache_dir.strip():
        raise ValueError(
            "CACHE_DIR must be a non-empty string."
        )

    os.makedirs(
        cache_dir,
        exist_ok=True,
    )

    return os.path.join(
        cache_dir,
        _cache_key(
            endpoint,
            params,
        ) + ".json",
    )


def _cache_get(endpoint, params):
    """
    Read a cached API response from persistent storage cache first, then disk fallback.

    Returns:
        dict: cached response when fresh and valid.
        None: when no usable cache entry exists.
    """
    cache_k = _cache_key(endpoint, params)
    try:
        persistent = storage.get_api_cache(cache_k)
        if persistent is not None:
            return persistent
    except Exception:
        pass

    path = _cache_path(endpoint, params)

    if not os.path.exists(path):
        return None

    try:
        ttl_hours = float(getattr(config, "CACHE_TTL_HOURS", 20))

        if ttl_hours < 0:
            return None

        age_seconds = time.time() - os.path.getmtime(path)

        if age_seconds < 0 or age_seconds > ttl_hours * 3600:
            return None

        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, dict) or "response" not in data:
            return None

        return data

    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _cache_set(endpoint, params, data):
    """
    Store an API response in persistent storage cache and disk fallback.
    """
    if not isinstance(data, dict):
        raise ValueError("Cached API data must be a dictionary.")

    cache_k = _cache_key(endpoint, params)
    ttl_seconds = int(float(getattr(config, "CACHE_TTL_HOURS", 20)) * 3600)

    try:
        storage.set_api_cache(cache_k, endpoint, params, data, ttl_seconds)
    except Exception:
        pass

    path = _cache_path(endpoint, params)
    temp_path = path + f".tmp.{os.getpid()}"

    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, separators=(",", ":"))

        os.replace(temp_path, path)

    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Quota Management
# ---------------------------------------------------------------------------


def _check_and_consume_quota(endpoint, max_budget=None):
    """
    Atomically verify and reserve daily basketball credit quota.

    Fails closed: if quota storage cannot be queried or updated, raises
    APIBasketballQuotaExhaustedError to prevent unauthorized external requests.
    """
    global_limit = int(getattr(config, "API_BASKETBALL_DAILY_CREDIT_LIMIT", 100))
    limit = global_limit
    if max_budget is not None:
        try:
            budget_val = int(max_budget)
            if budget_val > 0:
                limit = min(limit, budget_val)
        except (TypeError, ValueError):
            pass

    today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    try:
        reserved = storage.reserve_api_request("api_basketball", today_str, today_str, endpoint, limit)
    except Exception as exc:
        raise APIBasketballQuotaExhaustedError(
            f"Basketball quota storage error; failing closed: {exc}"
        ) from exc

    if not reserved:
        raise APIBasketballQuotaExhaustedError(
            f"API-Basketball credit limit reached ({limit})."
        )


# ---------------------------------------------------------------------------
# API response validation
# ---------------------------------------------------------------------------


def _validate_api_response(data):
    """
    Validate the basic API-Sports response envelope.

    This does not validate every endpoint-specific field. Individual public
    functions perform that validation after this function succeeds.
    """
    if not isinstance(data, dict):
        raise ValueError(
            "API-Basketball response must be a JSON object."
        )

    if "response" not in data:
        raise ValueError(
            "API-Basketball response is missing the response field."
        )

    errors = data.get(
        "errors"
    )

    if errors:
        raise RuntimeError(
            "API-Basketball returned errors: "
            f"{errors}"
        )

    return data


def _parse_json_response(response):
    """Decode and validate the provider's JSON response."""
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError(
            "API-Basketball returned invalid JSON."
        ) from exc

    return _validate_api_response(
        data
    )


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------


def _retry_after_seconds(response, default):
    """
    Read Retry-After when supplied by the provider.

    The value is deliberately capped so a malformed provider response cannot
    make a GitHub Actions worker sleep indefinitely.
    """
    raw = response.headers.get(
        "Retry-After"
    )

    if raw is not None:
        try:
            seconds = float(raw)

            if seconds >= 0:
                return min(
                    seconds,
                    60.0,
                )
        except (
            TypeError,
            ValueError,
        ):
            pass

    return min(
        float(default),
        60.0,
    )


def _sleep_before_retry(seconds):
    """Sleep for a bounded retry delay."""
    seconds = max(
        0.0,
        min(
            float(seconds),
            60.0,
        ),
    )

    time.sleep(
        seconds
    )


# ---------------------------------------------------------------------------
# Core GET operation
# ---------------------------------------------------------------------------


def _get(endpoint, params, max_budget=None):
    """
    Perform a GET request with persistent cache-first behavior and hard quota enforcement.
    """
    endpoint = _validate_endpoint(endpoint)
    params = _validate_params(params)

    cached = _cache_get(endpoint, params)
    if cached is not None:
        return cached

    url = f"{_base_url()}/{endpoint}"
    backoff = RETRY_BACKOFF_SECONDS
    last_exception = None

    for attempt in range(1, MAX_RETRIES + 1):
        _check_and_consume_quota(endpoint, max_budget=max_budget)

        try:
            response = requests.get(
                url,
                headers=_headers(),
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

        except requests.RequestException as exc:
            last_exception = exc
            if attempt >= MAX_RETRIES:
                raise APIBasketballError(
                    f"API-Basketball request failed after {MAX_RETRIES} attempts: {exc}"
                ) from exc

            print(
                f"  Basketball API network error; retrying in {backoff}s (attempt {attempt}/{MAX_RETRIES})..."
            )
            _sleep_before_retry(backoff)
            backoff *= 2
            continue

        status = response.status_code

        if status in RETRYABLE_STATUS_CODES:
            if attempt >= MAX_RETRIES:
                response.raise_for_status()

            if status == 429:
                delay = _retry_after_seconds(response, backoff)
                print(
                    f"  Basketball API rate limited (429); retrying in {delay:g}s (attempt {attempt}/{MAX_RETRIES})..."
                )
            else:
                delay = min(float(backoff), 60.0)
                print(
                    f"  Basketball API temporary HTTP {status}; retrying in {delay:g}s (attempt {attempt}/{MAX_RETRIES})..."
                )

            _sleep_before_retry(delay)
            backoff *= 2
            continue

        response.raise_for_status()
        data = _parse_json_response(response)

        _cache_set(endpoint, params, data)
        return data

    if last_exception is not None:
        raise last_exception

    raise APIBasketballError("API-Basketball request failed.")


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------


def _season_for_date(parsed_date):
    """
    Derive the season year used by API-Sports basketball schedule queries.

    For the NBA-style season used by this project:
    August-December belongs to the season beginning that calendar year;
    January-July belongs to the season that began the previous year.
    """
    year = parsed_date.year

    if parsed_date.month >= 8:
        season = year
    else:
        season = year - 1

    if season <= 0:
        raise ValueError(
            "Unable to derive a valid basketball season."
        )

    return season


def get_league_games_page(league_id, season, page=1, max_budget=None):
    """
    Retrieve one page of basketball games for a league season with pagination metadata.

    Returns dict:
        {
            "games": list_of_games,
            "page": int,
            "expected_pages": int
        }
    """
    league_id = _validate_positive_int(league_id, "league_id")
    season = _validate_positive_int(season, "season")
    if page < 1:
        page = 1

    params = {
        "league": league_id,
        "season": season,
        "page": page,
    }

    data = _get("games", params, max_budget=max_budget)

    paging = data.get("paging")
    if isinstance(paging, dict):
        current = paging.get("current")
        total = paging.get("total")

        if (
            isinstance(current, bool)
            or not isinstance(current, int)
            or current < 1
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < 1
            or current > total
        ):
            raise APIBasketballError(
                f"Malformed pagination metadata from API-Basketball: current={current!r}, total={total!r}"
            )
    else:
        total = page

    response = data.get("response", [])
    if not isinstance(response, list):
        response = []

    return {
        "games": response,
        "page": page,
        "expected_pages": total,
    }


def get_games_by_date(
    date_str,
    league_id,
):
    """
    Return basketball games for one date and configured league.

    This is the canonical schedule function used by main.py and
    telegram_bot.py.
    """
    parsed_date = _validate_date(
        date_str
    )

    league_id = _validate_positive_int(
        league_id,
        "league_id",
    )

    season = _season_for_date(
        parsed_date
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
        "response"
    )

    if not isinstance(
        response,
        list,
    ):
        raise ValueError(
            "API-Basketball games response must be a list."
        )

    return response


def get_live_games():
    """
    Return currently live basketball games.

    The API-Sports live-games query is intentionally separate from
    get_games_by_date() because live games must not depend on the caller
    guessing the correct season/date parameters.
    """
    data = _get(
        "games",
        {
            "live": "all",
        },
    )

    response = data.get(
        "response"
    )

    if not isinstance(
        response,
        list,
    ):
        raise ValueError(
            "API-Basketball live-games response must be a list."
        )

    return response


def get_game_result(
    game_id,
):
    """
    Return one basketball game record by ID.

    Returns None when the provider has no matching record.
    """
    game_id = _validate_positive_int(
        game_id,
        "game_id",
    )

    data = _get(
        "games",
        {
            "id": game_id,
        },
    )

    response = data.get(
        "response"
    )

    if not isinstance(
        response,
        list,
    ):
        raise ValueError(
            "API-Basketball game-result response must be a list."
        )

    if not response:
        return None

    game = response[0]

    if not isinstance(
        game,
        dict,
    ):
        raise ValueError(
            "API-Basketball game-result item must be a dictionary."
        )

    return game

# ---------------------------------------------------------------------------
# Team statistics
# ---------------------------------------------------------------------------


def get_team_statistics(
    team_id,
    league_id,
    season,
):
    """
    Return season-level statistics for one basketball team.

    basketball_model.py expects the API response to contain:

        points
          for
            average
              all
          against
            average
              all

    This wrapper does not transform those statistics because the model
    owns the interpretation of the provider's statistical schema.
    """
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
        "response"
    )

    if not isinstance(
        response,
        dict,
    ):
        raise ValueError(
            "API-Basketball statistics response must be a dictionary."
        )

    return response


# ---------------------------------------------------------------------------
# Compatibility helpers
# ---------------------------------------------------------------------------


def get_games_for_date(
    date_str,
    league_id,
):
    """
    Backward-compatible alias for get_games_by_date().

    The canonical name is get_games_by_date(). Keeping this alias prevents
    older callers or tests from breaking while the rest of the application
    migrates to the canonical function.
    """
    return get_games_by_date(
        date_str,
        league_id,
    )


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def clear_expired_cache():
    """
    Remove expired basketball cache files.

    This is maintenance only and is not called during prediction.

    Returns:
        int: number of files removed.
    """
    cache_dir = getattr(
        config,
        "CACHE_DIR",
        ".api_cache",
    )

    if not isinstance(
        cache_dir,
        str,
    ) or not cache_dir.strip():
        return 0

    if not os.path.isdir(
        cache_dir
    ):
        return 0

    try:
        ttl_hours = float(
            getattr(
                config,
                "CACHE_TTL_HOURS",
                20,
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        return 0

    if ttl_hours < 0:
        return 0

    now = time.time()
    removed = 0

    try:
        filenames = os.listdir(
            cache_dir
        )
    except OSError:
        return 0

    for filename in filenames:
        if not filename.startswith(
            "basketball_"
        ):
            continue

        if not filename.endswith(
            ".json"
        ):
            continue

        path = os.path.join(
            cache_dir,
            filename,
        )

        try:
            age_seconds = (
                now
                - os.path.getmtime(path)
            )

            if (
                age_seconds >= 0
                and age_seconds
                > ttl_hours * 3600
            ):
                os.remove(
                    path
                )
                removed += 1

        except OSError:
            continue

    return removed
