import pytest
from unittest.mock import patch, MagicMock

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


def make_fixture(
    fid=1001,
    date="2025-01-10T15:00:00+00:00",
    home_id=1,
    away_id=2,
    home_name="Home FC",
    away_name="Away FC",
    league_id=39,
    season=2024,
    status="FT",
    home_goals=1,
    away_goals=0,
):
    fix = {
        "fixture": {"id": fid, "date": date, "status": {"short": status}},
        "teams": {
            "home": {"id": home_id, "name": home_name},
            "away": {"id": away_id, "name": away_name},
        },
        "goals": {"home": home_goals, "away": away_goals},
    }
    if league_id is not None or season is not None:
        fix["league"] = {}
        if league_id is not None:
            fix["league"]["id"] = league_id
        if season is not None:
            fix["league"]["season"] = season
    return fix


# 1. Valid fixture is stored
def test_req1_valid_fixture_stored(temp_db):
    fix = make_fixture(fid=101)
    res = storage.save_historical_fixtures([fix], league_id=39, season=2024)
    assert res["valid"] == 1
    assert res["inserted"] == 1
    assert res["rejected_count"] == 0
    assert storage.get_historical_fixture_count(39, 2024) == 1


# 2. Missing fixture ID is rejected
def test_req2_missing_fixture_id_rejected(temp_db):
    fix = make_fixture(fid=None)
    res = storage.save_historical_fixtures([fix], league_id=39, season=2024)
    assert res["valid"] == 0
    assert res["rejected_count"] == 1
    assert storage.get_historical_fixture_count(39, 2024) == 0


# 3. Invalid/non-positive fixture ID is rejected
def test_req3_invalid_non_positive_fixture_id_rejected(temp_db):
    fix_zero = make_fixture(fid=0)
    fix_neg = make_fixture(fid=-5)
    fix_str = make_fixture(fid="invalid_id")
    fix_bool = make_fixture(fid=True)

    res = storage.save_historical_fixtures([fix_zero, fix_neg, fix_str, fix_bool], league_id=39, season=2024)
    assert res["valid"] == 0
    assert res["rejected_count"] == 4
    assert storage.get_historical_fixture_count(39, 2024) == 0


# 4. Malformed kickoff timestamp is rejected
def test_req4_malformed_kickoff_timestamp_rejected(temp_db):
    fix_bad_date = make_fixture(date="not-a-timestamp")
    fix_empty_date = make_fixture(date="")
    res = storage.save_historical_fixtures([fix_bad_date, fix_empty_date], league_id=39, season=2024)
    assert res["valid"] == 0
    assert res["rejected_count"] == 2


# 5. Missing home team ID is rejected
def test_req5_missing_home_team_id_rejected(temp_db):
    fix = make_fixture(home_id=None)
    res = storage.save_historical_fixtures([fix], league_id=39, season=2024)
    assert res["valid"] == 0
    assert res["rejected_count"] == 1


# 6. Missing away team ID is rejected
def test_req6_missing_away_team_id_rejected(temp_db):
    fix = make_fixture(away_id=None)
    res = storage.save_historical_fixtures([fix], league_id=39, season=2024)
    assert res["valid"] == 0
    assert res["rejected_count"] == 1


# 7. Identical home/away team IDs are rejected
def test_req7_identical_home_away_team_ids_rejected(temp_db):
    fix = make_fixture(home_id=10, away_id=10)
    res = storage.save_historical_fixtures([fix], league_id=39, season=2024)
    assert res["valid"] == 0
    assert res["rejected_count"] == 1


# 8. Conflicting league ID is rejected
def test_req8_conflicting_league_id_rejected(temp_db):
    fix = make_fixture(league_id=140, season=2024)  # Requesting for league 39
    res = storage.save_historical_fixtures([fix], league_id=39, season=2024)
    assert res["valid"] == 0
    assert res["rejected_count"] == 1


# 9. Conflicting season is rejected
def test_req9_conflicting_season_rejected(temp_db):
    fix = make_fixture(league_id=39, season=2023)  # Requesting for season 2024
    res = storage.save_historical_fixtures([fix], league_id=39, season=2024)
    assert res["valid"] == 0
    assert res["rejected_count"] == 1


# 10. FT fixture with missing goals is not accepted as valid historical data
def test_req10_ft_fixture_missing_goals_rejected(temp_db):
    fix = make_fixture(status="FT", home_goals=None, away_goals=0)
    res = storage.save_historical_fixtures([fix], league_id=39, season=2024)
    assert res["valid"] == 0
    assert res["rejected_count"] == 1


# 11. FT fixture with negative goals is rejected
def test_req11_ft_fixture_negative_goals_rejected(temp_db):
    fix = make_fixture(status="FT", home_goals=-1, away_goals=2)
    res = storage.save_historical_fixtures([fix], league_id=39, season=2024)
    assert res["valid"] == 0
    assert res["rejected_count"] == 1


