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

    import data_resolver
    monkeypatch.setattr(data_resolver.api_football, "get_league_fixtures", fail_call)
    monkeypatch.setattr(data_resolver.api_football, "get_league_fixtures_with_metadata", fail_call)

    res = backtest.run_real_backtest(league_id=39, season=2024, sample_size=2, min_prior_matches=1)
    assert res["graded"] > 0


def test_historical_storage_status_and_score_validation(temp_db):
    """
    Verify that save_historical_fixtures(require_completed=True) strictly validates fixture completion
    and score presence, rejecting NS, PST, missing status, or missing goals.
    """
    league_id = 39
    season = 2024

    # 1. NS -> rejected
    f_ns = {
        "fixture": {"id": 10001, "date": "2024-08-10T15:00:00+00:00", "status": {"short": "NS"}},
        "teams": {"home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
        "goals": {"home": None, "away": None},
    }
    res_ns = storage.save_historical_fixtures([f_ns], league_id=league_id, season=season, require_completed=True)
    assert res_ns["inserted"] == 0
    assert res_ns["rejected_count"] == 1

    # 2. PST -> rejected
    f_pst = {
        "fixture": {"id": 10002, "date": "2024-08-10T15:00:00+00:00", "status": {"short": "PST"}},
        "teams": {"home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
        "goals": {"home": None, "away": None},
    }
    res_pst = storage.save_historical_fixtures([f_pst], league_id=league_id, season=season, require_completed=True)
    assert res_pst["inserted"] == 0
    assert res_pst["rejected_count"] == 1

    # 3. Missing status (status=None) -> rejected
    f_no_status = {
        "fixture": {"id": 10003, "date": "2024-08-10T15:00:00+00:00", "status": None},
        "teams": {"home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
        "goals": {"home": 1, "away": 0},
    }
    res_no_status = storage.save_historical_fixtures([f_no_status], league_id=league_id, season=season, require_completed=True)
    assert res_no_status["inserted"] == 0
    assert res_no_status["rejected_count"] == 1

    # 4. FT missing home goals -> rejected
    f_missing_home_goals = {
        "fixture": {"id": 10004, "date": "2024-08-10T15:00:00+00:00", "status": {"short": "FT"}},
        "teams": {"home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
        "goals": {"home": None, "away": 0},
    }
    res_no_hgoals = storage.save_historical_fixtures([f_missing_home_goals], league_id=league_id, season=season, require_completed=True)
    assert res_no_hgoals["inserted"] == 0
    assert res_no_hgoals["rejected_count"] == 1

    # 5. FT missing away goals -> rejected
    f_missing_away_goals = {
        "fixture": {"id": 10005, "date": "2024-08-10T15:00:00+00:00", "status": {"short": "FT"}},
        "teams": {"home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
        "goals": {"home": 2, "away": None},
    }
    res_no_agoals = storage.save_historical_fixtures([f_missing_away_goals], league_id=league_id, season=season, require_completed=True)
    assert res_no_agoals["inserted"] == 0
    assert res_no_agoals["rejected_count"] == 1

    # 6. FT + valid goals -> ACCEPTED
    f_ft = {
        "fixture": {"id": 10006, "date": "2024-08-10T15:00:00+00:00", "status": {"short": "FT"}},
        "teams": {"home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
        "goals": {"home": 2, "away": 1},
    }
    res_ft = storage.save_historical_fixtures([f_ft], league_id=league_id, season=season, require_completed=True)
    assert res_ft["inserted"] == 1

    # 7. AET + valid goals -> ACCEPTED
    f_aet = {
        "fixture": {"id": 10007, "date": "2024-08-11T15:00:00+00:00", "status": {"short": "AET"}},
        "teams": {"home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
        "goals": {"home": 3, "away": 2},
    }
    res_aet = storage.save_historical_fixtures([f_aet], league_id=league_id, season=season, require_completed=True)
    assert res_aet["inserted"] == 1

    # 8. PEN + valid goals -> ACCEPTED
    f_pen = {
        "fixture": {"id": 10008, "date": "2024-08-12T15:00:00+00:00", "status": {"short": "PEN"}},
        "teams": {"home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
        "goals": {"home": 1, "away": 1},
    }
    res_pen = storage.save_historical_fixtures([f_pen], league_id=league_id, season=season, require_completed=True)
    assert res_pen["inserted"] == 1


def test_data_resolver_recent_and_h2h_status_filtering(temp_db):
    """
    Verify DataResolver's recent matches and H2H methods reject matches with missing or uncompleted status
    both from returning and from saving to permanent historical storage.
    """
    from data_resolver import DataResolver
    import team_identity

    team_identity.bootstrap_historical_team_identity("Arsenal", "api_football", 1)
    team_identity.bootstrap_historical_team_identity("Chelsea", "api_football", 2)
    team_identity.bootstrap_historical_team_identity("Liverpool", "api_football", 3)
    team_identity.bootstrap_historical_team_identity("Spurs", "api_football", 4)

    resolver = DataResolver()

    # Raw API-Football fixtures
    raw_af_recent = [
        # Missing status -> MUST BE REJECTED
        {
            "fixture": {"id": 20001, "date": "2024-08-01T15:00:00+00:00", "status": None},
            "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 2, "name": "Chelsea"}},
            "goals": {"home": 1, "away": 0},
            "league": {"id": 39, "season": 2024},
        },
        # NS status -> MUST BE REJECTED
        {
            "fixture": {"id": 20002, "date": "2024-08-02T15:00:00+00:00", "status": {"short": "NS"}},
            "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 3, "name": "Liverpool"}},
            "goals": {"home": None, "away": None},
            "league": {"id": 39, "season": 2024},
        },
        # FT status -> MUST BE ACCEPTED
        {
            "fixture": {"id": 20003, "date": "2024-08-03T15:00:00+00:00", "status": {"short": "FT"}},
            "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 4, "name": "Spurs"}},
            "goals": {"home": 2, "away": 1},
            "league": {"id": 39, "season": 2024},
        },
    ]

    with patch("api_football.get_recent_form", return_value=raw_af_recent):
        recent_matches = resolver.get_team_recent_matches(team_id=1, team_name="Arsenal", league_id=39, season=2024, last=5)

    # Only 1 valid completed match should be returned
    assert len(recent_matches) == 1
    assert recent_matches[0]["fixture"]["id"] == 20003

    # Permanent storage must contain ONLY the valid completed match (id 20003)
    stored = storage.get_historical_fixtures(39, 2024)
    stored_fids = [f["fixture"]["id"] for f in stored]
    assert stored_fids == [20003]

    # Now test H2H status filtering
    raw_af_h2h = [
        # Missing status -> MUST BE REJECTED
        {
            "fixture": {"id": 30001, "date": "2024-08-01T15:00:00+00:00", "status": {}},
            "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 2, "name": "Chelsea"}},
            "goals": {"home": 1, "away": 0},
            "league": {"id": 39, "season": 2024},
        },
        # PST status -> MUST BE REJECTED
        {
            "fixture": {"id": 30002, "date": "2024-08-02T15:00:00+00:00", "status": {"short": "PST"}},
            "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 2, "name": "Chelsea"}},
            "goals": {"home": None, "away": None},
            "league": {"id": 39, "season": 2024},
        },
        # PEN status -> MUST BE ACCEPTED
        {
            "fixture": {"id": 30003, "date": "2024-08-03T15:00:00+00:00", "status": {"short": "PEN"}},
            "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 2, "name": "Chelsea"}},
            "goals": {"home": 1, "away": 1},
            "league": {"id": 39, "season": 2024},
        },
    ]

    with patch("api_football.get_head_to_head", return_value=raw_af_h2h):
        h2h_matches = resolver.get_head_to_head(home_id=1, away_id=2, league_id=39, last=5)

    assert len(h2h_matches) == 1
    assert h2h_matches[0]["fixture"]["id"] == 30003

    stored_after_h2h = storage.get_historical_fixtures(39, 2024)
    stored_h2h_fids = [f["fixture"]["id"] for f in stored_after_h2h]
    assert 30003 in stored_h2h_fids
    assert 30001 not in stored_h2h_fids
    assert 30002 not in stored_h2h_fids
