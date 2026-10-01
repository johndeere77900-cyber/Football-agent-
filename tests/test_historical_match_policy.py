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


def test_market_grading_consumer_semantics():
    import market_grading

    # A. Regular FT fixture (2-1)
    ft_fix = {
        "fixture": {"status": {"short": "FT"}},
        "goals": {"home": 2, "away": 1},
        "score": {"fulltime": {"home": 2, "away": 1}},
    }
    m_ft = market_grading.grade_fixture_markets(ft_fix)["goal_markets"]
    assert m_ft["match_result"]["outcome"] == "home_win"
    assert m_ft["over_under"]["over_2_5"]["outcome"] == "over"

    # B. Knockout AET fixture (regulation 1-1, extra time 2-1)
    aet_fix = {
        "fixture": {"status": {"short": "AET"}},
        "goals": {"home": 2, "away": 1},
        "score": {"fulltime": {"home": 1, "away": 1}},
    }
    m_aet = market_grading.grade_fixture_markets(aet_fix)["goal_markets"]
    assert m_aet["match_result"]["outcome"] == "draw" # 1X2 uses regulation time (1-1)
    assert m_aet["over_under"]["over_2_5"]["outcome"] == "over" # Totals uses match goals (2-1 = 3)

    # C. Knockout PEN fixture (regulation 1-1, penalty shootout 4-3)
    pen_fix = {
        "fixture": {"status": {"short": "PEN"}},
        "goals": {"home": 1, "away": 1},
        "score": {
            "fulltime": {"home": 1, "away": 1},
            "penalty": {"home": 4, "away": 3},
        },
    }
    m_pen = market_grading.grade_fixture_markets(pen_fix)["goal_markets"]
    assert m_pen["match_result"]["outcome"] == "draw"
    assert m_pen["over_under"]["over_2_5"]["outcome"] == "under" # Shootout goals (4-3) NOT counted as match goals

    # D. Missing fulltime score on AET fixture fails 1X2 safely
    aet_missing_ft = {
        "fixture": {"status": {"short": "AET"}},
        "goals": {"home": 2, "away": 1},
        "score": {},
    }
    m_missing = market_grading.grade_fixture_markets(aet_missing_ft)["goal_markets"]
    assert m_missing["match_result"] is None # 1X2 fails closed instead of using extra time score


def test_backtest_actual_outcome_consumer_semantics():
    import backtest

    # AET match: regulation 1-1, final goals 2-1
    aet_fix = {
        "fixture": {"id": 1, "date": "2025-01-01T15:00:00+00:00", "status": {"short": "AET"}},
        "goals": {"home": 2, "away": 1},
        "score": {"fulltime": {"home": 1, "away": 1}},
    }
    assert backtest._actual_match_result(aet_fix) == "draw"
    assert backtest._actual_btts(aet_fix) == "yes"


def test_all_consumers_agree_on_aet_pen_settlement():
    import backtest
    import market_grading

    aet_fixture = {
        "fixture": {"id": 99, "date": "2025-01-01T15:00:00+00:00", "status": {"short": "AET"}},
        "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
        "goals": {"home": 2, "away": 1},
        "score": {"fulltime": {"home": 1, "away": 1}},
    }

    # 1. backtest._actual_match_result()
    act_backtest_1x2 = backtest._actual_match_result(aet_fixture)

    # 2. grade_fixture_markets() match_result
    graded = market_grading.grade_fixture_markets(aet_fixture)["goal_markets"]
    act_grading_1x2 = graded["match_result"]["outcome"]

    # 3. backtest._grade_prediction_markets() match_result
    pred_markets = {
        "match_result": {"draw": 0.8, "home_win": 0.1, "away_win": 0.1},
        "over_under": {"over_2_5": 0.7, "under_2_5": 0.3},
    }
    graded_backtest = backtest._grade_prediction_markets(pred_markets, aet_fixture)
    sel_1x2 = graded_backtest["selected"]["match_result"]

    # ALL THREE MUST AGREE ON REGULATION 1X2 ("draw")
    assert act_backtest_1x2 == "draw"
    assert act_grading_1x2 == "draw"
    assert sel_1x2["actual"] == "draw"
    assert sel_1x2["won"] is True

    # OVER/UNDER MUST USE MATCH GOALS (2+1 = 3 > 2.5 => over)
    assert graded["over_under"]["over_2_5"]["outcome"] == "over"
    assert graded_backtest["selected"]["over_under"]["2.5"]["actual"] == "over"
    assert graded_backtest["selected"]["over_under"]["2.5"]["won"] is True


def test_historical_features_and_h2h_consumer_semantics():
    import historical_features
    import historical_h2h

    pen_fix = {
        "fixture": {"id": 10, "date": "2025-01-01T15:00:00+00:00", "status": {"short": "PEN"}},
        "teams": {"home": {"id": 1, "name": "A"}, "away": {"id": 2, "name": "B"}},
        "goals": {"home": 1, "away": 1},
        "score": {
            "fulltime": {"home": 1, "away": 1},
            "penalty": {"home": 5, "away": 4},
        },
    }
    fixtures = [pen_fix]

    # Verify penalties are excluded from team goal averages and H2H goals
    avgs = historical_features.team_goal_averages(fixtures, 1, "2026-01-01T00:00:00+00:00")
    assert avgs["goals_for"] == 1.0
    assert avgs["goals_against"] == 1.0

    snap = historical_h2h.historical_h2h_snapshot(fixtures, 1, 2, "2026-01-01T00:00:00+00:00")
    assert snap["goals_for"] == 1.0
    assert snap["goals_against"] == 1.0
