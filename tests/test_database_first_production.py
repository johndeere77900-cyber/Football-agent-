"""
Tests for database-first feature architecture, provider ID isolation,
strict temporal causality, fallback persistence validation, and API credit efficiency.
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


# --- 1. Single prior match is insufficient when MIN_HISTORICAL_SAMPLE is 5 ---
def test_single_prior_match_insufficient():
    """Verify that a single prior match in DB triggers fallback to API when MIN_HISTORICAL_SAMPLE is 5."""
    league_id = 39
    season = 2024

    # DB has only 1 match for home team and 1 for away team
    historical_matches = [
        _sample_fixture_record(101, "2024-09-01T15:00:00+00:00", 1, 2, 2, 1, league_id, season),
    ]
    storage.save_historical_fixtures(historical_matches, league_id, season)

    target_fixture = {
        "fixture": {
            "id": 200,
            "date": "2024-09-20T15:00:00+00:00",
            "status": {"short": "NS"},
        },
        "league": {"id": league_id, "season": season, "name": "Premier League"},
        "teams": {
            "home": {"id": 1, "name": "Team_1"},
            "away": {"id": 2, "name": "Team_2"},
        },
    }

    mock_stats = {
        "fixtures": {"played": {"total": 5}},
        "goals": {"for": {"average": {"total": "1.5"}}, "against": {"average": {"total": "1.0"}}},
    }
    mock_form = [
        _sample_fixture_record(301, "2024-09-10T15:00:00+00:00", 1, 10, 2, 0, league_id, season),
        _sample_fixture_record(302, "2024-09-12T15:00:00+00:00", 2, 10, 1, 0, league_id, season),
    ]

    with patch("data_resolver.api_football.get_team_statistics", return_value=mock_stats) as mock_stats_call, \
         patch("data_resolver.api_football.get_recent_form", return_value=mock_form), \
         patch("data_resolver.api_football.get_head_to_head", return_value=[]):

        pred = main.predict_fixture(target_fixture, league_avg_goals=2.5)

        # Must fall back to API because 1 match < MIN_HISTORICAL_SAMPLE (5)
        assert mock_stats_call.called
        assert pred["prediction_record"]["provenance"]["provider"] == "api_football"


# --- 2. Both teams must independently satisfy minimum history ---
def test_both_teams_must_satisfy_minimum_history():
    """Verify DB-first fails if home team has 5 matches but away team has only 2."""
    league_id = 39
    season = 2024

    historical_matches = [
        # Home team (1) has 5 matches
        _sample_fixture_record(101, "2024-09-01T15:00:00+00:00", 1, 10, 2, 1, league_id, season),
        _sample_fixture_record(102, "2024-09-02T15:00:00+00:00", 1, 11, 1, 0, league_id, season),
        _sample_fixture_record(103, "2024-09-03T15:00:00+00:00", 1, 12, 2, 0, league_id, season),
        _sample_fixture_record(104, "2024-09-04T15:00:00+00:00", 1, 13, 3, 1, league_id, season),
        _sample_fixture_record(105, "2024-09-05T15:00:00+00:00", 1, 14, 1, 1, league_id, season),
        # Away team (2) has only 2 matches
        _sample_fixture_record(106, "2024-09-02T15:00:00+00:00", 2, 10, 3, 2, league_id, season),
        _sample_fixture_record(107, "2024-09-03T15:00:00+00:00", 2, 11, 0, 1, league_id, season),
    ]
    storage.save_historical_fixtures(historical_matches, league_id, season)

    target_fixture = {
        "fixture": {"id": 200, "date": "2024-09-20T15:00:00+00:00", "status": {"short": "NS"}},
        "league": {"id": league_id, "season": season, "name": "Premier League"},
        "teams": {"home": {"id": 1, "name": "Team_1"}, "away": {"id": 2, "name": "Team_2"}},
    }

    mock_stats = {
        "fixtures": {"played": {"total": 5}},
        "goals": {"for": {"average": {"total": "1.5"}}, "against": {"average": {"total": "1.0"}}},
    }
    mock_form = [
        _sample_fixture_record(301, "2024-09-10T15:00:00+00:00", 1, 10, 2, 0, league_id, season),
        _sample_fixture_record(302, "2024-09-12T15:00:00+00:00", 2, 10, 1, 0, league_id, season),
    ]

    with patch("data_resolver.api_football.get_team_statistics", return_value=mock_stats) as mock_stats_call, \
         patch("data_resolver.api_football.get_recent_form", return_value=mock_form), \
         patch("data_resolver.api_football.get_head_to_head", return_value=[]):

        pred = main.predict_fixture(target_fixture, league_avg_goals=2.5)

        # Must fall back to API because away team has only 2 matches
        assert mock_stats_call.called
        assert pred["prediction_record"]["provenance"]["provider"] == "api_football"


# --- 3 & 4 & 5. Future, same-time, and missing/invalid dates excluded ---
def test_temporal_leakage_exclusion():
    """Verify future matches, same-timestamp matches, target fixture, and invalid timestamps are excluded."""
    league_id = 39
    season = 2024
    cutoff = "2024-10-10T15:00:00+00:00"

    fixtures = [
        _sample_fixture_record(1, "2024-10-01T15:00:00+00:00", 1, 2, 2, 0, league_id, season, status="FT"),
        _sample_fixture_record(2, "2024-10-10T15:00:00+00:00", 1, 2, 1, 1, league_id, season, status="FT"), # Same-time
        _sample_fixture_record(3, "2024-10-15T15:00:00+00:00", 1, 2, 3, 0, league_id, season, status="FT"), # Future
        _sample_fixture_record(4, "invalid_date_str", 1, 2, 1, 0, league_id, season, status="FT"),        # Invalid date
    ]

    import historical_features
    history = historical_features.team_match_history(fixtures, team_id=1, cutoff=cutoff)

    assert len(history) == 1
    assert history[0]["fixture"]["id"] == 1


# --- 6. Unfinished matches not persisted as historical evidence ---
def test_unfinished_matches_rejected_from_persistence():
    """Verify scheduled/unfinished matches (NS, 1H) are rejected when require_completed=True."""
    league_id = 39
    season = 2024
    cutoff = "2024-10-20T15:00:00+00:00"

    fixtures = [
        _sample_fixture_record(1, "2024-10-01T15:00:00+00:00", 1, 2, None, None, league_id, season, status="NS"),
        _sample_fixture_record(2, "2024-10-02T15:00:00+00:00", 1, 3, None, None, league_id, season, status="1H"),
    ]

    res = storage.save_historical_fixtures(fixtures, league_id, season, cutoff=cutoff, require_completed=True)

    assert res["valid"] == 0
    assert res["rejected_count"] == 2


# --- 7 & 8. Missing or invalid final scores not persisted ---
def test_missing_or_invalid_scores_rejected():
    """Verify matches with missing, negative, or non-integer scores are rejected."""
    league_id = 39
    season = 2024

    fixtures = [
        _sample_fixture_record(1, "2024-10-01T15:00:00+00:00", 1, 2, None, 1, league_id, season, status="FT"),
        _sample_fixture_record(2, "2024-10-02T15:00:00+00:00", 1, 3, -1, 0, league_id, season, status="FT"),
        _sample_fixture_record(3, "2024-10-03T15:00:00+00:00", 1, 4, "1.5", 0, league_id, season, status="FT"),
    ]

    res = storage.save_historical_fixtures(fixtures, league_id, season, require_completed=True)

    assert res["valid"] == 0
    assert res["rejected_count"] == 3


# --- 9. Wrong league/season records rejected ---
def test_wrong_league_season_rejected():
    """Verify fallback fixtures belonging to a different league/season are rejected."""
    league_id = 39
    season = 2024

    fixtures = [
        _sample_fixture_record(1, "2024-10-01T15:00:00+00:00", 1, 2, 2, 0, league_id=140, season=2024, status="FT"),
        _sample_fixture_record(2, "2024-10-02T15:00:00+00:00", 1, 3, 1, 0, league_id=39, season=2023, status="FT"),
    ]

    res = storage.save_historical_fixtures(fixtures, league_id=39, season=2024, require_completed=True)

    assert res["valid"] == 0
    assert res["rejected_count"] == 2


# --- 10. Provider IDs cannot cross-contaminate ---
def test_provider_id_isolation_safety():
    """Verify football_data_org fixtures fail closed before making API-Football requests."""
    fixture = {
        "fixture": {"id": 700, "date": "2024-11-01T15:00:00+00:00", "status": {"short": "NS"}},
        "league": {"id": 39, "season": 2024, "name": "Premier League"},
        "teams": {"home": {"id": 999, "name": "FD_Team_Home"}, "away": {"id": 888, "name": "FD_Team_Away"}},
        "provider_provenance": {"provider": "football_data_org", "provider_type": "secondary"},
    }

    with patch("api_football.get_team_statistics") as mock_api:
        pred = main.predict_fixture(fixture, league_avg_goals=2.5)

        mock_api.assert_not_called()
        assert pred["insufficient_data"] is True
        assert pred["failure_stage"] == "season_team_stats"


# --- 11. Valid fallback historical records persisted ---
def test_football_live_prediction_api_fallback_and_persistence():
    """Verify API fallback persists valid acquired historical matches into DB."""
    league_id = 39
    season = 2024

    target_fixture = {
        "fixture": {"id": 300, "date": "2024-09-25T15:00:00+00:00", "status": {"short": "NS"}},
        "league": {"id": league_id, "season": season, "name": "Premier League"},
        "teams": {"home": {"id": 10, "name": "Team_10"}, "away": {"id": 20, "name": "Team_20"}},
    }

    mock_stats = {
        "fixtures": {"played": {"total": 5}},
        "goals": {"for": {"average": {"total": "1.8"}}, "against": {"average": {"total": "1.0"}}},
    }
    mock_form_matches = [
        _sample_fixture_record(301, "2024-09-10T15:00:00+00:00", 10, 30, 2, 0, league_id, season),
        _sample_fixture_record(302, "2024-09-12T15:00:00+00:00", 20, 30, 1, 0, league_id, season),
    ]

    with patch("api_football.get_team_statistics", return_value=mock_stats) as mock_team_stats, \
         patch("api_football.get_recent_form", return_value=mock_form_matches) as mock_recent, \
         patch("api_football.get_head_to_head", return_value=[]):

        assert storage.get_historical_fixture_count(league_id, season) == 0

        pred = main.predict_fixture(target_fixture, league_avg_goals=2.5)

        assert mock_team_stats.called
        assert pred["insufficient_data"] is False
        assert pred["prediction_record"]["provenance"]["provider"] == "api_football"

        # Auto-persisted fallback fixtures to DB
        assert storage.get_historical_fixture_count(league_id, season) >= 2


# --- 12. DB-first features used when sufficient DB data exists ---
def test_football_live_prediction_db_first_no_api_calls():
    """Verify football prediction uses stored DB fixtures without making API calls when DB history exists."""
    league_id = 39
    season = 2024

    historical_matches = [
        _sample_fixture_record(101, "2024-09-01T15:00:00+00:00", 1, 2, 2, 1, league_id, season),
        _sample_fixture_record(102, "2024-09-02T15:00:00+00:00", 1, 3, 1, 0, league_id, season),
        _sample_fixture_record(103, "2024-09-03T15:00:00+00:00", 1, 4, 2, 0, league_id, season),
        _sample_fixture_record(104, "2024-09-04T15:00:00+00:00", 1, 5, 3, 1, league_id, season),
        _sample_fixture_record(105, "2024-09-05T15:00:00+00:00", 1, 6, 1, 1, league_id, season),

        _sample_fixture_record(106, "2024-09-02T15:00:00+00:00", 2, 3, 3, 2, league_id, season),
        _sample_fixture_record(107, "2024-09-03T15:00:00+00:00", 2, 4, 0, 1, league_id, season),
        _sample_fixture_record(108, "2024-09-04T15:00:00+00:00", 2, 5, 2, 2, league_id, season),
        _sample_fixture_record(109, "2024-09-05T15:00:00+00:00", 2, 6, 1, 0, league_id, season),
    ]
    storage.save_historical_fixtures(historical_matches, league_id, season)

    target_fixture = {
        "fixture": {"id": 200, "date": "2024-09-20T15:00:00+00:00", "status": {"short": "NS"}},
        "league": {"id": league_id, "season": season, "name": "Premier League"},
        "teams": {"home": {"id": 1, "name": "Team_1"}, "away": {"id": 2, "name": "Team_2"}},
    }

    with patch("api_football.get_team_statistics") as mock_team_stats, \
         patch("api_football.get_recent_form") as mock_recent_form:

        pred = main.predict_fixture(target_fixture, league_avg_goals=2.7)

        mock_team_stats.assert_not_called()
        mock_recent_form.assert_not_called()

        assert pred["insufficient_data"] is False
        assert pred["prediction_record"]["provenance"]["provider"] == "internal_db"
        assert pred["feature_snapshot"]["season"]["home"]["matches"] == 5
        assert pred["feature_snapshot"]["season"]["away"]["matches"] == 5


# --- 13 & 14. Insufficient DB + insufficient fallback data produces INSUFFICIENT_DATA ---
def test_insufficient_data_no_fabrication():
    """Verify missing DB and API data returns INSUFFICIENT_DATA with explicit failure stage."""
    fixture = {
        "fixture": {"id": 800, "date": "2024-11-10T15:00:00+00:00", "status": {"short": "NS"}},
        "league": {"id": 39, "season": 2024, "name": "Premier League"},
        "teams": {"home": {"id": 55, "name": "Team_55"}, "away": {"id": 66, "name": "Team_66"}},
    }

    with patch("api_football.get_team_statistics", return_value=None):
        pred = main.predict_fixture(fixture, league_avg_goals=2.5)

        assert pred["insufficient_data"] is True
        assert pred["failure_stage"] == "season_team_stats"
        assert pred["markets"] is None


# --- 15. Basketball follows equivalent temporal and persistence rules ---
def test_basketball_temporal_and_persistence_rules():
    """Verify basketball DB-first execution and fallback persistence validation."""
    league_id = 12
    season = 2024

    # Seed 5 valid completed games per team
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
        "teams": {"home": {"id": 100, "name": "BB_Team_100"}, "away": {"id": 200, "name": "BB_Team_200"}},
    }

    with patch("basketball_api.get_team_statistics") as mock_bb_stats:
        pred = basketball_model.predict_game(target_game, data_cutoff_timestamp="2024-10-15T20:00:00+00:00")

        mock_bb_stats.assert_not_called()
        assert pred.get("insufficient_data") is not True
        assert pred["fixture_id"] == 600

    # Also test that saving uncompleted basketball games with require_completed=True rejects them
    uncompleted_games = [
        _sample_basketball_game_record(999, "2024-10-01T20:00:00+00:00", 100, 200, None, None, league_id, season, status="NS"),
    ]
    res = storage.save_historical_basketball_games(uncompleted_games, league_id, season, require_completed=True)
    assert res["valid"] == 0
    assert res["rejected_count"] == 1
