"""
Thin, defensive wrapper around the football-data.org v4 API (secondary provider).

Design goals:
- Defensive request handling, caching, timeout handling, rate-limit handling, malformed-response handling.
- Quota tracking via storage under provider key 'football_data_org'.
- Reuses persistent database cache and local disk cache fallback.
- Never fabricates statistics.
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

import config
import storage


class FootballDataAPIError(RuntimeError):
    """Expected football-data.org network/service failure."""


class FootballDataQuotaExhaustedError(FootballDataAPIError):
    """Raised when football-data.org request quota is exhausted."""


MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 3
REQUEST_TIMEOUT_SECONDS = 15


def _headers():
    api_key = getattr(config, "FOOTBALL_DATA_API_KEY", None) or os.environ.get("FOOTBALL_DATA_API_KEY")
    if not api_key:
        raise FootballDataAPIError("FOOTBALL_DATA_API_KEY is not configured.")
    return {"X-Auth-Token": api_key.strip()}


def _base_url():
    return getattr(config, "FOOTBALL_DATA_BASE_URL", "https://api.football-data.org/v4").rstrip("/")


def _cache_key(endpoint, params):
    payload = {"endpoint": endpoint, "params": params}
    return "football_data_org_" + json.dumps(payload, sort_keys=True, separators=(",", ":"))


import hashlib

def _cache_path(cache_key):
    cache_dir = getattr(config, "CACHE_DIR", ".api_cache")
    os.makedirs(cache_dir, exist_ok=True)
    safe_key = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, safe_key + ".json")


def _cache_get(cache_key):
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
        ttl_hours = float(getattr(config, "CACHE_TTL_HOURS", 20))
        if ttl_hours < 0:
            return None
        age = time.time() - os.path.getmtime(path)
        if age > ttl_hours * 3600:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _cache_set(cache_key, endpoint, params, data):
    ttl_seconds = int(float(getattr(config, "CACHE_TTL_HOURS", 20)) * 3600)
    try:
        storage.set_api_cache(cache_key, endpoint, params, data, ttl_seconds)
    except Exception:
        pass

    path = _cache_path(cache_key)
    temp = path + f".tmp.{os.getpid()}"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(temp, path)
    except OSError:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass


def _check_and_consume_quota(endpoint, max_budget=None):
    global_limit = int(getattr(config, "FOOTBALL_DATA_DAILY_LIMIT", 100))
    limit = min(global_limit, max_budget) if max_budget else global_limit
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        reserved = storage.reserve_api_request("football_data_org", today_str, today_str, endpoint, limit)
    except Exception as exc:
        raise FootballDataQuotaExhaustedError(f"Storage error during quota check: {exc}") from exc

    if not reserved:
        raise FootballDataQuotaExhaustedError(f"football-data.org daily request limit reached ({limit}).")


def _get(endpoint, params=None, max_budget=None):
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be a dictionary.")

    endpoint = endpoint.strip("/")
    cache_key = _cache_key(endpoint, params)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{_base_url()}/{endpoint}"
    backoff = RETRY_BACKOFF_SECONDS

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
            if attempt >= MAX_RETRIES:
                raise FootballDataAPIError(f"football-data.org request failed after {MAX_RETRIES} attempts: {exc}") from exc
            time.sleep(backoff)
            backoff *= 2
            continue

        status = response.status_code

        if status == 429:
            if attempt >= MAX_RETRIES:
                raise FootballDataQuotaExhaustedError("football-data.org rate limit (429) reached.")
            time.sleep(backoff)
            backoff *= 2
            continue

        if status in {500, 502, 503, 504}:
            if attempt >= MAX_RETRIES:
                raise FootballDataAPIError(f"football-data.org server error (HTTP {status}).")
            time.sleep(backoff)
            backoff *= 2
            continue

        try:
            data = response.json()
        except ValueError as exc:
            raise FootballDataAPIError("football-data.org returned invalid JSON.") from exc

        if not response.ok or not isinstance(data, dict):
            message = data.get("message") if isinstance(data, dict) else f"HTTP {status}"
            raise FootballDataAPIError(f"football-data.org error: {message}")

        _cache_set(cache_key, endpoint, params, data)
        return data

    raise FootballDataAPIError(f"football-data.org request failed: /{endpoint}")


# ---------------------------------------------------------------------------
# PUBLIC API OPERATIONS
# ---------------------------------------------------------------------------

def get_competition_matches(comp_code, season=None):
    params = {}
    if season is not None:
        params["season"] = season
    data = _get(f"competitions/{comp_code}/matches", params=params)
    if not isinstance(data, dict):
        return {"matches": [], "metadata": {}}

    matches = data.get("matches", [])
    if not isinstance(matches, list):
        matches = []

    res_set = data.get("resultSet", {}) if isinstance(data.get("resultSet"), dict) else {}
    comp_obj = data.get("competition", {}) if isinstance(data.get("competition"), dict) else {}
    filters_obj = data.get("filters", {}) if isinstance(data.get("filters"), dict) else {}

    metadata = {
        "count": res_set.get("count"),
        "played": res_set.get("played"),
        "first": res_set.get("first"),
        "last": res_set.get("last"),
        "competition_code": comp_obj.get("code"),
        "season": filters_obj.get("season") or season,
    }

    return {"matches": matches, "metadata": metadata}


def get_competition_standings(comp_code, season=None):
    params = {}
    if season is not None:
        params["season"] = season
    data = _get(f"competitions/{comp_code}/standings", params=params)
    standings_list = data.get("standings", []) if isinstance(data, dict) else []
    if not isinstance(standings_list, list):
        return []

    flattened = []
    for grp in standings_list:
        if isinstance(grp, dict) and isinstance(grp.get("table"), list):
            flattened.extend(grp["table"])
    return flattened


def get_competition_teams(comp_code, season=None):
    params = {}
    if season is not None:
        params["season"] = season
    data = _get(f"competitions/{comp_code}/teams", params=params)
    return data.get("teams", []) if isinstance(data, dict) else []
