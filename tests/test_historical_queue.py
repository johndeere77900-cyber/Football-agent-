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

    # First football league (39) succeeds
    # Second football league (140) hits quota exhaustion
    def mock_football_sync(league_id, season, with_enrichment=False, refresh=False):
        if league_id == 39:
            return {
                "sport": "football",
                "league_id": 39,
                "season": 2024,
                "status": "COMPLETE",
                "api_requests_consumed": 50,
                "quota_budget_stopped": False,
            }
        elif league_id == 140:
            return {
                "sport": "football",
                "league_id": 140,
                "season": 2024,
                "status": "INCOMPLETE",
                "api_requests_consumed": 0,
                "quota_budget_stopped": True,
            }
        raise RuntimeError("sync_historical_fixtures should not be called for football league 135 after football quota exhaustion")

    def mock_basketball_sync(league_id, season, refresh=False):
        return {
            "sport": "basketball",
            "league_id": league_id,
            "season": season,
            "status": "COMPLETE",
            "api_requests_consumed": 2,
            "quota_budget_stopped": False,
        }

    with patch("historical_sync.sync_historical_fixtures", side_effect=mock_football_sync) as mock_fb, \
         patch("historical_sync.sync_historical_basketball_games", side_effect=mock_basketball_sync) as mock_bb:

        summary = historical_sync.run_historical_queue(season=2024)

    # Queue should call sync_historical_fixtures for football 39 & 140, and call sync_historical_basketball_games for basketball 12
    assert mock_fb.call_count == 2
    assert mock_bb.call_count == 1
    assert summary["processed_count"] == 4

    fb_reports = [r for r in summary["reports"] if r.get("sport") == "football" or "league_id" in r and r.get("sport") != "basketball"]
    bb_report = [r for r in summary["reports"] if r.get("sport") == "basketball"][0]

    assert bb_report["status"] == "COMPLETE"
    assert bb_report["api_requests_consumed"] == 2

    # Football 135 should be skipped cleanly with quota_budget_stopped=True
    fb_135 = [r for r in fb_reports if r.get("league_id") == 135][0]
    assert fb_135["status"] == "INCOMPLETE"
    assert fb_135["quota_budget_stopped"] is True
    assert fb_135["api_requests_consumed"] == 0


def test_queue_sport_budget_independence(temp_db, monkeypatch):
    """Verify that football quota exhaustion does not starve basketball acquisition, and vice versa."""
    monkeypatch.setattr(config, "ALLOWED_LEAGUE_IDS", [39, 140])
    monkeypatch.setattr(config, "ALLOWED_BASKETBALL_LEAGUE_IDS", [12])

    def mock_football_sync(league_id, season, with_enrichment=False, refresh=False):
        return {
            "sport": "football",
            "league_id": league_id,
            "season": season,
            "status": "INCOMPLETE",
            "api_requests_consumed": 0,
            "quota_budget_stopped": True,
        }

    def mock_basketball_sync(league_id, season, refresh=False):
        return {
            "sport": "basketball",
            "league_id": league_id,
            "season": season,
            "status": "COMPLETE",
            "api_requests_consumed": 15,
            "quota_budget_stopped": False,
        }

    with patch("historical_sync.sync_historical_fixtures", side_effect=mock_football_sync) as mock_fb, \
         patch("historical_sync.sync_historical_basketball_games", side_effect=mock_basketball_sync) as mock_bb:

        summary = historical_sync.run_historical_queue(seasons=[2021, 2022])

    # Football 39 (2021) returns quota_budget_stopped=True on first call
    # Football 140 (2021), 39 (2022), 140 (2022) should be skipped without additional sync_historical_fixtures calls
    assert mock_fb.call_count == 1

    # Basketball 12 for 2021 and 2022 should BOTH run and succeed because basketball budget is independent!
    assert mock_bb.call_count == 2


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


