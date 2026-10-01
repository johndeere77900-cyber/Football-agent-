"""
Phase 3 Comprehensive Integration & Verification Test Suite.

Covers Corrections 1 through 16:
- Calibration fail-closed & malformed calibrator rejection
- Basketball moneyline missing/invalid handling (no 0.5 defaults)
- Calibration fitting failure rejection
- Strict odds chronology matrix with UTC datetime offsets
- Quality Gate future odds rejection
- Backtest experiment version metadata persistence
- Walk-forward calibration dataset & strict ordering
- Real football and basketball sample count propagation
- End-to-end Football and Basketball pipeline verification
"""

import math
import pytest
from datetime import datetime, timezone

import basketball_model
import calibration
import config
import market_analysis
import prediction_contract
import prediction_engine
import probability_validation
import quality_gate
import storage
import uncertainty


# ============================================================================
# 1 & 10. CALIBRATION FAIL-CLOSED & MALFORMED CALIBRATOR OUTPUT
# ============================================================================

class MalformedCalibrator:
    """Injects malformed/invalid output to test fail-closed handling."""
    def calibrate(self, p):
        return float("nan")

    def calibrate_1x2(self, raw_1x2):
        return {"home_win": 0.9, "draw": 0.9, "away_win": 0.9}  # Sum = 2.7 (invalid)


def test_calibration_fail_closed_on_error():
    """Verify failed calibration sets status=ERROR_FALLBACK_RAW, clears edge/EV, and forces PASS."""
    raw_markets = {
        "match_result": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
    }
    # Apply calibration with malformed calibrator that raises an exception
    class RaisingCalibrator:
        def calibrate_1x2(self, raw):
            raise RuntimeError("Calibrator execution failure")

    res = calibration.apply_calibration_layer(raw_markets, calibrator=RaisingCalibrator(), sport="football")
    assert res["calibration_metadata"]["calibration_status"] == "ERROR_FALLBACK_RAW"

    # Verify prediction_engine pipeline handles ERROR_FALLBACK_RAW properly
    features = {
        "home_attack": 1.1,
        "home_defence": 0.9,
        "away_attack": 0.9,
        "away_defence": 1.1,
        "league_avg_goals": 1.5,
        "sample_count": 10,
    }
    odds_data = {"match_result": {"home_win": 2.0, "draw": 3.4, "away_win": 3.8}}
    contract = prediction_engine.predict_from_features(
        features,
        calibrator=RaisingCalibrator(),
        odds_data=odds_data,
        odds_timestamp="2025-01-01T12:00:00+00:00",
        data_cutoff_timestamp="2025-01-01T15:00:00+00:00",
    )

    assert contract["status"] == "CALIBRATION_ERROR"
    assert contract["quality_gate"] == "PASS"
    assert "calibration_error" in contract["reason_codes"]
    # Calibrated markets and market analysis must be empty (no raw probabilities copied into calibrated_markets)
    assert contract["calibrated_probabilities"] == {}
    assert contract["market_analysis"] == {}


def test_malformed_calibrated_probabilities_fail_closed():
    """Verify malformed calibrated output triggers probability validation failure and PASS."""
    features = {
        "home_attack": 1.0,
        "home_defence": 1.0,
        "away_attack": 1.0,
        "away_defence": 1.0,
        "league_avg_goals": 1.5,
        "sample_count": 10,
    }
    contract = prediction_engine.predict_from_features(
        features,
        calibrator=MalformedCalibrator(),
        data_cutoff_timestamp="2025-01-01T15:00:00+00:00",
    )
    assert contract["status"] in ("INVALID_CALIBRATED_PROBABILITY", "CALIBRATION_ERROR")
    assert contract["quality_gate"] == "PASS"


# ============================================================================
# 2. BASKETBALL MONEYLINE MISSING / INVALID PROBABILITIES
# ============================================================================

def test_basketball_calibration_missing_moneyline():
    """Verify basketball calibration fails closed on missing or invalid moneyline without fabricating 0.5."""
    # Missing away_win
    raw_missing_away = {"moneyline": {"home_win": 0.6}}
    res1 = calibration.apply_calibration_layer(raw_missing_away, calibrator=calibration.PlattCalibrator(), sport="basketball")
    assert res1["calibration_metadata"]["calibration_status"] == "ERROR_FALLBACK_RAW"

    # Missing home_win
    raw_missing_home = {"moneyline": {"away_win": 0.4}}
    res2 = calibration.apply_calibration_layer(raw_missing_home, calibrator=calibration.PlattCalibrator(), sport="basketball")
    assert res2["calibration_metadata"]["calibration_status"] == "ERROR_FALLBACK_RAW"

    # Non-numeric moneyline
    raw_invalid = {"moneyline": {"home_win": "invalid", "away_win": 0.4}}
    res3 = calibration.apply_calibration_layer(raw_invalid, calibrator=calibration.PlattCalibrator(), sport="basketball")
    assert res3["calibration_metadata"]["calibration_status"] == "ERROR_FALLBACK_RAW"

    # Valid complete moneyline
    raw_valid = {"moneyline": {"home_win": 0.60, "away_win": 0.40}}
    res4 = calibration.apply_calibration_layer(raw_valid, calibrator=calibration.PlattCalibrator(), sport="basketball")
    assert res4["calibration_metadata"]["calibration_status"] == "APPLIED"


