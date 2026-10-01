"""
Unit and integration regression tests for Phase 4 Safety / Anti-Bias / Evaluation Hardening.
"""

import math
import pytest
from unittest.mock import MagicMock, patch

import backtest
import basketball_model
import calibration
import confidence
import config
import prediction_contract
import prediction_engine
import quality_gate
import storage


# ==============================================================================
# 1. SIGNAL VS PASS SEPARATION
# ==============================================================================

def test_signal_vs_pass_separation_football():
    """Verify that football evaluation strictly separates ALL, SIGNAL, and PASS predictions."""
    log_entries = [
        {
            "quality_gate": "SIGNAL",
            "actual": "home_win",
            "correct": True,
            "calibration_status": "APPLIED",
            "prediction": {
                "markets": {"match_result": {"home_win": 0.70, "draw": 0.20, "away_win": 0.10}},
                "uncertainty": {"state": "low_uncertainty"},
                "confidence": {"label": "High"},
                "quality_gate": "SIGNAL",
            },
            "market_grading": {
                "selected": {"match_result": {"pick": "home_win", "won": True}},
                "outcomes": {"match_result": {"outcome": "home_win"}},
            },
        },
        {
            "quality_gate": "PASS",
            "actual": "away_win",
            "correct": False,
            "calibration_status": "UNAVAILABLE",
            "prediction": {
                "markets": {"match_result": {"home_win": 0.50, "draw": 0.30, "away_win": 0.20}},
                "uncertainty": {"state": "high_uncertainty"},
                "confidence": {"label": "Toss-up"},
                "quality_gate": "PASS",
            },
            "market_grading": {
                "selected": {"match_result": {"pick": "home_win", "won": False}},
                "outcomes": {"match_result": {"outcome": "away_win"}},
            },
        },
    ]

    group_all = backtest.evaluate_football_log_group(log_entries)
    group_signal = backtest.evaluate_football_log_group([e for e in log_entries if e["quality_gate"] == "SIGNAL"])
    group_pass = backtest.evaluate_football_log_group([e for e in log_entries if e["quality_gate"] == "PASS"])

    assert group_all["total_graded_samples"] == 2
    assert group_signal["total_graded_samples"] == 1
    assert group_pass["total_graded_samples"] == 1

    assert group_signal["match_result"]["accuracy"] == 1.0
    assert group_pass["match_result"]["accuracy"] == 0.0


def test_signal_vs_pass_separation_basketball():
    """Verify basketball evaluation separates SIGNAL and PASS predictions."""
    log_entries = [
        {
            "quality_gate": "SIGNAL",
            "actual": "home_win",
            "correct": True,
            "calibration_status": "APPLIED",
            "prediction": {
                "markets": {"moneyline": {"home_win": 0.80, "away_win": 0.20}},
                "uncertainty": {"state": "low_uncertainty"},
                "confidence": {"label": "High"},
                "quality_gate": "SIGNAL",
            },
            "actual_points": {"home": 110, "away": 95},
        },
        {
            "quality_gate": "PASS",
            "actual": "away_win",
            "correct": False,
            "calibration_status": "UNAVAILABLE",
            "prediction": {
                "markets": {"moneyline": {"home_win": 0.55, "away_win": 0.45}},
                "uncertainty": {"state": "high_uncertainty"},
                "confidence": {"label": "Toss-up"},
                "quality_gate": "PASS",
            },
            "actual_points": {"home": 90, "away": 105},
        },
    ]

    group_signal = backtest.evaluate_basketball_log_group([e for e in log_entries if e["quality_gate"] == "SIGNAL"])
    group_pass = backtest.evaluate_basketball_log_group([e for e in log_entries if e["quality_gate"] == "PASS"])

    assert group_signal["moneyline"]["sample_count"] == 1
    assert group_signal["moneyline"]["accuracy"] == 1.0
    assert group_pass["moneyline"]["sample_count"] == 1
    assert group_pass["moneyline"]["accuracy"] == 0.0


# ==============================================================================
# 2. MULTI-SEASON EVALUATION INTEGRITY
# ==============================================================================

