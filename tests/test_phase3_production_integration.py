"""
Production Integration & End-to-End Backtest Test Suite.

Tests:
- Production prediction contract -> storage persistence without default fallbacks
- Live prediction path consistency & PRE_MATCH vs LIVE isolation
- Real database-first Football E2E backtest orchestration
- Real database-first Basketball E2E backtest orchestration
- Actual backtest walk-forward calibration order verification (target fixture T excluded from its own calibrator training)
"""

import math
import pytest
from datetime import datetime, timezone

import backtest
import basketball_api
import basketball_model
import calibration
import config
import main
import market_analysis
import prediction_contract
import prediction_engine
import quality_gate
import storage
import time_utils


# ============================================================================
# 10. PRODUCTION PERSISTENCE TEST
# ============================================================================

def test_production_prediction_persistence():
    """Verify production prediction contract reaches storage without generic defaults."""
    storage.init_db()

    features = {
        "home_attack": 1.2,
        "home_defence": 0.8,
        "away_attack": 0.9,
        "away_defence": 1.1,
        "league_avg_goals": 1.5,
        "sample_count": 15,
        "h2h_available": True,
    }

    contract = prediction_engine.predict_from_features(
        features,
        fixture_id=999888,
        league_id=39,
        season=2024,
        data_cutoff_timestamp="2025-01-10T12:00:00+00:00",
    )

    # Save to storage
    pred_id = storage.save_prediction(
        fixture_id=999888,
        match_date="2025-01-10T15:00:00+00:00",
        home_team="Arsenal",
        away_team="Chelsea",
        league="Premier League",
        markets=contract["markets"],
        confidence={"label": "High", "top_pick": "home_win", "top_probability": 0.55},
        prediction_record=contract,
    )

    assert pred_id is True

    # Retrieve from DB and assert exact match with Phase 3 metadata
    conn, db_type = storage._connect()
    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT model_version, feature_version, calibration_version, quality_gate,
                           edge, ev, uncertainty_state
                    FROM predictions WHERE fixture_id = %s
                    """,
                    (999888,),
                )
                row = cur.fetchone()
        else:
            row = conn.execute(
                """
                SELECT model_version, feature_version, calibration_version, quality_gate,
                       edge, ev, uncertainty_state
                FROM predictions WHERE fixture_id = ?
                """,
                (999888,),
            ).fetchone()

        assert row is not None
        assert row[0] == contract["model_version"]
        assert row[1] == contract["feature_version"]
        assert row[2] == contract["calibration_version"]
        assert row[3] == contract["quality_gate"]
    finally:
        conn.close()


# ============================================================================
# 11. LIVE PATH CONSISTENCY TEST
# ============================================================================

def test_live_path_consistency():
    """Verify live predictions are saved with prediction_context='LIVE' and distinguished from PRE_MATCH."""
    storage.init_db()

    contract = prediction_contract.build_prediction_contract(
        sport="football",
        fixture_id=888777,
        league_id=39,
        season=2024,
        raw_markets={"match_result": {"home_win": 0.4, "draw": 0.3, "away_win": 0.3}},
        calibrated_markets={"match_result": {"home_win": 0.4, "draw": 0.3, "away_win": 0.3}},
        calibration_metadata={
            "calibration_version": "v3.0.0",
            "calibration_method": "NONE",
            "calibration_status": "UNAVAILABLE",
        },
        market_analysis={},
        uncertainty_info={"state": "sufficient_data"},
        quality_gate_result={"decision": "PASS", "reasons": []},
    )

    storage.save_prediction(
        fixture_id=888777,
        match_date="2025-01-10T15:00:00+00:00",
        home_team="Team Live A",
        away_team="Team Live B",
        league="Premier League",
        markets=contract["markets"],
        confidence={"label": "Moderate", "top_pick": "home_win", "top_probability": 0.40},
        prediction_context="LIVE",
        prediction_record=contract,
    )

    conn, db_type = storage._connect()
    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute("SELECT prediction_context FROM predictions WHERE fixture_id = %s", (888777,))
                ctx = cur.fetchone()[0]
        else:
            ctx = conn.execute("SELECT prediction_context FROM predictions WHERE fixture_id = ?", (888777,)).fetchone()[0]

        assert ctx == "LIVE"
    finally:
        conn.close()


# ============================================================================
# 7. REAL DATABASE-FIRST FOOTBALL BACKTEST E2E TEST
# ============================================================================

def test_football_backtest_database_first_e2e(monkeypatch):
    """Run real Football backtest orchestration database-first with zero API calls."""
    import api_football

    def fail_api(*args, **kwargs):
        pytest.fail("Live API call attempted during historical backtest!")

    monkeypatch.setattr(api_football, "get_league_fixtures", fail_api)
    monkeypatch.setattr(api_football, "get_league_fixtures_with_metadata", fail_api)

    fixtures = [
        {
            "fixture": {"id": 100 + i, "date": f"2024-01-{i:02d}T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
            "goals": {"home": 2, "away": 1},
        }
        for i in range(1, 25)
    ]

    monkeypatch.setattr(storage, "get_historical_dataset_status", lambda l, s, sport="football": {"status": "COMPLETE", "fixture_count": 24})
    monkeypatch.setattr(storage, "get_historical_fixture_count", lambda l, s: 24)
    monkeypatch.setattr(storage, "get_historical_fixtures", lambda l, s: fixtures)

    result = backtest.run_real_backtest(league_id=39, season=2024, sample_size=5, min_prior_matches=1)

    assert result["status"] == "COMPLETED"
    assert result["graded"] > 0
    assert result["persisted"] is True
    assert len(result["log"]) > 0


# ============================================================================
# 8. REAL DATABASE-FIRST BASKETBALL BACKTEST E2E TEST
# ============================================================================

def test_basketball_backtest_database_first_e2e(monkeypatch):
    """Run real Basketball backtest orchestration database-first with zero API calls."""
    import basketball_api

    def fail_api(*args, **kwargs):
        pytest.fail("Live Basketball API call attempted during backtest!")

    monkeypatch.setattr(basketball_api, "get_games_by_date", fail_api)

    games = [
        {
            "id": 200 + i,
            "date": f"2024-01-{i:02d}T20:00:00+00:00",
            "league": {"id": 12, "season": 2024, "name": "NBA"},
            "teams": {"home": {"id": 1, "name": "Lakers"}, "away": {"id": 2, "name": "Celtics"}},
            "scores": {"home": {"total": 110}, "away": {"total": 105}},
            "status": {"short": "FT"},
        }
        for i in range(1, 25)
    ]

    monkeypatch.setattr(storage, "get_historical_dataset_status", lambda l, s, sport="basketball": {"status": "COMPLETE", "fixture_count": 24})
    monkeypatch.setattr(storage, "get_historical_basketball_game_count", lambda l, s: 24)
    monkeypatch.setattr(storage, "get_historical_basketball_games", lambda l, s: games)

    result = backtest.run_basketball_backtest(league_id=12, season=2024, sample_size=5, min_prior_matches=1)

    assert result["status"] == "COMPLETED"
    assert result["graded"] > 0
    assert result["persisted"] is True


# ============================================================================
# 9. ACTUAL BACKTEST CALIBRATION ORDER TEST
# ============================================================================

def test_backtest_actual_calibration_order(monkeypatch):
    """Instrument calibration.train_walk_forward_calibrator to prove target fixture T is excluded from calibration observations."""
    observed_calibrator_call_cutoffs = []
    observed_sample_timestamps = []

    original_train = calibration.train_walk_forward_calibrator

    def instrumented_train(prior_samples, cutoff_timestamp, sport="football"):
        observed_calibrator_call_cutoffs.append(cutoff_timestamp)
        for s in prior_samples:
            observed_sample_timestamps.append((cutoff_timestamp, s.get("timestamp")))
            # Assert strict inequality: every sample in calibration history MUST be strictly earlier than target cutoff_timestamp
            assert time_utils.is_strictly_before(s.get("timestamp"), cutoff_timestamp)
        return original_train(prior_samples, cutoff_timestamp, sport=sport)

    monkeypatch.setattr(calibration, "train_walk_forward_calibrator", instrumented_train)

    fixtures = [
        {
            "fixture": {"id": 300 + i, "date": f"2024-01-{i:02d}T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Team A"}, "away": {"id": 20, "name": "Team B"}},
            "goals": {"home": 1, "away": 0},
        }
        for i in range(1, 25)
    ]

    monkeypatch.setattr(storage, "get_historical_dataset_status", lambda l, s, sport="football": {"status": "COMPLETE", "fixture_count": 24})
    monkeypatch.setattr(storage, "get_historical_fixture_count", lambda l, s: 24)
    monkeypatch.setattr(storage, "get_historical_fixtures", lambda l, s: fixtures)

    backtest.run_real_backtest(league_id=39, season=2024, sample_size=5, min_prior_matches=1)

    assert len(observed_calibrator_call_cutoffs) > 0
