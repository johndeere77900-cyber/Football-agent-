import pytest

import config
import main


@pytest.fixture
def football_fixture():
    return {
        "fixture": {
            "id": 123,
            "date": "2026-09-28T15:00:00+00:00",
            "status": {
                "short": "NS",
                "elapsed": None,
            },
        },
        "teams": {
            "home": {
                "id": 1,
                "name": "Home FC",
            },
            "away": {
                "id": 2,
                "name": "Away FC",
            },
        },
        "league": {
            "id": 39,
            "name": "Premier League",
            "season": 2026,
        },
        "goals": {
            "home": None,
            "away": None,
        },
    }


def team_stats():
    return {
        "fixtures": {
            "played": {
                "total": 10,
            }
        },
        "goals": {
            "for": {
                "average": {
                    "total": 1.6,
                }
            },
            "against": {
                "average": {
                    "total": 1.0,
                }
            },
        },
        "cards": {
            "yellow": {
                "0-15": {
                    "total": 4,
                },
                "16-30": {
                    "total": 5,
                },
            }
        },
    }


def recent_matches(team_id):
    other = 99 if team_id == 1 else 98

    return [
        {
            "teams": {
                "home": {
                    "id": team_id,
                },
                "away": {
                    "id": other,
                },
            },
            "goals": {
                "home": 2,
                "away": 1,
            },
        },
        {
            "teams": {
                "home": {
                    "id": other,
                },
                "away": {
                    "id": team_id,
                },
            },
            "goals": {
                "home": 0,
                "away": 1,
            },
        },
    ]


def h2h_matches():
    return [
        {
            "teams": {
                "home": {
                    "id": 1,
                },
                "away": {
                    "id": 2,
                },
            },
            "goals": {
                "home": 2,
                "away": 0,
            },
        },
        {
            "teams": {
                "home": {
                    "id": 2,
                },
                "away": {
                    "id": 1,
                },
            },
            "goals": {
                "home": 1,
                "away": 1,
            },
        },
    ]


def patch_prediction_dependencies(
    monkeypatch,
):
    monkeypatch.setattr(
        main.api_football,
        "get_team_statistics",
        lambda team_id, league_id, season:
        team_stats(),
    )

    monkeypatch.setattr(
        main.api_football,
        "get_recent_form",
        lambda team_id, last:
        recent_matches(team_id),
    )

    monkeypatch.setattr(
        main.api_football,
        "get_head_to_head",
        lambda home_id, away_id, last:
        h2h_matches(),
    )

    monkeypatch.setattr(
        main.storage,
        "get_team_rating",
        lambda team_id: 1500.0,
    )


def test_predict_fixture_uses_shared_prediction_engine(
    monkeypatch,
    football_fixture,
):
    patch_prediction_dependencies(
        monkeypatch
    )

    called = {}

    original = (
        main.prediction_engine
        .predict_from_features
    )

    def wrapped(
        features,
        elo_probabilities=None,
        elo_weight=None,
    ):
        called["features"] = features
        called["elo"] = elo_probabilities
        called["weight"] = elo_weight

        return original(
            features,
            elo_probabilities,
            elo_weight,
        )

    monkeypatch.setattr(
        main.prediction_engine,
        "predict_from_features",
        wrapped,
    )

    result = main.predict_fixture(
        football_fixture,
        1.35,
    )

    assert result["insufficient_data"] is False

    assert (
        called["features"]
        ["league_avg_goals"]
        == 1.35
    )

    assert called["elo"] is not None

    assert (
        called["weight"]
        == config.ELO_BLEND_WEIGHT
    )


def test_production_does_not_use_legacy_backtest_prediction_helpers(
    monkeypatch,
    football_fixture,
):
    patch_prediction_dependencies(
        monkeypatch
    )

    def fail(*args, **kwargs):
        raise AssertionError(
            "Legacy backtest prediction "
            "helper was called."
        )

    monkeypatch.setattr(
        main.backtest,
        "estimate_expected_goals_from_stats",
        fail,
    )

    monkeypatch.setattr(
        main.backtest,
        "estimate_recent_form_goals",
        fail,
    )

    monkeypatch.setattr(
        main.backtest,
        "estimate_head_to_head_goals",
        fail,
    )

    monkeypatch.setattr(
        main.backtest,
        "blend_three",
        fail,
    )

    result = main.predict_fixture(
        football_fixture,
        1.35,
    )

    assert result["insufficient_data"] is False


def test_production_preserves_supported_markets_and_cards(
    monkeypatch,
    football_fixture,
):
    patch_prediction_dependencies(
        monkeypatch
    )

    result = main.predict_fixture(
        football_fixture,
        1.35,
    )

    markets = result["markets"]

    assert "match_result" in markets
    assert "double_chance" in markets
    assert "over_under" in markets
    assert "btts" in markets
    assert "team_goals" in markets
    assert "top_scorelines" in markets
    assert "cards" in markets

    assert (
        "over_4_5"
        in markets["over_under"]
    )

    assert (
        "over_5_5"
        in markets["over_under"]
    )

    assert (
        "home_over_2_5"
        in markets["team_goals"]
    )


