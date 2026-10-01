"""
Production Integration & End-to-End Backtest Test Suite.

Tests:
- Production prediction contract -> storage persistence without default fallbacks
- Real production football pre-match route (main.run_daily) -> storage persistence
- Real production football live route (main.run_daily) -> storage persistence
- Real production basketball route (main.run_daily_basketball) -> storage persistence
- Genuine database-first Football E2E backtest orchestration (no mocked storage getters)
- Genuine database-first Basketball E2E backtest orchestration (no mocked storage getters)
- Actual backtest walk-forward calibration order verification (target fixture T excluded from its own calibrator training)
"""

import math
import pytest
from datetime import datetime, timezone

import api_football
import backtest
import basketball_api
import basketball_model
import calibration
import config
import live_model
import main
import market_analysis
import prediction_contract
import prediction_engine
import quality_gate
import storage
import time_utils


# ============================================================================
# 1. REAL PRODUCTION FOOTBALL PRE-MATCH ROUTE TEST
# ============================================================================

def test_production_football_pre_match_route_e2e(tmp_path, monkeypatch):
    """Exercise main.run_daily -> main.predict_fixture -> storage and assert stored Phase 3 metadata."""
    db_file = tmp_path / "prod_football_pre_match.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    storage.init_db()

    fixture_item = {
        "fixture": {"id": 9001, "date": "2025-01-10T15:00:00+00:00", "status": {"short": "NS"}},
        "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
        "league": {"id": 39, "season": 2024, "name": "Premier League"},
    }

    mock_stats = {
        "fixtures": {"played": {"total": 10}},
        "goals": {
            "for": {"average": {"total": "1.8"}},
            "against": {"average": {"total": "0.9"}},
        },
    }

    monkeypatch.setattr(api_football, "get_fixtures_by_date", lambda date_str, league_id=None: [fixture_item])
    monkeypatch.setattr(api_football, "get_league_standings", lambda lid, ssn: [])
    monkeypatch.setattr(api_football, "get_team_statistics", lambda tid, lid, ssn: mock_stats)
    monkeypatch.setattr(api_football, "get_recent_form", lambda tid, last=8: [{"teams": {"home": {"id": tid}, "away": {"id": 99}}, "goals": {"home": 2, "away": 0}}])
    monkeypatch.setattr(api_football, "get_head_to_head", lambda h, a, last=6: [])

    # Execute main.run_daily (actual production entry point)
    main.run_daily("2025-01-10", league_id=39, limit=1, fetch_odds=False)

    # Inspect DB to verify the REAL production path persisted Phase 3 metadata
    conn, _ = storage._connect()
    try:
        row = conn.execute(
            """
            SELECT model_version, feature_version, calibration_version, quality_gate, prediction_context
            FROM predictions WHERE fixture_id = ?
            """,
            (9001,),
        ).fetchone()

        assert row is not None
        assert row[0] == config.MODEL_VERSION
        assert row[1] == config.FEATURE_VERSION
        assert row[2] == config.CALIBRATION_VERSION
        assert row[3] in ("SIGNAL", "PASS")
        assert row[4] == "PRE_MATCH"
    finally:
        conn.close()


# ============================================================================
# 2. REAL PRODUCTION FOOTBALL LIVE ROUTE TEST
# ============================================================================

