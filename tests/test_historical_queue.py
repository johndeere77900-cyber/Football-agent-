"""
Unit tests for historical queue orchestration in historical_sync.py.
Tests COMPLETE skip, sequential processing, quota stop, and resume behaviors.
"""

from unittest.mock import MagicMock, patch
import pytest

import config
import historical_sync
import main


def test_queue_defaults():
    """Verify queue uses config allowed leagues when leagues argument is omitted."""
    with patch("storage.get_api_request_count", return_value=0), \
         patch("storage.get_historical_dataset_status", return_value={"status": "COMPLETE", "fixture_count": 100}), \
         patch("historical_sync.sync_historical_fixtures") as mock_sync:
        res = historical_sync.run_historical_queue(sport="football", seasons=[2024])

        assert res["sport"] == "football"
        assert res["leagues"] == config.ALLOWED_LEAGUE_IDS
        assert res["seasons"] == [2024]
        assert res["quota_budget_exhausted"] is False
        assert len(res["datasets_processed"]) == len(config.ALLOWED_LEAGUE_IDS)
        mock_sync.assert_not_called()


def test_queue_complete_skip():
    """Verify datasets with status COMPLETE are skipped with 0 API requests."""
    with patch("storage.get_api_request_count", return_value=0), \
         patch("storage.get_historical_dataset_status", return_value={"status": "COMPLETE", "fixture_count": 380}), \
         patch("historical_sync.sync_historical_fixtures") as mock_sync:
        res = historical_sync.run_historical_queue(
            sport="football",
            leagues=[39],
            seasons=[2024],
            refresh=False,
        )

        assert len(res["datasets_processed"]) == 1
        rep = res["datasets_processed"][0]
        assert rep["status"] == "COMPLETE"
        assert rep["skipped_reason"] == "Dataset already COMPLETE"
        assert rep["api_requests_consumed"] == 0
        mock_sync.assert_not_called()


def test_queue_sequential_processing():
    """Verify queue processes league/season pairs sequentially in expected order."""
    call_order = []

    def mock_sync_fixtures(league_id, season, **kwargs):
        call_order.append((league_id, season))
        return {
            "league_id": league_id,
            "season": season,
            "status": "COMPLETE",
            "api_requests_consumed": 5,
            "quota_budget_stopped": False,
        }

    with patch("storage.get_api_request_count", return_value=0), \
         patch("storage.get_historical_dataset_status", return_value={"status": "NONE", "fixture_count": 0}), \
         patch("historical_sync.sync_historical_fixtures", side_effect=mock_sync_fixtures):
        res = historical_sync.run_historical_queue(
            sport="football",
            leagues=[39, 140],
            seasons=[2023, 2024],
        )

        assert call_order == [(39, 2023), (39, 2024), (140, 2023), (140, 2024)]
        assert len(res["datasets_processed"]) == 4
        assert res["quota_budget_exhausted"] is False


def test_queue_quota_stop():
    """Verify queue stops execution cleanly when daily API budget limit is reached."""
    call_count = 0

    def mock_sync_fixtures(league_id, season, **kwargs):
        nonlocal call_count
        call_count += 1
        return {
            "league_id": league_id,
            "season": season,
            "status": "INCOMPLETE",
            "api_requests_consumed": 50,
            "quota_budget_stopped": True,
        }

    with patch("storage.get_api_request_count", return_value=0), \
         patch("storage.get_historical_dataset_status", return_value={"status": "NONE", "fixture_count": 0}), \
         patch("historical_sync.sync_historical_fixtures", side_effect=mock_sync_fixtures):
        res = historical_sync.run_historical_queue(
            sport="football",
            leagues=[39, 140, 135],
            seasons=[2024],
            historical_budget=50,
        )

        assert call_count == 1
        assert len(res["datasets_processed"]) == 1
        assert res["quota_budget_exhausted"] is True


def test_queue_resume():
    """Verify that after a quota stop, a subsequent run skips completed items and resumes cleanly."""
    dataset_statuses = {
        (39, 2024): {"status": "COMPLETE", "fixture_count": 380},
        (140, 2024): {"status": "INCOMPLETE", "fixture_count": 100, "pages_completed": 2},
    }

    def mock_get_status(league_id, season, sport="football"):
        return dataset_statuses.get((league_id, season), {"status": "NONE", "fixture_count": 0})

    synced_leagues = []

    def mock_sync_fixtures(league_id, season, **kwargs):
        synced_leagues.append((league_id, season))
        return {
            "league_id": league_id,
            "season": season,
            "status": "COMPLETE",
            "api_requests_consumed": 10,
            "quota_budget_stopped": False,
        }

    with patch("storage.get_api_request_count", return_value=0), \
         patch("storage.get_historical_dataset_status", side_effect=mock_get_status), \
         patch("historical_sync.sync_historical_fixtures", side_effect=mock_sync_fixtures):
        res = historical_sync.run_historical_queue(
            sport="football",
            leagues=[39, 140],
            seasons=[2024],
        )

        # League 39 is COMPLETE -> skipped
        # League 140 is INCOMPLETE -> synced
        assert synced_leagues == [(140, 2024)]
        assert len(res["datasets_processed"]) == 2
        assert res["datasets_processed"][0]["skipped_reason"] == "Dataset already COMPLETE"
        assert res["datasets_processed"][1]["status"] == "COMPLETE"


def test_main_cli_historical_queue_parsing():
    """Verify main.py CLI handles --historical-queue, --leagues, and --seasons arguments."""
    with patch("storage.init_db"), \
         patch("historical_sync.run_historical_queue", return_value={"datasets_processed": [1, 2]}) as mock_queue:
        test_args = [
            "main.py",
            "--historical-queue",
            "--sport", "football",
            "--leagues", "39,140",
            "--seasons", "2023,2024",
            "--with-enrichment",
        ]
        with patch("sys.argv", test_args):
            exit_code = main.main()

            assert exit_code == 0
            mock_queue.assert_called_once_with(
                sport="football",
                leagues=[39, 140],
                seasons=[2023, 2024],
                with_enrichment=True,
                refresh=False,
            )
