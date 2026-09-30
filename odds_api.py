"""
Optional odds comparison via The Odds API.

Prediction priority:
    The model prediction is authoritative.
    Odds are supplementary information only.

This module is deliberately defensive because The Odds API is an
optional external dependency with a tighter request budget than the
football data provider.

Responsibilities:
- validate inputs;
- fail closed when the odds provider is unavailable or quota exhausted;
- persistent API response caching to protect monthly credit budget;
- enforce monthly request limit (500 requests / month);
- avoid unnecessary duplicate requests;
- retry only transient failures with bounded backoff;
- safely match the requested fixture;
- safely convert decimal odds to implied probabilities;
- never fabricate odds data.

A failure in this module must never become a reason to discard an
otherwise valid football prediction.
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone

import requests

import config
import storage


# ----------------------------------------------------------------------
# Request / cache configuration
# ----------------------------------------------------------------------

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

DEFAULT_CACHE_TTL_MINUTES = 15


# ----------------------------------------------------------------------
# Validation helpers
# ----------------------------------------------------------------------


def _validate_text(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string.")

    value = value.strip()
    if not value:
        raise ValueError(f"{name} must not be empty.")

    return value


def _validate_sport_key(sport_key):
    return _validate_text(sport_key, "sport_key")


def _validate_regions(regions):
    return _validate_text(regions, "regions")


def _api_key():
    key = getattr(config, "ODDS_API_KEY", None)
    if not isinstance(key, str):
        raise ValueError("ODDS_API_KEY is not configured.")

    key = key.strip()
    if not key:
        raise ValueError("ODDS_API_KEY is not configured.")

    return key


def _base_url():
    base_url = getattr(config, "ODDS_API_BASE_URL", None)
    if not isinstance(base_url, str):
        raise ValueError("ODDS_API_BASE_URL is not configured.")

    base_url = base_url.strip().rstrip("/")
    if not base_url:
        raise ValueError("ODDS_API_BASE_URL is empty.")

    return base_url


# ----------------------------------------------------------------------
# Cache helpers
# ----------------------------------------------------------------------


def _cache_ttl_seconds():
    raw = getattr(config, "ODDS_CACHE_TTL_MINUTES", DEFAULT_CACHE_TTL_MINUTES)
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        minutes = DEFAULT_CACHE_TTL_MINUTES

    if minutes < 0:
        return 0.0

    return minutes * 60.0


def _cache_key(sport_key, home_team, away_team, regions):
    payload = {
        "sport_key": sport_key,
        "home_team": home_team,
        "away_team": away_team,
        "regions": regions,
    }

    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"odds_{digest}"


def _cache_path(sport_key, home_team, away_team, regions):
    cache_dir = getattr(config, "CACHE_DIR", ".api_cache")
    if not isinstance(cache_dir, str) or not cache_dir.strip():
        raise ValueError("CACHE_DIR must be a non-empty string.")

    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(
        cache_dir,
        _cache_key(sport_key, home_team, away_team, regions) + ".json",
    )


def _cache_get(sport_key, home_team, away_team, regions):
    cache_key = _cache_key(sport_key, home_team, away_team, regions)

    try:
        persistent = storage.get_api_cache(cache_key)
        if persistent is not None and isinstance(persistent, dict) and "events" in persistent:
            return persistent
    except Exception:
        pass

    path = _cache_path(sport_key, home_team, away_team, regions)
    if not os.path.exists(path):
        return None

    try:
        age_seconds = time.time() - os.path.getmtime(path)
        if age_seconds < 0 or age_seconds > _cache_ttl_seconds():
            return None

        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, dict) and "events" in data and isinstance(data["events"], list):
            return data

    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    return None


def _cache_set(sport_key, home_team, away_team, regions, data):
    if not isinstance(data, dict):
        raise ValueError("Cached odds data must be a dictionary.")

    events = data.get("events")
    if not isinstance(events, list):
        raise ValueError("Cached odds data must contain an events list.")

    cache_key = _cache_key(sport_key, home_team, away_team, regions)
    ttl_seconds = _cache_ttl_seconds()

    try:
        params = {
            "sport_key": sport_key,
            "home_team": home_team,
            "away_team": away_team,
            "regions": regions,
        }
        storage.set_api_cache(cache_key, "sports/odds", params, data, ttl_seconds)
    except Exception:
        pass

    path = _cache_path(sport_key, home_team, away_team, regions)
    temp_path = path + ".tmp" + f".{os.getpid()}"

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


# ----------------------------------------------------------------------
# Quota Management
# ----------------------------------------------------------------------


def _check_and_consume_odds_quota():
    """
    Atomically check and reserve monthly request quota.

    Fails closed: if quota storage cannot be queried or updated, returns False
    so the system does not make untracked Odds API calls.
    """
    limit = int(getattr(config, "ODDS_API_MONTHLY_REQUEST_LIMIT", 500))
    current_month_pattern = datetime.now(timezone.utc).strftime("%Y-%m") + "%"
    current_month_str = datetime.now(timezone.utc).strftime("%Y-%m")

    try:
        reserved = storage.reserve_api_request("odds_api", current_month_pattern, current_month_str, "sports/odds", limit)
    except Exception as exc:
        print(f"Odds API quota storage error ({exc}); failing closed.")
        return False

    if not reserved:
        print(f"Odds API monthly limit reached ({limit}/{limit}). Continuing prediction without odds.")
        return False

    return True


# ----------------------------------------------------------------------
# API response validation & HTTP Request
# ----------------------------------------------------------------------


def _validate_events(data):
    if not isinstance(data, list):
        raise ValueError("The Odds API response must be a list.")
    return {"events": data}


def _parse_response(response):
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError("The Odds API returned invalid JSON.") from exc

    return _validate_events(data)


def _retry_after_seconds(response, default):
    raw = response.headers.get("Retry-After")
    if raw is not None:
        try:
            seconds = float(raw)
            if seconds >= 0:
                return min(seconds, 60.0)
        except (TypeError, ValueError):
            pass

    return min(float(default), 60.0)


def _sleep_before_retry(seconds):
    seconds = max(0.0, min(float(seconds), 60.0))
    time.sleep(seconds)


def _get_events(sport_key, home_team, away_team, regions):
    """
    Fetch Odds API events with persistent cache-first behavior and monthly quota protection.
    """
    sport_key = _validate_sport_key(sport_key)
    home_team = _validate_text(home_team, "home_team")
    away_team = _validate_text(away_team, "away_team")
    regions = _validate_regions(regions)

    # 1. Check cache FIRST
    cached = _cache_get(sport_key, home_team, away_team, regions)
    if cached is not None:
        return cached

    api_key = _api_key()
    url = f"{_base_url()}/sports/{sport_key}/odds"
    params = {
        "apiKey": api_key,
        "regions": regions,
        "markets": "h2h",
        "oddsFormat": "decimal",
    }

    backoff = RETRY_BACKOFF_SECONDS

    # 2. HTTP attempt loop
    for attempt in range(1, MAX_RETRIES + 1):
        # Quota reservation BEFORE EVERY ACTUAL NETWORK ATTEMPT
        if not _check_and_consume_odds_quota():
            return {"events": []}

        try:
            response = requests.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            if attempt >= MAX_RETRIES:
                raise

            _sleep_before_retry(backoff)
            backoff *= 2
            continue

        status = response.status_code

        if status in RETRYABLE_STATUS_CODES:
            if attempt >= MAX_RETRIES:
                response.raise_for_status()

            if status == 429:
                delay = _retry_after_seconds(response, backoff)
            else:
                delay = min(float(backoff), 60.0)

            _sleep_before_retry(delay)
            backoff *= 2
            continue

        response.raise_for_status()
        data = _parse_response(response)
        _cache_set(sport_key, home_team, away_team, regions, data)
        return data

    raise RuntimeError("The Odds API request failed.")


# ----------------------------------------------------------------------
# Team / fixture matching
# ----------------------------------------------------------------------


def _normalize_team_name(name):
    if not isinstance(name, str):
        return ""

    normalized = name.strip().lower()
    replacements = (
        ("&", "and"),
        (".", " "),
        (",", " "),
        ("'", ""),
        ("-", " "),
        ("_", " "),
    )

    for old, new in replacements:
        normalized = normalized.replace(old, new)

    return " ".join(normalized.split())


def _team_names_match(requested, provided):
    requested = _normalize_team_name(requested)
    provided = _normalize_team_name(provided)

    if not requested or not provided:
        return False

    if requested == provided:
        return True

    suffixes = (
        " fc",
        " afc",
        " cf",
        " sc",
        " bc",
        " basketball",
        " football club",
    )

    for suffix in suffixes:
        if provided == requested + suffix or requested == provided + suffix:
            return True

    return False


def _teams_match(event, home_team, away_team):
    if not isinstance(event, dict):
        return False

    event_home = event.get("home_team")
    event_away = event.get("away_team")

    return _team_names_match(home_team, event_home) and _team_names_match(away_team, event_away)


# ----------------------------------------------------------------------
# Odds conversion
# ----------------------------------------------------------------------


def _decimal_to_implied_prob(decimal_odds):
    try:
        decimal_odds = float(decimal_odds)
    except (TypeError, ValueError):
        return None

    if decimal_odds <= 1.0 or decimal_odds != decimal_odds:
        return None

    probability = 1.0 / decimal_odds
    if not (0.0 < probability <= 1.0):
        return None

    return probability


def _average(values):
    valid = []
    for value in values:
        probability = _decimal_to_implied_prob(value)
        if probability is not None:
            valid.append(probability)

    if not valid:
        return None

    return sum(valid) / len(valid)


# ----------------------------------------------------------------------
# Market summarization
# ----------------------------------------------------------------------


def _summarize_odds(event):
    if not isinstance(event, dict):
        return None

    home_team = event.get("home_team")
    away_team = event.get("away_team")

    if not isinstance(home_team, str) or not isinstance(away_team, str):
        return None

    home_odds, draw_odds, away_odds = [], [], []
    bookmakers = event.get("bookmakers", [])
    if not isinstance(bookmakers, list):
        bookmakers = []

    bookmakers_counted = 0

    for bookmaker in bookmakers:
        if not isinstance(bookmaker, dict):
            continue

        markets = bookmaker.get("markets", [])
        if not isinstance(markets, list):
            continue

        bookmaker_h2h_found = False

        for market in markets:
            if not isinstance(market, dict) or market.get("key") != "h2h":
                continue

            outcomes = market.get("outcomes", [])
            if not isinstance(outcomes, list):
                continue

            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue

                name = outcome.get("name")
                price = outcome.get("price")
                if not isinstance(name, str):
                    continue

                if name == home_team and _decimal_to_implied_prob(price) is not None:
                    home_odds.append(price)
                    bookmaker_h2h_found = True
                elif name == away_team and _decimal_to_implied_prob(price) is not None:
                    away_odds.append(price)
                    bookmaker_h2h_found = True
                elif name.strip().lower() == "draw" and _decimal_to_implied_prob(price) is not None:
                    draw_odds.append(price)
                    bookmaker_h2h_found = True

        if bookmaker_h2h_found:
            bookmakers_counted += 1

    result = {"bookmakers_counted": bookmakers_counted}
    implied_home = _average(home_odds)
    implied_draw = _average(draw_odds)
    implied_away = _average(away_odds)

    if implied_home is not None:
        result["implied_home_win"] = implied_home
    if implied_draw is not None:
        result["implied_draw"] = implied_draw
    if implied_away is not None:
        result["implied_away_win"] = implied_away

    if len(result) == 1:
        return None

    return result


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def get_odds_for_match(sport_key, home_team, away_team, regions="uk,eu"):
    """
    Return bookmaker implied probabilities for one fixture.
    """
    try:
        sport_key = _validate_sport_key(sport_key)
        home_team = _validate_text(home_team, "home_team")
        away_team = _validate_text(away_team, "away_team")
        regions = _validate_regions(regions)
    except ValueError:
        return None

    try:
        data = _get_events(sport_key, home_team, away_team, regions)
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        print(f"  Odds API unavailable: {exc}")
        return None

    events = data.get("events", [])
    if not isinstance(events, list):
        return None

    for event in events:
        if not _teams_match(event, home_team, away_team):
            continue

        return _summarize_odds(event)

    return None


def clear_expired_cache():
    """Remove expired Odds API cache files."""
    cache_dir = getattr(config, "CACHE_DIR", ".api_cache")
    if not isinstance(cache_dir, str) or not cache_dir.strip() or not os.path.isdir(cache_dir):
        return 0

    ttl_seconds = _cache_ttl_seconds()
    now = time.time()
    removed = 0

    try:
        filenames = os.listdir(cache_dir)
    except OSError:
        return 0

    for filename in filenames:
        if not filename.startswith("odds_") or not filename.endswith(".json"):
            continue

        path = os.path.join(cache_dir, filename)
        try:
            age_seconds = now - os.path.getmtime(path)
            if age_seconds >= 0 and age_seconds > ttl_seconds:
                os.remove(path)
                removed += 1
        except OSError:
            continue

    return removed
