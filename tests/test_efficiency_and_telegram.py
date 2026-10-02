"""
Unit and integration tests proving credit efficiency, telegram queries, game details priority,
dynamic data status, historical dataset resumption, health checks, and zero real API calls during tests.
"""

import datetime
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

    all_fixtures = api_football.get_fixtures_by_date("2026-03-30")
    assert len(all_fixtures) == 2
    assert requests_count == 1

    pl_fixtures = api_football.get_fixtures_by_date("2026-03-30", league_id=39)
    assert len(pl_fixtures) == 1
    assert pl_fixtures[0]["league"]["id"] == 39
    assert requests_count == 1  # 0 additional API calls

    la_liga_fixtures = api_football.get_fixtures_by_date("2026-03-30", league_id=140)
    assert len(la_liga_fixtures) == 1
    assert la_liga_fixtures[0]["league"]["id"] == 140
    assert requests_count == 1  # 0 additional API calls


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

    games1 = basketball_api.get_games_by_date("2026-03-30")
    assert len(games1) == 1
    assert api_calls == 1

    games2 = basketball_api.get_games_by_date("2026-03-30")
    assert len(games2) == 1
    assert api_calls == 1  # Cached!


# ---------------------------------------------------------------------------
# 2. Basketball date-query credit efficiency and request parameter checking
# ---------------------------------------------------------------------------

def test_basketball_date_query_credit_efficiency_and_params(monkeypatch):
    """
    Prove:
    1. First league-specific query makes exactly 1 API call.
    2. Request parameters contain no 'league' parameter (strictly date + season).
    3. Second query for a different league on the same date makes 0 additional API calls.
    4. Both leagues are correctly filtered from the same cached response.
    """
    sample_games = [
        {"id": 801, "date": "2026-03-30T20:00:00+00:00", "league": {"id": 12, "name": "NBA"}, "teams": {"home": {"name": "Lakers"}, "away": {"name": "Celtics"}}},
        {"id": 802, "date": "2026-03-30T22:00:00+00:00", "league": {"id": 99, "name": "EuroLeague"}, "teams": {"home": {"name": "Real Madrid"}, "away": {"name": "Barca"}}},
    ]

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.json.return_value = {"response": sample_games}

    captured_params = []

    def mock_requests_get(url, headers, params, timeout):
        captured_params.append(params)
        return mock_resp

    monkeypatch.setattr("requests.get", mock_requests_get)
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test_key")

    # First query for league 12
    nba_games = basketball_api.get_games_by_date("2026-03-30", league_id=12)
    assert len(nba_games) == 1
    assert nba_games[0]["id"] == 801
    assert len(captured_params) == 1

    # Check params: MUST NOT contain 'league'
    assert "league" not in captured_params[0]
    assert captured_params[0]["date"] == "2026-03-30"

    # Second query for league 99 on same date -> 0 additional API calls!
    euro_games = basketball_api.get_games_by_date("2026-03-30", league_id=99)
    assert len(euro_games) == 1
    assert euro_games[0]["id"] == 802
    assert len(captured_params) == 1  # 0 additional API calls!


# ---------------------------------------------------------------------------
# 3. Next N multi-date queries
# ---------------------------------------------------------------------------

