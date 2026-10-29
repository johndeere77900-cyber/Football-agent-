"""
Tests for resume, queue execution, dataset verification, and CLI exit codes.
"""

from tests import completion_helpers
from unittest.mock import patch, MagicMock
import pytest
import storage
import config
import main
import historical_sync

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_resume.db")
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    storage.init_db()
    yield

def test_verify_historical_datasets_returns_exit_code_1_on_failed_verification(monkeypatch):
    """--verify-historical-datasets must return exit code 1 if any target dataset fails verification."""
    # Create an incomplete dataset manifest
    storage.mark_historical_dataset_incomplete(39, 2020, fixture_count=50, error_reason="partial")

    monkeypatch.setattr("sys.argv", ["main.py", "--verify-historical-datasets", "--league", "39", "--season", "2020"])
    exit_code = main.main()
    assert exit_code == 1

def test_verify_historical_datasets_returns_exit_code_0_on_successful_verification(monkeypatch):
    """--verify-historical-datasets returns exit code 0 when target dataset is valid and COMPLETE."""
    league_id = 39
    season = 2020
    teams = [f"football_team_39_team_{i}" for i in range(1, 21)]
    for i, t in enumerate(teams, start=1):
        storage.save_team_identity("football", t, "football_data_csv", str(i), f"Team {i}", f"Team {i}", league_id=league_id)

    fixtures = []
    fid = 1
    for i, h in enumerate(teams, start=1):
        for j, a in enumerate(teams, start=1):
            if i == j:
                continue
            fixtures.append({
                "fixture": {"id": fid, "date": "2020-09-01T15:00:00Z", "status": {"short": "FT"}},
                "league": {"id": league_id, "season": season},
                "teams": {"home": {"id": i, "name": f"Team {i}"}, "away": {"id": j, "name": f"Team {j}"}},
                "goals": {"home": 1, "away": 0},
                "canonical_home_id": h,
                "canonical_away_id": a,
                "provider_provenance": {"provider": "football_data_csv"}
            })
            fid += 1

    storage.save_historical_fixtures(fixtures, league_id, season, source="football_data_csv")
    completion_helpers.mark_complete(
        league_id, season, fixture_count=380, source="football_data_csv", expected_pages=1, pages_completed=1, acquisition_complete=True, expected_team_ids=teams
    )

    monkeypatch.setattr("sys.argv", ["main.py", "--verify-historical-datasets", "--league", "39", "--season", "2020"])
    exit_code = main.main()
    assert exit_code == 0

def test_resume_from_incomplete_checkpoint(monkeypatch):
    """Resuming an incomplete dataset continues without losing stored fixtures or double counting."""
    league_id = 39
    season = 2020
    teams = [f"football_team_39_team_{i}" for i in range(1, 21)]
    for i, t in enumerate(teams, start=1):
        storage.save_team_identity("football", t, "football_data_csv", str(i), f"Team {i}", f"Team {i}", league_id=league_id)

    # Initial state: incomplete manifest with 90 stored fixtures
    fixtures_1 = []
    for i, h in enumerate(teams[:10], start=1):
        for j, a in enumerate(teams[:10], start=1):
            if i == j:
                continue
            fid = (i - 1) * 20 + j
            fixtures_1.append({
                "fixture": {"id": fid, "date": f"2020-09-{(i%28)+1:02d}T15:00:00Z", "status": {"short": "FT"}},
                "league": {"id": league_id, "season": season},
                "teams": {"home": {"id": i, "name": f"Team {i}"}, "away": {"id": j, "name": f"Team {j}"}},
                "goals": {"home": 1, "away": 0},
                "canonical_home_id": h,
                "canonical_away_id": a,
                "provider_provenance": {"provider": "football_data_csv"}
            })

    storage.save_historical_fixtures(fixtures_1, league_id, season, source="football_data_csv")
    storage.mark_historical_dataset_incomplete(league_id, season, fixture_count=len(fixtures_1), error_reason="partial")

    # Mock provider returning full 380 fixtures
    fixtures_all = []
    for i, h in enumerate(teams, start=1):
        for j, a in enumerate(teams, start=1):
            if i == j:
                continue
            fid = (i - 1) * 20 + j
            fixtures_all.append({
                "fixture": {"id": fid, "date": f"2020-09-{(i%28)+1:02d}T15:00:00Z", "status": {"short": "FT"}},
                "league": {"id": league_id, "season": season},
                "teams": {"home": {"id": i, "name": f"Team {i}"}, "away": {"id": j, "name": f"Team {j}"}},
                "goals": {"home": 1, "away": 0},
                "canonical_home_id": h,
                "canonical_away_id": a,
                "provider_provenance": {"provider": "football_data_csv"}
            })

    def mock_csv(lid, ssn, *args, **kwargs):
        return fixtures_all

    monkeypatch.setattr("data_resolver._fetch_direct_csv_league_fixtures", mock_csv)
    monkeypatch.setattr("data_resolver._get_authoritative_historical_team_ids", lambda l, s, **kwargs: teams)

    report = historical_sync.sync_historical_fixtures(league_id, season)
    assert report["status"] == "COMPLETE"
    assert report["final_stored_count"] == 380

def test_changed_expected_page_count_fails_closed(monkeypatch):
    """If total expected page count changes mid-sync, acquisition fails closed."""
    league_id = 39
    season = 2020

    page_calls = 0
    def mock_af_page(lid, ssn, page=1, **kwargs):
        nonlocal page_calls
        page_calls += 1
        if page == 1:
            return {"fixtures": [{"fixture": {"id": 1, "date": "2020-09-01T15:00:00Z", "status": {"short": "FT"}}, "league": {"id": lid, "season": ssn}, "teams": {"home": {"id": 1, "name": "A"}, "away": {"id": 2, "name": "B"}}, "goals": {"home": 1, "away": 0}}], "expected_pages": 3, "page": 1, "source": "api_football"}
        else:
            # Expected pages changed from 3 to 5!
            return {"fixtures": [{"fixture": {"id": 2, "date": "2020-09-02T15:00:00Z", "status": {"short": "FT"}}, "league": {"id": lid, "season": ssn}, "teams": {"home": {"id": 1, "name": "A"}, "away": {"id": 2, "name": "B"}}, "goals": {"home": 1, "away": 0}}], "expected_pages": 5, "page": 2, "source": "api_football"}

    monkeypatch.setattr("data_resolver._fetch_direct_csv_league_fixtures", lambda l, s, *args, **kwargs: [])
    monkeypatch.setattr("soccerdata_provider.get_match_history_games", lambda c, s, league_id=None: ("SOURCE_UNAVAILABLE", [], {}))
    monkeypatch.setattr("soccerdata_provider.get_sofascore_historical_games", lambda c, s, league_id=None: ("SOURCE_UNAVAILABLE", [], {}))
    monkeypatch.setattr("api_football.get_league_fixtures_page", mock_af_page)

    report = historical_sync.sync_historical_fixtures(league_id, season)
    assert report["status"] == "INCOMPLETE"
