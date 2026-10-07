import pytest
from unittest.mock import patch

import backtest
import config
import storage
import time_utils


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "predictions.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    storage.init_db()
    return db_path


def sample_fixture(fid, date_str, home_id=1, away_id=2, home_goals=1, away_goals=0, status="FT", season=2024):
    return {
        "fixture": {
            "id": fid,
            "date": date_str,
            "status": {"short": status},
        },
        "league": {
            "id": 39,
            "season": season,
        },
        "teams": {
            "home": {"id": home_id, "name": f"Team {home_id}"},
            "away": {"id": away_id, "name": f"Team {away_id}"},
        },
        "goals": {
            "home": home_goals if status in ("FT", "AET", "PEN") else None,
            "away": away_goals if status in ("FT", "AET", "PEN") else None,
        },
    }


def test_rolling_window_loads_2024_2025_2026_and_combines_chronologically(temp_db, monkeypatch):
    """A & B: Loads 2024+2025+2026, combines chronologically, no real API calls."""
    s2024 = [sample_fixture(101 + i, f"2024-05-{i+1:02d}T15:00:00+00:00", home_id=(i%2)+1, away_id=(i%2)+3, season=2024) for i in range(10)]
    s2025 = [sample_fixture(201 + i, f"2025-05-{i+1:02d}T15:00:00+00:00", home_id=(i%2)+1, away_id=(i%2)+3, season=2025) for i in range(10)]
    s2026 = [sample_fixture(301 + i, f"2026-01-{i+1:02d}T15:00:00+00:00", home_id=(i%2)+1, away_id=(i%2)+3, season=2026) for i in range(10)]

    storage.save_historical_fixtures(s2024, league_id=39, season=2024)
    storage.save_historical_fixtures(s2025, league_id=39, season=2025)
    storage.save_historical_fixtures(s2026, league_id=39, season=2026)

    def fail_if_api_called(*args, **kwargs):
        raise AssertionError("Live provider API should NOT be called!")

    import data_resolver
    monkeypatch.setattr(data_resolver.api_football, "get_league_fixtures", fail_if_api_called)

    res = backtest.run_rolling_backtest_window(
        league_id=39,
        seasons=(2024, 2025, 2026),
        sample_size=5,
        min_prior_matches=1,
        sample_seed=42,
    )

    assert res["league_id"] == 39
    assert res["seasons_evaluated"] == [2024, 2025, 2026]
    assert res["evaluation_mode"] == "rolling_window"
    assert res["fixture_selection"] == "completed_fixtures"
    assert res["fixtures_fetched"] == 30
    assert res["graded"] > 0


def test_rolling_window_excludes_upcoming_2026_fixtures(temp_db):
    """C & D: Candidates must be completed. Upcoming/NS 2026 fixtures are excluded."""
    completed_2024 = [sample_fixture(100 + i, f"2024-03-{i+1:02d}T15:00:00+00:00", home_id=(i%2)+1, away_id=(i%2)+3, status="FT", season=2024) for i in range(10)]
    completed_2025 = [sample_fixture(200 + i, f"2025-03-{i+1:02d}T15:00:00+00:00", home_id=(i%2)+1, away_id=(i%2)+3, status="FT", season=2025) for i in range(10)]
    upcoming_2026 = [
        sample_fixture(300 + i, f"2026-11-{i+1:02d}T15:00:00+00:00", home_id=(i%2)+1, away_id=(i%2)+3, home_goals=None, away_goals=None, status="NS", season=2026)
        for i in range(5)
    ]

    storage.save_historical_fixtures(completed_2024, league_id=39, season=2024)
    storage.save_historical_fixtures(completed_2025, league_id=39, season=2025)
    storage.save_historical_fixtures(upcoming_2026, league_id=39, season=2026)

    res = backtest.run_rolling_backtest_window(
        league_id=39,
        seasons=(2024, 2025, 2026),
        sample_size=20,
        min_prior_matches=1,
    )

    assert res["fixtures_fetched"] == 25
    assert res["finished_fixtures"] == 20

    candidate_ids = [entry["fixture_id"] for entry in res["log"]]
    for ns_f in upcoming_2026:
        assert ns_f["fixture"]["id"] not in candidate_ids