def test_multi_season_partial_failure_handling():
    """Verify that multi-season evaluation tracks partial or failed seasons and rejects COMPLETE overall status."""
    seasons = [2021, 2022, 2023]

    def mock_run_real_backtest(league_id, season, **kwargs):
        if season == 2021:
            return {"status": "COMPLETED", "graded": 50, "correct": 30, "log": []}
        elif season == 2022:
            return {"status": "INCOMPLETE", "graded": 0, "correct": 0, "log": []}
        else:
            raise RuntimeError("Database connection lost for 2023")

    with patch("backtest.run_real_backtest", side_effect=mock_run_real_backtest):
        res = backtest.run_multi_season_backtest(league_id=39, seasons=seasons)

    assert res["is_aggregate_complete"] is False
    assert res["overall_status"] in ("PARTIAL", "FAILED")
    assert res["overall_status"] != "COMPLETE"

    statuses = res["season_statuses"]
    assert statuses[2021]["status"] == "COMPLETE"
    assert statuses[2022]["status"] == "PARTIAL"
    assert statuses[2023]["status"] == "FAILED"
    assert statuses[2023]["error_reason"] == "Database connection lost for 2023"
    assert res["contributing_seasons"] == [2021]


# ==============================================================================
# 3. EMPIRICAL BASELINE POINT-IN-TIME LEAKAGE SAFETY
# ==============================================================================

def test_empirical_baseline_no_target_outcome_leakage():
    """
    Regression test proving evaluation target outcomes CANNOT define their own baseline.
    Changing the target actual outcome of an evaluation entry must have ZERO effect on its prior baseline prediction distribution.
    """
    log_entry_a = {
        "date": "2025-01-10T15:00:00+00:00",
        "actual": "away_win",  # Target outcome is away_win
        "prior_empirical_distribution": {"home_win": 0.60, "draw": 0.25, "away_win": 0.15},
    }

    log_entry_b = {
        "date": "2025-01-10T15:00:00+00:00",
        "actual": "home_win",  # Target outcome changed to home_win
        "prior_empirical_distribution": {"home_win": 0.60, "draw": 0.25, "away_win": 0.15},
    }

    base_a = backtest.compute_point_in_time_empirical_baseline([log_entry_a], outcomes=("home_win", "draw", "away_win"))
    base_b = backtest.compute_point_in_time_empirical_baseline([log_entry_b], outcomes=("home_win", "draw", "away_win"))

    assert base_a["leakage_safe"] is True
    assert base_b["leakage_safe"] is True
    assert base_a["sample_count"] == 1
    assert base_b["sample_count"] == 1


# ==============================================================================
# 4. CROSS-MARKET SAFEST-PICK ISOLATION & MARKET SCOPING
# ==============================================================================

def test_market_scoped_safest_pick_no_cross_market_rankings():
    """Verify safest_pick returns market-scoped selections without cross-market rankings."""
    candidates_dict = {
        "match_result": [("Home Win", 0.60), ("Draw", 0.25), ("Away Win", 0.15)],
        "over_under": [("Over 2.5 Goals", 0.85), ("Under 2.5 Goals", 0.15)],
        "btts": [("BTTS Yes", 0.55), ("BTTS No", 0.45)],
    }
    pick = confidence.safest_pick(candidates_dict)

    assert pick is not None
    assert pick["is_authoritative"] is False
    assert pick["informational_only"] is True
    assert "by_market" in pick
    # Confirm no top-level cross-market ranking overrides
    assert "top_overall" not in pick
    assert pick["by_market"]["match_result"]["label"] == "Home Win"
    assert pick["by_market"]["match_result"]["probability"] == 0.60
    assert pick["by_market"]["over_under"]["label"] == "Over 2.5 Goals"
    assert pick["by_market"]["over_under"]["probability"] == 0.85


def test_safest_pick_does_not_override_quality_gate():
    """Verify that Quality Gate decision is independent of safest_pick."""
    unc_info = {"feature_coverage": 0.20, "historical_sample_count": 2, "state": "high_uncertainty"}
    gate_res = quality_gate.evaluate_quality_gate(
        uncertainty_info=unc_info,
        probability_valid=True,
        calibration_status="UNAVAILABLE",
    )
    assert gate_res["decision"] == "PASS"


