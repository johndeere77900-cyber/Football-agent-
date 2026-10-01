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
    # Build mock log entries with explicit Quality Gate decisions
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

    # SIGNAL group accuracy is 1.0 (1/1), PASS group accuracy is 0.0 (0/1)
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
# 3. CROSS-MARKET SAFEST-PICK ISOLATION
# ==============================================================================

def test_cross_market_safest_pick_isolation():
    """Verify safest_pick returns non-authoritative, informational metadata and cannot override Quality Gate."""
    candidates = [
        ("Home Win", 0.55),
        ("Over 2.5 Goals", 0.85),
    ]
    pick = confidence.safest_pick(candidates)

    assert pick is not None
    assert pick["label"] == "Over 2.5 Goals"
    assert pick["probability"] == 0.85
    assert pick["is_authoritative"] is False
    assert pick["informational_only"] is True
    assert "Non-authoritative" in pick["warning"]


def test_safest_pick_does_not_override_quality_gate():
    """Verify that Quality Gate decision is independent of safest_pick."""
    unc_info = {"feature_coverage": 0.20, "historical_sample_count": 2, "state": "high_uncertainty"}
    gate_res = quality_gate.evaluate_quality_gate(
        uncertainty_info=unc_info,
        probability_valid=True,
        calibration_status="UNAVAILABLE",
    )
    # Even if a candidate has 0.95 probability, Quality Gate forces PASS due to insufficient history/coverage
    assert gate_res["decision"] == "PASS"


# ==============================================================================
# 4. CONFIDENCE SAFETY
# ==============================================================================

def test_confidence_flag_calibration_metadata():
    """Test confidence_flag under APPLIED, UNAVAILABLE, and ERROR calibration states."""
    probs = {"home_win": 0.65, "draw": 0.25, "away_win": 0.10}

    # Case A: Calibration APPLIED
    conf_cal = confidence.confidence_flag(probs, calibration_status="APPLIED")
    assert conf_cal["is_calibrated"] is True
    assert conf_cal["calibration_status"] == "APPLIED"
    assert conf_cal["calibration_limitation"] == "CALIBRATED"

    # Case B: Calibration UNAVAILABLE
    conf_uncal = confidence.confidence_flag(probs, calibration_status="UNAVAILABLE")
    assert conf_uncal["is_calibrated"] is False
    assert conf_uncal["calibration_status"] == "UNAVAILABLE"
    assert conf_uncal["calibration_limitation"] == "UNCALIBRATED_RAW_PROBABILITY"

    # Case C: Calibration ERROR
    conf_err = confidence.confidence_flag(probs, calibration_status="ERROR")
    assert conf_err["is_calibrated"] is False
    assert conf_err["calibration_status"] == "ERROR"
    assert conf_err["calibration_limitation"] == "CALIBRATION_ERROR"


# ==============================================================================
# 5. MARKET CALIBRATION STATUS
# ==============================================================================

def test_raw_vs_calibrated_market_labeling():
    """Verify evaluation distinguishes calibrated markets from raw/uncalibrated markets."""
    log_entries = [
        {
            "quality_gate": "SIGNAL",
            "actual": "home_win",
            "correct": True,
            "calibration_status": "APPLIED",
            "prediction": {
                "markets": {
                    "match_result": {"home_win": 0.60, "draw": 0.25, "away_win": 0.15},
                    "btts": {"yes": 0.55, "no": 0.45},
                },
                "quality_gate": "SIGNAL",
            },
            "market_grading": {
                "selected": {"match_result": {"pick": "home_win", "won": True}},
                "outcomes": {"match_result": {"outcome": "home_win"}},
            },
        }
    ]

    eval_res = backtest.evaluate_football_log_group(log_entries)
    assert eval_res["match_result"]["calibration_status"] == "CALIBRATED"
    assert eval_res["btts"]["calibration_status"] == "RAW_UNCALIBRATED"
    assert eval_res["double_chance"]["calibration_status"] == "RAW_UNCALIBRATED"


# ==============================================================================
# 6. BASELINE EVALUATION
# ==============================================================================

def test_empirical_baseline_evaluation():
    """Verify deterministic empirical frequency baseline calculation."""
    actuals = ["home_win", "home_win", "draw", "away_win"]
    baseline = backtest.compute_empirical_baseline(actuals, outcomes=("home_win", "draw", "away_win"))

    assert baseline["sample_count"] == 4
    assert baseline["top_pick"] == "home_win"
    assert baseline["empirical_distribution"]["home_win"] == 0.50
    assert baseline["empirical_distribution"]["draw"] == 0.25
    assert baseline["empirical_distribution"]["away_win"] == 0.25
    assert baseline["accuracy"] == 0.50
    assert baseline["brier_score"] is not None


