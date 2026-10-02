"""
Tests for database-first feature architecture, provider ID isolation,
strict temporal causality, and API credit efficiency in production prediction.
"""

from unittest.mock import patch, MagicMock
import pytest
import config
import main
import basketball_model
import storage
import time_utils


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Use an isolated SQLite test database for database-first tests."""
    db_file = tmp_path / "test_db_first.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    storage.init_db()
    yield


def _sample_fixture_record(fixture_id, kickoff_at, home_id, away_id, home_goals, away_goals, league_id=39, season=2024, status="FT"):
    return {
        "fixture": {
            "id": fixture_id,
            "date": kickoff_at,
            "status": {"short": status, "long": "Match Finished"},
        },
        "league": {
            "id": league_id,
            "season": season,
            "name": "Premier League",
        },
        "teams": {
            "home": {"id": home_id, "name": f"Team_{home_id}"},
            "away": {"id": away_id, "name": f"Team_{away_id}"},
        },
        "goals": {
            "home": home_goals,
            "away": away_goals,
        },
    }


def _sample_basketball_game_record(game_id, game_date, home_id, away_id, home_pts, away_pts, league_id=12, season=2024, status="FT"):
    return {
        "id": game_id,
        "date": game_date,
        "league": {
            "id": league_id,
            "season": season,
            "name": "NBA",
        },
        "status": {"short": status},
        "teams": {
            "home": {"id": home_id, "name": f"BB_Team_{home_id}"},
            "away": {"id": away_id, "name": f"BB_Team_{away_id}"},
        },
        "scores": {
            "home": {"total": home_pts},
            "away": {"total": away_pts},
        },
    }


def test_football_live_prediction_db_first_no_api_calls():
    """Verify football prediction uses stored DB fixtures without making API calls when DB history exists."""
    league_id = 39
    season = 2024

    # Seed database with completed historical fixtures
    historical_matches = [
        _sample_fixture_record(101, "2024-09-01T15:00:00+00:00", 1, 2, 2, 1, league_id, season),
        _sample_fixture_record(102, "2024-09-08T15:00:00+00:00", 1, 3, 1, 0, league_id, season),
        _sample_fixture_record(103, "2024-09-15T15:00:00+00:00", 2, 3, 3, 2, league_id, season),
    ]
    storage.save_historical_fixtures(historical_matches, league_id, season)

    # Target fixture scheduled after historical matches
    target_fixture = {
        "fixture": {
            "id": 200,
            "date": "2024-09-20T15:00:00+00:00",
            "status": {"short": "NS", "long": "Not Started"},
        },
        "league": {
            "id": league_id,
            "season": season,
            "name": "Premier League",
        },
        "teams": {
            "home": {"id": 1, "name": "Team_1"},
            "away": {"id": 2, "name": "Team_2"},
        },
    }

    with patch("api_football.get_team_statistics") as mock_team_stats, \
         patch("api_football.get_recent_form") as mock_recent_form, \
         patch("api_football.get_head_to_head") as mock_h2h:

        pred = main.predict_fixture(target_fixture, league_avg_goals=2.7)

        # Assert no API-Football network calls were made
        mock_team_stats.assert_not_called()
        mock_recent_form.assert_not_called()
        mock_h2h.assert_not_called()

        assert pred["insufficient_data"] is False
        assert pred["prediction_record"]["provenance"]["provider"] == "internal_db"
        assert pred["feature_snapshot"]["season"]["home"]["matches"] == 2
        assert pred["feature_snapshot"]["season"]["away"]["matches"] == 2


def test_football_live_prediction_api_fallback_and_persistence():
    """Verify fallback to API occurs when DB is empty, and API response can be persisted."""
    league_id = 39
    season = 2024

    target_fixture = {
        "fixture": {
            "id": 300,
            "date": "2024-09-25T15:00:00+00:00",
            "status": {"short": "NS"},
        },
        "league": {
            "id": league_id,
            "season": season,
            "name": "Premier League",
        },
        "teams": {
            "home": {"id": 10, "name": "Team_10"},
            "away": {"id": 20, "name": "Team_20"},
        },
    }

    mock_stats = {
        "fixtures": {"played": {"total": 5}},
        "goals": {
            "for": {"average": {"total": "1.8"}},
            "against": {"average": {"total": "1.0"}},
        },
    }

    mock_form_matches = [
        _sample_fixture_record(301, "2024-09-10T15:00:00+00:00", 10, 30, 2, 0, league_id, season),
        _sample_fixture_record(302, "2024-09-12T15:00:00+00:00", 20, 30, 1, 0, league_id, season),
    ]

    with patch("api_football.get_team_statistics", return_value=mock_stats) as mock_team_stats, \
         patch("api_football.get_recent_form", return_value=mock_form_matches) as mock_recent, \
         patch("api_football.get_head_to_head", return_value=[]) as mock_h2h:

        assert storage.get_historical_fixture_count(league_id, season) == 0

        pred = main.predict_fixture(target_fixture, league_avg_goals=2.5)

        assert mock_team_stats.called
        assert pred["insufficient_data"] is False
        assert pred["prediction_record"]["provenance"]["provider"] == "api_football"

        # Verify predict_fixture automatically persisted acquired historical matches to DB
        db_count = storage.get_historical_fixture_count(league_id, season)
        assert db_count == 2


def test_basketball_live_prediction_db_first():
    """Verify basketball prediction uses stored DB games without calling API-Basketball."""
    league_id = 12
    season = 2024

    # Seed at least 5 games per team to meet minimum sample threshold
    db_games = [
        _sample_basketball_game_record(501, "2024-10-01T20:00:00+00:00", 100, 200, 110, 105, league_id, season),
        _sample_basketball_game_record(502, "2024-10-02T20:00:00+00:00", 100, 300, 115, 100, league_id, season),
        _sample_basketball_game_record(503, "2024-10-03T20:00:00+00:00", 100, 400, 108, 102, league_id, season),
        _sample_basketball_game_record(504, "2024-10-04T20:00:00+00:00", 100, 500, 120, 110, league_id, season),
        _sample_basketball_game_record(505, "2024-10-05T20:00:00+00:00", 100, 600, 105, 99, league_id, season),

        _sample_basketball_game_record(506, "2024-10-01T20:00:00+00:00", 200, 300, 102, 98, league_id, season),
        _sample_basketball_game_record(507, "2024-10-02T20:00:00+00:00", 200, 400, 106, 101, league_id, season),
        _sample_basketball_game_record(508, "2024-10-03T20:00:00+00:00", 200, 500, 112, 108, league_id, season),
        _sample_basketball_game_record(509, "2024-10-04T20:00:00+00:00", 200, 600, 99, 95, league_id, season),
    ]
    storage.save_historical_basketball_games(db_games, league_id, season)

    target_game = {
        "id": 600,
        "date": "2024-10-15T20:00:00+00:00",
        "league": {"id": league_id, "season": season, "name": "NBA"},
        "teams": {
            "home": {"id": 100, "name": "BB_Team_100"},
            "away": {"id": 200, "name": "BB_Team_200"},
        },
    }

    with patch("basketball_api.get_team_statistics") as mock_bb_stats:
        pred = basketball_model.predict_game(target_game, data_cutoff_timestamp="2024-10-15T20:00:00+00:00")

        mock_bb_stats.assert_not_called()
        assert pred.get("insufficient_data") is not True
        assert pred["sport"] == "basketball"
        assert pred["fixture_id"] == 600


def test_provider_id_isolation_safety():
    """Verify football_data_org fixtures fail closed before making API-Football requests."""
    fixture = {
        "fixture": {
            "id": 700,
            "date": "2024-11-01T15:00:00+00:00",
            "status": {"short": "NS"},
        },
        "league": {
            "id": 39,
            "season": 2024,
            "name": "Premier League",
        },
        "teams": {
            "home": {"id": 999, "name": "FD_Team_Home"},
            "away": {"id": 888, "name": "FD_Team_Away"},
        },
        "provider_provenance": {
            "provider": "football_data_org",
            "provider_type": "secondary",
        },
    }

    with patch("api_football.get_team_statistics") as mock_api:
        pred = main.predict_fixture(fixture, league_avg_goals=2.5)

        mock_api.assert_not_called()
        assert pred["insufficient_data"] is True
        assert pred["failure_stage"] == "season_team_stats"
        assert "Secondary provider" in pred["reason"]


def test_temporal_leakage_exclusion():
    """Verify future matches, same-timestamp matches, target fixture, and unfinished matches are excluded."""
    league_id = 39
    season = 2024
    cutoff = "2024-10-10T15:00:00+00:00"

    fixtures = [
        # Valid prior completed match
        _sample_fixture_record(1, "2024-10-01T15:00:00+00:00", 1, 2, 2, 0, league_id, season, status="FT"),
        # Same timestamp match -> MUST BE EXCLUDED
        _sample_fixture_record(2, "2024-10-10T15:00:00+00:00", 1, 2, 1, 1, league_id, season, status="FT"),
        # Future match -> MUST BE EXCLUDED
        _sample_fixture_record(3, "2024-10-15T15:00:00+00:00", 1, 2, 3, 0, league_id, season, status="FT"),
        # Unfinished match prior to cutoff -> MUST BE EXCLUDED
        _sample_fixture_record(4, "2024-10-05T15:00:00+00:00", 1, 2, 0, 0, league_id, season, status="NS"),
    ]

    import historical_features
    history = historical_features.team_match_history(fixtures, team_id=1, cutoff=cutoff)

    assert len(history) == 1
    assert history[0]["fixture"]["id"] == 1


def test_insufficient_data_no_fabrication():
    """Verify that missing required data returns explicit failure stage rather than fabricated stats."""
    fixture = {
        "fixture": {
            "id": 800,
            "date": "2024-11-10T15:00:00+00:00",
            "status": {"short": "NS"},
        },
        "league": {
            "id": 39,
            "season": 2024,
            "name": "Premier League",
        },
        "teams": {
            "home": {"id": 55, "name": "Team_55"},
            "away": {"id": 66, "name": "Team_66"},
        },
    }

    with patch("api_football.get_team_statistics", return_value=None):
        pred = main.predict_fixture(fixture, league_avg_goals=2.5)

        assert pred["insufficient_data"] is True
        assert pred["failure_stage"] == "season_team_stats"
        assert pred["markets"] is None
        assert pred["confidence"] is None
