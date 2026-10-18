"""
Tests for quota configuration parsing and hard quota enforcement in api_football and storage.
"""

from unittest.mock import patch, MagicMock
import pytest
import config
import storage
import api_football
from api_football import APIFootballQuotaExhaustedError, APIFootballError

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_quota.db")
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    storage.init_db()
    yield

def test_config_historical_budget_parsing(monkeypatch):
    """Test defensive parsing of API_FOOTBALL_HISTORICAL_DAILY_BUDGET in config.py."""
    # 1. Missing value
    monkeypatch.delenv("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", raising=False)
    assert config._parse_historical_daily_budget("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50, 50) == 50

    # 2. "0"
    monkeypatch.setenv("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", "0")
    assert config._parse_historical_daily_budget("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50, 50) == 0

    # 3. "30"
    monkeypatch.setenv("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", "30")
    assert config._parse_historical_daily_budget("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50, 50) == 30

    # 4. "50"
    monkeypatch.setenv("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", "50")
    assert config._parse_historical_daily_budget("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50, 50) == 50

    # 5. "51" (clamped to 50)
    monkeypatch.setenv("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", "51")
    assert config._parse_historical_daily_budget("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50, 50) == 50

    # 6. "-1"
    monkeypatch.setenv("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", "-1")
    assert config._parse_historical_daily_budget("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50, 50) == 0

    # 7. "1.9" (fractional rejected, fallback to default 50)
    monkeypatch.setenv("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", "1.9")
    assert config._parse_historical_daily_budget("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50, 50) == 50

    # 8. "invalid"
    monkeypatch.setenv("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", "invalid")
    assert config._parse_historical_daily_budget("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50, 50) == 50

    # 9. Whitespace around an integer " 30 "
    monkeypatch.setenv("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", " 30 ")
    assert config._parse_historical_daily_budget("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50, 50) == 30

def test_exactly_50_historical_attempts_allowed_51st_denied(monkeypatch):
    """Verify exactly 50 historical attempts reserve quota, and the 51st attempt raises APIFootballQuotaExhaustedError."""
    monkeypatch.setattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50)
    monkeypatch.setattr(config, "API_FOOTBALL_DAILY_CREDIT_LIMIT", 100)

    for i in range(50):
        api_football._check_and_consume_quota("fixtures", request_type="historical")

    assert storage.get_api_request_count("api_football", "2026-10-18") == 50

    with pytest.raises(APIFootballQuotaExhaustedError):
        api_football._check_and_consume_quota("fixtures", request_type="historical")

def test_cache_hit_consumes_zero_quota(monkeypatch):
    """Verify a persistent cache hit consumes zero request quota."""
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test_key")

    # Store payload in cache
    cache_key = api_football._cache_key("fixtures", {"id": 12345})
    storage.set_api_cache(cache_key, "fixtures", {"id": 12345}, {"response": [{"fixture": {"id": 12345}}]}, 3600)

    initial_count = storage.get_api_request_count("api_football", "2026-10-18")

    res = api_football.get_fixture_result(12345)
    assert res == {"fixture": {"id": 12345}}

    final_count = storage.get_api_request_count("api_football", "2026-10-18")
    assert final_count == initial_count

def test_retry_attempts_counted_individually(monkeypatch):
    """Verify each retry after network error or 500 error consumes an individual quota reservation attempt."""
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test_key")

    mock_resp_500 = MagicMock()
    mock_resp_500.status_code = 500
    mock_resp_500.ok = False

    initial_count = storage.get_api_request_count("api_football", "2026-10-18")

    with patch("requests.get", return_value=mock_resp_500):
        with pytest.raises(APIFootballError):
            api_football._get("fixtures", {"id": 999}, request_type="historical")

    final_count = storage.get_api_request_count("api_football", "2026-10-18")
    assert final_count == initial_count + api_football.MAX_RETRIES

def test_mixed_live_and_historical_calls_obey_both_limits(monkeypatch):
    """
    Verify mixed live and historical calls obey both limits:
    Historical is capped at 50, live can continue up to global credit limit (100).
    """
    monkeypatch.setattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50)
    monkeypatch.setattr(config, "API_FOOTBALL_DAILY_CREDIT_LIMIT", 100)

    # Make 50 historical requests
    for i in range(50):
        api_football._check_and_consume_quota("fixtures", request_type="historical")

    # 51st historical request is denied
    with pytest.raises(APIFootballQuotaExhaustedError):
        api_football._check_and_consume_quota("fixtures", request_type="historical")

    # Live requests are still allowed up to 100 total
    for i in range(50):
        api_football._check_and_consume_quota("fixtures", request_type="live")

    # 101st total request (51st live) is denied
    with pytest.raises(APIFootballQuotaExhaustedError):
        api_football._check_and_consume_quota("fixtures", request_type="live")

def test_quota_db_error_fails_closed(monkeypatch):
    """Quota storage database error must fail closed with APIFootballQuotaExhaustedError."""
    with patch("storage.reserve_api_request", side_effect=RuntimeError("Database transaction lock failure")):
        with pytest.raises(APIFootballQuotaExhaustedError):
            api_football._check_and_consume_quota("fixtures", request_type="historical")
