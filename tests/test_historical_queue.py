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


def test_queue_skips_complete_datasets(temp_db, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_LEAGUE_IDS", [39, 140])

    # Mark league 39 season 2024 COMPLETE
    storage.mark_historical_dataset_complete(
        league_id=39,
        season=2024,
        fixture_count=380,
        sport="football",
        expected_pages=10,
        pages_completed=10,
        acquisition_complete=True,
    )

    fake_report_140 = {
        "league_id": 140,
        "season": 2024,
        "status": "COMPLETE",
        "api_requests_consumed": 5,
        "quota_budget_stopped": False,
    }
    fake_report_bball = {
        "sport": "basketball",
        "league_id": 12,
        "season": 2024,
        "status": "COMPLETE",
        "api_requests_consumed": 3,
        "quota_budget_stopped": False,
    }

    with patch("historical_sync.sync_historical_fixtures", return_value=fake_report_140) as mock_football_sync, \
         patch("historical_sync.sync_historical_basketball_games", return_value=fake_report_bball) as mock_basketball_sync:

        result = historical_sync.run_historical_queue(season=2024)

    # Football sync should be called only for league 140 (league 39 was skipped without calling sync)
    assert mock_football_sync.call_count == 1
    assert mock_football_sync.call_args[1]["league_id"] == 140

    # Basketball sync should be called for league 12
    assert mock_basketball_sync.call_count == 1
    assert mock_basketball_sync.call_args[1]["league_id"] == 12

    assert result["processed_count"] == 3
    assert result["reports"][0]["league_id"] == 39
    assert result["reports"][0]["status"] == "COMPLETE"
    assert result["reports"][0]["api_requests_consumed"] == 0
    assert result["reports"][0]["skipped_reason"] == "Dataset already COMPLETE"


def test_queue_sequential_processing_all_configured_leagues(temp_db, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_LEAGUE_IDS", [39, 140, 135])

    def mock_football_sync(league_id, season, with_enrichment=False, refresh=False):
        return {
            "league_id": league_id,
            "season": season,
            "status": "COMPLETE",
            "api_requests_consumed": 2,
            "quota_budget_stopped": False,
        }

    def mock_bball_sync(league_id, season, refresh=False):
        return {
            "sport": "basketball",
            "league_id": league_id,
            "season": season,
            "status": "COMPLETE",
            "api_requests_consumed": 1,
            "quota_budget_stopped": False,
        }

    with patch("historical_sync.sync_historical_fixtures", side_effect=mock_football_sync) as mock_fb, \
         patch("historical_sync.sync_historical_basketball_games", side_effect=mock_bball_sync) as mock_bb:

        summary = historical_sync.run_historical_queue(season=2024)

    assert mock_fb.call_count == 3
    processed_leagues = [call[1]["league_id"] for call in mock_fb.call_args_list]
    assert processed_leagues == [39, 140, 135]

    assert mock_bb.call_count == 1
    assert mock_bb.call_args[1]["league_id"] == 12

    assert summary["processed_count"] == 4


def test_queue_stops_cleanly_on_quota_exhaustion(temp_db, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_LEAGUE_IDS", [39, 140, 135])

    # First league (39) succeeds
    # Second league (140) hits quota exhaustion
    def mock_football_sync(league_id, season, with_enrichment=False, refresh=False):
        if league_id == 39:
            return {
                "league_id": 39,
                "season": 2024,
                "status": "COMPLETE",
                "api_requests_consumed": 50,
                "quota_budget_stopped": False,
            }
        elif league_id == 140:
            return {
                "league_id": 140,
                "season": 2024,
                "status": "INCOMPLETE",
                "api_requests_consumed": 0,
                "quota_budget_stopped": True,
            }
        raise RuntimeError("Should not reach league 135 or basketball 12")

    with patch("historical_sync.sync_historical_fixtures", side_effect=mock_football_sync), \
         patch("historical_sync.sync_historical_basketball_games") as mock_bb:

        summary = historical_sync.run_historical_queue(season=2024)

    # Queue should stop immediately when league 140 returns quota_budget_stopped: True
    assert summary["processed_count"] == 2
    assert summary["reports"][0]["league_id"] == 39
    assert summary["reports"][1]["league_id"] == 140
    assert summary["reports"][1]["quota_budget_stopped"] is True
    mock_bb.assert_not_called()


def test_queue_resumes_from_first_unfinished_dataset(temp_db, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_LEAGUE_IDS", [39, 140, 135])

    # Run 1: League 39 completes, League 140 hits quota and stays INCOMPLETE
    storage.mark_historical_dataset_complete(
        league_id=39, season=2024, fixture_count=380, sport="football", expected_pages=10, pages_completed=10, acquisition_complete=True
    )
    storage.mark_historical_dataset_incomplete(
        league_id=140, season=2024, fixture_count=150, sport="football", expected_pages=10, pages_completed=4, acquisition_complete=False
    )

    # Run 2: Resume queue execution
    calls_fb = []
    calls_bb = []

    def mock_football_sync(league_id, season, with_enrichment=False, refresh=False):
        calls_fb.append(league_id)
        return {
            "league_id": league_id,
            "season": season,
            "status": "COMPLETE",
            "api_requests_consumed": 10,
            "quota_budget_stopped": False,
        }

    def mock_bball_sync(league_id, season, refresh=False):
        calls_bb.append(league_id)
        return {
            "sport": "basketball",
            "league_id": league_id,
            "season": season,
            "status": "COMPLETE",
            "api_requests_consumed": 10,
            "quota_budget_stopped": False,
        }

    with patch("historical_sync.sync_historical_fixtures", side_effect=mock_football_sync), \
         patch("historical_sync.sync_historical_basketball_games", side_effect=mock_bball_sync):

        summary = historical_sync.run_historical_queue(season=2024)

    # League 39 was skipped in sync_historical_fixtures because it was already COMPLETE in storage
    assert 39 not in calls_fb
    assert calls_fb == [140, 135]
    assert calls_bb == [12]
    assert summary["processed_count"] == 4
    # The first report in summary should be the skipped COMPLETE league 39
    assert summary["reports"][0]["league_id"] == 39
    assert summary["reports"][0]["skipped_reason"] == "Dataset already COMPLETE"