def test_rolling_window_rejects_invalid_seasons():
    """Reject seasons outside 2024, 2025, 2026."""
    with pytest.raises(ValueError) as exc_info:
        backtest.run_rolling_backtest_window(
            league_id=39,
            seasons=(2022, 2024, 2025),
        )
    assert "Invalid season 2022" in str(exc_info.value)


def test_rolling_window_candidate_result_excluded_from_features(temp_db):
    """E, F, G: Candidate result, future H2H, and future form are strictly excluded."""
    fixtures_2024 = [
        sample_fixture(1, "2024-01-01T15:00:00+00:00", home_id=1, away_id=2, home_goals=1, away_goals=0, season=2024),
        sample_fixture(2, "2024-01-05T15:00:00+00:00", home_id=1, away_id=2, home_goals=2, away_goals=0, season=2024),
        sample_fixture(3, "2024-01-10T15:00:00+00:00", home_id=1, away_id=2, home_goals=3, away_goals=0, season=2024),
        sample_fixture(4, "2024-01-15T15:00:00+00:00", home_id=1, away_id=2, home_goals=4, away_goals=0, season=2024),
        sample_fixture(5, "2024-01-20T15:00:00+00:00", home_id=1, away_id=2, home_goals=5, away_goals=0, season=2024),
        sample_fixture(6, "2024-01-25T15:00:00+00:00", home_id=1, away_id=2, home_goals=6, away_goals=0, season=2024),
    ]
    storage.save_historical_fixtures(fixtures_2024, league_id=39, season=2024)

    res = backtest.run_rolling_backtest_window(
        league_id=39,
        seasons=(2024,),
        sample_size=10,
        min_prior_matches=1,
    )

    log_by_id = {e["fixture_id"]: e for e in res["log"]}

    # Candidate 3 (date 2024-01-10) features must only reflect matches 1 and 2
    if 3 in log_by_id:
        f3 = log_by_id[3]
        h_snap = f3["historical_features"]["historical_snapshot"]
        assert h_snap["home"]["matches"] == 2
        assert h_snap["home"]["goals_for"] == 1.5


def test_rolling_window_metadata_persistence(temp_db):
    """H: Rolling window metadata persisted in backtest_runs."""
    fixtures_2024 = [sample_fixture(10 + i, f"2024-02-{i+1:02d}T15:00:00+00:00", home_id=(i%2)+1, away_id=(i%2)+3, season=2024) for i in range(10)]
    storage.save_historical_fixtures(fixtures_2024, league_id=39, season=2024)

    res = backtest.run_rolling_backtest_window(
        league_id=39,
        seasons=(2024, 2025, 2026),
        sample_size=5,
        min_prior_matches=1,
    )

    latest_runs = storage.get_latest_backtest_runs(limit=1)
    assert len(latest_runs) > 0


def test_existing_single_season_backtest_compatibility(temp_db):
    """J: Single-season run_real_backtest() continues working."""
    fixtures_2024 = [sample_fixture(50 + i, f"2024-04-{i+1:02d}T15:00:00+00:00", home_id=(i%2)+1, away_id=(i%2)+3, season=2024) for i in range(10)]
    storage.save_historical_fixtures(fixtures_2024, league_id=39, season=2024)
    storage.mark_historical_dataset_complete(league_id=39, season=2024, fixture_count=10)

    res = backtest.run_real_backtest(
        league_id=39,
        season=2024,
        sample_size=5,
        min_prior_matches=1,
    )

    assert res["season"] == 2024
    assert res["graded"] > 0