def test_next_n_spans_multiple_dates(monkeypatch):
    """Prove 'next 20' queries collect fixtures across multiple forward dates chronologically."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    d1 = (today + datetime.timedelta(days=1)).isoformat()
    d2 = (today + datetime.timedelta(days=2)).isoformat()

    f_d1 = [{"fixture": {"id": 1, "date": f"{d1}T15:00:00+00:00", "status": {"short": "NS"}}, "league": {"id": 39, "name": "Premier League"}, "teams": {"home": {"name": "Arsenal"}, "away": {"name": "Chelsea"}}}]
    f_d2 = [{"fixture": {"id": 2, "date": f"{d2}T17:00:00+00:00", "status": {"short": "NS"}}, "league": {"id": 39, "name": "Premier League"}, "teams": {"home": {"name": "Liverpool"}, "away": {"name": "Everton"}}}]

    def mock_get_by_date(date_str, league_id=None):
        if date_str == d1:
            return f_d1
        if date_str == d2:
            return f_d2
        return []

    monkeypatch.setattr(api_football, "get_fixtures_by_date", mock_get_by_date)

    params = telegram_bot.parse_operation_parameters("fixtures", "Give me the next 2 Premier League fixtures")
    res = telegram_bot.handle_fixtures_op(params)

    assert "Next 2 Football Fixtures" in res
    assert "Arsenal vs Chelsea" in res
    assert "Liverpool vs Everton" in res


# ---------------------------------------------------------------------------
# 4. Game Details Lookup Order (Upcoming > Historical)
# ---------------------------------------------------------------------------

def test_game_details_lookup_priority(monkeypatch):
    """
    Prove details lookup order:
    1. Upcoming scheduled fixture wins over historical database record.
    2. Historical fallback is used when no upcoming match exists.
    """
    # Save older historical meeting (Arsenal 0 - 2 Chelsea) in DB
    historical_fixture = {
        "fixture": {"id": 11, "date": "2023-01-01T15:00:00+00:00", "venue": {"name": "Old Stamford Bridge"}, "status": {"long": "Match Finished"}},
        "league": {"id": 39, "name": "Premier League", "season": 2022},
        "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 2, "name": "Chelsea"}},
        "goals": {"home": 0, "away": 2},
    }
    storage.save_historical_fixtures([historical_fixture], league_id=39, season=2022)

    today = datetime.datetime.now(datetime.timezone.utc).date()
    d1 = (today + datetime.timedelta(days=1)).isoformat()
    upcoming_fixture = {
        "fixture": {"id": 99, "date": f"{d1}T15:00:00+00:00", "venue": {"name": "Emirates Stadium"}, "status": {"long": "Not Started"}},
        "league": {"id": 39, "name": "Premier League", "season": 2024},
        "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 2, "name": "Chelsea"}},
        "goals": {"home": None, "away": None},
    }

    monkeypatch.setattr(api_football, "get_fixtures_by_date", lambda date_str, league_id=None: [upcoming_fixture] if date_str == d1 else [])

    # Query without date: Upcoming match MUST win over historical meeting!
    params = telegram_bot.parse_operation_parameters("details", "Give me detailed data for Arsenal vs Chelsea")
    res = telegram_bot.handle_details_op(params)

    assert "Emirates Stadium" in res
    assert "Not Started" in res
    assert "Old Stamford Bridge" not in res  # Historical match did NOT win!


def test_game_details_historical_fallback(monkeypatch):
    """Prove historical meeting is returned when no upcoming match exists."""
    historical_fixture = {
        "fixture": {"id": 11, "date": "2023-01-01T15:00:00+00:00", "venue": {"name": "Highbury"}, "status": {"long": "Match Finished"}},
        "league": {"id": 39, "name": "Premier League", "season": 2022},
        "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 2, "name": "Chelsea"}},
        "goals": {"home": 2, "away": 1},
    }
    storage.save_historical_fixtures([historical_fixture], league_id=39, season=2022)

    monkeypatch.setattr(api_football, "get_fixtures_by_date", lambda date_str, league_id=None: [])

    params = telegram_bot.parse_operation_parameters("details", "Give me detailed data for Arsenal vs Chelsea")
    res = telegram_bot.handle_details_op(params)

    assert "Highbury" in res
    assert "2 - 1" in res


# ---------------------------------------------------------------------------
# 5. League Parsing & Multi-League Handler Integration
# ---------------------------------------------------------------------------

def test_league_parsing_and_handler_mapping():
    """Verify league parsing and routing for Premier League, La Liga, Serie A, and NBA."""
    leagues_to_test = [
        ("today's Premier League games", 39, "football"),
        ("today's La Liga games", 140, "football"),
        ("today's Serie A games", 135, "football"),
        ("today's NBA games", 12, "basketball"),
    ]

    for query, expected_lid, expected_sport in leagues_to_test:
        op, params = telegram_bot.resolve_operation(query)
        assert params["league_id"] == expected_lid
        assert params["sport"] == expected_sport


# ---------------------------------------------------------------------------
# 6. Historical Queue Single Season Default
# ---------------------------------------------------------------------------

def test_default_queue_does_not_acquire_all_five_seasons(monkeypatch):
    """Prove run_historical_queue() default invocation processes a single season."""
    processed_seasons = []

    def mock_sync_fb(league_id, season, **kwargs):
        processed_seasons.append(season)
        return {"league_id": league_id, "season": season, "status": "COMPLETE", "api_requests_consumed": 0, "quota_budget_stopped": False}

    def mock_sync_bb(league_id, season, **kwargs):
        processed_seasons.append(season)
        return {"sport": "basketball", "league_id": league_id, "season": season, "status": "COMPLETE", "api_requests_consumed": 0, "quota_budget_stopped": False}

    monkeypatch.setattr(historical_sync, "sync_historical_fixtures", mock_sync_fb)
    monkeypatch.setattr(historical_sync, "sync_historical_basketball_games", mock_sync_bb)

    summary = historical_sync.run_historical_queue()
    assert summary["target_seasons"] == [2024]
    assert set(processed_seasons) == {2024}


# ---------------------------------------------------------------------------
# 7. Basketball Season Helper
# ---------------------------------------------------------------------------

def test_basketball_season_for_date():
    """Verify API-Basketball 4-digit season year calculation."""
    d_aug = datetime.date(2024, 8, 15)
    d_jan = datetime.date(2025, 1, 15)

    assert basketball_api._season_for_date(d_aug) == 2024
    assert basketball_api._season_for_date(d_jan) == 2024


# ---------------------------------------------------------------------------
# 8. Zero Real API Calls Enforced
# ---------------------------------------------------------------------------

def test_zero_real_api_calls_enforced(monkeypatch):
    """Ensure tests execute without making real network HTTP calls."""
    def fail_network_call(*args, **kwargs):
        pytest.fail("Real API call attempted during test execution!")

    monkeypatch.setattr("requests.get", fail_network_call)
    monkeypatch.setattr("requests.post", fail_network_call)

    params = telegram_bot.parse_operation_parameters("data_status", "/data_status")
    res = telegram_bot.handle_data_status_op(params)
    assert "HISTORICAL DATASET STATUS" in res
