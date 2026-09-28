import math

import poisson_model


def test_poisson_pmf_zero_events():
    probability = poisson_model.poisson_pmf(0, 1.5)

    assert probability > 0
    assert probability < 1


def test_poisson_pmf_invalid_lambda():
    assert poisson_model.poisson_pmf(0, -1) == 0.0


def test_expected_goals_home_advantage():
    neutral = poisson_model.expected_goals(
        1.0,
        1.0,
        1.5,
    )

    home = poisson_model.expected_goals(
        1.0,
        1.0,
        1.5,
        is_home=True,
    )

    away = poisson_model.expected_goals(
        1.0,
        1.0,
        1.5,
        is_home=False,
    )

    assert home > neutral
    assert away < neutral


def test_scoreline_grid_probabilities_sum_to_one():
    grid = poisson_model.build_scoreline_grid(
        1.5,
        1.1,
    )

    total = sum(
        sum(row)
        for row in grid
    )

    assert math.isclose(
        total,
        1.0,
        rel_tol=0,
        abs_tol=1e-12,
    )


def test_scoreline_grid_contains_valid_probabilities():
    grid = poisson_model.build_scoreline_grid(
        1.5,
        1.1,
    )

    for row in grid:
        for probability in row:
            assert 0.0 <= probability <= 1.0


def test_negative_expected_goals_are_rejected():
    try:
        poisson_model.build_scoreline_grid(
            -1.0,
            1.0,
        )
    except ValueError:
        return

    raise AssertionError(
        "Negative expected goals should raise ValueError"
    )


def test_market_probabilities_contains_required_markets():
    markets = poisson_model.market_probabilities(
        1.5,
        1.1,
    )

    assert "match_result" in markets
    assert "double_chance" in markets
    assert "over_under" in markets
    assert "btts" in markets
    assert "team_goals" in markets
    assert "top_scorelines" in markets


def test_match_result_probabilities_sum_to_one():
    markets = poisson_model.market_probabilities(
        1.5,
        1.1,
    )

    probabilities = markets["match_result"]

    assert math.isclose(
        sum(probabilities.values()),
        1.0,
        rel_tol=0,
        abs_tol=1e-12,
    )


def test_match_result_probabilities_are_valid():
    markets = poisson_model.market_probabilities(
        1.5,
        1.1,
    )

    for probability in markets["match_result"].values():
        assert 0.0 <= probability <= 1.0


def test_double_chance_probabilities_are_valid():
    markets = poisson_model.market_probabilities(
        1.5,
        1.1,
    )

    double_chance = markets["double_chance"]

    assert 0.0 <= double_chance["home_or_draw"] <= 1.0
    assert 0.0 <= double_chance["away_or_draw"] <= 1.0
    assert 0.0 <= double_chance["home_or_away"] <= 1.0


def test_double_chance_is_derived_from_match_result():
    markets = poisson_model.market_probabilities(
        1.5,
        1.1,
    )

    result = markets["match_result"]
    double_chance = markets["double_chance"]

    assert math.isclose(
        double_chance["home_or_draw"],
        result["home_win"] + result["draw"],
        abs_tol=1e-12,
    )

    assert math.isclose(
        double_chance["away_or_draw"],
        result["away_win"] + result["draw"],
        abs_tol=1e-12,
    )

    assert math.isclose(
        double_chance["home_or_away"],
        result["home_win"] + result["away_win"],
        abs_tol=1e-12,
    )


def test_over_under_markets_are_complementary():
    markets = poisson_model.market_probabilities(
        1.5,
        1.1,
    )

    over_under = markets["over_under"]

    for line in ("1_5", "2_5", "3_5", "4_5", "5_5"):
        over = over_under[f"over_{line}"]
        under = over_under[f"under_{line}"]

        assert math.isclose(
            over + under,
            1.0,
            abs_tol=1e-12,
        )


def test_btts_is_complementary():
    markets = poisson_model.market_probabilities(
        1.5,
        1.1,
    )

    btts = markets["btts"]

    assert math.isclose(
        btts["yes"] + btts["no"],
        1.0,
        abs_tol=1e-12,
    )


def test_team_goal_markets_are_complementary():
    markets = poisson_model.market_probabilities(
        1.5,
        1.1,
    )

    team_goals = markets["team_goals"]

    pairs = [
        ("home_over_0_5", "home_under_0_5"),
        ("home_over_1_5", "home_under_1_5"),
        ("home_over_2_5", "home_under_2_5"),
        ("away_over_0_5", "away_under_0_5"),
        ("away_over_1_5", "away_under_1_5"),
        ("away_over_2_5", "away_under_2_5"),
    ]

    for over_key, under_key in pairs:
        assert math.isclose(
            team_goals[over_key] + team_goals[under_key],
            1.0,
            abs_tol=1e-12,
        )


def test_top_scorelines_are_sorted():
    markets = poisson_model.market_probabilities(
        1.5,
        1.1,
    )

    scorelines = markets["top_scorelines"]

    assert len(scorelines) == 5

    probabilities = [
        item["probability"]
        for item in scorelines
    ]

    assert probabilities == sorted(
        probabilities,
        reverse=True,
    )


def test_cards_market_is_complementary():
    markets = poisson_model.cards_market(
        1.8,
        2.0,
        line=3.5,
    )

    assert math.isclose(
        markets["over"] + markets["under"],
        1.0,
        abs_tol=1e-12,
    )


def test_cards_market_probabilities_are_valid():
    markets = poisson_model.cards_market(
        1.8,
        2.0,
        line=3.5,
    )

    assert 0.0 <= markets["over"] <= 1.0
    assert 0.0 <= markets["under"] <= 1.0