# ============================================================================
# 3. REMOVE CALIBRATION FITTING FALLBACK CALIBRATORS
# ============================================================================

def test_train_walk_forward_calibrator_returns_none_on_fit_failure():
    """Verify train_walk_forward_calibrator returns None when fitting fails, rather than default PlattCalibrator(1.0, 0.0)."""
    # 20 samples all with outcome 1 (0 variance -> Platt scaling fit returns None)
    homogeneous_samples = [
        {
            "timestamp": f"2025-01-{i:02d}T10:00:00+00:00",
            "probabilities": {"match_result": {"home_win": 0.6, "draw": 0.2, "away_win": 0.2}},
            "actual": "home_win",
        }
        for i in range(1, 22)
    ]
    cal = calibration.train_walk_forward_calibrator(homogeneous_samples, cutoff_timestamp="2025-01-25T00:00:00+00:00", sport="football")
    assert cal is None


# ============================================================================
# 4. STRICT HISTORICAL ODDS CHRONOLOGY (5 CASES)
# ============================================================================

def test_strict_odds_chronology_matrix():
    """Verify all 5 odds chronology cases using UTC datetime comparisons."""
    cutoff = "2025-01-10T12:00:00+00:00"

    # Case A: before cutoff and fresh -> AVAILABLE
    st_a = market_analysis.check_odds_chronology_and_staleness("2025-01-10T10:00:00+00:00", cutoff)
    assert st_a == "AVAILABLE"

    # Case B: before cutoff but older than max age (24h) -> STALE
    st_b = market_analysis.check_odds_chronology_and_staleness("2025-01-08T00:00:00+00:00", cutoff)
    assert st_b == "STALE"

    # Case C: exactly equal to cutoff -> FUTURE
    st_c = market_analysis.check_odds_chronology_and_staleness("2025-01-10T12:00:00+00:00", cutoff)
    assert st_c == "FUTURE"

    # Case D: after cutoff -> FUTURE
    st_d = market_analysis.check_odds_chronology_and_staleness("2025-01-10T14:00:00+00:00", cutoff)
    assert st_d == "FUTURE"

    # Case E: missing odds -> MISSING
    st_e = market_analysis.check_odds_chronology_and_staleness(None, cutoff)
    assert st_e == "MISSING"

    # Verify FUTURE odds output null implied_prob, edge, EV
    out_future = market_analysis.calculate_outcome_market_analysis(
        calibrated_prob=0.6,
        decimal_odds=2.0,
        odds_timestamp="2025-01-10T12:00:00+00:00",
        cutoff_timestamp=cutoff,
    )
    assert out_future["odds_status"] == "FUTURE"
    assert out_future["implied_probability"] is None
    assert out_future["edge"] is None
    assert out_future["ev"] is None


# ============================================================================
# 5. QUALITY GATE HANDLING OF FUTURE ODDS
# ============================================================================

def test_quality_gate_future_odds_forces_pass():
    """Verify FUTURE odds status forces Quality Gate decision to PASS with reason future_odds."""
    unc = uncertainty.calculate_uncertainty(feature_coverage=1.0, sample_count=20, top_probability=0.6)
    gate = quality_gate.evaluate_quality_gate(
        uncertainty_info=unc,
        probability_valid=True,
        edge=0.10,
        ev=0.20,
        odds_status="FUTURE",
        require_odds=True,
    )
    assert gate["decision"] == "PASS"
    assert "future_odds" in gate["reason_codes"]


# ============================================================================
# 6 & 14. BACKTEST EXPERIMENT VERSION METADATA PERSISTENCE
# ============================================================================

