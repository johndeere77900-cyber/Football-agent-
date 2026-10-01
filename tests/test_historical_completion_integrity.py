import pytest
from unittest.mock import patch

import backtest
import config
import historical_sync
import storage
import api_football


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "predictions.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    storage.init_db()
    return db_path


def sample_fixture(fid, date="2025-01-10T15:00:00+00:00", home_id=1, away_id=2):
    return {
        "fixture": {"id": fid, "date": date, "status": {"short": "FT"}},
        "teams": {
            "home": {"id": home_id, "name": f"Team {home_id}"},
            "away": {"id": away_id, "name": f"Team {away_id}"},
        },
        "goals": {"home": 1, "away": 0},
    }


def dataset_with_history():
    fixtures = []
    for i in range(12):
        d = f"2025-01-{i+1:02d}T15:00:00+00:00"
        h = (i % 4) + 1
        a = ((i + 1) % 4) + 1
        if h == a:
            a = (a % 4) + 1
        fixtures.append(sample_fixture(9500 + i, date=d, home_id=h, away_id=a))
    return fixtures


def test_A_all_pagination_pages_succeed_dataset_becomes_complete(temp_db):
    fixtures = [sample_fixture(9001), sample_fixture(9002)]
    def mock_page_fetch(league_id, season, page, max_budget=None):
        if page == 1:
            return {"fixtures": [fixtures[0]], "page": 1, "expected_pages": 2}
        return {"fixtures": [fixtures[1]], "page": 2, "expected_pages": 2}

    with patch("api_football.get_league_fixtures_page", side_effect=mock_page_fetch):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "COMPLETE"
    status = storage.get_historical_dataset_status(39, 2024)
    assert status["status"] == "COMPLETE"
    assert status["fixture_count"] == 2


def test_B_pagination_stops_before_final_page_dataset_remains_incomplete(temp_db):
    fixtures = [sample_fixture(9001)]
    def mock_page_fetch(league_id, season, page, max_budget=None):
        if page == 1:
            return {"fixtures": fixtures, "page": 1, "expected_pages": 3}
        raise RuntimeError("Stopped on page 2")

    with patch("api_football.get_league_fixtures_page", side_effect=mock_page_fetch):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"
    status = storage.get_historical_dataset_status(39, 2024)
    assert status["status"] == "INCOMPLETE"


def test_C_quota_exhaustion_during_later_page_dataset_remains_incomplete(temp_db):
    with patch("api_football.get_league_fixtures_page", side_effect=api_football.APIFootballQuotaExhaustedError("Quota exhausted")):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"
    assert report["quota_budget_stopped"] is True
    status = storage.get_historical_dataset_status(39, 2024)
    assert status["status"] == "INCOMPLETE"


def test_D_malformed_pagination_metadata_dataset_remains_incomplete(temp_db):
    with patch("api_football.get_league_fixtures_page", side_effect=api_football.APIFootballError("Malformed pagination metadata")):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"
    status = storage.get_historical_dataset_status(39, 2024)
    assert status["status"] == "INCOMPLETE"


def test_E_complete_dataset_second_sync_makes_zero_api_calls(temp_db):
    fixtures = [sample_fixture(9001)]
    storage.save_historical_fixtures(fixtures, league_id=39, season=2024)
    storage.mark_historical_dataset_complete(39, 2024, fixture_count=1)

    with patch("api_football.get_league_fixtures_page") as mock_get:
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        mock_get.assert_not_called()

    assert report["status"] == "COMPLETE"
    assert report["api_requests_consumed"] == 0


def test_F_manifest_count_mismatch_with_stored_rows_backtest_refuses(temp_db):
    fixtures = dataset_with_history()
    storage.save_historical_fixtures(fixtures, league_id=39, season=2024)
    # Manifest says 100, actual stored is 12
    storage.mark_historical_dataset_complete(39, 2024, fixture_count=100)

    with pytest.raises(RuntimeError) as exc_info:
        backtest.run_real_backtest(league_id=39, season=2024)

    assert "Historical dataset integrity mismatch" in str(exc_info.value)


def test_G_complete_dataset_matching_count_backtest_runs_zero_api_calls(temp_db, monkeypatch):
    fixtures = dataset_with_history()
    storage.save_historical_fixtures(fixtures, league_id=39, season=2024)
    storage.mark_historical_dataset_complete(39, 2024, fixture_count=len(fixtures))

    def fail_call(*args, **kwargs):
        raise AssertionError("api_football must NOT be called during backtest!")

    monkeypatch.setattr(backtest.api_football, "get_league_fixtures", fail_call)
    monkeypatch.setattr(backtest.api_football, "get_league_fixtures_with_metadata", fail_call)

    res = backtest.run_real_backtest(league_id=39, season=2024, sample_size=2, min_prior_matches=1)
    assert res["graded"] > 0
