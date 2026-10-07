from unittest.mock import MagicMock, patch
import pytest

from data_resolver import DataResolver
import storage


@pytest.fixture(autouse=True)
def init_test_db():
    storage.init_db()


@patch("api_football.get_fixtures_by_date")
def test_data_resolver_primary_success(mock_api_fb):
    mock_api_fb.return_value = [
        {
            "fixture": {"id": 1, "date": "2025-01-15T15:00:00Z", "status": {"short": "NS"}},
            "league": {"id": 39, "season": 2024, "name": "Premier League"},
            "teams": {
                "home": {"id": 10, "name": "Arsenal"},
                "away": {"id": 20, "name": "Chelsea"},
            },
        }
    ]

    resolver = DataResolver()
    data, meta = resolver.get_fixtures_for_date("2025-01-15", league_id=39)

    assert len(data) == 1
    assert meta["data_source"] == "api_football"
    assert meta["primary_attempted"] is True
    assert meta["fallback_used"] is False
    assert meta["resolver_status"] == "PRIMARY_SUCCESS"


@patch("api_football.get_fixtures_by_date")
@patch("football_data_api.get_competition_matches")
def test_data_resolver_fallback_success(mock_fd_matches, mock_api_fb):
    mock_api_fb.side_effect = RuntimeError("API Football Unavailable")

    mock_fd_matches.return_value = [
        {
            "id": 999,
            "utcDate": "2025-01-15T15:00:00Z",
            "status": "SCHEDULED",
            "competition": {"name": "Premier League"},
            "season": {"startDate": "2024-08-01"},
            "homeTeam": {"id": 101, "name": "Arsenal FC", "shortName": "Arsenal"},
            "awayTeam": {"id": 102, "name": "Chelsea FC", "shortName": "Chelsea"},
            "score": {"fullTime": {"home": None, "away": None}},
        }
    ]

    resolver = DataResolver()
    data, meta = resolver.get_fixtures_for_date("2025-01-15", league_id=39)

    assert len(data) == 1
    assert data[0]["fixture"]["id"] == 999
    assert data[0]["teams"]["home"]["name"] == "Arsenal"
    assert meta["data_source"] == "football_data_org"
    assert meta["primary_attempted"] is True
    assert meta["fallback_used"] is True
    assert meta["resolver_status"] in ("SECONDARY_SUCCESS", "FALLBACK_SUCCESS")


@patch("api_football.get_league_standings")
@patch("football_data_api.get_competition_standings")
def test_data_resolver_standings_fallback(mock_fd_standings, mock_api_fb_standings):
    mock_api_fb_standings.side_effect = RuntimeError("Primary failure")
    mock_fd_standings.return_value = [
        {
            "position": 1,
            "team": {"id": 10, "name": "Arsenal", "shortName": "Arsenal"},
            "playedGames": 20,
            "won": 15,
            "draw": 3,
            "lost": 2,
            "points": 48,
            "goalsFor": 45,
            "goalsAgainst": 15,
            "goalDifference": 30,
        }
    ]

    resolver = DataResolver()
    standings, meta = resolver.get_standings(39, season=2024)

    assert len(standings) == 1
    assert standings[0]["rank"] == 1
    assert standings[0]["points"] == 48
    assert meta["data_source"] == "football_data_org"
    assert meta["fallback_used"] is True


@patch("api_football.get_fixtures_by_date")
@patch("football_data_api.get_competition_matches")
def test_primary_valid_empty_returns_primary_success_zero_secondary_calls(mock_fd_matches, mock_api_fb):
    """
    Prove VALID_EMPTY sufficiency:
    When primary provider legitimately returns [] (confirmed 0 scheduled matches),
    DataResolver returns PRIMARY_SUCCESS immediately and makes ZERO secondary API calls.
    """
    mock_api_fb.return_value = []

    resolver = DataResolver()
    data, meta = resolver.get_fixtures_for_date("2025-01-15", league_id=39)

    assert data == []
    assert meta["resolver_status"] == "PRIMARY_SUCCESS"
    assert meta["fallback_used"] is False
    assert mock_fd_matches.call_count == 0  # Zero secondary calls!


