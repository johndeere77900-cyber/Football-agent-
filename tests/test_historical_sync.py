import pytest
from unittest.mock import patch, MagicMock
import config
import storage
import historical_sync


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "predictions.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    storage.init_db()
    return db_path


def test_historical_sync_saves_fixtures(temp_db, monkeypatch):
    fixtures_sample = [
        {
            "fixture": {"id": 5001, "date": "2025-01-10T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {
                "home": {"id": 1, "name": "Team A"},
                "away": {"id": 2, "name": "Team B"},
            },
            "goals": {"home": 2, "away": 1},
        },
        {
            "fixture": {"id": 5002, "date": "2025-01-17T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {
                "home": {"id": 2, "name": "Team B"},
                "away": {"id": 3, "name": "Team C"},
            },
            "goals": {"home": 0, "away": 0},
        },
    ]

    with patch("api_football.get_league_fixtures", return_value=fixtures_sample):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["league_id"] == 39
    assert report["season"] == 2024
    assert report["existing_before"] == 0
    assert report["fixtures_received"] == 2
    assert report["newly_stored"] == 2
    assert report["final_stored_count"] == 2

    # Verify database contents
    stored = storage.get_historical_fixtures(39, 2024)
    assert len(stored) == 2


def test_historical_sync_repeated_runs_are_idempotent(temp_db):
    fixtures_sample = [
        {
            "fixture": {"id": 5001, "date": "2025-01-10T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
            "goals": {"home": 2, "away": 1},
        }
    ]

    with patch("api_football.get_league_fixtures", return_value=fixtures_sample):
        report1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        report2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report1["newly_stored"] == 1
    assert report2["newly_stored"] == 0
    assert report2["already_existing_skipped"] == 1
    assert report2["final_stored_count"] == 1


def test_historical_sync_respects_quota_budget(temp_db, monkeypatch):
    monkeypatch.setattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 5)

    # Pre-fill api_request_counts to simulate 5 requests already used today
    for i in range(5):
        storage.record_api_request("api_football", "fixtures")

    with patch("api_football.get_league_fixtures") as mock_get:
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        mock_get.assert_not_called()

    assert report["quota_budget_stopped"] is True
    assert report["fixtures_received"] == 0
