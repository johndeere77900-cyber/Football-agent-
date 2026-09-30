import pytest

import backtest
import config


def fixture(
    date,
    home_id,
    away_id,
    home_goals,
    away_goals,
    status="FT",
    fid=None,
):
    if fid is None:
        fid = int(date.split("T")[0].replace("-", "") + f"{home_id}{away_id}")
    return {
        "fixture": {
            "id": fid,
            "date": date,
            "status": {
                "short": status,
            },
        },
        "teams": {
            "home": {
                "id": home_id,
                "name": f"Team {home_id}",
            },
            "away": {
                "id": away_id,
                "name": f"Team {away_id}",
            },
        },
        "goals": {
            "home": home_goals,
            "away": away_goals,
        },
    }


def historical_dataset():
    return [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            2,
            0,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            3,
            4,
            1,
            1,
        ),
        fixture(
            "2025-01-03T15:00:00+00:00",
            1,
            3,
            1,
            0,
        ),
        fixture(
            "2025-01-04T15:00:00+00:00",
            2,
            4,
            0,
            2,
        ),
        fixture(
            "2025-01-05T15:00:00+00:00",
            1,
            4,
            2,
            1,
        ),
        fixture(
            "2025-01-06T15:00:00+00:00",
            2,
            3,
            1,
            2,
        ),
        fixture(
            "2025-01-07T15:00:00+00:00",
            3,
            1,
            0,
            1,
        ),
        fixture(
            "2025-01-08T15:00:00+00:00",
            4,
            2,
            1,
            1,
        ),
        fixture(
            "2025-01-09T15:00:00+00:00",
            1,
            2,
            2,
            1,
        ),
        fixture(
            "2025-01-10T15:00:00+00:00",
            3,
            4,
            2,
            0,
        ),
        fixture(
            "2025-01-11T15:00:00+00:00",
            1,
            3,
            3,
            1,
        ),
        fixture(
            "2025-01-12T15:00:00+00:00",
            2,
            4,
            2,
            2,
        ),
        fixture(
            "2025-01-13T15:00:00+00:00",
            4,
            1,
            0,
            2,
        ),
        fixture(
            "2025-01-14T15:00:00+00:00",
            3,
            2,
            1,
            1,
        ),
        fixture(
            "2025-01-15T15:00:00+00:00",
            1,
            4,
            1,
            0,
        ),
        fixture(
            "2025-01-16T15:00:00+00:00",
            2,
            3,
            0,
            1,
        ),
        fixture(
            "2025-01-17T15:00:00+00:00",
            3,
            1,
            1,
            1,
        ),
        fixture(
            "2025-01-18T15:00:00+00:00",
            4,
            2,
            2,
            1,
        ),
        fixture(
            "2025-01-19T15:00:00+00:00",
            1,
            2,
            1,
            0,
        ),
        fixture(
            "2025-01-20T15:00:00+00:00",
            3,
            4,
            0,
            0,
        ),
        fixture(
            "2025-01-21T15:00:00+00:00",
            2,
            1,
            1,
            2,
        ),
    ]


def test_historical_prediction_uses_shared_prediction_engine(monkeypatch):
    fixtures = historical_dataset()

    called = []

    original = backtest.prediction_engine.predict_historical_fixture

    def wrapped(*args, **kwargs):
        called.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        backtest.prediction_engine,
        "predict_historical_fixture",
        wrapped,
    )

    candidate = fixtures[-1]

    result = backtest._historical_prediction_for_fixture(
        fixtures,
        candidate,
        min_prior_matches=5,
    )

    assert result is not None
    assert len(called) == 1

    call = called[0]

    assert call["league_avg_goals"] > 0
    assert call["home_elo"] is not None
    assert call["away_elo"] is not None