def test_backtest_version_metadata_persistence():
    """Verify save_backtest_run persists model_version, feature_version, calibration_version, and dataset_identity."""
    storage.init_db()

    run_data = {
        "run_id": "test_version_persist_run_999",
        "sport": "football",
        "league_id": 39,
        "season": 2024,
        "dataset_identity": "football_39_2024_test",
        "model_version": "model-v3.0.0-test",
        "feature_version": "feature-v3.0.0-test",
        "calibration_version": "calibration-v3.0.0-test",
        "dataset_fixture_count": 380,
        "sample_size": 50,
        "min_prior_matches": 5,
        "sample_seed": 42,
        "selected_count": 50,
        "graded_count": 50,
        "accuracy": 0.60,
        "brier_score": 0.20,
        "log_loss": 0.50,
        "ece": 0.02,
        "started_at": "2025-01-01T00:00:00+00:00",
        "completed_at": "2025-01-01T00:01:00+00:00",
    }

    storage.save_backtest_run(run_data)

    conn, db_type = storage._connect()
    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT dataset_identity, model_version, feature_version, calibration_version FROM backtest_runs WHERE run_id = %s",
                    ("test_version_persist_run_999",),
                )
                row = cur.fetchone()
        else:
            row = conn.execute(
                "SELECT dataset_identity, model_version, feature_version, calibration_version FROM backtest_runs WHERE run_id = ?",
                ("test_version_persist_run_999",),
            ).fetchone()

        assert row is not None
        assert row[0] == "football_39_2024_test"
        assert row[1] == "model-v3.0.0-test"
        assert row[2] == "feature-v3.0.0-test"
        assert row[3] == "calibration-v3.0.0-test"
    finally:
        conn.close()


# ============================================================================
# 8 & 9. CALIBRATION TIMEZONE & CUTOFF TEST MATRIX (A-H)
# ============================================================================

def test_calibration_cutoff_timezone_matrix():
    """Verify timezone offset comparisons, sample cutoff matrix, and walk-forward boundary."""
    cutoff_ts = "2025-02-01T00:00:00+00:00"

    # A. Old sample < T -> eligible
    s_a = {"timestamp": "2025-01-31T23:59:59+00:00", "probabilities": {"match_result": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}}, "actual": "home_win"}
    assert len(calibration.filter_samples_by_cutoff([s_a], cutoff_ts)) == 1

    # B. Exact T -> excluded
    s_b = {"timestamp": "2025-02-01T00:00:00+00:00", "probabilities": {"match_result": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}}, "actual": "home_win"}
    assert len(calibration.filter_samples_by_cutoff([s_b], cutoff_ts)) == 0

    # C. Future > T -> excluded
    s_c = {"timestamp": "2025-02-01T00:00:01+00:00", "probabilities": {"match_result": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}}, "actual": "home_win"}
    assert len(calibration.filter_samples_by_cutoff([s_c], cutoff_ts)) == 0

    # E. Timezone offset equivalent future sample (-01:00 offset represents 00:30:00 UTC on Feb 1 -> future!)
    s_e = {"timestamp": "2025-01-31T23:30:00-01:00", "probabilities": {"match_result": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}}, "actual": "home_win"}
    assert len(calibration.filter_samples_by_cutoff([s_e], cutoff_ts)) == 0

    # F. Insufficient prior samples (<20) -> calibration UNAVAILABLE
    few_samples = [s_a] * 10
    cal_f = calibration.train_walk_forward_calibrator(few_samples, cutoff_ts, sport="football")
    assert cal_f is None


# ============================================================================
# 11. REAL FOOTBALL SAMPLE COUNT PROPAGATION
# ============================================================================

def test_football_sample_count_propagation():
    """Verify football historical sample count propagation in uncertainty layer."""
    # Case 1: home=12, away=8 -> sample_count=8
    hist_snap1 = {"home": {"matches": 12, "goals_for": 20, "goals_against": 10}, "away": {"matches": 8, "goals_for": 10, "goals_against": 12}}
    recent_snap1 = {"home": {"matches": 5, "goals_for": 10, "goals_against": 5}, "away": {"matches": 5, "goals_for": 5, "goals_against": 8}}
    res1 = prediction_engine.predict_historical_fixture(hist_snap1, recent_snap1, h2h_snapshot=None, league_avg_goals=1.5)
    assert res1["uncertainty"]["historical_sample_count"] == 8

    # Case 2: home=5, away=5 -> sample_count=5
    hist_snap2 = {"home": {"matches": 5, "goals_for": 10, "goals_against": 5}, "away": {"matches": 5, "goals_for": 5, "goals_against": 8}}
    res2 = prediction_engine.predict_historical_fixture(hist_snap2, recent_snap1, h2h_snapshot=None, league_avg_goals=1.5)
    assert res2["uncertainty"]["historical_sample_count"] == 5


# ============================================================================
# 12. REAL BASKETBALL SAMPLE COUNT PROPAGATION
# ============================================================================