def test_production_football_live_route_e2e(tmp_path, monkeypatch):
    """Exercise main.run_daily with a live fixture and verify storage persistence as LIVE context."""
    db_file = tmp_path / "prod_football_live.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    storage.init_db()

    live_fixture_item = {
        "fixture": {"id": 9002, "date": "2025-01-10T15:00:00+00:00", "status": {"short": "1H", "elapsed": 30}},
        "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
        "goals": {"home": 1, "away": 0},
        "league": {"id": 39, "season": 2024, "name": "Premier League"},
    }

    mock_stats = {
        "fixtures": {"played": {"total": 10}},
        "goals": {
            "for": {"average": {"total": "1.8"}},
            "against": {"average": {"total": "0.9"}},
        },
    }

    monkeypatch.setattr(api_football, "get_fixtures_by_date", lambda date_str, league_id=None: [live_fixture_item])
    monkeypatch.setattr(api_football, "get_league_standings", lambda lid, ssn: [])
    monkeypatch.setattr(api_football, "get_team_statistics", lambda tid, lid, ssn: mock_stats)
    monkeypatch.setattr(api_football, "get_recent_form", lambda tid, last=8: [{"teams": {"home": {"id": tid}, "away": {"id": 99}}, "goals": {"home": 2, "away": 0}}])
    monkeypatch.setattr(api_football, "get_head_to_head", lambda h, a, last=6: [])

    # Execute main.run_daily for live match
    main.run_daily("2025-01-10", league_id=39, limit=1, fetch_odds=False)

    conn, _ = storage._connect()
    try:
        row = conn.execute(
            """
            SELECT prediction_context, model_version, feature_version, calibration_version, quality_gate
            FROM predictions WHERE fixture_id = ?
            """,
            (9002,),
        ).fetchone()

        assert row is not None
        assert row[0] == "LIVE"
        assert row[1] == config.MODEL_VERSION
        assert row[2] == config.FEATURE_VERSION
        assert row[3] == config.CALIBRATION_VERSION
        assert row[4] in ("SIGNAL", "PASS")
    finally:
        conn.close()


# ============================================================================
# 3. REAL PRODUCTION BASKETBALL ROUTE TEST
# ============================================================================

def test_production_basketball_route_e2e(tmp_path, monkeypatch):
    """Exercise main.run_daily_basketball -> storage and assert stored Phase 3 metadata."""
    db_file = tmp_path / "prod_basketball.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    storage.init_db()

    game_item = {
        "id": 9003,
        "date": "2026-03-30T20:00:00+00:00",
        "teams": {"home": {"id": 1, "name": "Lakers"}, "away": {"id": 2, "name": "Celtics"}},
        "league": {"id": 12, "season": 2024, "name": "NBA"},
        "status": {"short": "NS"},
    }

    mock_bball_stats = {
        "matches": 10,
        "points": {"for": {"average": {"all": 115.0}}, "against": {"average": {"all": 105.0}}},
    }

    monkeypatch.setattr(basketball_api, "get_games_by_date", lambda date_str, league_id=None: [game_item])
    monkeypatch.setattr(basketball_api, "get_team_statistics", lambda tid, lid, ssn: mock_bball_stats)

    # Execute main.run_daily_basketball (actual production entry point)
    main.run_daily_basketball("2026-03-30", limit=1)

    conn, _ = storage._connect()
    try:
        row = conn.execute(
            """
            SELECT model_version, feature_version, calibration_version, quality_gate, prediction_context
            FROM basketball_predictions WHERE game_id = ?
            """,
            (9003,),
        ).fetchone()

        assert row is not None
        assert row[0] == config.MODEL_VERSION
        assert row[1] == config.FEATURE_VERSION
        assert row[2] == config.CALIBRATION_VERSION
        assert row[3] in ("SIGNAL", "PASS")
        assert row[4] == "PRE_MATCH"
    finally:
        conn.close()


# ============================================================================
# 7. REAL DATABASE-FIRST FOOTBALL BACKTEST E2E TEST (NO MOCKED STORAGE GETTERS)
# ============================================================================

