"""
Thin, defensive wrapper around the API-Football v3 API.

Design goals:
- Prediction-first reliability.
- Aggressive reuse of persistent storage cache and local cache to protect API credit budget.
- Strict quota enforcement (100 daily API credits hard limit).
- Robust pagination for historical league fixtures.
- Dedicated fixture result TTL strategy (6 minutes).
- No fabricated data.
- Validate identifiers before making requests.
- Cache successful responses, including valid empty responses.
- Retry only transient failures with bounded limits.
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

import config
import storage


# ============================================================================
# API ERRORS
# ============================================================================


class APIFootballError(RuntimeError):
    """Expected API-Football/network-service failure."""


class APIFootballQuotaExhaustedError(APIFootballError):
    """Raised when API-Football daily request quota is exhausted."""


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


def _validate_positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _validate_positive_int_like(value, name):
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer.") from exc

    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _validate_date(date_str):
    if not isinstance(date_str, str):
        raise ValueError("date_str must be a string in YYYY-MM-DD format.")

    try:
        parsed = time.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("date_str must use YYYY-MM-DD format.") from exc

    normalized = time.strftime("%Y-%m-%d", parsed)
    if normalized != date_str:
        raise ValueError("date_str must use YYYY-MM-DD format.")
    return date_str


def _validate_nonempty_text(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string.")
    value = value.strip()
    if not value:
        raise ValueError(f"{name} cannot be empty.")
    return value


# ============================================================================
# API HEADERS
# ============================================================================


def _headers():
    api_key = getattr(config, "API_FOOTBALL_KEY", None)
    if not api_key:
        raise APIFootballError("API_FOOTBALL_KEY is not configured.")
    return {"x-apisports-key": api_key}


# ============================================================================
# CACHE HELPERS
# ============================================================================


def _cache_path(key):
    cache_dir = getattr(config, "CACHE_DIR", ".api_cache")
    os.makedirs(cache_dir, exist_ok=True)
    safe_key = (
        str(key)
        .replace("/", "_")
        .replace("\\", "_")
        .replace("?", "_")
        .replace("&", "_")
        .replace(":", "_")
        .replace(" ", "_")
    )
    return os.path.join(cache_dir, safe_key + ".json")


def _get_ttl_seconds(endpoint, params, data=None):
    """Determine cache TTL based on endpoint and response content."""
    # Fixture result endpoint
    if endpoint == "fixtures" and ("id" in params or "ids" in params):
        # If response contains a finished match, cache standard TTL, else short result TTL (6 min)
        if data and isinstance(data.get("response"), list) and data["response"]:
            first_fixture = data["response"][0]
            if isinstance(first_fixture, dict):
                status_short = (
                    first_fixture.get("fixture", {})
                    .get("status", {})
                    .get("short", "")
                )
                if status_short in {"FT", "AET", "PEN", "CANC", "ABD"}:
                    return int(getattr(config, "CACHE_TTL_HOURS", 20) * 3600)

        return int(getattr(config, "FIXTURE_RESULT_CACHE_TTL_MINUTES", 6) * 60)

    return int(getattr(config, "CACHE_TTL_HOURS", 20) * 3600)


def _cache_key(endpoint, params):
    return endpoint + "_" + json.dumps(params, sort_keys=True, separators=(",", ":"))


def _cache_get(cache_key, endpoint, params):
    """Check persistent database cache first, then local disk fallback."""
    try:
        persistent = storage.get_api_cache(cache_key)
        if persistent is not None:
            return persistent
    except Exception:
        pass

    path = _cache_path(cache_key)
    if not os.path.exists(path):
        return None

    try:
        modified = os.path.getmtime(path)
        age_seconds = time.time() - modified
        ttl_seconds = _get_ttl_seconds(endpoint, params)

        if age_seconds > ttl_seconds:
            return None

        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)

        if isinstance(data, dict):
            return data
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    return None


def _cache_set(cache_key, endpoint, params, data):
    """Save response payload to persistent storage cache and local disk cache."""
    ttl_seconds = _get_ttl_seconds(endpoint, params, data)

    try:
        storage.set_api_cache(cache_key, endpoint, params, data, ttl_seconds)
    except Exception:
        pass

    path = _cache_path(cache_key)
    temp_path = path + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(temp_path, path)
    except OSError:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


# ============================================================================
# QUOTA MANAGEMENT
# ============================================================================


def _check_and_consume_quota(endpoint, max_budget=None):
    """
    Atomically verify and reserve daily credit quota.

    Fails closed: if quota storage cannot be queried or updated, raises
    APIFootballQuotaExhaustedError to prevent unauthorized/untracked external API requests.
    """
    global_limit = int(getattr(config, "API_FOOTBALL_DAILY_CREDIT_LIMIT", 100))
    limit = global_limit
    if max_budget is not None:
        try:
            budget_val = int(max_budget)
            if budget_val > 0:
                limit = min(limit, budget_val)
        except (TypeError, ValueError):
            pass

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        reserved = storage.reserve_api_request("api_football", today_str, today_str, endpoint, limit)
    except Exception as exc:
        raise APIFootballQuotaExhaustedError(
            f"Quota storage error; failing closed: {exc}"
        ) from exc

    if not reserved:
        raise APIFootballQuotaExhaustedError(
            f"API-Football credit limit reached ({limit})."
        )


# ============================================================================
# CORE HTTP REQUEST
# ============================================================================


def _get(endpoint, params, max_budget=None):
    """
    Perform a GET request with persistent cache-first behavior and hard quota enforcement.
    """
    endpoint = _validate_nonempty_text(endpoint, "endpoint")
    if not isinstance(params, dict):
        raise ValueError("params must be a dictionary.")

    cache_key = _cache_key(endpoint, params)
    cached = _cache_get(cache_key, endpoint, params)
    if cached is not None:
        return cached

    url = f"{config.API_FOOTBALL_BASE_URL}/{endpoint.lstrip('/')}"
    backoff = RETRY_BACKOFF_SECONDS

    # Filter out unsupported 'page' parameter for API-Football /fixtures requests
    http_params = dict(params)
    if endpoint.rstrip("/") == "fixtures" and "page" in http_params:
        del http_params["page"]

    for attempt in range(1, MAX_RETRIES + 1):
        # Quota check before every actual network attempt
        _check_and_consume_quota(endpoint, max_budget=max_budget)

        try:
            response = requests.get(
                url,
                headers=_headers(),
                params=http_params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            if attempt >= MAX_RETRIES:
                raise APIFootballError(
                    f"API-Football request failed after {MAX_RETRIES} attempts: {exc}"
                ) from exc

            print(
                f"API-Football network error; retrying in {backoff}s (attempt {attempt}/{MAX_RETRIES})",
                flush=True,
            )
            time.sleep(backoff)
            backoff *= 2
            continue

        status = response.status_code

        if status == 429:
            if attempt >= MAX_RETRIES:
                raise APIFootballError(
                    f"API-Football rate limit reached after {MAX_RETRIES} attempts."
                )

            retry_after = response.headers.get("Retry-After")
            try:
                wait_seconds = max(1, min(int(retry_after), 60))
            except (TypeError, ValueError):
                wait_seconds = backoff

            print(
                f"API-Football rate limited (429); retrying in {wait_seconds}s (attempt {attempt}/{MAX_RETRIES})",
                flush=True,
            )
            time.sleep(wait_seconds)
            backoff *= 2
            continue

        if status in {500, 502, 503, 504}:
            if attempt >= MAX_RETRIES:
                raise APIFootballError(
                    f"API-Football server error after {MAX_RETRIES} attempts: HTTP {status}"
                )

            print(
                f"API-Football server error ({status}); retrying in {backoff}s (attempt {attempt}/{MAX_RETRIES})",
                flush=True,
            )
            time.sleep(backoff)
            backoff *= 2
            continue

        try:
            data = response.json()
        except ValueError as exc:
            raise APIFootballError(
                f"API-Football returned invalid JSON for /{endpoint} (HTTP {status})."
            ) from exc

        if not isinstance(data, dict):
            raise APIFootballError(
                f"API-Football returned a non-object JSON response for /{endpoint}."
            )

        if not response.ok:
            errors = data.get("errors")
            raise APIFootballError(
                f"API-Football HTTP error: {status}; errors={errors!r}"
            )

        api_errors = data.get("errors")
        if api_errors:
            raise APIFootballError(f"API-Football API error: {api_errors!r}")

        # Capture defensive rate limit information from headers if available
        headers = response.headers
        remaining_str = (
            headers.get("x-ratelimit-requests-remaining")
            or headers.get("X-RateLimit-Remaining")
        )
        limit_str = (
            headers.get("x-ratelimit-requests-limit")
            or headers.get("X-RateLimit-Limit")
        )
        if remaining_str is not None:
            try:
                remaining_int = int(remaining_str)
                if remaining_int <= 0:
                    print(
                        f"API-Football response header reports remaining requests = {remaining_int} (limit: {limit_str}). Failing safe.",
                        flush=True,
                    )
                    _cache_set(cache_key, endpoint, params, data)
                    raise APIFootballQuotaExhaustedError(
                        f"API-Football header reported remaining quota exhausted ({remaining_int})."
                    )
            except (TypeError, ValueError):
                pass

        _cache_set(cache_key, endpoint, params, data)
        return data

    raise APIFootballError(f"API-Football request failed: /{endpoint}")


# ============================================================================
# FIXTURES
# ============================================================================


def get_fixtures_by_date(date_str, league_id=None):
    """
    Return fixtures for a calendar date.

    Credit efficiency rule:
    Always request the full-date query (`params = {"date": date_str}`) from the API
    if uncached so that a single request covers ALL leagues for that date.
    Cache the full response, then separate/filter for `league_id` locally.
    """
    date_str = _validate_date(date_str)
    if league_id is not None:
        league_id = _validate_positive_int_like(league_id, "league_id")

    # Check for cached full-date response first
    full_date_key = _cache_key("fixtures", {"date": date_str})
    cached_full_date = _cache_get(full_date_key, "fixtures", {"date": date_str})

    def _filter_league(items):
        if league_id is None:
            return items
        filtered = []
        for f in items:
            if not isinstance(f, dict):
                continue
            lg_obj = f.get("league")
            if isinstance(lg_obj, dict) and lg_obj.get("id") is not None:
                try:
                    raw_id = int(lg_obj.get("id"))
                except (TypeError, ValueError):
                    raw_id = None
                if raw_id == league_id:
                    filtered.append(f)
        return filtered

    if cached_full_date is not None:
        response = cached_full_date.get("response", [])
        if not isinstance(response, list):
            return []
        return _filter_league(response)

    # Fetch full date from API (no league param) to cover all leagues in 1 call
    data = _get("fixtures", {"date": date_str})
    response = data.get("response", [])
    if not isinstance(response, list):
        return []

    return _filter_league(response)


def get_live_fixtures():
    """Return currently live football fixtures."""
    data = _get("fixtures", {"live": "all"})
    response = data.get("response", [])
    if not isinstance(response, list):
        return []
    return response


def get_team_statistics(team_id, league_id, season):
    team_id = _validate_positive_int_like(team_id, "team_id")
    league_id = _validate_positive_int_like(league_id, "league_id")
    season = _validate_positive_int_like(season, "season")

    params = {
        "team": team_id,
        "league": league_id,
        "season": season,
    }

    data = _get("teams/statistics", params)
    response = data.get("response", {})
    if not isinstance(response, dict):
        return {}
    return response


def get_head_to_head(team_a_id, team_b_id, last=10):
    team_a_id = _validate_positive_int_like(team_a_id, "team_a_id")
    team_b_id = _validate_positive_int_like(team_b_id, "team_b_id")

    if isinstance(last, bool) or not isinstance(last, int) or last <= 0:
        raise ValueError("last must be a positive integer.")

    params = {
        "h2h": f"{team_a_id}-{team_b_id}",
        "last": last,
    }

    data = _get("fixtures/headtohead", params)
    response = data.get("response", [])
    if not isinstance(response, list):
        return []
    return response


def get_recent_form(team_id, last=8, league_id=None, season=None):
    team_id = _validate_positive_int_like(team_id, "team_id")

    if isinstance(last, bool) or not isinstance(last, int) or last <= 0:
        raise ValueError("last must be a positive integer.")

    params = {
        "team": team_id,
        "last": last,
        "status": "FT",
    }
    if league_id is not None:
        params["league"] = _validate_positive_int_like(league_id, "league_id")
    if season is not None:
        params["season"] = _validate_positive_int_like(season, "season")

    data = _get("fixtures", params)
    response = data.get("response", [])
    if not isinstance(response, list):
        return []
    return response


# ============================================================================
# SINGLE FIXTURE / SEASON DATA & PAGINATION
# ============================================================================


def get_fixture_result(fixture_id):
    """Retrieve result for a single fixture using result TTL cache."""
    fixture_id = _validate_positive_int_like(fixture_id, "fixture_id")

    data = _get("fixtures", {"id": fixture_id})
    response = data.get("response", [])

    if isinstance(response, list) and response and isinstance(response[0], dict):
        return response[0]

    return None


def get_league_fixtures_page(league_id, season, page=1, max_budget=None):
    """
    Retrieve one single page of fixtures for a league season with strict pagination metadata validation.

    Fails closed if pagination metadata is missing, malformed, or inconsistent with requested page.

    Returns dict:
        {
            "fixtures": list_of_fixtures,
            "page": int,
            "expected_pages": int
        }
    """
    league_id = _validate_positive_int_like(league_id, "league_id")
    season = _validate_positive_int_like(season, "season")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer.")

    kwargs = {"max_budget": max_budget} if max_budget is not None else {}
    page_data = _get("fixtures", {"league": league_id, "season": season, "page": page}, **kwargs)

    if not isinstance(page_data, dict):
        raise APIFootballError("API-Football response must be a JSON object.")

    response = page_data.get("response")
    if not isinstance(response, list):
        raise APIFootballError("API-Football response missing valid 'response' list.")

    paging = page_data.get("paging")
    if not isinstance(paging, dict):
        raise APIFootballError("Missing or invalid 'paging' object in API-Football response.")

    current = paging.get("current")
    total = paging.get("total")

    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total < 1
    ):
        raise APIFootballError(
            f"Malformed pagination metadata from API-Football: total={total!r}"
        )

    if (
        isinstance(current, bool)
        or not isinstance(current, int)
        or current < 1
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total < 1
        or current > total
    ):
        raise APIFootballError(
            f"Malformed pagination metadata from API-Football: current={current!r}, total={total!r}"
        )

    if current != page:
        raise APIFootballError(
            f"Inconsistent pagination metadata: current page ({current}) != requested page ({page})."
        )

    return {
        "fixtures": response,
        "page": page,
        "expected_pages": total,
    }


def get_league_fixtures_with_metadata(league_id, season, start_page=1, max_budget=None):
    """
    Retrieve fixtures for a league season starting from start_page with explicit completion metadata.
    """
    league_id = _validate_positive_int_like(league_id, "league_id")
    season = _validate_positive_int_like(season, "season")
    if start_page < 1:
        start_page = 1

    kwargs = {"max_budget": max_budget} if max_budget is not None else {}
    page_1 = get_league_fixtures_page(league_id, season, page=start_page, **kwargs)
    total = page_1["expected_pages"]

    raw_pages = [page_1["fixtures"]]
    pages_completed = start_page

    if total > start_page:
        for p in range(start_page + 1, total + 1):
            p_data = get_league_fixtures_page(league_id, season, page=p, **kwargs)
            if p_data["expected_pages"] != total:
                raise APIFootballError(
                    f"Inconsistent pagination metadata across multi-page fetch: expected {total}, got {p_data['expected_pages']} on page {p}"
                )
            raw_pages.append(p_data["fixtures"])
            pages_completed += 1

    fixtures = []
    seen_ids = set()
    for page_items in raw_pages:
        for item in page_items:
            if isinstance(item, dict):
                fid = item.get("fixture", {}).get("id")
                if fid is not None:
                    try:
                        fid = int(fid)
                    except (TypeError, ValueError):
                        pass
                if fid and fid in seen_ids:
                    continue
                if fid:
                    seen_ids.add(fid)
            fixtures.append(item)

    return {
        "fixtures": fixtures,
        "expected_pages": total,
        "pages_completed": pages_completed,
        "acquisition_complete": (pages_completed == total),
    }


def get_league_fixtures(league_id, season, max_budget=None):
    """
    Retrieve all fixtures for a league season with pagination support (compatibility wrapper).
    """
    res = get_league_fixtures_with_metadata(league_id, season, max_budget=max_budget)
    return res.get("fixtures", [])


def get_enriched_fixtures(fixture_ids, batch_size=FIXTURE_BATCH_SIZE, max_budget=None):
    """Retrieve enriched fixture records in batches."""
    if not isinstance(fixture_ids, (list, tuple, set)):
        raise ValueError("fixture_ids must be a list, tuple, or set.")

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer.")

    clean_ids = []
    for fixture_id in fixture_ids:
        if fixture_id in (None, ""):
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

    kwargs = {"max_budget": max_budget} if max_budget is not None else {}
    enriched = {}
    for start in range(0, len(clean_ids), batch_size):
        batch = clean_ids[start : start + batch_size]
        ids = "-".join(str(value) for value in batch)

        data = _get("fixtures", {"ids": ids}, **kwargs)
        response = data.get("response", [])

        if not isinstance(response, list):
            continue

        for fixture in response:
            if not isinstance(fixture, dict):
                continue

            fixture_id = fixture.get("fixture", {}).get("id")
            try:
                fixture_id = int(fixture_id)
            except (TypeError, ValueError):
                continue

            if fixture_id > 0:
                enriched[fixture_id] = fixture

    return enriched


# ============================================================================
# STANDINGS / LEAGUES
# ============================================================================


def get_league_standings(league_id, season):
    """Return flattened league standings."""
    league_id = _validate_positive_int_like(league_id, "league_id")
    season = _validate_positive_int_like(season, "season")

    data = _get("standings", {"league": league_id, "season": season})
    response = data.get("response", [])

    if not isinstance(response, list) or not response:
        return []

    try:
        groups = response[0].get("league", {}).get("standings", [])
    except (AttributeError, IndexError, TypeError):
        return []

    if not isinstance(groups, list):
        return []

    flattened = []
    for group in groups:
        if not isinstance(group, list):
            continue
        for team in group:
            if isinstance(team, dict):
                flattened.append(team)

    return flattened


def search_leagues(name):
    name = _validate_nonempty_text(name, "name")
    data = _get("leagues", {"search": name})
    response = data.get("response", [])

    if not isinstance(response, list):
        return []

    return response


def get_league_coverage(league_id):
    league_id = _validate_positive_int_like(league_id, "league_id")
    data = _get("leagues", {"id": league_id})
    response = data.get("response", [])

    if not isinstance(response, list) or not response:
        return []

    first = response[0]
    if not isinstance(first, dict):
        return []

    seasons = first.get("seasons", [])
    return seasons if isinstance(seasons, list) else []


# ============================================================================
# RAW DEBUG
# ============================================================================


def raw_debug_call(endpoint, params):
    """
    Diagnostic-only API call.
    Bypasses caching, but strictly enforces atomic quota reservation, retry accounting,
    and fail-closed behavior for every network attempt.
    """
    endpoint = _validate_nonempty_text(endpoint, "endpoint")
    if not isinstance(params, dict):
        raise ValueError("params must be a dictionary.")

    url = f"{config.API_FOOTBALL_BASE_URL}/{endpoint.lstrip('/')}"
    backoff = RETRY_BACKOFF_SECONDS

    for attempt in range(1, MAX_RETRIES + 1):
        _check_and_consume_quota(endpoint)

        try:
            response = requests.get(
                url,
                headers=_headers(),
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            if attempt >= MAX_RETRIES:
                raise APIFootballError(
                    f"API-Football debug request failed after {MAX_RETRIES} attempts: {exc}"
                ) from exc

            print(
                f"API-Football debug request network error; retrying in {backoff}s (attempt {attempt}/{MAX_RETRIES})",
                flush=True,
            )
            time.sleep(backoff)
            backoff *= 2
            continue

        try:
            data = response.json()
        except ValueError as exc:
            raise APIFootballError("API-Football debug request returned invalid JSON.") from exc

        if not response.ok:
            raise APIFootballError(
                f"API-Football debug request failed: HTTP {response.status_code}; errors={data.get('errors')!r}"
            )

        return data

    raise APIFootballError(f"API-Football debug request failed: /{endpoint}")