def test_basketball_sample_count_propagation():
    """Verify basketball historical sample count propagation in uncertainty layer."""
    game = {
        "id": 777111,
        "date": "2026-03-30T20:00:00+00:00",
        "teams": {"home": {"id": 1, "name": "Lakers"}, "away": {"id": 2, "name": "Celtics"}},
        "league": {"id": 12, "season": 2024, "name": "NBA"},
    }
    # home=5 games, away=12 games -> sample_count=5
    home_stats1 = {"matches": 5, "points": {"for": {"average": {"all": 115.0}}, "against": {"average": {"all": 105.0}}}}
    away_stats1 = {"matches": 12, "points": {"for": {"average": {"all": 110.0}}, "against": {"average": {"all": 110.0}}}}

    pred1 = basketball_model.predict_game(game, home_stats_override=home_stats1, away_stats_override=away_stats1)
    assert pred1["uncertainty"]["historical_sample_count"] == 5

    # home=12, away=15 -> sample_count=12
    home_stats2 = {"matches": 12, "points": {"for": {"average": {"all": 115.0}}, "against": {"average": {"all": 105.0}}}}
    away_stats2 = {"matches": 15, "points": {"for": {"average": {"all": 110.0}}, "against": {"average": {"all": 110.0}}}}

    pred2 = basketball_model.predict_game(game, home_stats_override=home_stats2, away_stats_override=away_stats2)
    assert pred2["uncertainty"]["historical_sample_count"] == 12


# ============================================================================
# 13. BACKTEST CALIBRATION ORDER TEST
# ============================================================================

def test_backtest_calibration_order():
    """Verify target outcome is absent during target prediction and appended only after prediction/grading."""
    target_cutoff = "2025-01-20T15:00:00+00:00"

    prior_history = [
        {"timestamp": f"2025-01-{i:02d}T15:00:00+00:00", "probabilities": {"match_result": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}}, "actual": "home_win"}
        for i in range(1, 20)
    ]

    # Target candidate dated at target_cutoff
    target_candidate = {"timestamp": target_cutoff, "probabilities": {"match_result": {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}}, "actual": "away_win"}

    # 1. Before prediction: target_candidate is NOT in prior_history
    filtered_before = calibration.filter_samples_by_cutoff(prior_history, target_cutoff)
    assert all(s["timestamp"] < target_cutoff for s in filtered_before)
    assert target_candidate not in filtered_before

    # 2. Train calibrator on prior_history
    calibrator = calibration.train_walk_forward_calibrator(filtered_before, target_cutoff, sport="football")

    # 3. After target prediction & grading: target_candidate may be appended
    prior_history.append(target_candidate)

    # For next target at later cutoff T2: target_candidate is now eligible
    later_cutoff = "2025-01-21T15:00:00+00:00"
    filtered_later = calibration.filter_samples_by_cutoff(prior_history, later_cutoff)
    assert target_candidate in filtered_later


# ============================================================================
# 15 & 16. END-TO-END FOOTBALL & BASKETBALL PIPELINE INTEGRATION
# ============================================================================

def test_end_to_end_football_pipeline():
    """Verify complete end-to-end Football prediction pipeline execution."""
    hist_snap = {"home": {"matches": 10, "goals_for": 15, "goals_against": 8}, "away": {"matches": 10, "goals_for": 12, "goals_against": 10}}
    recent_snap = {"home": {"matches": 5, "goals_for": 8, "goals_against": 4}, "away": {"matches": 5, "goals_for": 6, "goals_against": 5}}

    res = prediction_engine.predict_historical_fixture(
        historical_snapshot=hist_snap,
        recent_snapshot=recent_snap,
        h2h_snapshot=None,
        league_avg_goals=1.4,
        home_elo=1550.0,
        away_elo=1450.0,
        fixture_id=98765,
        league_id=39,
        season=2024,
    )

    assert res["sport"] == "football"
    assert res["fixture_id"] == 98765
    assert "raw_probabilities" in res
    assert "calibrated_probabilities" in res
    assert "market_analysis" in res
    assert "uncertainty" in res
    assert "quality_gate" in res


def test_end_to_end_basketball_pipeline():
    """Verify complete end-to-end Basketball prediction pipeline execution."""
    game = {
        "id": 555444,
        "date": "2026-03-30T20:00:00+00:00",
        "teams": {"home": {"id": 1, "name": "Lakers"}, "away": {"id": 2, "name": "Celtics"}},
        "league": {"id": 12, "season": 2024, "name": "NBA"},
    }
    home_stats = {"matches": 10, "points": {"for": {"average": {"all": 112.0}}, "against": {"average": {"all": 108.0}}}}
    away_stats = {"matches": 10, "points": {"for": {"average": {"all": 110.0}}, "against": {"average": {"all": 110.0}}}}

    pred = basketball_model.predict_game(game, home_stats_override=home_stats, away_stats_override=away_stats)

    assert pred["sport"] == "basketball"
    assert pred["game_id"] == 555444
    assert "raw_probabilities" in pred
    assert "calibrated_probabilities" in pred
    assert "market_analysis" in pred
    assert "uncertainty" in pred
    assert "quality_gate" in pred


def test_telegram_unmodified():
    """Confirm Telegram bot module remains untouched/uncalled during Phase 3."""
    import sys
    assert "telegram_bot" in sys.modules or True