# ==============================================================================
# 5. CONFIDENCE SAFETY
# ==============================================================================

def test_confidence_flag_calibration_metadata():
    """Test confidence_flag under APPLIED, UNAVAILABLE, and ERROR calibration states."""
    probs = {"home_win": 0.65, "draw": 0.25, "away_win": 0.10}

    conf_cal = confidence.confidence_flag(probs, calibration_status="APPLIED")
    assert conf_cal["is_calibrated"] is True
    assert conf_cal["calibration_status"] == "APPLIED"
    assert conf_cal["calibration_limitation"] == "CALIBRATED"

    conf_uncal = confidence.confidence_flag(probs, calibration_status="UNAVAILABLE")
    assert conf_uncal["is_calibrated"] is False
    assert conf_uncal["calibration_status"] == "UNAVAILABLE"
    assert conf_uncal["calibration_limitation"] == "UNCALIBRATED_RAW_PROBABILITY"

    conf_err = confidence.confidence_flag(probs, calibration_status="ERROR")
    assert conf_err["is_calibrated"] is False
    assert conf_err["calibration_status"] == "ERROR"
    assert conf_err["calibration_limitation"] == "CALIBRATION_ERROR"


# ==============================================================================
# 6. MARKET CALIBRATION STATUS & MIXED-STATUS EVALUATION
# ==============================================================================

def test_mixed_calibration_status_labeling():
    """Verify evaluation accurately identifies PARTIALLY_CALIBRATED when entries have mixed calibration status."""
    log_entries = [
        {
            "quality_gate": "SIGNAL",
            "actual": "home_win",
            "correct": True,
            "calibration_status": "APPLIED",
            "prediction": {
                "markets": {"match_result": {"home_win": 0.60, "draw": 0.25, "away_win": 0.15}},
            },
            "market_grading": {"selected": {"match_result": {"pick": "home_win", "won": True}}, "outcomes": {}},
        },
        {
            "quality_gate": "PASS",
            "actual": "draw",
            "correct": False,
            "calibration_status": "UNAVAILABLE",
            "prediction": {
                "markets": {"match_result": {"home_win": 0.40, "draw": 0.35, "away_win": 0.25}},
            },
            "market_grading": {"selected": {"match_result": {"pick": "home_win", "won": False}}, "outcomes": {}},
        },
    ]

    eval_res = backtest.evaluate_football_log_group(log_entries)
    assert eval_res["match_result"]["calibration_status"] == "PARTIALLY_CALIBRATED"
    assert eval_res["btts"]["calibration_status"] == "RAW_UNCALIBRATED"


# ==============================================================================
# 7. ODDS BASELINE STRICT CHRONOLOGY & VALIDITY
# ==============================================================================

def test_odds_baseline_rejects_missing_timestamps_and_invalid_chronology():
    """Verify compute_odds_baseline requires BOTH VALID status AND explicit valid timestamps."""
    log_entries = [
        {
            # VALID status BUT missing odds timestamp -> EXCLUDED
            "actual": "home_win",
            "prediction": {
                "market_analysis": {"match_result": {"home_win": {"implied_probability": 0.50}}},
                "uncertainty": {"odds_status": "VALID", "odds_timestamp": None},
                "data_cutoff_timestamp": "2025-01-01T12:00:00+00:00",
            },
        },
        {
            # VALID status AND explicit timestamps AND odds_timestamp < cutoff_timestamp -> INCLUDED
            "actual": "home_win",
            "prediction": {
                "market_analysis": {
                    "match_result": {
                        "home_win": {"implied_probability": 0.60},
                        "draw": {"implied_probability": 0.20},
                        "away_win": {"implied_probability": 0.20},
                    }
                },
                "uncertainty": {
                    "odds_status": "VALID",
                    "odds_timestamp": "2025-01-01T10:00:00+00:00",
                },
                "data_cutoff_timestamp": "2025-01-01T12:00:00+00:00",
            },
        },
    ]

    baseline = backtest.compute_odds_baseline(log_entries, sport="football")
    assert baseline["sample_count"] == 1
    assert baseline["odds_available"] is True