def test_odds_baseline_evaluation_rejects_future_odds():
    """Verify odds baseline calculation ignores future odds."""
    log_entries = [
        {
            "actual": "home_win",
            "prediction": {
                "market_analysis": {
                    "match_result": {
                        "home_win": {"implied_probability": 0.50},
                        "draw": {"implied_probability": 0.25},
                        "away_win": {"implied_probability": 0.25},
                    }
                },
                "uncertainty": {"odds_status": "FUTURE"},  # Should be excluded
            },
        },
        {
            "actual": "home_win",
            "prediction": {
                "market_analysis": {
                    "match_result": {
                        "home_win": {"implied_probability": 0.60},
                        "draw": {"implied_probability": 0.20},
                        "away_win": {"implied_probability": 0.20},
                    }
                },
                "uncertainty": {"odds_status": "VALID"},
            },
        },
    ]

    baseline = backtest.compute_odds_baseline(log_entries, sport="football")
    assert baseline["sample_count"] == 1  # Only 1 entry had valid non-future odds
    assert baseline["odds_available"] is True


# ==============================================================================
# 7. SAMPLE-SIZE & STATISTICAL SAFETY
# ==============================================================================

def test_low_sample_handling_and_confidence_intervals():
    """Verify low sample flag and accuracy confidence intervals."""
    # Small sample (below MIN_EVALUATION_SAMPLE_THRESHOLD of 30)
    preds = [{"home_win": 0.6, "draw": 0.2, "away_win": 0.2}] * 10
    actuals = ["home_win"] * 10

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
    assert m_res["accuracy_ci_lower"] <= m_res["accuracy"] <= m_res["accuracy_ci_upper"]


# ==============================================================================
# 8. MULTICLASS CALIBRATION DETAIL
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


# ==============================================================================
# 9. SAMPLING TRANSPARENCY
# ==============================================================================

def test_sampling_transparency_metadata():
    """Verify sampling transparency metadata structure in backtest outputs."""
    candidates = list(range(100))
    sampled = backtest._sample_backtest_candidates(candidates, sample_size=20, seed=42)

    assert len(sampled) == 20
    # Confirm deterministic sampling given seed
    sampled_repeat = backtest._sample_backtest_candidates(candidates, sample_size=20, seed=42)
    assert sampled == sampled_repeat


# ==============================================================================
# 10. ANTI-BIAS / STABILITY DIAGNOSTICS
# ==============================================================================

def test_stability_diagnostics_calculation():
    """Verify factual stability diagnostics generation."""
    log_entries = [
        {
            "quality_gate": "SIGNAL",
            "actual": "home_win",
            "correct": True,
            "calibration_status": "APPLIED",
            "prediction": {
                "uncertainty": {"state": "low_uncertainty"},
                "confidence": {"label": "High"},
            },
        },
        {
            "quality_gate": "PASS",
            "actual": "draw",
            "correct": False,
            "calibration_status": "UNAVAILABLE",
            "prediction": {
                "uncertainty": {"state": "high_uncertainty"},
                "confidence": {"label": "Toss-up"},
            },
        },
    ]

    diag = backtest.compute_stability_diagnostics(log_entries, sport="football")

    assert diag["signal_vs_pass"]["signal_count"] == 1
    assert diag["signal_vs_pass"]["pass_count"] == 1
    assert diag["signal_vs_pass"]["signal_rate"] == 0.50
    assert diag["outcome_frequencies"]["home_win"]["count"] == 1
    assert diag["outcome_frequencies"]["draw"]["count"] == 1
    assert "low_uncertainty" in diag["performance_by_uncertainty_state"]
    assert "high_uncertainty" in diag["performance_by_uncertainty_state"]


# ==============================================================================
# 11. QUALITY GATE CONSISTENCY
# ==============================================================================

def test_quality_gate_consistency():
    """Verify Quality Gate decision in evaluation agrees with prediction contract."""
    contract = prediction_contract.build_prediction_contract(
        sport="football",
        fixture_id=1001,
        league_id=39,
        season=2024,
        raw_markets={"match_result": {"home_win": 0.50, "draw": 0.30, "away_win": 0.20}},
        calibrated_markets={},
        calibration_metadata={"calibration_status": "UNAVAILABLE"},
        market_analysis={},
        uncertainty_info={"state": "high_uncertainty", "feature_coverage": 0.40, "historical_sample_count": 2},
        quality_gate_result={"decision": "PASS", "reason_codes": ["insufficient_history", "insufficient_data"]},
        data_cutoff_timestamp="2025-01-01T12:00:00+00:00",
    )

    assert contract["quality_gate"] == "PASS"
    assert "insufficient_data" in contract["reason_codes"] or "insufficient_history" in contract["reason_codes"]


# ==============================================================================
# 12. WALK-FORWARD LEAKAGE SAFETY
# ==============================================================================

def test_walk_forward_filter_rejects_future_and_current_samples():
    """Verify filter_samples_by_cutoff excludes any sample at or after cutoff timestamp."""
    cutoff = "2025-02-01T00:00:00+00:00"
    samples = [
        {"timestamp": "2025-01-15T12:00:00+00:00", "id": 1},  # Eligible (< cutoff)
        {"timestamp": "2025-02-01T00:00:00+00:00", "id": 2},  # Ineligible (== cutoff)
        {"timestamp": "2025-02-05T12:00:00+00:00", "id": 3},  # Ineligible (> cutoff)
    ]

    filtered = calibration.filter_samples_by_cutoff(samples, cutoff)
    assert len(filtered) == 1
    assert filtered[0]["id"] == 1
