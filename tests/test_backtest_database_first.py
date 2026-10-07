import pytest
from unittest.mock import patch

import backtest
import config
import storage


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "predictions.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    storage.init_db()
    return db_path


def sample_fixture(fid, date, home_id=1, away_id=2, home_goals=1, away_goals=0):
    return {
        "fixture": {
            "id": fid,
            "date": date,
            "status": {"short": "FT"},
        },
        "teams": {
            "home": {"id": home_id, "name": f"Team {home_id}"},
            "away": {"id": away_id, "name": f"Team {away_id}"},
        },
        "goals": {"home": home_goals, "away": away_goals},
    }


def test_backtest_reads_from_storage_and_zero_api_calls(temp_db, monkeypatch):
    fixtures = [
        sample_fixture(6001 + i, f"2025-01-{i+1:02d}T15:00:00+00:00", home_id=(i%2)+1, away_id=(i%2)+3)
        for i in range(10)
    ]
    storage.save_historical_fixtures(fixtures, league_id=39, season=2025)
    storage.mark_historical_dataset_complete(league_id=39, season=2025, fixture_count=len(fixtures))

    def fail_if_called(*args, **kwargs):
        raise AssertionError("api_football should NOT be called by backtest!")

    import data_resolver
    monkeypatch.setattr(data_resolver.api_football, "get_league_fixtures", fail_if_called)
    monkeypatch.setattr(data_resolver.api_football, "get_enriched_fixtures", fail_if_called)

    res = backtest.run_real_backtest(
        league_id=39,
        season=2025,
        sample_size=2,
        min_prior_matches=1,
        sample_seed=42,
    )

    assert res["fixtures_fetched"] == 10
    assert res["graded"] > 0


def test_backtest_fails_if_dataset_missing(temp_db):
    with pytest.raises(RuntimeError) as exc_info:
        backtest.run_real_backtest(
            league_id=39,
            season=2025,
        )

    assert "Historical dataset missing or incomplete" in str(exc_info.value)


def test_backtest_enrichment_uses_storage(temp_db, monkeypatch):
    fixtures = [
        sample_fixture(7001 + i, f"2025-01-{i+1:02d}T15:00:00+00:00", home_id=(i%2)+1, away_id=(i%2)+3)
        for i in range(10)
    ]
    storage.save_historical_fixtures(fixtures, league_id=39, season=2025)
    storage.mark_historical_dataset_complete(league_id=39, season=2025, fixture_count=len(fixtures))

    enrichment_map = {
        7001: {"fixture": {"id": 7001}, "statistics": []},
        7002: {"fixture": {"id": 7002}, "statistics": []},
    }
    storage.save_historical_enrichment(enrichment_map)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("api_football should NOT be called by backtest!")

    import data_resolver
    monkeypatch.setattr(data_resolver.api_football, "get_league_fixtures", fail_if_called)
    monkeypatch.setattr(data_resolver.api_football, "get_enriched_fixtures", fail_if_called)

    res = backtest.run_real_backtest(
        league_id=39,
        season=2025,
        sample_size=2,
        min_prior_matches=1,
        enrich_statistics=True,
    )

    assert res["statistics_enriched"] is True