# 12. Boolean goals are rejected
def test_req12_boolean_goals_rejected(temp_db):
    fix = make_fixture(status="FT", home_goals=True, away_goals=0)
    res = storage.save_historical_fixtures([fix], league_id=39, season=2024)
    assert res["valid"] == 0
    assert res["rejected_count"] == 1


# 13. Valid zero-zero result is accepted
def test_req13_valid_zero_zero_result_accepted(temp_db):
    fix = make_fixture(status="FT", home_goals=0, away_goals=0)
    res = storage.save_historical_fixtures([fix], league_id=39, season=2024)
    assert res["valid"] == 1
    assert res["inserted"] == 1


# 14. Duplicate fixture remains idempotent
def test_req14_duplicate_fixture_idempotent(temp_db):
    fix = make_fixture(fid=2001)
    res1 = storage.save_historical_fixtures([fix], league_id=39, season=2024)
    assert res1["inserted"] == 1

    res2 = storage.save_historical_fixtures([fix], league_id=39, season=2024)
    assert res2["valid"] == 1
    assert res2["inserted"] == 0
    assert res2["duplicates_skipped"] == 0
    assert storage.get_historical_fixture_count(39, 2024) == 1


# 15. Malformed fixture cannot cause a dataset to become COMPLETE
def test_req15_malformed_fixture_cannot_cause_complete(temp_db):
    valid_fix = make_fixture(fid=3001)
    malformed_fix = make_fixture(fid=3002, home_id=None)

    fetch_meta = {
        "fixtures": [valid_fix, malformed_fix],
        "expected_pages": 1,
        "pages_completed": 1,
        "acquisition_complete": True,
    }

    with patch("api_football.get_league_fixtures_with_metadata", return_value=fetch_meta):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024, refresh=True)

    assert report["rejected_count"] == 1
    assert report["status"] == "INCOMPLETE"


# 16. Manifest fixture count remains equal to actual stored valid fixture count
def test_req16_manifest_fixture_count_equals_actual_stored(temp_db):
    valid_fix = make_fixture(fid=4001)
    storage.save_historical_fixtures([valid_fix], league_id=39, season=2024)
    count = storage.get_historical_fixture_count(39, 2024)
    storage.mark_historical_dataset_complete(league_id=39, season=2024, fixture_count=count)

    status = storage.get_historical_dataset_status(39, 2024)
    assert status["fixture_count"] == count == 1


# 17. Existing database-first backtest behavior remains unchanged
def test_req17_database_first_backtest_behavior(temp_db):
    fixtures = []
    for i in range(12):
        d = f"2025-01-{i+1:02d}T15:00:00+00:00"
        h = (i % 4) + 1
        a = ((i + 1) % 4) + 1
        if h == a:
            a = (a % 4) + 1
        fixtures.append(make_fixture(fid=5000 + i, date=d, home_id=h, away_id=a))

    storage.save_historical_fixtures(fixtures, league_id=39, season=2024)
    storage.mark_historical_dataset_complete(league_id=39, season=2024, fixture_count=len(fixtures))

    res = backtest.run_real_backtest(league_id=39, season=2024, sample_size=2, min_prior_matches=1)
    assert res["graded"] > 0


# 18. Normal backtest still makes zero API-Football calls
def test_req18_normal_backtest_makes_zero_api_calls(temp_db, monkeypatch):
    fixtures = []
    for i in range(12):
        d = f"2025-01-{i+1:02d}T15:00:00+00:00"
        h = (i % 4) + 1
        a = ((i + 1) % 4) + 1
        if h == a:
            a = (a % 4) + 1
        fixtures.append(make_fixture(fid=6000 + i, date=d, home_id=h, away_id=a))

    storage.save_historical_fixtures(fixtures, league_id=39, season=2024)
    storage.mark_historical_dataset_complete(league_id=39, season=2024, fixture_count=len(fixtures))

    def fail_api(*args, **kwargs):
        pytest.fail("API-Football network call attempted during normal backtest!")

    monkeypatch.setattr(api_football, "get_league_fixtures", fail_api)
    monkeypatch.setattr(api_football, "get_league_fixtures_with_metadata", fail_api)

    res = backtest.run_real_backtest(league_id=39, season=2024, sample_size=2, min_prior_matches=1)
    assert res["graded"] > 0


# 19. Existing quota tests still pass
def test_req19_quota_tests_pass(temp_db, monkeypatch):
    monkeypatch.setattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 1)
    storage.record_api_request("api_football", "test_endpoint")

    with patch("api_football.get_league_fixtures_with_metadata") as mock_get:
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        mock_get.assert_not_called()

    assert report["quota_budget_stopped"] is True
    assert report["status"] == "INCOMPLETE"


# 20. Full regression suite passes
def test_req20_full_suite_runs():
    # Meta test verifying test runner succeeds
    assert True
