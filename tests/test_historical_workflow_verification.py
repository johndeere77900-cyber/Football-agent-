"""
Tests for historical workflow season selection and post-acquisition dataset verification.
"""

import os
import subprocess
import pytest
from unittest.mock import patch, MagicMock

import config
import storage
import historical_sync


@pytest.fixture(autouse=True)
def init_test_db(tmp_path, monkeypatch):
    db_file = tmp_path / "verification_test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    storage.init_db()


def test_verify_persisted_historical_datasets_football_and_basketball_pass(tmp_path):
    # Populate a valid football dataset (39, 2024)
    valid_fixture = {
        "fixture": {"id": 10001, "date": "2024-09-15T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024, "name": "Premier League"},
        "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
        "goals": {"home": 2, "away": 1},
    }
    storage.save_historical_fixtures([valid_fixture], 39, 2024)

    # Populate a valid basketball dataset (12, 2024)
    valid_game = {
        "id": 20001,
        "date": "2024-10-20T19:00:00+00:00",
        "league": {"id": 12, "season": 2024},
        "teams": {"home": {"id": 101, "name": "Lakers"}, "away": {"id": 102, "name": "Celtics"}},
        "scores": {"home": {"total": 110}, "away": {"total": 105}},
        "status": {"short": "FT"},
    }
    storage.save_historical_basketball_games([valid_game], 12, 2024)

    storage.mark_historical_dataset_incomplete(league_id=39, season=2024, fixture_count=1, sport="football")
    storage.mark_historical_dataset_incomplete(league_id=12, season=2024, fixture_count=1, sport="basketball")

    res_fb = historical_sync.verify_persisted_historical_datasets(sport="football", league_id=39, season=2024)
    assert res_fb["success"] is True
    assert res_fb["verified_count"] == 1
    assert res_fb["failed_count"] == 0

    res_bb = historical_sync.verify_persisted_historical_datasets(sport="basketball", league_id=12, season=2024)
    assert res_bb["success"] is True
    assert res_bb["verified_count"] == 1
    assert res_bb["failed_count"] == 0


def test_verify_persisted_historical_datasets_detects_count_mismatch():
    valid_fixture = {
        "fixture": {"id": 10001, "date": "2024-09-15T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024, "name": "Premier League"},
        "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
        "goals": {"home": 2, "away": 1},
    }
    storage.save_historical_fixtures([valid_fixture], 39, 2024)

    # Set manifest count to 999 (mismatch against actual stored 1)
    storage.mark_historical_dataset_incomplete(league_id=39, season=2024, fixture_count=999, sport="football")

    res = historical_sync.verify_persisted_historical_datasets(sport="football", league_id=39, season=2024)
    assert res["success"] is False
    assert res["failed_count"] == 1
    assert any("count_mismatch" in str(iss) for iss in res["results"][0]["issues"])


def test_workflow_default_seasons_queue_input_parsing():
    # Test bash parsing logic matching .github/workflows/historical.yml
    bash_cmd = """
    RAW_SEASONS=""
    RAW_SEASON=""
    RUN_QUEUE="true"

    if [ -n "$RAW_SEASONS" ]; then
      SELECTED_INPUT="$RAW_SEASONS"
    elif [ -n "$RAW_SEASON" ]; then
      SELECTED_INPUT="$RAW_SEASON"
    elif [ "$RUN_QUEUE" = "true" ]; then
      SELECTED_INPUT="2020 2021 2022 2023 2024 2025 2026"
    else
      SELECTED_INPUT="2024"
    fi

    echo "$SELECTED_INPUT"
    """
    proc = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True, check=True)
    parsed = proc.stdout.strip()
    assert parsed == "2020 2021 2022 2023 2024 2025 2026"
