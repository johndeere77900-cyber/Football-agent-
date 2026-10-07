import pytest
from unittest.mock import patch, MagicMock
import storage

def test_save_prediction_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "is_neon", lambda: False)
    monkeypatch.setattr(storage.config, "DB_PATH", str(tmp_path / "test.db"))
    storage.init_db()

    snap_id = storage.save_prediction_snapshot(
        fixture_id=101,
        prediction_timestamp="2026-03-30T12:00:00Z",
        kickoff_at="2026-03-30T15:00:00Z",
        sport="football",
        league_id=39,
        season=2024,
        home_team="Arsenal",
        away_team="Chelsea",
        prediction_context="PRE_MATCH",
        model_version="v3.0.0",
        feature_version="v3.0.0",
        calibration_version="v3.0.0",
        data_cutoff_timestamp="2026-03-30T11:59:59Z",
        markets={"match_result": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2}},
        quality_gate="PASS",
    )

    assert snap_id is not None and snap_id > 0

    # Test that multiple snapshots for the same fixture_id do NOT cause a unique constraint violation
    snap_id2 = storage.save_prediction_snapshot(
        fixture_id=101,
        prediction_timestamp="2026-03-30T14:00:00Z",
        kickoff_at="2026-03-30T15:00:00Z",
        sport="football",
        league_id=39,
        season=2024,
        home_team="Arsenal",
        away_team="Chelsea",
        prediction_context="LIVE",
        model_version="v3.0.0",
        feature_version="v3.0.0",
        calibration_version="v3.0.0",
        data_cutoff_timestamp="2026-03-30T13:59:59Z",
        markets={"match_result": {"home_win": 0.7, "draw": 0.2, "away_win": 0.1}},
        quality_gate="PASS",
    )

    assert snap_id2 is not None and snap_id2 > snap_id