def test_historical_prediction_has_all_core_feature_snapshots():
    fixtures = historical_dataset()

    result = backtest._historical_prediction_for_fixture(
        fixtures,
        fixtures[-1],
        min_prior_matches=5,
    )

    assert result is not None

    assert result["historical_snapshot"] is not None
    assert result["recent_snapshot"] is not None
    assert result["elo_snapshot"] is not None

    assert (
        result["prediction"]["markets"]["match_result"]
    )

    assert (
        result["prediction"]["markets"]["over_under"]
    )

    assert (
        result["prediction"]["markets"]["btts"]
    )

    assert (
        result["prediction"]["markets"]["team_goals"]
    )


def test_prediction_cutoff_excludes_prediction_fixture():
    fixtures = historical_dataset()

    candidate = fixtures[-1]

    cutoff = candidate["fixture"]["date"]

    historical = backtest.historical_features.historical_feature_snapshot(
        fixtures,
        candidate["teams"]["home"]["id"],
        candidate["teams"]["away"]["id"],
        cutoff,
        minimum_matches=5,
    )

    assert historical is not None

    assert cutoff not in historical["home"].get(
        "source_dates",
        []
    )

    recent = backtest.historical_features.fixture_recent_form(
        fixtures,
        candidate["teams"]["home"]["id"],
        candidate["teams"]["away"]["id"],
        cutoff,
        window=config.RECENT_FORM_MATCHES,
        minimum_matches=5,
    )

    assert recent is not None

    assert cutoff not in recent["home"]["source_dates"]
    assert cutoff not in recent["away"]["source_dates"]


def test_prediction_cutoff_excludes_future_results():
    fixtures = historical_dataset()

    candidate = fixtures[-2]

    future_fixture = fixtures[-1]

    cutoff = candidate["fixture"]["date"]

    baseline = backtest._historical_prediction_for_fixture(
        fixtures[:-1],
        candidate,
        min_prior_matches=5,
    )

    with_future = backtest._historical_prediction_for_fixture(
        fixtures,
        candidate,
        min_prior_matches=5,
    )

    assert baseline is not None
    assert with_future is not None

    assert (
        baseline["prediction"]["markets"]
        == with_future["prediction"]["markets"]
    )

    assert future_fixture["fixture"]["date"] > cutoff


def test_historical_elo_is_pre_match():
    fixtures = historical_dataset()

    candidate = fixtures[-1]

    cutoff = candidate["fixture"]["date"]

    snapshot = backtest.historical_elo.fixture_elo_snapshot(
        fixtures,
        candidate["teams"]["home"]["id"],
        candidate["teams"]["away"]["id"],
        cutoff,
    )

    assert snapshot["home_rating"] is not None
    assert snapshot["away_rating"] is not None


def test_actual_result_grading():
    home_win = fixture(
        "2025-01-01T15:00:00+00:00",
        1,
        2,
        2,
        0,
    )

    draw = fixture(
        "2025-01-02T15:00:00+00:00",
        1,
        2,
        1,
        1,
    )

    away_win = fixture(
        "2025-01-03T15:00:00+00:00",
        1,
        2,
        0,
        2,
    )

    assert backtest._actual_match_result(home_win) == "home_win"
    assert backtest._actual_match_result(draw) == "draw"
    assert backtest._actual_match_result(away_win) == "away_win"


def test_actual_result_rejects_missing_goals():
    match = fixture(
        "2025-01-01T15:00:00+00:00",
        1,
        2,
        None,
        0,
    )

    assert backtest._actual_match_result(match) is None


def test_backtest_uses_single_league_fixture_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "predictions.db"))
    backtest.storage.init_db()
    fixtures = historical_dataset()
    backtest.storage.save_historical_fixtures(fixtures, league_id=39, season=2025)

    result = backtest.run_real_backtest(
        league_id=39,
        season=2025,
        sample_size=2,
        min_prior_matches=5,
        sample_seed=42,
    )

    assert result["sample_size"] == 2
    assert result["min_prior_matches"] == 5
    assert result["sample_seed"] == 42


