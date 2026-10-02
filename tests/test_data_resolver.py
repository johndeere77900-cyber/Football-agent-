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