def test_queue_no_season_args_defaults_to_target_seasons(temp_db, monkeypatch):
    """Prove that run_historical_queue() with no season arguments defaults to config.TARGET_SEASONS."""
    processed_seasons = set()

    def mock_football_sync(league_id, season, with_enrichment=False, refresh=False):
        processed_seasons.add(season)
        return {
            "league_id": league_id,
            "season": season,
            "status": "COMPLETE",
            "api_requests_consumed": 0,
            "quota_budget_stopped": False,
        }

    def mock_bball_sync(league_id, season, refresh=False):
        processed_seasons.add(season)
        return {
            "sport": "basketball",
            "league_id": league_id,
            "season": season,
            "status": "COMPLETE",
            "api_requests_consumed": 0,
            "quota_budget_stopped": False,
        }

    with patch("historical_sync.sync_historical_fixtures", side_effect=mock_football_sync), \
         patch("historical_sync.sync_historical_basketball_games", side_effect=mock_bball_sync):

        summary = historical_sync.run_historical_queue()

    assert summary["target_seasons"] == config.TARGET_SEASONS
    assert processed_seasons == set(config.TARGET_SEASONS)


def test_queue_seasons_override_default(temp_db, monkeypatch):
    """Prove that passing explicit seasons=[...] overrides the default TARGET_SEASONS."""
    processed_seasons = set()

    def mock_football_sync(league_id, season, with_enrichment=False, refresh=False):
        processed_seasons.add(season)
        return {
            "league_id": league_id,
            "season": season,
            "status": "COMPLETE",
            "api_requests_consumed": 0,
            "quota_budget_stopped": False,
        }

    def mock_bball_sync(league_id, season, refresh=False):
        processed_seasons.add(season)
        return {
            "sport": "basketball",
            "league_id": league_id,
            "season": season,
            "status": "COMPLETE",
            "api_requests_consumed": 0,
            "quota_budget_stopped": False,
        }

    with patch("historical_sync.sync_historical_fixtures", side_effect=mock_football_sync), \
         patch("historical_sync.sync_historical_basketball_games", side_effect=mock_bball_sync):

        summary = historical_sync.run_historical_queue(seasons=[2021, 2022])

    assert summary["target_seasons"] == [2021, 2022]
    assert processed_seasons == {2021, 2022}


def test_queue_single_season_override_default(temp_db, monkeypatch):
    """Prove that passing explicit season=<year> overrides the default TARGET_SEASONS."""
    processed_seasons = set()

    def mock_football_sync(league_id, season, with_enrichment=False, refresh=False):
        processed_seasons.add(season)
        return {
            "league_id": league_id,
            "season": season,
            "status": "COMPLETE",
            "api_requests_consumed": 0,
            "quota_budget_stopped": False,
        }

    def mock_bball_sync(league_id, season, refresh=False):
        processed_seasons.add(season)
        return {
            "sport": "basketball",
            "league_id": league_id,
            "season": season,
            "status": "COMPLETE",
            "api_requests_consumed": 0,
            "quota_budget_stopped": False,
        }

    with patch("historical_sync.sync_historical_fixtures", side_effect=mock_football_sync), \
         patch("historical_sync.sync_historical_basketball_games", side_effect=mock_bball_sync):

        summary = historical_sync.run_historical_queue(season=2023)

    assert summary["target_seasons"] == [2023]
    assert processed_seasons == {2023}


def test_main_cli_historical_queue_dispatches_correct_seasons(temp_db, monkeypatch):
    """Prove main.py CLI handles --historical-queue with no season args, --seasons, and --season."""
    import sys
    import main

    captured_calls = []

    def mock_run_queue(seasons=None, season=None, with_enrichment=False, refresh=False):
        captured_calls.append({"seasons": seasons, "season": season})
        return {"target_seasons": seasons or ([season] if season else config.TARGET_SEASONS), "processed_count": 0, "reports": []}

    monkeypatch.setattr(historical_sync, "run_historical_queue", mock_run_queue)

    # 1. No season args
    monkeypatch.setattr(sys, "argv", ["main.py", "--historical-queue"])
    main.main()
    assert captured_calls[-1] == {"seasons": None, "season": None}

    # 2. --seasons 2022 2023 2024
    monkeypatch.setattr(sys, "argv", ["main.py", "--historical-queue", "--seasons", "2022", "2023", "2024"])
    main.main()
    assert captured_calls[-1] == {"seasons": [2022, 2023, 2024], "season": None}

    # 3. --season 2023
    monkeypatch.setattr(sys, "argv", ["main.py", "--historical-queue", "--season", "2023"])
    main.main()
    assert captured_calls[-1] == {"seasons": None, "season": 2023}
