"""
Phase 3 Basketball prediction engine & pipeline tests.
"""

import pytest

import basketball_model
import config
import market_analysis
import probability_validation
import quality_gate
import uncertainty


def test_basketball_pit_cutoff_and_scoring():
    """Test basketball scoring extraction and validation."""
    stats = {
        "points": {
            "for": {"average": {"all": 112.5}},
            "against": {"average": {"all": 108.0}},
        }
    }
    pts_for, pts_against = basketball_model._extract_scoring(stats)
    assert pts_for == 112.5
    assert pts_against == 108.0


def test_basketball_probability_validation_and_fail_closed():
    """Test basketball probability validation layer."""
    # Valid basketball markets
    markets = {
        "moneyline": {"home_win": 0.60, "away_win": 0.40},
        "total_points": {"over": 0.52, "under": 0.48},
    }
    norm_markets = probability_validation.validate_all_probabilities(markets, sport="basketball")
    assert norm_markets["moneyline"]["home_win"] == pytest.approx(0.60)

    # Invalid moneyline sum
    with pytest.raises(probability_validation.ProbabilityValidationError):
        probability_validation.validate_all_probabilities(
            {"moneyline": {"home_win": 0.80, "away_win": 0.80}},
            sport="basketball"
        )


def test_basketball_odds_and_ev():
    """Test basketball odds, edge, and EV calculation."""
    out = market_analysis.calculate_outcome_market_analysis(
        calibrated_prob=0.55,
        decimal_odds=1.91,
        odds_timestamp="2026-03-30T12:00:00+00:00",
        cutoff_timestamp="2026-03-30T12:00:00+00:00",
    )
    assert out["odds_status"] == "AVAILABLE"
    assert out["implied_probability"] == pytest.approx(1.0 / 1.91)
    assert out["ev"] == pytest.approx(0.55 * 1.91 - 1.0)


def test_basketball_quality_gate_and_reason_codes():
    """Test basketball Quality Gate decision and reason codes."""
    unc_info = uncertainty.calculate_uncertainty(
        feature_coverage=1.0,
        sample_count=10,
        top_probability=0.65,
        calibration_status="APPLIED",
        odds_status="AVAILABLE",
    )
    gate = quality_gate.evaluate_quality_gate(
        uncertainty_info=unc_info,
        probability_valid=True,
        edge=0.03,
        ev=0.05,
        calibration_status="APPLIED",
        odds_status="AVAILABLE",
    )
    assert gate["decision"] == "SIGNAL"
    assert gate["reason_codes"] == []


def test_basketball_prediction_contract():
    """Test basketball prediction contract fields and version metadata."""
    game = {
        "id": 888222,
        "date": "2026-03-30T20:00:00+00:00",
        "teams": {"home": {"id": 1, "name": "Lakers"}, "away": {"id": 2, "name": "Celtics"}},
        "league": {"id": 12, "season": 2024, "name": "NBA"},
    }
    home_stats = {"points": {"for": {"average": {"all": 115.0}}, "against": {"average": {"all": 105.0}}}}
    away_stats = {"points": {"for": {"average": {"all": 110.0}}, "against": {"average": {"all": 110.0}}}}

    pred = basketball_model.predict_game(game, home_stats_override=home_stats, away_stats_override=away_stats)

    assert pred["sport"] == "basketball"
    assert pred["game_id"] == 888222
    assert pred["model_version"] == config.MODEL_VERSION
    assert pred["feature_version"] == config.FEATURE_VERSION
    assert pred["calibration_version"] == config.CALIBRATION_VERSION
    assert "quality_gate" in pred
    assert "reason_codes" in pred
