"""
Production Integration & End-to-End Backtest Test Suite.

Tests:
- Production prediction contract -> storage persistence without default fallbacks
- Live prediction path consistency & PRE_MATCH vs LIVE isolation
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
import main
import market_analysis
import prediction_contract
import prediction_engine
import quality_gate
import storage
import time_utils


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
# 9. ACTUAL BACKTEST CALIBRATION ORDER TEST
# ============================================================================

def test_backtest_actual_calibration_order(tmp_path, monkeypatch):
    """Instrument calibration.train_walk_forward_calibrator to prove target fixture T is excluded from calibration observations."""
    db_file = tmp_path / "e2e_calib_order.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    storage.init_db()

    observed_calibrator_call_cutoffs = []

    original_train = calibration.train_walk_forward_calibrator

    def instrumented_train(prior_samples, cutoff_timestamp, sport="football"):
        observed_calibrator_call_cutoffs.append(cutoff_timestamp)
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

    backtest.run_real_backtest(league_id=39, season=2024, sample_size=5, min_prior_matches=1)

    assert len(observed_calibrator_call_cutoffs) > 0


# ============================================================================
# 4. ACTUAL PRODUCTION PRE-MATCH AND LIVE ROUTES INTEGRATION TESTS
# ============================================================================

def test_production_football_pre_match_route_e2e(tmp_path, monkeypatch):
    """Exercise main.predict_fixture -> prediction_engine -> storage and assert stored Phase 3 metadata."""
    db_file = tmp_path / "prod_pre_match.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    storage.init_db()

    fixture_item = {
        "fixture": {"id": 7001, "date": "2025-01-10T15:00:00+00:00", "status": {"short": "NS"}},
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

    monkeypatch.setattr(api_football, "get_team_statistics", lambda tid, lid, ssn: mock_stats)
    monkeypatch.setattr(api_football, "get_recent_form", lambda tid, last=8: [{"teams": {"home": {"id": tid}, "away": {"id": 99}}, "goals": {"home": 2, "away": 0}}])
    monkeypatch.setattr(api_football, "get_head_to_head", lambda h, a, last=6: [])

    # Call production predict_fixture
    prediction = main.predict_fixture(fixture_item, league_avg_goals=1.4)
    assert not prediction["insufficient_data"]

    # Save via production storage path
    storage.save_prediction(
        fixture_id=prediction["fixture_id"],
        match_date=prediction["date"],
        home_team=prediction["home_team"],
        away_team=prediction["away_team"],
        league=prediction["league"],
        markets=prediction["markets"],
        confidence=prediction["confidence"],
        home_team_id=prediction["home_team_id"],
        away_team_id=prediction["away_team_id"],
        prediction_context="PRE_MATCH",
        prediction_record=prediction["prediction_record"],
    )

    # Retrieve from database and verify Phase 3 contract metadata
    conn, db_type = storage._connect()
    try:
        row = conn.execute(
            """
            SELECT model_version, feature_version, calibration_version, quality_gate, prediction_context
            FROM predictions WHERE fixture_id = ?
            """,
            (7001,),
        ).fetchone()

        assert row is not None
        assert row[0] == config.MODEL_VERSION
        assert row[1] == config.FEATURE_VERSION
        assert row[2] == config.CALIBRATION_VERSION
        assert row[3] == prediction["prediction_record"]["quality_gate"]
        assert row[4] == "PRE_MATCH"
    finally:
        conn.close()


def test_production_football_live_route_e2e(tmp_path, monkeypatch):
    """Exercise main.predict_fixture with live match status and verify storage persistence as LIVE context."""
    db_file = tmp_path / "prod_live.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    storage.init_db()

    fixture_item = {
        "fixture": {"id": 7002, "date": "2025-01-10T15:00:00+00:00", "status": {"short": "1H", "elapsed": 25}},
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

    monkeypatch.setattr(api_football, "get_team_statistics", lambda tid, lid, ssn: mock_stats)
    monkeypatch.setattr(api_football, "get_recent_form", lambda tid, last=8: [{"teams": {"home": {"id": tid}, "away": {"id": 99}}, "goals": {"home": 2, "away": 0}}])
    monkeypatch.setattr(api_football, "get_head_to_head", lambda h, a, last=6: [])

    prediction = main.predict_fixture(fixture_item, league_avg_goals=1.4)
    assert prediction["is_live"] is True

    prediction_context = "LIVE" if prediction.get("is_live") else "PRE_MATCH"
    storage.save_prediction(
        fixture_id=prediction["fixture_id"],
        match_date=prediction["date"],
        home_team=prediction["home_team"],
        away_team=prediction["away_team"],
        league=prediction["league"],
        markets=prediction["markets"],
        confidence=prediction["confidence"],
        home_team_id=prediction["home_team_id"],
        away_team_id=prediction["away_team_id"],
        prediction_context=prediction_context,
        prediction_record=prediction["prediction_record"],
    )

    conn, _ = storage._connect()
    try:
        row = conn.execute("SELECT prediction_context, model_version FROM predictions WHERE fixture_id = ?", (7002,)).fetchone()
        assert row is not None
        assert row[0] == "LIVE"
        assert row[1] == config.MODEL_VERSION
    finally:
        conn.close()


def test_production_basketball_route_e2e(tmp_path, monkeypatch):
    """Exercise basketball_model.predict_game -> storage.save_basketball_prediction and assert stored metadata."""
    db_file = tmp_path / "prod_basketball.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    storage.init_db()

    game = {
        "id": 8001,
        "date": "2026-03-30T20:00:00+00:00",
        "teams": {"home": {"id": 1, "name": "Lakers"}, "away": {"id": 2, "name": "Celtics"}},
        "league": {"id": 12, "season": 2024, "name": "NBA"},
    }
    home_stats = {"matches": 10, "points": {"for": {"average": {"all": 115.0}}, "against": {"average": {"all": 105.0}}}}
    away_stats = {"matches": 10, "points": {"for": {"average": {"all": 110.0}}, "against": {"average": {"all": 110.0}}}}

    contract = basketball_model.predict_game(game, home_stats_override=home_stats, away_stats_override=away_stats)

    storage.save_basketball_prediction(
        game_id=contract["game_id"],
        game_date=contract.get("data_cutoff_timestamp") or game["date"],
        home_team=contract["home_team"],
        away_team=contract["away_team"],
        league=contract["league"],
        markets=contract["markets"],
        confidence=contract["confidence"],
        prediction_context="PRE_MATCH",
        prediction_record=contract,
    )

    conn, _ = storage._connect()
    try:
        row = conn.execute("SELECT model_version, feature_version, calibration_version, quality_gate FROM basketball_predictions WHERE game_id = ?", (8001,)).fetchone()
        assert row is not None
        assert row[0] == config.MODEL_VERSION
        assert row[1] == config.FEATURE_VERSION
        assert row[2] == config.CALIBRATION_VERSION
        assert row[3] == contract["quality_gate"]
    finally:
        conn.close()
