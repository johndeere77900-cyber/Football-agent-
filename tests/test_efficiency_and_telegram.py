"""
Unit and integration tests proving credit efficiency, telegram queries, game details,
dynamic data status, historical dataset resumption, and zero real API calls during tests.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import api_football
import basketball_api
import config
import historical_sync
import storage
import telegram_bot


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Isolated database and cache directory for every test."""
    db_file = tmp_path / "test_efficiency.db"
    cache_dir = tmp_path / ".api_cache"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    monkeypatch.setattr(config, "REQUIRE_NEON", False)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")

    storage.init_db()
    return db_file


# ---------------------------------------------------------------------------
# 1. One date request serves multiple leagues & cache prevents duplicate requests
# ---------------------------------------------------------------------------

def test_one_date_request_serves_multiple_leagues(monkeypatch):
    """Prove a single cached date request serves multiple leagues locally without duplicate API calls."""
    sample_fixtures = [
        {"fixture": {"id": 101, "date": "2026-03-30T15:00:00+00:00"}, "league": {"id": 39, "name": "Premier League"}, "teams": {"home": {"name": "Arsenal"}, "away": {"name": "Chelsea"}}},
        {"fixture": {"id": 102, "date": "2026-03-30T17:00:00+00:00"}, "league": {"id": 140, "name": "La Liga"}, "teams": {"home": {"name": "Real Madrid"}, "away": {"name": "Barcelona"}}},
    ]

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.json.return_value = {"response": sample_fixtures}

    requests_count = 0

    def mock_requests_get(*args, **kwargs):
        nonlocal requests_count
        requests_count += 1
        return mock_resp

    monkeypatch.setattr("requests.get", mock_requests_get)
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test_key")

    # First call for full date -> executes 1 HTTP request & populates cache
    all_fixtures = api_football.get_fixtures_by_date("2026-03-30")
    assert len(all_fixtures) == 2
    assert requests_count == 1

    # Second call for specific league 39 -> served from cached full date (0 new HTTP calls)
    pl_fixtures = api_football.get_fixtures_by_date("2026-03-30", league_id=39)
    assert len(pl_fixtures) == 1
    assert pl_fixtures[0]["league"]["id"] == 39
    assert requests_count == 1  # No extra HTTP call made!

    # Third call for specific league 140 -> served from cached full date (0 new HTTP calls)
    la_liga_fixtures = api_football.get_fixtures_by_date("2026-03-30", league_id=140)
    assert len(la_liga_fixtures) == 1
    assert la_liga_fixtures[0]["league"]["id"] == 140
    assert requests_count == 1  # Still no extra HTTP call made!


def test_cache_prevents_duplicate_requests(monkeypatch):
    """Prove that persistent cache prevents duplicate API calls."""
    sample_games = [
        {"id": 201, "date": "2026-03-30T20:00:00+00:00", "league": {"id": 12, "name": "NBA"}, "teams": {"home": {"name": "Lakers"}, "away": {"name": "Celtics"}}},
    ]

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.json.return_value = {"response": sample_games}

    api_calls = 0

    def mock_requests_get(*args, **kwargs):
        nonlocal api_calls
        api_calls += 1
        return mock_resp

    monkeypatch.setattr("requests.get", mock_requests_get)
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test_key")

    # First fetch -> HTTP request executed and cached
    games1 = basketball_api.get_games_by_date("2026-03-30")
    assert len(games1) == 1
    assert api_calls == 1

    # Second fetch -> served from persistent cache (0 new HTTP requests)
    games2 = basketball_api.get_games_by_date("2026-03-30")
    assert len(games2) == 1
    assert api_calls == 1  # Cached!


# ---------------------------------------------------------------------------
# 2. COMPLETE datasets require zero new requests & incomplete datasets resume
# ---------------------------------------------------------------------------

def test_complete_datasets_require_zero_new_requests(monkeypatch):
    """Prove COMPLETE datasets require 0 new API requests during historical sync."""
    storage.mark_historical_dataset_complete(
        league_id=39,
        season=2024,
        fixture_count=380,
        sport="football",
        expected_pages=10,
        pages_completed=10,
        acquisition_complete=True,
    )

    fetch_called = False

    def mock_page(*args, **kwargs):
        nonlocal fetch_called
        fetch_called = True
        return {"fixtures": [], "expected_pages": 10}

    monkeypatch.setattr(api_football, "get_league_fixtures_page", mock_page)

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "COMPLETE"
    assert report["api_requests_consumed"] == 0
    assert not fetch_called


def test_incomplete_datasets_resume(monkeypatch):
    """Prove incomplete datasets resume from page_completed + 1."""
    storage.mark_historical_dataset_incomplete(
        league_id=39,
        season=2024,
        fixture_count=100,
        sport="football",
        expected_pages=5,
        pages_completed=2,
        acquisition_complete=False,
    )

    requested_pages = []

    def mock_page(league_id, season, page=1, **kwargs):
        requested_pages.append(page)
        return {
            "fixtures": [
                {
                    "fixture": {"id": 1000 + page, "date": "2024-10-01T15:00:00+00:00", "status": {"short": "FT"}},
                    "league": {"id": 39, "season": 2024},
                    "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
                    "goals": {"home": 2, "away": 1},
                }
            ],
            "expected_pages": 5,
        }

    monkeypatch.setattr(api_football, "get_league_fixtures_page", mock_page)

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    # Should resume from page 3, 4, 5
    assert requested_pages == [3, 4, 5]
    assert report["status"] == "COMPLETE"


