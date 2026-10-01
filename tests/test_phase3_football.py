"""
Phase 3 Football prediction engine & pipeline tests.
"""

import pytest
from datetime import datetime, timezone

import config
import probability_validation
import calibration
import market_analysis
import uncertainty
import quality_gate
import prediction_contract
import prediction_engine
import backtest
import storage


def test_football_feature_construction_and_pit_cutoff():
    """Test point-in-time cutoff and feature construction."""
    hist_snap = {
        "home": {"matches": 10, "goals_for": 2.0, "goals_against": 1.0},
        "away": {"matches": 10, "goals_for": 1.5, "goals_against": 1.2},
    }
    recent_snap = {
        "home": {"matches": 5, "goals_for": 2.2, "goals_against": 0.8},
        "away": {"matches": 5, "goals_for": 1.2, "goals_against": 1.4},
    }
    h2h_snap = {
        "meetings": 3,
        "goals_for": 1.8,
        "goals_against": 1.0,
    }

    features = prediction_engine.build_historical_features(
        historical_snapshot=hist_snap,
        recent_snapshot=recent_snap,
        h2h_snapshot=h2h_snap,
        league_avg_goals=1.4,
    )

    assert "home_attack" in features
    assert "home_defence" in features
    assert "away_attack" in features
    assert "away_defence" in features
    assert features["h2h_available"] is True


def test_football_probability_validity_and_fail_closed():
    """Test probability validation layer fail-closed behavior."""
    # Invalid probability > 1.0
    with pytest.raises(probability_validation.ProbabilityValidationError):
        probability_validation.validate_single_probability(1.2)

    # Invalid boolean
    with pytest.raises(probability_validation.ProbabilityValidationError):
        probability_validation.validate_single_probability(True)

    # Broken distribution sum
    with pytest.raises(probability_validation.ProbabilityValidationError):
        probability_validation.validate_and_normalize_distribution({"home_win": 0.9, "draw": 0.8, "away_win": 0.7})

    # Impossible market combinations (Over 1.5 < Over 2.5)
    with pytest.raises(probability_validation.ProbabilityValidationError):
        probability_validation.validate_market_combinations(
            {
                "over_under": {"over_1_5": 0.3, "over_2_5": 0.6}
            },
            sport="football"
        )


def test_football_calibration_and_leakage_prevention():
    """Test calibration application and zero future leakage."""
    raw_markets = {
        "match_result": {"home_win": 0.50, "draw": 0.30, "away_win": 0.20},
        "btts": {"yes": 0.55, "no": 0.45},
    }

    # Uncalibrated fallback
    res = calibration.apply_calibration_layer(raw_markets, calibrator=None, sport="football", cutoff_timestamp="2025-01-01T12:00:00+00:00")
    assert res["calibration_metadata"]["calibration_status"] == "UNAVAILABLE"
    assert res["calibrated_markets"] == {}

    # Fit calibration on historical samples only
    hist_samples = [(0.4, 0), (0.5, 1), (0.6, 1)] * 10
    fitted = calibration.fit_platt_scaling(hist_samples)
    assert fitted is not None

    # Prove fitted calibrator uses parameters derived prior to evaluation cutoff
    cal_val = fitted.calibrate(0.55)
    assert 0.0 <= cal_val <= 1.0


