"""
Tests for B2 Infrastructure: Neon persistence, API credit quotas, persistent caching,
pagination, fixture result TTL, prediction context, Telegram bot memory, batch grading, and migration utility.
"""

import json
import os
import pytest
import sqlite3

import api_football
import config
import generate_dashboard
import main
import migrate_sqlite_to_neon
import odds_api
import storage
import telegram_bot


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"response": []}
        self.headers = headers or {}
        self.ok = 200 <= status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


# ============================================================================
# QUOTA STORAGE FAIL-CLOSED & RETRY ACCOUNTING TESTS
# ============================================================================


def test_api_football_quota_read_failure_fails_closed(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    def bad_reserve(provider, date_pattern, request_date, endpoint, limit):
        raise RuntimeError("Database connection down during quota reservation")

    monkeypatch.setattr(storage, "reserve_api_request", bad_reserve)

    def fake_get(url, headers, params, timeout):
        raise AssertionError("Network must not be called when quota storage fails closed.")

    monkeypatch.setattr(api_football.requests, "get", fake_get)

    with pytest.raises(api_football.APIFootballQuotaExhaustedError) as exc_info:
        api_football.get_fixtures_by_date("2026-09-30")

    assert "failing closed" in str(exc_info.value)


def test_odds_api_quota_read_failure_fails_closed(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "ODDS_API_KEY", "test-odds-key")
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    def bad_reserve(provider, date_pattern, request_date, endpoint, limit):
        raise RuntimeError("Database connection down during odds quota reservation")

    monkeypatch.setattr(storage, "reserve_api_request", bad_reserve)

    def fake_get(url, params, timeout):
        raise AssertionError("Network must not be called when odds quota fails closed.")

    monkeypatch.setattr(odds_api.requests, "get", fake_get)

    odds = odds_api.get_odds_for_match("soccer_epl", "Arsenal", "Chelsea")
    assert odds is None


def test_odds_api_retry_attempts_consume_quota(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "ODDS_API_KEY", "test-odds-key")
    monkeypatch.setattr(config, "ODDS_API_MONTHLY_REQUEST_LIMIT", 500)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    calls = []

    def fake_get(url, params, timeout):
        calls.append(1)
        if len(calls) < 2:
            import requests
            raise requests.ConnectionError("Transient connection error")
        return FakeResponse(payload=[{"home_team": "Arsenal", "away_team": "Chelsea", "bookmakers": []}])

    monkeypatch.setattr(odds_api.requests, "get", fake_get)
    monkeypatch.setattr(odds_api, "_sleep_before_retry", lambda s: None)

    month_str = odds_api.time.strftime("%Y-%m", odds_api.time.gmtime())
    month_pattern = month_str + "%"

    start_count = storage.get_api_request_count("odds_api", month_pattern)
    odds = odds_api.get_odds_for_match("soccer_epl", "Arsenal", "Chelsea")
    end_count = storage.get_api_request_count("odds_api", month_pattern)

    # 1 initial attempt + 1 retry = 2 HTTP calls = 2 quota units consumed!
    assert len(calls) == 2
    assert end_count - start_count == 2


def test_api_football_atomic_quota_transition_99_to_100_to_exhausted(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")
    monkeypatch.setattr(config, "API_FOOTBALL_DAILY_CREDIT_LIMIT", 100)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    today_str = api_football.time.strftime("%Y-%m-%d", api_football.time.gmtime())

    # Pre-fill request count to 99
    for _ in range(99):
        storage.record_api_request("api_football", "fixtures", today_str)

    assert storage.get_api_request_count("api_football", today_str) == 99

    calls = []
    def fake_get(url, headers, params, timeout):
        calls.append(1)
        return FakeResponse(payload={"response": [{"fixture": {"id": 100}}]})

    monkeypatch.setattr(api_football.requests, "get", fake_get)

    # 100th request succeeds
    res = api_football.get_fixtures_by_date("2026-09-30")
    assert len(res) == 1
    assert storage.get_api_request_count("api_football", today_str) == 100

    # 101st request for a different (uncached) date is blocked by quota
    with pytest.raises(api_football.APIFootballQuotaExhaustedError):
        api_football.get_fixtures_by_date("2026-10-01")

    # Network called only once (for the 100th request)
    assert len(calls) == 1


# ============================================================================
# PERSISTENT CACHE CROSS-RUN ISOLATION TESTS
# ============================================================================


def test_persistent_database_cache_hit_prevents_network_call_without_local_disk_file(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "empty_disk_cache"))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    params = {"date": "2026-09-30"}
    cache_key = api_football._cache_key("fixtures", params)
    cached_payload = {"response": [{"fixture": {"id": 12345}}]}

    # Store payload directly in database persistent cache
    storage.set_api_cache(cache_key, "fixtures", params, cached_payload, ttl_seconds=3600)

    # Confirm disk file does not exist (simulating new ephemeral GitHub Actions runner)
    disk_path = api_football._cache_path(cache_key)
    if os.path.exists(disk_path):
        os.remove(disk_path)

    def fake_get(url, headers, params, timeout):
        raise AssertionError("Network must not be called when persistent database cache hits!")

    monkeypatch.setattr(api_football.requests, "get", fake_get)

    res = api_football.get_fixtures_by_date("2026-09-30")
    assert len(res) == 1
    assert res[0]["fixture"]["id"] == 12345


# ============================================================================
# BATCH FOOTBALL RESULT GRADING TESTS
# ============================================================================


def test_batch_football_result_grading_20_fixtures_use_1_request(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    markets = {"match_result": {"home_win": 0.50, "draw": 0.30, "away_win": 0.20}}
    conf = {"label": "High", "top_pick": "Home Win", "top_probability": 0.50}

    # Save 20 pending predictions
    for fid in range(1001, 1021):
        storage.save_prediction(
            fixture_id=fid,
            match_date="2026-09-30",
            home_team=f"Home {fid}",
            away_team=f"Away {fid}",
            league="Premier League",
            markets=markets,
            confidence=conf,
        )

    network_calls = []

    def fake_get(url, headers, params, timeout):
        network_calls.append(params)
        ids = params.get("ids", "").split("-")
        return FakeResponse(
            payload={
                "response": [
                    {
                        "fixture": {"id": int(i), "status": {"short": "FT"}},
                        "goals": {"home": 2, "away": 1},
                    }
                    for i in ids if i
                ]
            }
        )

    monkeypatch.setattr(api_football.requests, "get", fake_get)

    main.run_grading()

    # 20 fixtures in batch size 20 should make exactly 1 network call
    assert len(network_calls) == 1
    assert len(network_calls[0]["ids"].split("-")) == 20

    pending_remaining = storage.get_pending_fixtures()
    assert len(pending_remaining) == 0


def test_batch_football_result_grading_21_fixtures_use_2_requests(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    markets = {"match_result": {"home_win": 0.50, "draw": 0.30, "away_win": 0.20}}
    conf = {"label": "High", "top_pick": "Home Win", "top_probability": 0.50}

    # Save 21 pending predictions
    for fid in range(2001, 2022):
        storage.save_prediction(
            fixture_id=fid,
            match_date="2026-09-30",
            home_team=f"Home {fid}",
            away_team=f"Away {fid}",
            league="Premier League",
            markets=markets,
            confidence=conf,
        )

    network_calls = []

    def fake_get(url, headers, params, timeout):
        network_calls.append(params)
        ids = params.get("ids", "").split("-")
        return FakeResponse(
            payload={
                "response": [
                    {
                        "fixture": {"id": int(i), "status": {"short": "FT"}},
                        "goals": {"home": 1, "away": 0},
                    }
                    for i in ids if i
                ]
            }
        )

    monkeypatch.setattr(api_football.requests, "get", fake_get)

    main.run_grading()

    # 21 fixtures should make 2 network calls (20 + 1)
    assert len(network_calls) == 2
    assert len(network_calls[0]["ids"].split("-")) == 20
    assert len(network_calls[1]["ids"].split("-")) == 1


# ============================================================================
# API-FOOTBALL PAGINATION TESTS
# ============================================================================


def test_pagination_one_page(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append(params)
        return FakeResponse(
            payload={
                "paging": {"current": 1, "total": 1},
                "response": [{"fixture": {"id": 101}, "teams": {"home": {"name": "A"}, "away": {"name": "B"}}}],
            }
        )

    monkeypatch.setattr(api_football.requests, "get", fake_get)

    fixtures = api_football.get_league_fixtures(39, 2025)
    assert len(fixtures) == 1
    assert fixtures[0]["fixture"]["id"] == 101
    assert len(calls) == 1


def test_pagination_multi_page_and_deduplication(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append(params)
        page = params.get("page", 1)
        if page == 1:
            return FakeResponse(
                payload={
                    "paging": {"current": 1, "total": 2},
                    "response": [{"fixture": {"id": 101}}, {"fixture": {"id": 102}}],
                }
            )
        else:
            return FakeResponse(
                payload={
                    "paging": {"current": 2, "total": 2},
                    "response": [{"fixture": {"id": 102}}, {"fixture": {"id": 103}}], # ID 102 is duplicate
                }
            )

    monkeypatch.setattr(api_football.requests, "get", fake_get)

    fixtures = api_football.get_league_fixtures(39, 2025)
    assert len(fixtures) == 3
    ids = [f["fixture"]["id"] for f in fixtures]
    assert ids == [101, 102, 103]
    assert len(calls) == 2


def test_pagination_malformed_metadata(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    def fake_get(url, headers, params, timeout):
        return FakeResponse(
            payload={
                "paging": {"current": 2, "total": 1}, # current > total (malformed)
                "response": [{"fixture": {"id": 101}}],
            }
        )

    monkeypatch.setattr(api_football.requests, "get", fake_get)

    with pytest.raises(api_football.APIFootballError):
        api_football.get_league_fixtures(39, 2025)


# ============================================================================
# API-FOOTBALL DAILY QUOTA & RESULT TTL TESTS
# ============================================================================


def test_api_football_daily_quota_exhaustion(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")
    monkeypatch.setattr(config, "API_FOOTBALL_DAILY_CREDIT_LIMIT", 2)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    # Pre-fill request count to 2
    today_str = api_football.time.strftime("%Y-%m-%d", api_football.time.gmtime())
    storage.record_api_request("api_football", "fixtures", today_str)
    storage.record_api_request("api_football", "fixtures", today_str)

    def fake_get(url, headers, params, timeout):
        return FakeResponse(payload={"response": [{"fixture": {"id": 999}}]})

    monkeypatch.setattr(api_football.requests, "get", fake_get)

    # Next call without cache should raise APIFootballQuotaExhaustedError
    with pytest.raises(api_football.APIFootballQuotaExhaustedError):
        api_football.get_fixtures_by_date("2026-09-30")


def test_api_football_cached_response_reused_when_quota_exhausted(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")
    monkeypatch.setattr(config, "API_FOOTBALL_DAILY_CREDIT_LIMIT", 2)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    # Seed cache
    params = {"date": "2026-09-30"}
    key = api_football._cache_key("fixtures", params)
    storage.set_api_cache(key, "fixtures", params, {"response": [{"fixture": {"id": 888}}]}, 3600)

    # Exhaust quota
    today_str = api_football.time.strftime("%Y-%m-%d", api_football.time.gmtime())
    storage.record_api_request("api_football", "fixtures", today_str)
    storage.record_api_request("api_football", "fixtures", today_str)

    # Request should succeed from cache without making network request or raising quota error
    fixtures = api_football.get_fixtures_by_date("2026-09-30")
    assert len(fixtures) == 1
    assert fixtures[0]["fixture"]["id"] == 888


# ============================================================================
# ODDS API MONTHLY QUOTA & CACHE TESTS
# ============================================================================


def test_odds_api_monthly_quota_exhaustion_continues_prediction(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "ODDS_API_KEY", "test-odds-key")
    monkeypatch.setattr(config, "ODDS_API_MONTHLY_REQUEST_LIMIT", 2)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    # Exhaust monthly quota
    month_str = odds_api.time.strftime("%Y-%m", odds_api.time.gmtime())
    storage.record_api_request("odds_api", "sports/odds", month_str)
    storage.record_api_request("odds_api", "sports/odds", month_str)

    def fake_get(url, params, timeout):
        raise AssertionError("Network should not be called when odds quota exhausted.")

    monkeypatch.setattr(odds_api.requests, "get", fake_get)

    odds = odds_api.get_odds_for_match("soccer_epl", "Arsenal", "Chelsea")
    assert odds is None # Graceful optional behavior


# ============================================================================
# STORAGE & PRODUCTION ENV TESTS
# ============================================================================


def test_production_environment_fails_without_neon_url(monkeypatch):
    monkeypatch.setattr(config, "ENVIRONMENT", "production")
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)

    with pytest.raises(RuntimeError) as exc_info:
        storage.init_db()

    assert "NEON_DATABASE_URL is required in production environment" in str(exc_info.value)


def test_prediction_immutability_and_context_tagging(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    markets = {"match_result": {"home_win": 0.50, "draw": 0.30, "away_win": 0.20}}
    conf = {"label": "High", "top_pick": "Home Win", "top_probability": 0.50}

    # First save succeeds
    saved_1 = storage.save_prediction(
        fixture_id=5001,
        match_date="2026-09-30",
        home_team="Team A",
        away_team="Team B",
        league="Premier League",
        markets=markets,
        confidence=conf,
        prediction_context="PRE_MATCH",
    )
    assert saved_1 is True

    # Duplicate save is ignored and returns False
    saved_2 = storage.save_prediction(
        fixture_id=5001,
        match_date="2026-09-30",
        home_team="Team A Modified",
        away_team="Team B Modified",
        league="Premier League",
        markets=markets,
        confidence=conf,
        prediction_context="LIVE",
    )
    assert saved_2 is False


def test_atomic_result_recording_and_conflicting_result_rejection(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    markets = {"match_result": {"home_win": 0.60, "draw": 0.25, "away_win": 0.15}}
    conf = {"label": "High", "top_pick": "Home Win", "top_probability": 0.60}

    storage.save_prediction(
        fixture_id=6001,
        match_date="2026-09-30",
        home_team="Home Team",
        away_team="Away Team",
        league="La Liga",
        markets=markets,
        confidence=conf,
        home_team_id=10,
        away_team_id=20,
    )

    # First result recording
    res1 = storage.record_result(6001, 2, 1)
    assert res1 is True

    # Idempotent same result
    res2 = storage.record_result(6001, 2, 1)
    assert res2 is True

    # Conflicting result raises ValueError
    with pytest.raises(ValueError) as exc_info:
        storage.record_result(6001, 0, 3)

    assert "A different result is already recorded" in str(exc_info.value)


# ============================================================================
# TELEGRAM BOT MEMORY BOUNDING TESTS
# ============================================================================


def test_telegram_bot_memory_bounded_50_messages(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    chat_id = "test_chat_123"

    # Add 60 messages
    for i in range(1, 61):
        storage.save_telegram_message(chat_id, "user" if i % 2 == 1 else "assistant", f"Message {i}")

    messages = storage.get_recent_telegram_messages(chat_id, limit=100)
    assert len(messages) == 50
    assert messages[0]["text"] == "Message 11"
    assert messages[-1]["text"] == "Message 60"


# ============================================================================
# MIGRATION UTILITY TESTS
# ============================================================================


def test_migration_utility_dry_run_covers_6_datasets(tmp_path, monkeypatch):
    db_file = tmp_path / "source.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()

    markets = {"match_result": {"home_win": 0.50, "draw": 0.30, "away_win": 0.20}}
    conf = {"label": "High", "top_pick": "Home Win", "top_probability": 0.50}

    storage.save_prediction(
        fixture_id=7001,
        match_date="2026-09-30",
        home_team="Team X",
        away_team="Team Y",
        league="Serie A",
        markets=markets,
        confidence=conf,
    )

    storage.save_basketball_prediction(
        game_id=8001,
        game_date="2026-09-30",
        home_team="Team B1",
        away_team="Team B2",
        league="NBA",
        markets=markets,
        confidence=conf,
    )

    storage.save_telegram_message("chat_1", "user", "Hello agent")
    storage.set_api_cache("cache_1", "fixtures", {"id": 1}, {"res": 1}, 3600)
    storage.record_api_request("api_football", "fixtures", "2026-09-30")

    # Dry run check against target URL
    report = migrate_sqlite_to_neon.migrate(
        sqlite_path=str(db_file),
        target_url="postgresql://fake:fake@fake.neon.tech/fakedb",
        dry_run=True,
    )

    assert report["predictions"]["source"] == 1
    assert report["basketball_predictions"]["source"] == 1
    assert report["bot_memory"]["source"] >= 1
    assert report["api_cache"]["source"] == 1
    assert report["api_request_counts"]["source"] == 1
    assert report["verification"] == "DRY_RUN_PASSED"
    # Source file must be intact
    assert os.path.exists(str(db_file))