# ---------------------------------------------------------------------------
# 3. Telegram query features: league filtering, requested fixture count, basketball
# ---------------------------------------------------------------------------

def test_telegram_league_filtering_and_fixture_count(monkeypatch):
    """Test Telegram fixture query league filtering and requested quantity."""
    sample_fixtures = [
        {"fixture": {"id": 301, "date": "2026-03-30T15:00:00+00:00"}, "league": {"id": 39, "name": "Premier League"}, "teams": {"home": {"name": "Arsenal"}, "away": {"name": "Chelsea"}}},
        {"fixture": {"id": 302, "date": "2026-03-30T17:00:00+00:00"}, "league": {"id": 140, "name": "La Liga"}, "teams": {"home": {"name": "Real Madrid"}, "away": {"name": "Barcelona"}}},
        {"fixture": {"id": 303, "date": "2026-03-30T19:00:00+00:00"}, "league": {"id": 39, "name": "Premier League"}, "teams": {"home": {"name": "Liverpool"}, "away": {"name": "Everton"}}},
    ]

    monkeypatch.setattr(api_football, "get_fixtures_by_date", lambda date_str, league_id=None: sample_fixtures)

    # Test "Show today's Premier League games"
    params = telegram_bot.parse_operation_parameters("fixtures", "Show today's Premier League games")
    res = telegram_bot.handle_fixtures_op(params)

    assert "Premier League" in res
    assert "Arsenal vs Chelsea" in res
    assert "Liverpool vs Everton" in res
    assert "Real Madrid vs Barcelona" not in res  # Filtered out!

    # Test quantity "Give me the next 1 Premier League fixtures"
    params_q = telegram_bot.parse_operation_parameters("fixtures", "Give me the next 1 Premier League fixtures")
    res_q = telegram_bot.handle_fixtures_op(params_q)
    assert "Arsenal vs Chelsea" in res_q
    assert "Liverpool vs Everton" not in res_q  # Quantity limited to 1!


def test_telegram_basketball_queries(monkeypatch):
    """Test Telegram NBA basketball fixture queries."""
    sample_nba_games = [
        {"id": 501, "time": "20:00", "league": {"id": 12, "name": "NBA"}, "teams": {"home": {"name": "Lakers"}, "away": {"name": "Celtics"}}},
        {"id": 502, "time": "22:30", "league": {"id": 12, "name": "NBA"}, "teams": {"home": {"name": "Warriors"}, "away": {"name": "Bulls"}}},
    ]

    monkeypatch.setattr(basketball_api, "get_games_by_date", lambda date_str, league_id=None: sample_nba_games)

    params = telegram_bot.parse_operation_parameters("fixtures", "Show today's NBA games")
    res = telegram_bot.handle_fixtures_op(params)

    assert "NBA" in res
    assert "Lakers vs Celtics" in res
    assert "Warriors vs Bulls" in res


# ---------------------------------------------------------------------------
# 4. Detailed game lookup & /data_status
# ---------------------------------------------------------------------------

def test_detailed_game_lookup(monkeypatch):
    """Test detailed game lookup using local data first."""
    # Store a fixture in local historical DB
    fixture_data = {
        "fixture": {"id": 999, "date": "2026-03-30T15:00:00+00:00", "venue": {"name": "Emirates Stadium"}, "status": {"long": "Match Finished"}},
        "league": {"id": 39, "name": "Premier League", "season": 2024},
        "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 2, "name": "Chelsea"}},
        "goals": {"home": 3, "away": 1},
    }
    storage.save_historical_fixtures([fixture_data], league_id=39, season=2024)

    params = telegram_bot.parse_operation_parameters("details", "Give me detailed data for Arsenal vs Chelsea")
    res = telegram_bot.handle_details_op(params)

    assert "DETAILED FIXTURE DATA" in res
    assert "Arsenal vs Chelsea" in res
    assert "Emirates Stadium" in res
    assert "3 - 1" in res


def test_data_status_shows_all_datasets(monkeypatch):
    """Test /data_status displays all datasets stored in Neon/storage."""
    storage.mark_historical_dataset_complete(
        league_id=39, season=2024, fixture_count=380, sport="football", expected_pages=10, pages_completed=10, acquisition_complete=True
    )
    storage.mark_historical_dataset_complete(
        league_id=12, season=2024, fixture_count=1230, sport="basketball", expected_pages=1, pages_completed=1, acquisition_complete=True
    )

    params = telegram_bot.parse_operation_parameters("data_status", "/data_status")
    res = telegram_bot.handle_data_status_op(params)

    assert "HISTORICAL DATASET STATUS" in res
    assert "Football" in res
    assert "League 39" in res
    assert "Basketball" in res
    assert "League 12" in res
    assert "COMPLETE" in res


def test_zero_real_api_calls_enforced(monkeypatch):
    """Ensure tests execute without making real network HTTP calls."""
    def fail_network_call(*args, **kwargs):
        pytest.fail("Real API call attempted during test execution!")

    monkeypatch.setattr("requests.get", fail_network_call)
    monkeypatch.setattr("requests.post", fail_network_call)

    # Run data status and local details lookup without network calls
    params = telegram_bot.parse_operation_parameters("data_status", "/data_status")
    res = telegram_bot.handle_data_status_op(params)
    assert "HISTORICAL DATASET STATUS" in res
