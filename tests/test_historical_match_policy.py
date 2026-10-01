import pytest
import historical_match_policy


def test_match_status_classification():
    ft_fixture = {"fixture": {"status": {"short": "FT"}}}
    aet_fixture = {"fixture": {"status": {"short": "AET"}}}
    pen_fixture = {"fixture": {"status": {"short": "PEN"}}}
    live_fixture = {"fixture": {"status": {"short": "1H"}}}
    pst_fixture = {"fixture": {"status": {"short": "PST"}}}

    assert historical_match_policy.is_finished_match(ft_fixture) is True
    assert historical_match_policy.is_finished_match(aet_fixture) is True
    assert historical_match_policy.is_finished_match(pen_fixture) is True
    assert historical_match_policy.is_finished_match(live_fixture) is False

    assert historical_match_policy.is_live_match(live_fixture) is True
    assert historical_match_policy.is_postponed_or_cancelled(pst_fixture) is True


def test_football_match_goals():
    valid = {"goals": {"home": 2, "away": 1}}
    invalid_bool = {"goals": {"home": True, "away": 1}}
    invalid_negative = {"goals": {"home": -1, "away": 2}}

    assert historical_match_policy.get_football_match_goals(valid) == (2, 1)
    assert historical_match_policy.get_football_match_goals(invalid_bool) is None
    assert historical_match_policy.get_football_match_goals(invalid_negative) is None


def test_get_regulation_goals_and_1x2_outcome():
    aet_fixture = {
        "fixture": {"status": {"short": "AET"}},
        "goals": {"home": 3, "away": 2},
        "score": {"fulltime": {"home": 2, "away": 2}}
    }
    assert historical_match_policy.get_regulation_goals(aet_fixture) == (2, 2)
    assert historical_match_policy.get_1x2_regulation_outcome(aet_fixture) == "draw"
    assert historical_match_policy.get_totals_and_btts_goals(aet_fixture) == (3, 2)


def test_score_breakdown():
    fixture = {
        "score": {
            "fulltime": {"home": 1, "away": 1},
            "extratime": {"home": 2, "away": 1},
            "penalty": {"home": 4, "away": 3},
        }
    }

    breakdown = historical_match_policy.get_score_breakdown(fixture)
    assert breakdown["fulltime"] == (1, 1)
    assert breakdown["extratime"] == (2, 1)
    assert breakdown["penalty"] == (4, 3)


def test_basketball_status_and_points():
    ft_game = {
        "status": {"short": "FT"},
        "scores": {"home": {"total": 105}, "away": {"total": 98}},
    }

    assert historical_match_policy.is_finished_match(ft_game, sport="basketball") is True
    assert historical_match_policy.get_basketball_match_points(ft_game) == (105, 98)
    assert historical_match_policy.get_match_outcome(ft_game, sport="basketball") == "home_win"
