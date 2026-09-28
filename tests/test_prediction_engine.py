import pytest

import config
import prediction_engine


def team_feature(
    matches,
    goals_for,
    goals_against,
):
    return {
        "matches": matches,
        "goals_for": goals_for,
        "goals_against": goals_against,
    }


def make_snapshots():
    historical = {
        "home": team_feature(10, 15, 10),
        "away": team_feature(10, 10, 15),
    }

    recent = {
        "home": team_feature(8, 14, 8),
        "away": team_feature(8, 8, 14),
    }

    h2h = {
        "meetings": 4,
        "goals_for": 6,
        "goals_against": 4,
    }

    return historical, recent, h2h


def test_blend_signal_uses_configured_weights():
    result = prediction_engine.blend_signal(
        2.0,
        1.0,
        0.5,
    )

    expected = (
        2.0 * config.SEASON_WEIGHT
        + 1.0 * config.RECENT_FORM_WEIGHT
        + 0.5 * config.HEAD_TO_HEAD_WEIGHT
    )

    assert result == pytest.approx(expected)


def test_blend_elo_preserves_probability_sum():
    result = prediction_engine.blend_elo_match_result(
        {
            "home_win": 0.50,
            "draw": 0.25,
            "away_win": 0.25,
        },
        {
            "home": 0.60,
            "draw": 0.20,
            "away": 0.20,
        },
        0.15,
    )

    assert sum(result.values()) == pytest.approx(1.0)
    assert set(result) == {
        "home_win",
        "draw",
        "away_win",
    }


def test_blend_elo_rejects_invalid_weight():
    with pytest.raises(ValueError):
        prediction_engine.blend_elo_match_result(
            {
                "home_win": 0.5,
                "draw": 0.25,
                "away_win": 0.25,
            },
            {
                "home": 0.5,
                "draw": 0.25,
                "away": 0.25,
            },
            1.1,
        )


def test_double_chance_is_derived_from_match_result():
    result = prediction_engine.derive_double_chance(
        {
            "home_win": 0.50,
            "draw": 0.30,
            "away_win": 0.20,
        }
    )

    assert result == pytest.approx(
        {
            "home_or_draw": 0.80,
            "away_or_draw": 0.50,
            "home_or_away": 0.70,
        }
    )


def test_build_historical_features_returns_four_model_ratios():
    historical, recent, h2h = make_snapshots()

    result = prediction_engine.build_historical_features(
        historical,
        recent,
        h2h,
        1.5,
    )

    assert set(result) == {
        "home_attack",
        "home_defence",
        "away_attack",
        "away_defence",
        "league_avg_goals",
        "h2h_available",
    }

    assert result["league_avg_goals"] == 1.5
    assert result["h2h_available"] is True

    for key in (
        "home_attack",
        "home_defence",
        "away_attack",
        "away_defence",
    ):
        assert result[key] > 0


def test_missing_h2h_uses_neutral_h2h_signal():
    historical, recent, _ = make_snapshots()

    result = prediction_engine.build_historical_features(
        historical,
        recent,
        None,
        1.5,
    )

    assert result["h2h_available"] is False
    assert result["home_attack"] > 0
    assert result["away_attack"] > 0


def test_historical_features_reject_non_positive_league_average():
    historical, recent, h2h = make_snapshots()

    with pytest.raises(ValueError):
        prediction_engine.build_historical_features(
            historical,
            recent,
            h2h,
            0,
        )


def test_predict_from_features_preserves_complete_markets():
    historical, recent, h2h = make_snapshots()

    features = prediction_engine.build_historical_features(
        historical,
        recent,
        h2h,
        1.5,
    )

    result = prediction_engine.predict_from_features(
        features
    )

    markets = result["markets"]

    assert "match_result" in markets
    assert "double_chance" in markets
    assert "over_under" in markets
    assert "btts" in markets
    assert "team_goals" in markets
    assert "top_scorelines" in markets


def test_predict_from_features_match_result_sums_to_one():
    historical, recent, h2h = make_snapshots()

    features = prediction_engine.build_historical_features(
        historical,
        recent,
        h2h,
        1.5,
    )

    result = prediction_engine.predict_from_features(
        features
    )

    probabilities = result["markets"]["match_result"]

    assert sum(probabilities.values()) == pytest.approx(
        1.0,
        abs=1e-9,
    )