def test_football_backtest_database_first_e2e(tmp_path, monkeypatch):
    """Run real Football backtest orchestration database-first using real storage functions and zero API calls."""
    db_file = tmp_path / "e2e_football_backtest.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    storage.init_db()

    def fail_api(*args, **kwargs):
        pytest.fail("Live API call attempted during historical backtest!")

    monkeypatch.setattr(api_football, "get_league_fixtures", fail_api)
    monkeypatch.setattr(api_football, "get_league_fixtures_with_metadata", fail_api)

    raw_fixtures = [
        {
            "fixture": {"id": 100 + i, "date": f"2024-01-{i:02d}T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024, "name": "Premier League"},
            "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
            "goals": {"home": 2, "away": 1},
        }
        for i in range(1, 25)
    ]

    # Save fixtures through real storage function
    save_res = storage.save_historical_fixtures(raw_fixtures, league_id=39, season=2024)
    assert save_res["valid"] == 24
    assert save_res["inserted"] == 24

    # Mark dataset COMPLETE through real storage function
    storage.mark_historical_dataset_complete(
        league_id=39,
        season=2024,
        fixture_count=24,
        expected_pages=1,
        pages_completed=1,
        acquisition_complete=True,
    )

    # Execute actual backtest entry point against database storage
    result = backtest.run_real_backtest(league_id=39, season=2024, sample_size=5, min_prior_matches=1)

    assert result["status"] == "COMPLETED"
    assert result["graded"] > 0
    assert result["persisted"] is True
    assert len(result["log"]) > 0


# ============================================================================
# 8. REAL DATABASE-FIRST BASKETBALL BACKTEST E2E TEST (NO MOCKED STORAGE GETTERS)
# ============================================================================

def test_basketball_backtest_database_first_e2e(tmp_path, monkeypatch):
    """Run real Basketball backtest orchestration database-first using real storage functions and zero API calls."""
    db_file = tmp_path / "e2e_basketball_backtest.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    storage.init_db()

    def fail_api(*args, **kwargs):
        pytest.fail("Live Basketball API call attempted during backtest!")

    monkeypatch.setattr(basketball_api, "get_games_by_date", fail_api)

    raw_games = [
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

    # Save games through real storage function
    save_res = storage.save_historical_basketball_games(raw_games, league_id=12, season=2024)
    assert save_res["valid"] == 24
    assert save_res["inserted"] == 24

    # Mark dataset COMPLETE through real storage function
    storage.mark_historical_dataset_complete(
        league_id=12,
        season=2024,
        fixture_count=24,
        expected_pages=1,
        pages_completed=1,
        acquisition_complete=True,
        sport="basketball",
    )

    # Execute actual basketball backtest entry point against database storage
    result = backtest.run_basketball_backtest(league_id=12, season=2024, sample_size=5, min_prior_matches=1)

    assert result["status"] == "COMPLETED"
    assert result["graded"] > 0
    assert result["persisted"] is True


# ============================================================================
# 9. ACTUAL BACKTEST CALIBRATION ORDER AND FIXTURE ID EXCLUSION PROOF
# ============================================================================

def test_backtest_actual_calibration_order(tmp_path, monkeypatch):
    """Instrument calibration.train_walk_forward_calibrator to prove target fixture T and its ID are excluded from calibration observations."""
    db_file = tmp_path / "e2e_calib_order.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    storage.init_db()

    observed_calibrations = []

    original_train = calibration.train_walk_forward_calibrator

    def instrumented_train(prior_samples, cutoff_timestamp, sport="football"):
        sample_ids = {s.get("fixture_id") for s in prior_samples if s.get("fixture_id") is not None}
        sample_timestamps = [s.get("timestamp") for s in prior_samples]
        observed_calibrations.append((cutoff_timestamp, sample_ids, sample_timestamps))

        for s in prior_samples:
            # Assert strict inequality: every sample in calibration history MUST be strictly earlier than target cutoff_timestamp
            assert time_utils.is_strictly_before(s.get("timestamp"), cutoff_timestamp)

        return original_train(prior_samples, cutoff_timestamp, sport=sport)

    monkeypatch.setattr(calibration, "train_walk_forward_calibrator", instrumented_train)

    raw_fixtures = [
        {
            "fixture": {"id": 300 + i, "date": f"2024-01-{i:02d}T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024, "name": "Premier League"},
            "teams": {"home": {"id": 10, "name": "Team A"}, "away": {"id": 20, "name": "Team B"}},
            "goals": {"home": 1, "away": 0},
        }
        for i in range(1, 25)
    ]

    storage.save_historical_fixtures(raw_fixtures, league_id=39, season=2024)
    storage.mark_historical_dataset_complete(
        league_id=39,
        season=2024,
        fixture_count=24,
        expected_pages=1,
        pages_completed=1,
        acquisition_complete=True,
    )

    res = backtest.run_real_backtest(league_id=39, season=2024, sample_size=5, min_prior_matches=1)

    assert len(observed_calibrations) > 0

    # Prove identity exclusion for each target fixture in backtest log
    for entry in res["log"]:
        target_fid = entry["fixture_id"]
        target_date = entry["date"]
        for cutoff, sample_ids, timestamps in observed_calibrations:
            if cutoff == target_date:
                assert target_fid not in sample_ids, f"Target fixture ID {target_fid} must NOT be present in calibration history at cutoff {cutoff}"
                for ts in timestamps:
                    assert time_utils.is_strictly_before(ts, target_date), f"Sample timestamp {ts} must be strictly before cutoff {target_date}"