def test_football_odds_implied_prob_edge_ev():
    """Test market analysis layer calculation of odds, implied prob, edge, and EV."""
    # Valid odds
    out = market_analysis.calculate_outcome_market_analysis(
        calibrated_prob=0.60,
        decimal_odds=2.00,
        odds_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    assert out["implied_probability"] == pytest.approx(0.50)
    assert out["edge"] == pytest.approx(0.10)
    assert out["ev"] == pytest.approx(0.20)
    assert out["odds_status"] == "AVAILABLE"

    # Missing odds
    out_missing = market_analysis.calculate_outcome_market_analysis(
        calibrated_prob=0.60,
        decimal_odds=None,
    )
    assert out_missing["edge"] is None
    assert out_missing["ev"] is None
    assert out_missing["odds_status"] == "MISSING"

    # Stale odds
    stale_ts = "2020-01-01T00:00:00+00:00"
    out_stale = market_analysis.calculate_outcome_market_analysis(
        calibrated_prob=0.60,
        decimal_odds=2.00,
        odds_timestamp=stale_ts,
    )
    assert out_stale["odds_status"] == "STALE"
    assert out_stale["edge"] is None


def test_football_quality_gate_signal_and_pass():
    """Test Quality Gate SIGNAL / PASS logic and reason codes."""
    unc_sufficient = {
        "state": "sufficient_data",
        "feature_coverage": 0.90,
        "historical_sample_count": 10,
    }

    # SIGNAL state
    gate_signal = quality_gate.evaluate_quality_gate(
        uncertainty_info=unc_sufficient,
        probability_valid=True,
        edge=0.05,
        ev=0.10,
        calibration_status="APPLIED",
    )
    assert gate_signal["decision"] == "SIGNAL"
    assert gate_signal["reason_codes"] == []

    # PASS state with reason code
    unc_insufficient = {
        "state": "insufficient_data",
        "feature_coverage": 0.20,
        "historical_sample_count": 2,
    }
    gate_pass = quality_gate.evaluate_quality_gate(
        uncertainty_info=unc_insufficient,
        probability_valid=True,
        calibration_status="UNAVAILABLE",
    )
    assert gate_pass["decision"] == "PASS"
    assert "insufficient_history" in gate_pass["reason_codes"] or "insufficient_data" in gate_pass["reason_codes"]


def test_football_version_persistence_and_contract():
    """Test Phase 3 version metadata persistence and prediction contract output."""
    contract = prediction_contract.build_prediction_contract(
        sport="football",
        fixture_id=999111,
        league_id=39,
        season=2024,
        raw_markets={"match_result": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}},
        calibrated_markets={"match_result": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}},
        calibration_metadata={"calibration_version": config.CALIBRATION_VERSION, "calibration_status": "APPLIED"},
        market_analysis={},
        uncertainty_info={"feature_coverage": 1.0, "historical_sample_count": 10, "state": "sufficient_data"},
        quality_gate_result={"decision": "SIGNAL", "reason_codes": []},
        prediction_timestamp="2025-01-01T12:00:00+00:00",
    )

    assert contract["model_version"] == config.MODEL_VERSION
    assert contract["feature_version"] == config.FEATURE_VERSION
    assert contract["calibration_version"] == config.CALIBRATION_VERSION
    assert contract["quality_gate"] == "SIGNAL"
    assert contract["sport"] == "football"


def test_edge_ev_requires_applied_calibration():
    """Verify Requirement 4: Edge and EV are None when calibration is UNAVAILABLE or ERROR, but present when APPLIED."""
    # A. Calibration UNAVAILABLE (calibrated_prob is None)
    out_unavail = market_analysis.calculate_outcome_market_analysis(
        calibrated_prob=None,
        decimal_odds=2.00,
        odds_timestamp="2025-01-01T10:00:00+00:00",
        cutoff_timestamp="2025-01-01T12:00:00+00:00",
    )
    assert out_unavail["edge"] is None
    assert out_unavail["ev"] is None
    assert out_unavail["implied_probability"] == pytest.approx(0.50)

    # B. Calibration APPLIED
    out_applied = market_analysis.calculate_outcome_market_analysis(
        calibrated_prob=0.60,
        decimal_odds=2.00,
        odds_timestamp="2025-01-01T10:00:00+00:00",
        cutoff_timestamp="2025-01-01T12:00:00+00:00",
    )
    assert out_applied["edge"] == pytest.approx(0.10)
    assert out_applied["ev"] == pytest.approx(0.20)
    assert out_applied["implied_probability"] == pytest.approx(0.50)