def test_predict_from_features_preserves_team_goal_distributions():
    historical, recent, h2h = make_snapshots()

    features = prediction_engine.build_historical_features(
        historical,
        recent,
        h2h,
        1.5,
    )

    markets = prediction_engine.predict_from_features(
        features
    )["markets"]

    team_goals = markets["team_goals"]

    assert team_goals["home_over_0_5"] + team_goals["home_under_0_5"] == pytest.approx(
        1.0
    )

    assert team_goals["home_over_1_5"] + team_goals["home_under_1_5"] == pytest.approx(
        1.0
    )

    assert team_goals["home_over_2_5"] + team_goals["home_under_2_5"] == pytest.approx(
        1.0
    )

    assert team_goals["away_over_0_5"] + team_goals["away_under_0_5"] == pytest.approx(
        1.0
    )


def test_elo_blending_changes_only_the_1x2_distribution_and_double_chance():
    historical, recent, h2h = make_snapshots()

    features = prediction_engine.build_historical_features(
        historical,
        recent,
        h2h,
        1.5,
    )

    poisson_only = prediction_engine.predict_from_features(
        features
    )

    elo_result = prediction_engine.predict_from_features(
        features,
        elo_probabilities={
            "home": 0.70,
            "draw": 0.20,
            "away": 0.10,
        },
        elo_weight=0.15,
    )

    assert (
        elo_result["markets"]["match_result"]
        != poisson_only["markets"]["match_result"]
    )

    assert (
        elo_result["markets"]["double_chance"]
        != poisson_only["markets"]["double_chance"]
    )

    assert (
        elo_result["markets"]["over_under"]
        == poisson_only["markets"]["over_under"]
    )

    assert (
        elo_result["markets"]["btts"]
        == poisson_only["markets"]["btts"]
    )

    assert (
        elo_result["markets"]["team_goals"]
        == poisson_only["markets"]["team_goals"]
    )


def test_historical_prediction_uses_supplied_pre_match_elo():
    historical, recent, h2h = make_snapshots()

    result = prediction_engine.predict_historical_fixture(
        historical_snapshot=historical,
        recent_snapshot=recent,
        h2h_snapshot=h2h,
        league_avg_goals=1.5,
        home_elo=1600,
        away_elo=1450,
    )

    assert result["elo_probabilities"] is not None
    assert result["elo_weight"] == config.ELO_BLEND_WEIGHT
    assert sum(
        result["markets"]["match_result"].values()
    ) == pytest.approx(1.0)


def test_historical_prediction_requires_both_elo_ratings():
    historical, recent, h2h = make_snapshots()

    with pytest.raises(ValueError):
        prediction_engine.predict_historical_fixture(
            historical_snapshot=historical,
            recent_snapshot=recent,
            h2h_snapshot=h2h,
            league_avg_goals=1.5,
            home_elo=1600,
        )


def test_invalid_team_feature_is_rejected():
    historical, recent, h2h = make_snapshots()

    historical["home"]["matches"] = 0

    with pytest.raises(ValueError):
        prediction_engine.build_historical_features(
            historical,
            recent,
            h2h,
            1.5,
        )


def test_prediction_rejects_missing_required_features():
    with pytest.raises(ValueError):
        prediction_engine.predict_from_features(
            {
                "home_attack": 1.0,
                "home_defence": 1.0,
            }
        )


def test_prediction_is_deterministic():
    historical, recent, h2h = make_snapshots()

    first = prediction_engine.predict_historical_fixture(
        historical_snapshot=historical,
        recent_snapshot=recent,
        h2h_snapshot=h2h,
        league_avg_goals=1.5,
        home_elo=1550,
        away_elo=1500,
    )

    second = prediction_engine.predict_historical_fixture(
        historical_snapshot=historical,
        recent_snapshot=recent,
        h2h_snapshot=h2h,
        league_avg_goals=1.5,
        home_elo=1550,
        away_elo=1500,
    )

    assert first == second
