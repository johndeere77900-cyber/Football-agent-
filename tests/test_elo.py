import elo


def test_expected_score_is_symmetric():
    a = elo.expected_score(1500, 1500)

    assert abs(a - 0.5) < 1e-12


def test_expected_score_favors_higher_rating():
    stronger = elo.expected_score(1600, 1500)
    weaker = elo.expected_score(1400, 1500)

    assert stronger > 0.5
    assert weaker < 0.5


def test_update_ratings_home_win():
    home, away = elo.update_ratings(
        1500,
        1500,
        2,
        0,
    )

    assert home > 1500
    assert away < 1500


def test_update_ratings_draw():
    home, away = elo.update_ratings(
        1500,
        1500,
        1,
        1,
    )

    assert home < 1500
    assert away > 1500


def test_probability_distribution_has_all_outcomes():
    probabilities = elo.win_draw_loss_probabilities(
        1500,
        1500,
    )

    assert set(probabilities.keys()) == {
        "home",
        "draw",
        "away",
    }


def test_probability_distribution_sums_to_one():
    probabilities = elo.win_draw_loss_probabilities(
        1500,
        1500,
    )

    assert abs(sum(probabilities.values()) - 1.0) < 1e-12


def test_probability_distribution_is_valid():
    probabilities = elo.win_draw_loss_probabilities(
        1600,
        1500,
    )

    for probability in probabilities.values():
        assert 0.0 <= probability <= 1.0
