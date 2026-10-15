"""
Unit tests for odds validation in market_analysis.py (Phase 7).
"""

import math
import pytest
from market_analysis import calculate_outcome_market_analysis, analyze_market_odds


def test_valid_outcome_odds_analysis():
    res = calculate_outcome_market_analysis(
        calibrated_prob=0.5,
        decimal_odds=2.1,
        odds_timestamp="2025-01-01T12:00:00Z",
        cutoff_timestamp="2025-01-01T15:00:00Z",
    )
    assert res["odds_status"] == "AVAILABLE"
    assert res["odds"] == 2.1
    assert res["edge"] == pytest.approx(0.5 - (1.0 / 2.1))
    assert res["ev"] == pytest.approx((0.5 * 2.1) - 1.0)


def test_invalid_decimal_odds_nan_and_inf():
    res_nan = calculate_outcome_market_analysis(
        calibrated_prob=0.5,
        decimal_odds=float("nan"),
        odds_timestamp="2025-01-01T12:00:00Z",
        cutoff_timestamp="2025-01-01T15:00:00Z",
    )
    assert res_nan["odds_status"] == "INVALID_ODDS"
    assert res_nan["odds"] is None

    res_inf = calculate_outcome_market_analysis(
        calibrated_prob=0.5,
        decimal_odds=float("inf"),
        odds_timestamp="2025-01-01T12:00:00Z",
        cutoff_timestamp="2025-01-01T15:00:00Z",
    )
    assert res_inf["odds_status"] == "INVALID_ODDS"
    assert res_inf["odds"] is None


def test_invalid_decimal_odds_zero_or_negative():
    res_zero = calculate_outcome_market_analysis(
        calibrated_prob=0.5,
        decimal_odds=0.0,
        odds_timestamp="2025-01-01T12:00:00Z",
        cutoff_timestamp="2025-01-01T15:00:00Z",
    )
    assert res_zero["odds_status"] == "INVALID_ODDS"
    assert res_zero["odds"] is None

    res_neg = calculate_outcome_market_analysis(
        calibrated_prob=0.5,
        decimal_odds=-1.5,
        odds_timestamp="2025-01-01T12:00:00Z",
        cutoff_timestamp="2025-01-01T15:00:00Z",
    )
    assert res_neg["odds_status"] == "INVALID_ODDS"
    assert res_neg["odds"] is None


def test_analyze_market_odds_integration():
    calibrated_markets = {"match_result": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}}
    odds_data = {"match_result": {"home_win": 2.1, "draw": 3.4, "away_win": 5.0}}

    res = analyze_market_odds(
        calibrated_markets=calibrated_markets,
        odds_data=odds_data,
        odds_timestamp="2025-01-01T12:00:00Z",
        cutoff_timestamp="2025-01-01T15:00:00Z",
    )

    assert "match_result" in res
    assert res["match_result"]["home_win"]["odds"] == 2.1
    assert res["match_result"]["home_win"]["odds_status"] == "AVAILABLE"
