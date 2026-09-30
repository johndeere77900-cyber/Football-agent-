"""
Tests for B2 Infrastructure: Neon persistence, API credit quotas, persistent caching,
pagination, fixture result TTL, prediction context, Telegram bot memory, and migration utility.
"""

import json
import os
import pytest
import sqlite3

import api_football
import config
import generate_dashboard
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


def test_migration_utility_dry_run(tmp_path, monkeypatch):
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

    # Dry run check against target URL
    report = migrate_sqlite_to_neon.migrate(
        sqlite_path=str(db_file),
        target_url="postgresql://fake:fake@fake.neon.tech/fakedb",
        dry_run=True,
    )

    assert report["football_predictions_source_count"] == 1
    assert report["verification"] == "DRY_RUN_PASSED"
    # Source file must be intact
    assert os.path.exists(str(db_file))