def test_backtest_result_contains_market_probabilities(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "predictions.db"))
    backtest.storage.init_db()
    fixtures = historical_dataset()
    backtest.storage.save_historical_fixtures(fixtures, league_id=39, season=2025)

    result = backtest.run_real_backtest(
        league_id=39,
        season=2025,
        sample_size=2,
        min_prior_matches=5,
        sample_seed=42,
    )

    assert result["graded"] <= result["sample_size"]

    for row in result["log"]:
        assert set(row["probabilities"]) == {
            "home_win",
            "draw",
            "away_win",
        }

        assert sum(
            row["probabilities"].values()
        ) == pytest.approx(1.0)


def test_backtest_rejects_invalid_league_id():
    with pytest.raises(ValueError):
        backtest.run_real_backtest(
            league_id="39",
            season=2025,
        )


def test_backtest_rejects_invalid_season():
    with pytest.raises(ValueError):
        backtest.run_real_backtest(
            league_id=39,
            season="2025",
  )


def test_market_grading_is_returned_by_backtest(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "predictions.db"))
    backtest.storage.init_db()
    fixtures = historical_dataset()
    backtest.storage.save_historical_fixtures(fixtures, league_id=39, season=2025)

    result = backtest.run_real_backtest(
        league_id=39,
        season=2025,
        sample_size=2,
        min_prior_matches=5,
        sample_seed=42,
        enrich_statistics=True,
    )

    assert "market_summary" in result

    for row in result["log"]:
        assert "market_grading" in row
        assert "selected" in row["market_grading"]
        assert "outcomes" in row["market_grading"]

        assert (
            row["market_grading"]["outcomes"]
            ["match_result"]
            ["won"]
            in (True, False)
        )


def test_backtest_grades_goal_markets_beyond_1x2(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "predictions.db"))
    backtest.storage.init_db()
    fixtures = historical_dataset()
    backtest.storage.save_historical_fixtures(fixtures, league_id=39, season=2025)

    result = backtest.run_real_backtest(
        league_id=39,
        season=2025,
        sample_size=2,
        min_prior_matches=5,
        sample_seed=42,
        enrich_statistics=True,
    )

    assert result["market_summary"]

    assert "match_result" in result["market_summary"]
    assert "over_under" in result["market_summary"]
    assert "btts" in result["market_summary"]
    assert "team_goals" in result["market_summary"]


def test_backtest_does_not_fabricate_statistical_markets(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "predictions.db"))
    backtest.storage.init_db()
    fixtures = historical_dataset()
    backtest.storage.save_historical_fixtures(fixtures, league_id=39, season=2025)

    result = backtest.run_real_backtest(
        league_id=39,
        season=2025,
        sample_size=2,
        min_prior_matches=5,
        sample_seed=42,
        enrich_statistics=True,
    )

    assert result[
        "statistical_data_available"
    ]["corners"] == 0

    assert result[
        "statistical_data_available"
    ]["cards"] == 0

    for row in result["log"]:
        statistical_actuals = (
            row["market_grading"]
            ["statistical_actuals"]
        )

        assert statistical_actuals["corners"] == {}
        assert statistical_actuals["cards"] == {}


def test_backtest_can_disable_statistical_enrichment(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "predictions.db"))
    backtest.storage.init_db()
    fixtures = historical_dataset()
    backtest.storage.save_historical_fixtures(fixtures, league_id=39, season=2025)

    result = backtest.run_real_backtest(
        league_id=39,
        season=2025,
        sample_size=2,
        min_prior_matches=5,
        sample_seed=42,
        enrich_statistics=False,
    )

    assert result["statistics_enriched"] is False


def test_backtest_rejects_invalid_enrichment_flag():
    with pytest.raises(ValueError):
        backtest.run_real_backtest(
            league_id=39,
            season=2025,
            enrich_statistics="yes",
    )