# ==============================================================================
# 8. STABILITY / ANTI-BIAS DIAGNOSTICS
# ==============================================================================

def test_stability_diagnostics_probability_buckets_and_outcome_behavior():
    """Verify probability buckets, outcome behavior, and league/season breakdown in diagnostics."""
    log_entries = [
        {
            "league_id": 39,
            "season": 2024,
            "quality_gate": "SIGNAL",
            "actual": "home_win",
            "correct": True,
            "calibration_status": "APPLIED",
            "prediction": {
                "league_id": 39,
                "season": 2024,
                "markets": {"match_result": {"home_win": 0.72, "draw": 0.18, "away_win": 0.10}},
                "uncertainty": {"state": "low_uncertainty"},
                "confidence": {"label": "High"},
            },
        },
        {
            "league_id": 39,
            "season": 2024,
            "quality_gate": "PASS",
            "actual": "draw",
            "correct": False,
            "calibration_status": "UNAVAILABLE",
            "prediction": {
                "league_id": 39,
                "season": 2024,
                "markets": {"match_result": {"home_win": 0.45, "draw": 0.35, "away_win": 0.20}},
                "uncertainty": {"state": "high_uncertainty"},
                "confidence": {"label": "Toss-up"},
            },
        },
    ]

    diag = backtest.compute_stability_diagnostics(log_entries, sport="football")

    # Check signal vs pass
    assert diag["signal_vs_pass"]["signal_count"] == 1
    assert diag["signal_vs_pass"]["pass_count"] == 1

    # Check actual vs predicted outcome behavior
    assert "home_win" in diag["outcome_behavior"]
    assert "draw" in diag["outcome_behavior"]
    assert "away_win" in diag["outcome_behavior"]
    assert diag["outcome_behavior"]["home_win"]["actual_count"] == 1
    assert diag["outcome_behavior"]["draw"]["actual_count"] == 1

    # Check probability buckets
    assert "70%+" in diag["performance_by_probability_bucket"]
    assert "<50%" in diag["performance_by_probability_bucket"]
    assert diag["performance_by_probability_bucket"]["70%+"]["sample_count"] == 1

    # Check league/season breakdown
    assert "39_2024" in diag["league_season_breakdown"]
    assert diag["league_season_breakdown"]["39_2024"]["sample_count"] == 2


# ==============================================================================
# 9. LOW SAMPLE HANDLING & CONFIDENCE INTERVALS
# ==============================================================================

def test_low_sample_handling_and_confidence_intervals():
    """Verify low sample flag and accuracy confidence intervals."""
    eval_res = backtest.evaluate_football_log_group([
        {
            "actual": "home_win",
            "prediction": {"markets": {"match_result": {"home_win": 0.6, "draw": 0.2, "away_win": 0.2}}},
            "market_grading": {"selected": {"match_result": {"pick": "home_win", "won": True}}},
        }
    ] * 10)

    m_res = eval_res["match_result"]
    assert m_res["sample_count"] == 10
    assert m_res["is_low_sample"] is True
    assert m_res["sample_reliability"] == "INSUFFICIENT_SAMPLE"
    assert m_res["accuracy_ci_lower"] is not None
    assert m_res["accuracy_ci_upper"] is not None


# ==============================================================================
# 10. MULTICLASS 1X2 CALIBRATION BREAKDOWN
# ==============================================================================

def test_per_class_1x2_calibration():
    """Verify multiclass 1X2 calibration breakdown by outcome class."""
    preds = [
        {"home_win": 0.60, "draw": 0.25, "away_win": 0.15},
        {"home_win": 0.30, "draw": 0.40, "away_win": 0.30},
    ]
    actuals = ["home_win", "draw"]

    calib = backtest.compute_multiclass_1x2_calibration(preds, actuals)

    assert "ece" in calib
    assert "bins" in calib
    assert "by_class" in calib
    assert "home_win" in calib["by_class"]
    assert "draw" in calib["by_class"]
    assert "away_win" in calib["by_class"]
    assert calib["by_class"]["home_win"]["total_samples"] == 2
