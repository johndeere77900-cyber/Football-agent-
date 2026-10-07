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

    meta_return = {
        "fixtures": fixtures_sample,
        "page": 1,
        "expected_pages": 1,
    }

    with patch("api_football.get_league_fixtures_page", return_value=meta_return):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["league_id"] == 39
    assert report["season"] == 2024
    assert report["existing_before"] == 0
    assert report["fixtures_received"] == 2
    assert report["newly_stored"] == 2
    assert report["final_stored_count"] == 2
    assert report["status"] == "COMPLETE"

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

    meta_return = {
        "fixtures": fixtures_sample,
        "page": 1,
        "expected_pages": 1,
    }

    with patch("api_football.get_league_fixtures_page", return_value=meta_return) as mock_get:
        report1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        report2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        # Second run on COMPLETE dataset must NOT call get_league_fixtures_page
        mock_get.assert_called_once()

    assert report1["newly_stored"] == 1
    assert report1["status"] == "COMPLETE"
    assert report2["status"] == "COMPLETE"
    assert report2["api_requests_consumed"] == 0
    assert report2["final_stored_count"] == 1


def test_historical_sync_refresh_forces_reacquisition(temp_db):
    fixtures_sample = [
        {
            "fixture": {"id": 5001, "date": "2025-01-10T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
            "goals": {"home": 2, "away": 1},
        }
    ]

    meta_return = {
        "fixtures": fixtures_sample,
        "page": 1,
        "expected_pages": 1,
    }

    with patch("api_football.get_league_fixtures_page", return_value=meta_return) as mock_get:
        report1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        report2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024, refresh=True)
        assert mock_get.call_count == 2

    assert report2["already_existing_skipped"] == 1


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


def test_historical_sync_page_by_page_resumability(temp_db):
    page1_fixtures = [
        {
            "fixture": {"id": 6001, "date": "2025-01-01T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
            "goals": {"home": 1, "away": 0},
        }
    ]
    page2_fixtures = [
        {
            "fixture": {"id": 6002, "date": "2025-01-08T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 2, "name": "Team B"}, "away": {"id": 3, "name": "Team C"}},
            "goals": {"home": 2, "away": 2},
        }
    ]

    def mock_page_fetch(league_id, season, page, max_budget=None):
        if page == 1:
            return {"fixtures": page1_fixtures, "page": 1, "expected_pages": 2}
        elif page == 2:
            raise RuntimeError("Network error on page 2")
        raise RuntimeError("Unexpected page")

    # Run 1: Page 1 succeeds, Page 2 fails
    with patch("api_football.get_league_fixtures_page", side_effect=mock_page_fetch):
        report1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report1["status"] == "INCOMPLETE"
    assert report1["pages_completed"] == 1
    assert report1["expected_pages"] == 2
    assert report1["final_stored_count"] == 1

    # Verify Page 1 fixtures were safely persisted in storage
    stored_after_p1 = storage.get_historical_fixtures(39, 2024)
    assert len(stored_after_p1) == 1
    assert stored_after_p1[0]["fixture"]["id"] == 6001

    # Run 2: Resume sync starting from page 2
    def mock_page_fetch_resume(league_id, season, page, max_budget=None):
        if page == 2:
            return {"fixtures": page2_fixtures, "page": 2, "expected_pages": 2}
        raise RuntimeError(f"Unexpected page fetch for page {page}")

    with patch("api_football.get_league_fixtures_page", side_effect=mock_page_fetch_resume) as mock_resume:
        report2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report2["status"] == "COMPLETE"
    assert report2["pages_completed"] == 2
    assert report2["final_stored_count"] == 2
    stored_final = storage.get_historical_fixtures(39, 2024)
    assert len(stored_final) == 2


def test_historical_sync_empty_pages_count_persisted_on_resume(temp_db):
    page1_fixtures = []  # empty page!
    page2_fixtures = [
        {
            "fixture": {"id": 7002, "date": "2025-01-08T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 2, "name": "Team B"}, "away": {"id": 3, "name": "Team C"}},
            "goals": {"home": 2, "away": 2},
        }
    ]

    def mock_page_fetch(league_id, season, page, max_budget=None):
        if page == 1:
            return {"fixtures": page1_fixtures, "page": 1, "expected_pages": 2}
        elif page == 2:
            raise RuntimeError("Network error on page 2")
        raise RuntimeError("Unexpected page")

    with patch("api_football.get_league_fixtures_page", side_effect=mock_page_fetch):
        report1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report1["status"] == "INCOMPLETE"
    status_p1 = storage.get_historical_dataset_status(39, 2024)
    assert status_p1["empty_pages_count"] == 1

    def mock_page_fetch_resume(league_id, season, page, max_budget=None):
        if page == 2:
            return {"fixtures": page2_fixtures, "page": 2, "expected_pages": 2}
        raise RuntimeError(f"Unexpected page fetch for page {page}")

    with patch("api_football.get_league_fixtures_page", side_effect=mock_page_fetch_resume):
        report2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report2["status"] == "INCOMPLETE"
    status_p2 = storage.get_historical_dataset_status(39, 2024)
    assert status_p2["empty_pages_count"] == 1


def test_sync_historical_fixtures_season_guards(temp_db):
    """
    Verify strict football historical-season guard in sync_historical_fixtures.
    """
    with pytest.raises(ValueError, match="Football historical acquisition only supports seasons"):
        historical_sync.sync_historical_fixtures(league_id=39, season=2023)

    with pytest.raises(ValueError, match="Football historical acquisition only supports seasons"):
        historical_sync.sync_historical_fixtures(league_id=39, season=2027)
