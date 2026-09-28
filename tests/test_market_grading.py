import market_grading


def fixture(
    home_goals,
    away_goals,
    statistics=None,
):
    data = {
        "fixture": {
            "id": 100,
        },
        "teams": {
            "home": {
                "id": 1,
                "name": "Home",
            },
            "away": {
                "id": 2,
                "name": "Away",
            },
        },
        "goals": {
            "home": home_goals,
            "away": away_goals,
        },
    }

    if statistics is not None:
        data["statistics"] = statistics

    return data


def test_match_result_home_win():
    result = market_grading.grade_match_result(
        2,
        1,
    )

    assert result["outcome"] == "home_win"
    assert result["won"] is True


def test_match_result_draw():
    result = market_grading.grade_match_result(
        1,
        1,
    )

    assert result["outcome"] == "draw"


def test_match_result_away_win():
    result = market_grading.grade_match_result(
        0,
        2,
    )

    assert result["outcome"] == "away_win"


def test_double_chance():
    result = market_grading.grade_double_chance(
        2,
        1,
    )

    assert result["home_or_draw"]["won"] is True
    assert result["away_or_draw"]["won"] is False
    assert result["home_or_away"]["won"] is True


def test_over_under():
    result = market_grading.grade_over_under(
        2,
        1,
    )

    assert result["over_2_5"]["won"] is True
    assert result["under_2_5"]["won"] is False

    assert result["over_3_5"]["won"] is False
    assert result["under_3_5"]["won"] is True


def test_btts():
    result = market_grading.grade_btts(
        1,
        1,
    )

    assert result["yes"]["won"] is True
    assert result["no"]["won"] is False


def test_btts_no():
    result = market_grading.grade_btts(
        2,
        0,
    )

    assert result["yes"]["won"] is False
    assert result["no"]["won"] is True


def test_team_goals():
    result = market_grading.grade_team_goals(
        2,
        1,
    )

    assert result["home_over_1_5"]["won"] is True
    assert result["home_under_1_5"]["won"] is False

    assert result["away_over_1_5"]["won"] is False
    assert result["away_under_1_5"]["won"] is True


def test_invalid_goals_return_none():
    assert (
        market_grading.grade_goal_markets(
            None,
            1,
        )["match_result"]
        is None
    )


def test_missing_statistics_do_not_create_values():
    result = market_grading.grade_statistical_markets(
        fixture(
            1,
            0,
        )
    )

    assert result["corners"] == {}
    assert result["cards"] == {}


def test_corner_statistics_are_graded():
    data = fixture(
        1,
        0,
        statistics=[
            {
                "team": {"id": 1},
                "statistics": [
                    {
                        "type": "Corner Kicks",
                        "value": 6,
                    }
                ],
            },
            {
                "team": {"id": 2},
                "statistics": [
                    {
                        "type": "Corner Kicks",
                        "value": 4,
                    }
                ],
            },
        ],
    )

    result = market_grading.grade_statistical_markets(
        data
    )

    assert (
        result["corners"]["over_9_5"]["won"]
        is True
    )

    assert (
        result["corners"]["under_9_5"]["won"]
        is False
    )


def test_card_statistics_are_graded():
    data = fixture(
        1,
        0,
        statistics=[
            {
                "team": {"id": 1},
                "statistics": [
                    {
                        "type": "Yellow Cards",
                        "value": 2,
                    },
                    {
                        "type": "Red Cards",
                        "value": 0,
                    },
                ],
            },
            {
                "team": {"id": 2},
                "statistics": [
                    {
                        "type": "Yellow Cards",
                        "value": 2,
                    },
                    {
                        "type": "Red Cards",
                        "value": 0,
                    },
                ],
            },
        ],
    )

    result = market_grading.grade_statistical_markets(
        data
    )

    assert (
        result["cards"]["over_3_5"]["won"]
        is True
    )


def test_fixture_market_grading_contains_all_goal_markets():
    result = market_grading.grade_fixture_markets(
        fixture(
            2,
            1,
        )
    )

    goal_markets = result["goal_markets"]

    assert goal_markets["match_result"] is not None
    assert goal_markets["double_chance"] is not None
    assert goal_markets["over_under"] is not None
    assert goal_markets["btts"] is not None
    assert goal_markets["team_goals"] is not None
    assert goal_markets["scoreline"] is not None