@patch("api_football.get_fixtures_by_date")
@patch("football_data_api.get_competition_matches")
def test_primary_insufficient_malformed_triggers_secondary_fallback(mock_fd_matches, mock_api_fb):
    """
    Prove INSUFFICIENT_MALFORMED sufficiency:
    When primary provider returns malformed records (e.g. missing fixture or teams dicts),
    DataResolver considers secondary fallback.
    """
    mock_api_fb.return_value = [{"malformed_item_no_fixture_key": True}]

    mock_fd_matches.return_value = [
        {
            "id": 888,
            "utcDate": "2025-01-15T15:00:00Z",
            "status": "SCHEDULED",
            "competition": {"name": "Premier League"},
            "season": {"startDate": "2024-08-01"},
            "homeTeam": {"id": 101, "name": "Arsenal FC", "shortName": "Arsenal"},
            "awayTeam": {"id": 102, "name": "Chelsea FC", "shortName": "Chelsea"},
            "score": {"fullTime": {"home": None, "away": None}},
        }
    ]

    resolver = DataResolver()
    data, meta = resolver.get_fixtures_for_date("2025-01-15", league_id=39)

    assert len(data) == 1
    assert data[0]["fixture"]["id"] == 888
    assert meta["resolver_status"] == "SECONDARY_SUCCESS"
    assert meta["fallback_used"] is True


import api_football
import football_data_api
import soccerdata_provider


# Test A — API quota -> football-data fallback
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_A_api_quota_exhaustion_falls_back_to_football_data(mock_api_fb, mock_fd, mock_sd):
    mock_api_fb.side_effect = api_football.APIFootballQuotaExhaustedError("API-Football quota exhausted")
    mock_fd.return_value = {
        "matches": [
            {
                "id": 1001,
                "utcDate": "2024-08-17T14:00:00Z",
                "status": "FINISHED",
                "competition": {"name": "Premier League", "code": "PL"},
                "season": {"startDate": "2024-08-01"},
                "homeTeam": {"id": 10, "name": "Arsenal FC", "shortName": "Arsenal"},
                "awayTeam": {"id": 20, "name": "Chelsea FC", "shortName": "Chelsea"},
                "score": {"fullTime": {"home": 2, "away": 1}},
            }
        ],
        "metadata": {"count": 1, "played": 1},
    }

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "football_data_org"
    assert len(res["fixtures"]) == 1
    assert res["fixtures"][0]["fixture"]["id"] == 1001
    assert res["fixtures"][0]["provider_provenance"]["provider"] == "football_data_org"
    assert mock_api_fb.call_count == 1
    assert mock_fd.call_count == 1


# Test B — API error -> football-data fallback
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_B_api_error_falls_back_to_football_data(mock_api_fb, mock_fd, mock_sd):
    mock_api_fb.side_effect = api_football.APIFootballError("500 Internal Server Error")
    mock_fd.return_value = {
        "matches": [
            {
                "id": 1002,
                "utcDate": "2024-08-18T14:00:00Z",
                "status": "FINISHED",
                "competition": {"name": "Premier League", "code": "PL"},
                "season": {"startDate": "2024-08-01"},
                "homeTeam": {"id": 11, "name": "Liverpool FC", "shortName": "Liverpool"},
                "awayTeam": {"id": 21, "name": "Everton FC", "shortName": "Everton"},
                "score": {"fullTime": {"home": 3, "away": 0}},
            }
        ],
        "metadata": {"count": 1, "played": 1},
    }

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "football_data_org"
    assert len(res["fixtures"]) == 1
    assert res["fixtures"][0]["fixture"]["id"] == 1002
    assert res["fixtures"][0]["provider_provenance"]["provider"] == "football_data_org"


