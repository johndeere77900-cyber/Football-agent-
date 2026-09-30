import pytest
import storage
import backtest
import historical_basketball_features


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_basketball.db")
    monkeypatch.setattr("config.DB_PATH", db_file)
    monkeypatch.setattr("config.NEON_DATABASE_URL", None)
    monkeypatch.setattr("config.REQUIRE_NEON", False)
    storage.init_db()
    return db_file


def mock_basketball_games():
    games = []
    # Create 12 historical NBA games between team 1 (Lakers) and team 2 (Celtics)
    for i in range(1, 13):
        games.append({
            "id": 1000 + i,
            "date": f"2024-11-{i:02d}T20:00:00+00:00",
            "stage": "Regular Season",
            "status": {"short": "FT"},
            "league": {"id": 12, "season": 2024, "name": "NBA"},
            "teams": {
                "home": {"id": 1 if i % 2 == 1 else 2, "name": "Lakers" if i % 2 == 1 else "Celtics"},
                "away": {"id": 2 if i % 2 == 1 else 1, "name": "Celtics" if i % 2 == 1 else "Lakers"},
            },
            "scores": {
                "home": {"total": 110 + i},
                "away": {"total": 105 + i},
            },
        })
    return games


def test_historical_basketball_features(temp_db):
    games = mock_basketball_games()
    cutoff = "2024-11-10T20:00:00+00:00"

    avgs = historical_basketball_features.team_scoring_averages(games, team_id=1, cutoff=cutoff)
    assert avgs is not None
    assert avgs["matches"] == 9

    has_min = historical_basketball_features.game_has_minimum_history(games, home_team_id=1, away_team_id=2, cutoff=cutoff, minimum_matches=4)
    assert has_min is True


def test_basketball_backtest_database_first(temp_db, monkeypatch):
    games = mock_basketball_games()

    # Save to storage and mark COMPLETE
    storage.save_historical_basketball_games(games, league_id=12, season=2024)
    storage.mark_historical_dataset_complete(12, 2024, fixture_count=len(games), sport="basketball")

    # Prevent API calls
    def throw_api_error(*args, **kwargs):
        raise RuntimeError("API network call was unexpectedly made!")

    monkeypatch.setattr("basketball_api._get", throw_api_error)

    res = backtest.run_basketball_backtest(league_id=12, season=2024, sample_size=5, min_prior_matches=4)

    assert res["sport"] == "basketball"
    assert res["graded"] > 0
    assert "evaluation" in res
    assert "moneyline" in res["evaluation"]
    assert "total_points" in res["evaluation"]

    # Verify backtest run was saved to database
    runs = storage.get_latest_backtest_runs(sport="basketball")
    assert len(runs) >= 1
    assert runs[0][1] == "basketball"