def test_missing_one_season_team_is_insufficient(
    monkeypatch,
    football_fixture,
):
    monkeypatch.setattr(
        main.api_football,
        "get_team_statistics",
        lambda team_id, league_id, season:
        team_stats()
        if team_id == 1
        else {},
    )

    result = main.predict_fixture(
        football_fixture,
        1.35,
    )

    assert (
        result["insufficient_data"]
        is True
    )

    assert result["markets"] is None


def test_missing_recent_history_is_insufficient(
    monkeypatch,
    football_fixture,
):
    monkeypatch.setattr(
        main.api_football,
        "get_team_statistics",
        lambda team_id, league_id, season:
        team_stats(),
    )

    monkeypatch.setattr(
        main.api_football,
        "get_recent_form",
        lambda team_id, last: [],
    )

    monkeypatch.setattr(
        main.api_football,
        "get_head_to_head",
        lambda home_id, away_id, last: [],
    )

    monkeypatch.setattr(
        main.storage,
        "get_team_rating",
        lambda team_id: 1500.0,
    )

    result = main.predict_fixture(
        football_fixture,
        1.35,
    )

    assert (
        result["insufficient_data"]
        is True
    )


def test_live_path_remains_separate(
    monkeypatch,
    football_fixture,
):
    live_fixture = dict(
        football_fixture
    )

    live_fixture["fixture"] = dict(
        football_fixture["fixture"]
    )

    live_fixture["fixture"]["status"] = {
        "short": "2H",
        "elapsed": 67,
    }

    live_fixture["goals"] = {
        "home": 1,
        "away": 0,
    }

    patch_prediction_dependencies(
        monkeypatch
    )

    called = {}

    def fake_live(
        home_xg,
        away_xg,
        elapsed,
        status,
        home_goals,
        away_goals,
    ):
        called["args"] = (
            home_xg,
            away_xg,
            elapsed,
            status,
            home_goals,
            away_goals,
        )

        return {
            "is_live": True,
            "minutes_elapsed": elapsed,
            "minutes_remaining_estimate": 23,
            "current_score": {
                "home": home_goals,
                "away": away_goals,
            },
            "expected_additional_goals": {
                "home": 0.5,
                "away": 0.3,
            },
            "match_result": {
                "home_win": 0.60,
                "draw": 0.25,
                "away_win": 0.15,
            },
            "over_under": {
                "over_2_5": 0.55,
                "under_2_5": 0.45,
            },
            "btts": {
                "yes": 0.50,
                "no": 0.50,
            },
            "top_scorelines": [
                {
                    "score": "1-0",
                    "probability": 0.20,
                }
            ],
        }

    monkeypatch.setattr(
        main.live_model,
        "live_market_probabilities",
        fake_live,
    )

    result = main.predict_fixture(
        live_fixture,
        1.35,
    )

    assert result["is_live"] is True
    assert "args" in called
    assert "cards" not in result["markets"]


def test_odds_are_only_fetched_when_requested(
    monkeypatch,
    football_fixture,
):
    patch_prediction_dependencies(
        monkeypatch
    )

    calls = []

    monkeypatch.setattr(
        main.odds_api,
        "get_odds_for_match",
        lambda *args:
        calls.append(args)
        or {
            "bookmakers_counted": 1,
        },
    )

    without_odds = main.predict_fixture(
        football_fixture,
        1.35,
        fetch_odds=False,
    )

    assert (
        without_odds["odds_comparison"]
        is None
    )

    assert calls == []

    with_odds = main.predict_fixture(
        football_fixture,
        1.35,
        fetch_odds=True,
    )

    assert (
        with_odds["odds_comparison"]
        == {
            "bookmakers_counted": 1,
        }
    )

    assert len(calls) == 1


def test_safest_candidates_include_all_generated_goal_markets():
    markets = {
        "match_result": {
            "home_win": 0.50,
            "draw": 0.20,
            "away_win": 0.30,
        },
        "double_chance": {
            "home_or_draw": 0.70,
            "away_or_draw": 0.50,
            "home_or_away": 0.80,
        },
        "over_under": {
            "over_1_5": 0.80,
            "under_1_5": 0.20,
            "over_2_5": 0.60,
            "under_2_5": 0.40,
            "over_3_5": 0.40,
            "under_3_5": 0.60,
            "over_4_5": 0.20,
            "under_4_5": 0.80,
            "over_5_5": 0.10,
            "under_5_5": 0.90,
        },
        "btts": {
            "yes": 0.55,
            "no": 0.45,
        },
        "team_goals": {
            "home_over_0_5": 0.80,
            "home_under_0_5": 0.20,
            "home_over_1_5": 0.60,
            "home_under_1_5": 0.40,
            "home_over_2_5": 0.30,
            "home_under_2_5": 0.70,
            "away_over_0_5": 0.70,
            "away_under_0_5": 0.30,
            "away_over_1_5": 0.40,
            "away_under_1_5": 0.60,
            "away_over_2_5": 0.20,
            "away_under_2_5": 0.80,
        },
        "cards": {
            "over_line": 3.5,
            "over": 0.50,
            "under": 0.50,
        },
    }

    candidates = (
        main.build_football_safest_candidates(
            markets
        )
    )

    labels = {
        label
        for label, _ in candidates
    }

    assert "Over 4 5" in labels
    assert "Under 5 5" in labels
    assert "Home Over 2 5" in labels
    assert "Away Under 2 5" in labels
    assert "Over 3.5 Cards" in labels
