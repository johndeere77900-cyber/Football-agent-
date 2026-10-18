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
    storage.restore_quota_ledger_trust("Test fixture setup")
    storage.init_db()
    yield
    storage.restore_quota_ledger_trust("Test fixture teardown")

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

def test_unsupported_request_type_rejected_before_reservation_or_network(monkeypatch):
    """Unsupported or missing request_type must raise ValueError and produce zero reservations and zero network calls."""
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test_key")
    initial_count = storage.get_api_request_count("api_football", "2026-10-18")

    with patch("requests.get", side_effect=RuntimeError("Network call requested!")):
        for invalid_type in (None, "", "invalid_type", "historical_v2", 123):
            with pytest.raises(ValueError):
                api_football._get("fixtures", {"id": 100}, request_type=invalid_type)

    final_count = storage.get_api_request_count("api_football", "2026-10-18")
    assert final_count == initial_count


def test_storage_reserve_api_request_direct_boundary_validation():
    """Test storage.reserve_api_request boundary validation directly."""
    for invalid in (None, "", "   ", "invalid", "historical_v2", 100):
        with pytest.raises(ValueError):
            storage.reserve_api_request("api_football", "2026-10-18", "2026-10-18", "fixtures", 50, request_type=invalid)

    for bad_limit in (-1, -100, "50", 1.5, True):
        with pytest.raises(ValueError):
            storage.reserve_api_request("api_football", "2026-10-18", "2026-10-18", "fixtures", bad_limit, request_type="historical")


def test_concurrent_sqlite_historical_reservations_capped_at_50(tmp_path, monkeypatch):
    """Adversarial concurrency test: 70 simultaneous threads competing for 50 historical reservations on SQLite."""
    import concurrent.futures

    db_path = str(tmp_path / "concurrent_hist.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    storage.init_db()

    results = []

    def attempt_reservation(thread_id):
        try:
            return storage.reserve_api_request(
                provider="api_football",
                date_pattern="2026-10-18",
                request_date="2026-10-18",
                endpoint="fixtures",
                limit=50,
                request_type="historical",
                global_limit=100,
            )
        except Exception as exc:
            return exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(attempt_reservation, i) for i in range(70)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    successes = [r for r in results if r is True]
    failures = [r for r in results if r is False]
    exceptions = [r for r in results if isinstance(r, Exception)]

    assert len(exceptions) == 0, f"Encountered unexpected exceptions: {exceptions}"
    assert len(successes) == 50, f"Expected exactly 50 historical reservations to succeed, got {len(successes)}"
    assert len(failures) == 20, f"Expected 20 reservations to be denied, got {len(failures)}"

    final_count = storage.get_api_request_count("api_football", "2026-10-18")
    assert final_count == 50


def test_concurrent_sqlite_mixed_live_and_historical_reservations_capped_at_100(tmp_path, monkeypatch):
    """Adversarial concurrency test: simultaneous mixed live (70) and historical (70) threads competing against caps."""
    import concurrent.futures

    db_path = str(tmp_path / "concurrent_mixed.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    storage.init_db()

    results = []

    def attempt_reservation(req_type):
        try:
            return (
                req_type,
                storage.reserve_api_request(
                    provider="api_football",
                    date_pattern="2026-10-18",
                    request_date="2026-10-18",
                    endpoint="fixtures",
                    limit=50,
                    request_type=req_type,
                    global_limit=100,
                ),
            )
        except Exception as exc:
            return (req_type, exc)

    tasks = ["historical"] * 70 + ["live"] * 70

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(attempt_reservation, t) for t in tasks]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    hist_successes = [r for t, r in results if t == "historical" and r is True]
    live_successes = [r for t, r in results if t == "live" and r is True]
    exceptions = [r for t, r in results if isinstance(r, Exception)]

    assert len(exceptions) == 0, f"Encountered unexpected exceptions: {exceptions}"
    assert len(hist_successes) <= 50, f"Historical reservations exceeded cap of 50: {len(hist_successes)}"
    assert len(hist_successes) + len(live_successes) <= 100, f"Total reservations exceeded global cap of 100: {len(hist_successes) + len(live_successes)}"

    final_count = storage.get_api_request_count("api_football", "2026-10-18")
    assert final_count == len(hist_successes) + len(live_successes)


def test_max_budget_zero_blocks_all_requests(tmp_path, monkeypatch):
    """Test max_budget=0 results in 0 network requests for api_football, basketball_api, and football_data_api."""
    import basketball_api
    import football_data_api

    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test_key")
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test_key")

    with patch("requests.get", side_effect=RuntimeError("Network attempt triggered with max_budget=0!")):
        with pytest.raises(APIFootballQuotaExhaustedError):
            api_football._get("fixtures", {"id": 100}, max_budget=0, request_type="historical")

        with pytest.raises(basketball_api.APIBasketballQuotaExhaustedError):
            basketball_api._get("games", {"league": 12, "season": 2024}, max_budget=0, request_type="historical")

        with pytest.raises(football_data_api.FootballDataQuotaExhaustedError):
            football_data_api._get("competitions/PL/matches", max_budget=0)


def test_missing_table_recovery_fails_closed_persistently(tmp_path, monkeypatch):
    """Test missing-table SQLite recovery branch in storage.reserve_api_request() fails closed persistently across init_db calls."""
    import sqlite3

    db_file = str(tmp_path / "test_missing_table.db")
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)

    # 1. Initialize schema first
    storage.init_db()

    # 2. Intentionally drop api_request_counts table to simulate missing table
    conn = sqlite3.connect(db_file)
    conn.execute("DROP TABLE api_request_counts")
    conn.commit()
    conn.close()

    # 3. Missing table must fail closed (raise RuntimeError) and set corruption flag
    with pytest.raises(RuntimeError) as exc_info:
        storage.reserve_api_request(
            provider="api_football",
            date_pattern="2026-10-18",
            request_date="2026-10-18",
            endpoint="fixtures",
            limit=50,
            request_type="historical",
            global_limit=100,
        )
    assert "failing closed" in str(exc_info.value).lower()

    # 4. Subsequent reservation attempts MUST continue to fail closed persistently
    with pytest.raises(RuntimeError) as exc_info2:
        storage.reserve_api_request(
            provider="api_football",
            date_pattern="2026-10-18",
            request_date="2026-10-18",
            endpoint="fixtures",
            limit=50,
            request_type="historical",
            global_limit=100,
        )
    assert "failing closed" in str(exc_info2.value).lower()

    # 5. Calling ordinary init_db() MUST NOT clear corruption or permit requests!
    storage.init_db()
    with pytest.raises(RuntimeError) as exc_info3:
        storage.reserve_api_request(
            provider="api_football",
            date_pattern="2026-10-18",
            request_date="2026-10-18",
            endpoint="fixtures",
            limit=50,
            request_type="historical",
            global_limit=100,
        )
    assert "failing closed" in str(exc_info3.value).lower()

    try:
        # 6. Only explicit, auditable operator recovery via restore_quota_ledger_trust clears corruption
        storage.restore_quota_ledger_trust("Operator audited missing table and verified ledger integrity")
        res_next = storage.reserve_api_request(
            provider="api_football",
            date_pattern="2026-10-18",
            request_date="2026-10-18",
            endpoint="fixtures",
            limit=50,
            request_type="historical",
            global_limit=100,
        )
        assert res_next is True
        assert storage.get_api_request_count("api_football", "2026-10-18") == 1
    finally:
        storage.restore_quota_ledger_trust("Clean up after persistent test")
