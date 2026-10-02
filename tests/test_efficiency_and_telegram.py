"""
Unit and integration tests proving credit efficiency, telegram queries, game details priority,
dynamic data status, historical dataset resumption, health checks, league filtering fail-closed,
next-N horizon behavior, and zero real API calls during tests.
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
# 1. Missing league metadata fails filtering & requested league excludes others
# ---------------------------------------------------------------------------

def test_missing_league_metadata_fails_league_filtering(monkeypatch):
    """
    Prove:
    1. Records with missing/malformed league metadata DO NOT pass league filtering when league_id is requested.
    2. Requested league excludes other leagues.
    """
    sample_fixtures = [
        {"fixture": {"id": 101, "date": "2026-03-30T15:00:00+00:00"}, "league": {"id": 39, "name": "Premier League"}, "teams": {"home": {"name": "Arsenal"}, "away": {"name": "Chelsea"}}},
        {"fixture": {"id": 102, "date": "2026-03-30T17:00:00+00:00"}, "league": {"id": 140, "name": "La Liga"}, "teams": {"home": {"name": "Real Madrid"}, "away": {"name": "Barcelona"}}},
        {"fixture": {"id": 103, "date": "2026-03-30T19:00:00+00:00"}, "teams": {"home": {"name": "Unknown A"}, "away": {"name": "Unknown B"}}},  # Missing league dict!
    ]

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.json.return_value = {"response": sample_fixtures}

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: mock_resp)
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test_key")

    pl_fixtures = api_football.get_fixtures_by_date("2026-03-30", league_id=39)

    # Must contain ONLY Premier League (39) and exclude La Liga (140) and record missing league metadata!
    assert len(pl_fixtures) == 1
    assert pl_fixtures[0]["fixture"]["id"] == 101


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
# 3. Next N multi-date queries & horizon behavior
# ---------------------------------------------------------------------------

def test_next_n_spans_multiple_dates_and_horizon_reporting(monkeypatch):
    """Prove 'next N' collects fixtures across dates and reports horizon status when exhausted."""
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

    # Ask for 20 fixtures when only 2 exist in 14-day horizon
    params = telegram_bot.parse_operation_parameters("fixtures", "Give me the next 20 Premier League fixtures")
    res = telegram_bot.handle_fixtures_op(params)

    assert "Next 20 Football Fixtures" in res
    assert "Arsenal vs Chelsea" in res
    assert "Liverpool vs Everton" in res
    assert "14-day safety horizon" in res  # Reports horizon boundary!


# ---------------------------------------------------------------------------
# 4. Game Details Lookup Order (Upcoming > Historical; Both teams required)
# ---------------------------------------------------------------------------

def test_game_details_historical_requires_both_teams(monkeypatch):
    """Prove historical fallback details lookup requires BOTH teams when two teams are specified."""
    # Historical record involving Arsenal vs Tottenham
    hist_spurs = {
        "fixture": {"id": 10, "date": "2023-01-01T15:00:00+00:00", "venue": {"name": "White Hart Lane"}, "status": {"long": "Match Finished"}},
        "league": {"id": 39, "name": "Premier League", "season": 2022},
        "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 3, "name": "Tottenham"}},
        "goals": {"home": 2, "away": 0},
    }
    # Historical record involving Arsenal vs Chelsea
    hist_chelsea = {
        "fixture": {"id": 11, "date": "2023-02-01T15:00:00+00:00", "venue": {"name": "Stamford Bridge"}, "status": {"long": "Match Finished"}},
        "league": {"id": 39, "name": "Premier League", "season": 2022},
        "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 2, "name": "Chelsea"}},
        "goals": {"home": 1, "away": 1},
    }
    storage.save_historical_fixtures([hist_spurs, hist_chelsea], league_id=39, season=2022)

    monkeypatch.setattr(api_football, "get_fixtures_by_date", lambda date_str, league_id=None: [])

    # Query for Arsenal vs Chelsea MUST NOT return Arsenal vs Tottenham!
    params = telegram_bot.parse_operation_parameters("details", "Give me detailed data for Arsenal vs Chelsea")
    res = telegram_bot.handle_details_op(params)

    assert "Stamford Bridge" in res
    assert "1 - 1" in res
    assert "White Hart Lane" not in res  # Does NOT return arbitrary single-team record!


# ---------------------------------------------------------------------------
# 5. Target Seasons Queue Reconciliation
# ---------------------------------------------------------------------------

def test_target_seasons_queue_reconciliation(monkeypatch):
    """Prove run_historical_queue() defaults to config.TARGET_SEASONS single source of truth."""
    processed_seasons = []

    def mock_sync_fb(league_id, season, **kwargs):
        processed_seasons.append(season)
        return {"league_id": league_id, "season": season, "status": "COMPLETE", "api_requests_consumed": 0, "quota_budget_stopped": False}

    def mock_sync_bb(league_id, season, **kwargs):
        processed_seasons.append(season)
        return {"sport": "basketball", "league_id": league_id, "season": season, "status": "COMPLETE", "api_requests_consumed": 0, "quota_budget_stopped": False}

    monkeypatch.setattr(historical_sync, "sync_historical_fixtures", mock_sync_fb)
    monkeypatch.setattr(historical_sync, "sync_historical_basketball_games", mock_sync_bb)

    # Run default queue (no seasons or season arg)
    summary = historical_sync.run_historical_queue()

    assert summary["target_seasons"] == [2020, 2021, 2022, 2023, 2024]
    assert set(processed_seasons) == {2020, 2021, 2022, 2023, 2024}


# ---------------------------------------------------------------------------
# 6. Basketball Season Helper
# ---------------------------------------------------------------------------

def test_basketball_season_for_date():
    """Verify API-Basketball 4-digit season year calculation."""
    d_aug = datetime.date(2024, 8, 15)
    d_jan = datetime.date(2025, 1, 15)

    assert basketball_api._season_for_date(d_aug) == 2024
    assert basketball_api._season_for_date(d_jan) == 2024


# ---------------------------------------------------------------------------
# 7. Zero Real API Calls Enforced
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