# Test C — API-Football empty historical response -> fallback
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_C_api_empty_response_falls_back_to_football_data(mock_api_fb, mock_fd, mock_sd):
    mock_api_fb.return_value = {"fixtures": [], "expected_pages": 1, "current_page": 1}
    mock_fd.return_value = {
        "matches": [
            {
                "id": 1003,
                "utcDate": "2024-08-19T14:00:00Z",
                "status": "FINISHED",
                "competition": {"name": "Premier League", "code": "PL"},
                "season": {"startDate": "2024-08-01"},
                "homeTeam": {"id": 10, "name": "Arsenal FC", "shortName": "Arsenal"},
                "awayTeam": {"id": 20, "name": "Chelsea FC", "shortName": "Chelsea"},
                "score": {"fullTime": {"home": 2, "away": 1}},
            }
        ],
        "metadata": {"count": 1, "played": 1},
    }

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "football_data_org"
    assert len(res["fixtures"]) == 1
    assert res["fixtures"][0]["fixture"]["id"] == 1003
    assert res["fixtures"][0]["provider_provenance"]["provider"] == "football_data_org"


# Test D — Secondary failure -> SoccerData
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_D_secondary_failure_falls_back_to_soccerdata(mock_api_fb, mock_fd, mock_sd):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary error")
    mock_fd.side_effect = football_data_api.FootballDataAPIError("Secondary error")
    mock_sd.return_value = (
        "SOURCE_AVAILABLE",
        [
            {
                "fixture": {"id": "sd_1003", "date": "2024-08-19T14:00:00Z", "status": {"short": "FT"}},
                "league": {"id": 39, "season": 2024, "name": "Premier League"},
                "teams": {"home": {"id": "sd_10", "name": "Arsenal"}, "away": {"id": "sd_20", "name": "Chelsea"}},
                "goals": {"home": 2, "away": 1},
                "provider_provenance": {"provider": "soccerdata", "provider_type": "tertiary"},
            }
        ],
        {"status": "SOURCE_AVAILABLE", "count": 1},
    )

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "soccerdata"
    assert len(res["fixtures"]) == 1
    assert res["fixtures"][0]["fixture"]["id"] == "sd_1003"
    assert res["fixtures"][0]["provider_provenance"]["provider"] == "soccerdata"


# Test E — Fallback complete
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_E_fallback_complete_metadata_marks_dataset_complete(mock_api_fb, mock_fd, mock_sd):
    import historical_sync

    mock_api_fb.side_effect = api_football.APIFootballQuotaExhaustedError("Quota exhausted")
    matches = [
        {
            "id": 2000 + i,
            "utcDate": "2024-08-17T14:00:00Z",
            "status": "FINISHED",
            "competition": {"name": "Premier League", "code": "PL"},
            "season": {"startDate": "2024-08-01"},
            "homeTeam": {"id": (i % 20) + 1, "name": f"Team {(i % 20) + 1}"},
            "awayTeam": {"id": ((i + 1) % 20) + 1, "name": f"Team {((i + 1) % 20) + 1}"},
            "score": {"fullTime": {"home": 1, "away": 0}},
        }
        for i in range(380)
    ]
    mock_fd.return_value = {
        "matches": matches,
        "metadata": {"count": 380, "played": 380},
    }

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "COMPLETE"
    assert report["acquisition_complete"] is True
    assert report["valid_fixtures"] == 380

    status = storage.get_historical_dataset_status(39, 2024)
    assert status["status"] == "COMPLETE"
    assert status["acquisition_complete"] is True


# Test F — Fallback partial
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_F_fallback_partial_metadata_marks_dataset_incomplete(mock_api_fb, mock_fd, mock_sd):
    import historical_sync

    mock_api_fb.side_effect = api_football.APIFootballQuotaExhaustedError("Quota exhausted")
    mock_fd.return_value = {
        "matches": [
            {
                "id": 2001,
                "utcDate": "2024-08-17T14:00:00Z",
                "status": "FINISHED",
                "competition": {"name": "Premier League", "code": "PL"},
                "season": {"startDate": "2024-08-01"},
                "homeTeam": {"id": 10, "name": "Arsenal FC", "shortName": "Arsenal"},
                "awayTeam": {"id": 20, "name": "Chelsea FC", "shortName": "Chelsea"},
                "score": {"fullTime": {"home": 2, "away": 1}},
            }
        ],
        "metadata": {"count": 1, "played": 1, "is_partial": True},
    }

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"
    assert report["acquisition_complete"] is False

    status = storage.get_historical_dataset_status(39, 2024)
    assert status["status"] == "INCOMPLETE"
    assert status["acquisition_complete"] is False
