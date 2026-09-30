import pytest

import backtest


def fixture(
    date,
    fixture_id,
    home_id,
    away_id,
    home_goals,
    away_goals,
):
    return {
        "fixture": {
            "id": fixture_id,
            "date": date,
            "status": {
                "short": "FT",
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
            1,
            2,
            2,
            0,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            2,
            3,
            4,
            1,
            1,
        ),
        fixture(
            "2025-01-03T15:00:00+00:00",
            3,
            1,
            3,
            1,
            0,
        ),
        fixture(
            "2025-01-04T15:00:00+00:00",
            4,
            2,
            4,
            0,
            2,
        ),
        fixture(
            "2025-01-05T15:00:00+00:00",
            5,
            1,
            4,
            2,
            1,
        ),
        fixture(
            "2025-01-06T15:00:00+00:00",
            6,
            2,
            3,
            1,
            2,
        ),
        fixture(
            "2025-01-07T15:00:00+00:00",
            7,
            3,
            1,
            0,
            1,
        ),
        fixture(
            "2025-01-08T15:00:00+00:00",
            8,
            4,
            2,
            1,
            1,
        ),
        fixture(
            "2025-01-09T15:00:00+00:00",
            9,
            1,
            2,
            2,
            1,
        ),
        fixture(
            "2025-01-10T15:00:00+00:00",
            10,
            3,
            4,
            2,
            0,
        ),
        fixture(
            "2025-01-11T15:00:00+00:00",
            11,
            1,
            3,
            3,
            1,
        ),
        fixture(
            "2025-01-12T15:00:00+00:00",
            12,
            2,
            4,
            2,
            2,
        ),
        fixture(
            "2025-01-13T15:00:00+00:00",
            13,
            4,
            1,
            0,
            2,
        ),
        fixture(
            "2025-01-14T15:00:00+00:00",
            14,
            3,
            2,
            1,
            1,
        ),
        fixture(
            "2025-01-15T15:00:00+00:00",
            15,
            1,
            4,
            1,
            0,
        ),
        fixture(
            "2025-01-16T15:00:00+00:00",
            16,
            2,
            3,
            0,
            1,
        ),
        fixture(
            "2025-01-17T15:00:00+00:00",
            17,
            3,
            1,
            1,
            1,
        ),
        fixture(
            "2025-01-18T15:00:00+00:00",
            18,
            4,
            2,
            2,
            1,
        ),
        fixture(
            "2025-01-19T15:00:00+00:00",
            19,
            1,
            2,
            1,
            0,
        ),
        fixture(
            "2025-01-20T15:00:00+00:00",
            20,
            3,
            4,
            0,
            0,
        ),
        fixture(
            "2025-01-21T15:00:00+00:00",
            21,
            2,
            1,
            1,
            2,
        ),
    ]


def test_market_selection_grades_multiple_goal_markets():
    match = historical_dataset()[-1]

    prediction_markets = {
        "match_result": {
            "home_win": 0.60,
            "draw": 0.20,
            "away_win": 0.20,
        },
        "double_chance": {
            "home_or_draw": 0.80,
            "away_or_draw": 0.40,
            "home_or_away": 0.80,
        },
        "over_under": {
            "over_1_5": 0.70,
            "under_1_5": 0.30,
            "over_2_5": 0.45,
            "under_2_5": 0.55,
        },
        "btts": {
            "yes": 0.65,
            "no": 0.35,
        },
        "team_goals": {
            "home_over_0_5": 0.90,
            "home_under_0_5": 0.10,
            "away_over_0_5": 0.40,
            "away_under_0_5": 0.60,
        },
        "top_scorelines": [
            {
                "score": "1-0",
                "probability": 0.20,
            }
        ],
    }

    result = backtest._grade_prediction_markets(
        prediction_markets,
        match,
    )

    selected = result["selected"]

    assert selected["match_result"] is not None
    assert selected["double_chance"] is not None
    assert selected["btts"] is not None
    assert selected["over_under"]
    assert selected["team_goals"]
    assert selected["scoreline"] is not None


def test_market_summary_tracks_non_1x2_markets():
    summary = backtest._new_market_summary()

    selected = {
        "match_result": {
            "pick": "home_win",
            "probability": 0.60,
            "won": True,
        },
        "double_chance": {
            "pick": "home_or_draw",
            "probability": 0.80,
            "won": True,
        },
        "btts": {
            "pick": "yes",
            "probability": 0.65,
            "won": False,
        },
        "scoreline": {
            "pick": "1-0",
            "probability": 0.20,
            "won": True,
        },
        "over_under": {
            "2_5": {
                "pick": "under_2_5",
                "probability": 0.55,
                "won": True,
            }
        },
        "team_goals": {
            "home_0_5": {
                "pick": "home_over_0_5",
                "probability": 0.90,
                "won": True,
            }
        },
    }

    backtest._update_market_summary(
        summary,
        selected,
    )

    assert summary["match_result"]["graded"] == 1
    assert summary["match_result"]["correct"] == 1

    assert summary["double_chance"]["graded"] == 1

    assert summary["btts"]["graded"] == 1
    assert summary["btts"]["correct"] == 0

    assert summary["scoreline"]["graded"] == 1

    assert summary["over_under"]["2_5"]["graded"] == 1
    assert summary["team_goals"]["home_0_5"]["graded"] == 1


def test_statistical_enrichment_is_optional(monkeypatch, tmp_path):
    monkeypatch.setattr(backtest.config, "DB_PATH", str(tmp_path / "predictions.db"))
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
    assert result["statistical_data_available"]["corners"] == 0
    assert result["statistical_data_available"]["cards"] == 0


def test_statistical_enrichment_uses_one_batched_call(monkeypatch, tmp_path):
    monkeypatch.setattr(backtest.config, "DB_PATH", str(tmp_path / "predictions.db"))
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

    assert result["statistics_enriched"] is True


def test_missing_statistical_data_is_not_fabricated(monkeypatch, tmp_path):
    monkeypatch.setattr(backtest.config, "DB_PATH", str(tmp_path / "predictions.db"))
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

    assert result["statistical_data_available"] == {
        "corners": 0,
        "cards": 0,
    }

    for row in result["log"]:
        assert (
            row["market_grading"]["statistical_actuals"]
            == {
                "corners": {},
                "cards": {},
            }
        )


def test_invalid_enrichment_flag_is_rejected():
    with pytest.raises(ValueError):
        backtest.run_real_backtest(
            league_id=39,
            season=2025,
            sample_size=2,
            min_prior_matches=5,
            enrich_statistics="yes",
      )
