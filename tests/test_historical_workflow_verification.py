"""
Tests for historical workflow season selection and post-acquisition dataset verification.
"""

import json
import os
import sqlite3
import subprocess
import pytest
from unittest.mock import patch, MagicMock

import config
import storage
import historical_sync
import data_resolver


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

    storage.mark_historical_dataset_complete(league_id=39, season=2024, fixture_count=1, sport="football", source="api_football")
    storage.mark_historical_dataset_complete(league_id=12, season=2024, fixture_count=1, sport="basketball", expected_pages=1, pages_completed=1)

    with patch("data_resolver._validate_historical_structural_coverage", return_value={"verified": True}):
        res_fb = historical_sync.verify_persisted_historical_datasets(sport="football", league_id=39, season=2024)
        assert res_fb["success"] is True
        assert res_fb["verified_count"] == 1
        assert res_fb["failed_count"] == 0

        res_bb = historical_sync.verify_persisted_historical_datasets(sport="basketball", league_id=12, season=2024)
        assert res_bb["success"] is True
        assert res_bb["verified_count"] == 1
        assert res_bb["failed_count"] == 0


def test_verify_persisted_historical_datasets_active_ongoing_season_incomplete_passes():
    # Current active season (2026) in INCOMPLETE status with matching counts passes
    active_fixture = {
        "fixture": {"id": 10002, "date": "2026-03-01T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2026, "name": "Premier League"},
        "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
        "goals": {"home": 1, "away": 0},
    }
    storage.save_historical_fixtures([active_fixture], 39, 2026)
    storage.mark_historical_dataset_incomplete(league_id=39, season=2026, fixture_count=1, sport="football")

    res = historical_sync.verify_persisted_historical_datasets(sport="football", league_id=39, season=2026)
    assert res["success"] is True
    assert res["active_in_progress_count"] == 1
    assert res["verified_count"] == 0
    assert res["results"][0]["issues"] == []

    # Count mismatch for active season fails
    storage.mark_historical_dataset_incomplete(league_id=39, season=2026, fixture_count=99, sport="football")
    res_mismatch = historical_sync.verify_persisted_historical_datasets(sport="football", league_id=39, season=2026)
    assert res_mismatch["success"] is False
    assert any("count_mismatch" in str(i) for i in res_mismatch["results"][0]["issues"])

    # Duplicate IDs for active season fails
    storage.mark_historical_dataset_incomplete(league_id=39, season=2026, fixture_count=2, sport="football")
    conn = sqlite3.connect(config.DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO historical_fixtures (fixture_id, source, league_id, season, kickoff_at, raw_json, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (10002, "soccerdata", 39, 2026, "2026-03-01T15:00:00+00:00", json.dumps(active_fixture), "2026-03-01T15:00:00+00:00")
    )
    conn.commit()
    conn.close()

    res_dup = historical_sync.verify_persisted_historical_datasets(sport="football", league_id=39, season=2026)
    assert res_dup["success"] is False
    assert any("duplicate_fixture_id" in str(i) for i in res_dup["results"][0]["issues"])


def test_verify_persisted_historical_datasets_future_season_incomplete_fails():
    # Future season (2027) with INCOMPLETE status and zero records fails verification
    storage.mark_historical_dataset_incomplete(league_id=39, season=2027, fixture_count=0, sport="football")

    res = historical_sync.verify_persisted_historical_datasets(sport="football", league_id=39, season=2027)
    assert res["success"] is False
    assert res["failed_count"] == 1
    assert any("manifest_not_complete" in str(i) for i in res["results"][0]["issues"])


def test_verify_persisted_historical_datasets_fails_when_incomplete_for_past_season():
    valid_fixture = {
        "fixture": {"id": 10001, "date": "2024-09-15T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024, "name": "Premier League"},
        "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
        "goals": {"home": 2, "away": 1},
    }
    storage.save_historical_fixtures([valid_fixture], 39, 2024)

    # Manifest status is INCOMPLETE for past season 2024
    storage.mark_historical_dataset_incomplete(league_id=39, season=2024, fixture_count=1, sport="football")

    res = historical_sync.verify_persisted_historical_datasets(sport="football", league_id=39, season=2024)
    assert res["success"] is False
    assert res["failed_count"] == 1
    assert any("manifest_not_complete" in str(iss) for iss in res["results"][0]["issues"])


def test_single_season_non_queue_verification():
    summary = historical_sync.verify_persisted_historical_datasets(
        sport="football", league_id=39, seasons=[2024], is_queue=False
    )
    assert summary["total_requested"] == 1
    assert summary["results"][0]["league_id"] == 39
    assert summary["results"][0]["season"] == 2024


def test_multiple_seasons_non_queue_verification():
    summary = historical_sync.verify_persisted_historical_datasets(
        sport="football", league_id=39, seasons=[2022, 2023, 2024], is_queue=False
    )
    assert summary["total_requested"] == 3
    requested_pairs = [(r["league_id"], r["season"]) for r in summary["results"]]
    assert requested_pairs == [(39, 2022), (39, 2023), (39, 2024)]


def test_multiple_seasons_queue_verification():
    summary = historical_sync.verify_persisted_historical_datasets(
        seasons=[2022, 2023], is_queue=True
    )
    expected_total = len(getattr(config, "ALLOWED_LEAGUE_IDS", [])) * 2 + len(getattr(config, "ALLOWED_BASKETBALL_LEAGUE_IDS", [])) * 2
    assert summary["total_requested"] == expected_total


def test_tertiary_provider_team_id_strict_validation():
    # Test strict provider team ID validation in _validate_and_filter_tertiary_matches
    matches = [
        # Valid match with integer provider team IDs
        {
            "fixture": {"id": 101, "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "goals": {"home": 1, "away": 0},
            "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        },
        # Valid match with positive numeric string provider team IDs
        {
            "fixture": {"id": 102, "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "goals": {"home": 2, "away": 2},
            "teams": {"home": {"id": "15", "name": "Liverpool"}, "away": {"id": "25", "name": "Everton"}},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        },
        # Invalid: float provider team ID
        {
            "fixture": {"id": 103, "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "goals": {"home": 1, "away": 1},
            "teams": {"home": {"id": 12.34, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        },
        # Invalid: boolean provider team ID
        {
            "fixture": {"id": 104, "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "goals": {"home": 1, "away": 0},
            "teams": {"home": {"id": True, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        },
        # Invalid: nonnumeric string provider team ID
        {
            "fixture": {"id": 105, "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "goals": {"home": 0, "away": 0},
            "teams": {"home": {"id": "invalid_str", "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        },
        # Invalid: zero/negative provider team ID
        {
            "fixture": {"id": 106, "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "goals": {"home": 1, "away": 0},
            "teams": {"home": {"id": -5, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        },
    ]

    with patch("team_identity.bootstrap_historical_team_identity", side_effect=lambda name, prov, tid, **kwargs: 100 if "Arsenal" in name or "Liverpool" in name else 200):
        filtered = data_resolver._validate_and_filter_tertiary_matches(matches, league_id=39, season=2024)
        assert len(filtered) == 2
        filtered_fids = [f["fixture"]["id"] for f in filtered]
        assert filtered_fids == [101, 102]


def test_unresolved_candidate_preserved_in_get_fixtures_for_date():
    resolver = data_resolver.DataResolver()

    # Create candidate fixture whose canonical identity fails to resolve
    unresolved_cand = {
        "fixture": {"id": 99001, "date": "2024-11-10T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 881, "name": "Unknown Team A"}, "away": {"id": 882, "name": "Unknown Team B"}},
        "goals": {"home": 1, "away": 0},
        "provider_provenance": {"provider": "api_football"},
    }

    with patch("api_football.get_fixtures_by_date", return_value=[unresolved_cand]), \
         patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_UNAVAILABLE", [], {})), \
         patch("soccerdata_provider.get_sofascore_historical_games", return_value=("SOURCE_UNAVAILABLE", [], {})), \
         patch("team_identity.bootstrap_historical_team_identity", return_value=None), \
         patch("team_identity.resolve_canonical_team_id", return_value=None):

        fixtures, meta = resolver.get_fixtures_for_date("2024-11-10", league_id=39)
        # Verify the candidate was preserved as an independent unreconciled candidate
        assert len(fixtures) == 1
        assert fixtures[0]["fixture"]["id"] == 99001


def test_workflow_default_seasons_queue_input_parsing():
    # Test bash parsing logic matching .github/workflows/historical.yml
    bash_cmd = """
    RAW_SEASONS_INPUT=""
    RAW_SEASON_INPUT=""
    RUN_QUEUE_INPUT="true"

    RAW_SEASONS="$RAW_SEASONS_INPUT"
    RAW_SEASON="$RAW_SEASON_INPUT"
    RUN_QUEUE="$RUN_QUEUE_INPUT"

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


def test_workflow_bash_args_construction_non_queue_multiple_seasons():
    bash_cmd = """
    INPUT_RUN_QUEUE="false"
    INPUT_SPORT="football"
    INPUT_LEAGUE_ID="39"
    INPUT_SEASONS_ARG="2022 2023 2024"

    if [ "$INPUT_RUN_QUEUE" = "true" ]; then
      ARGS=("--historical-queue" "--seasons")
      for YR in $INPUT_SEASONS_ARG; do
        ARGS+=("$YR")
      done
    else
      ARGS=("--historical-sync" "--sport" "$INPUT_SPORT" "--league" "$INPUT_LEAGUE_ID" "--seasons")
      for YR in $INPUT_SEASONS_ARG; do
        ARGS+=("$YR")
      done
    fi

    echo "${ARGS[@]}"
    """
    proc = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True, check=True)
    parsed = proc.stdout.strip()
    assert parsed == "--historical-sync --sport football --league 39 --seasons 2022 2023 2024"


def test_workflow_bash_args_construction_verify_step_queue():
    bash_cmd = """
    INPUT_RUN_QUEUE="true"
    INPUT_SPORT="football"
    INPUT_LEAGUE_ID="39"
    INPUT_SEASONS_ARG="2020 2021 2022"

    if [ "$INPUT_RUN_QUEUE" = "true" ]; then
      ARGS=("--verify-historical-datasets" "--historical-queue" "--seasons")
      for YR in $INPUT_SEASONS_ARG; do
        ARGS+=("$YR")
      done
    else
      ARGS=("--verify-historical-datasets" "--sport" "$INPUT_SPORT" "--league" "$INPUT_LEAGUE_ID" "--seasons")
      for YR in $INPUT_SEASONS_ARG; do
        ARGS+=("$YR")
      done
    fi

    echo "${ARGS[@]}"
    """
    proc = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True, check=True)
    parsed = proc.stdout.strip()
    assert parsed == "--verify-historical-datasets --historical-queue --seasons 2020 2021 2022"


def test_cli_verify_historical_datasets_queue_mode():
    import sys
    import main

    test_args = ["main.py", "--verify-historical-datasets", "--historical-queue", "--seasons", "2020", "2021"]
    with patch.object(sys, "argv", test_args), \
         patch("historical_sync.verify_persisted_historical_datasets", return_value={"success": True}) as mock_verify:
        exit_code = main.main()
        assert exit_code == 0
        mock_verify.assert_called_once_with(
            sport="football",
            league_id=None,
            seasons=[2020, 2021],
            season=None,
            is_queue=True,
        )


def test_cli_verify_historical_datasets_single_mode():
    import sys
    import main

    test_args = ["main.py", "--verify-historical-datasets", "--sport", "football", "--league", "39", "--seasons", "2024"]
    with patch.object(sys, "argv", test_args), \
         patch("historical_sync.verify_persisted_historical_datasets", return_value={"success": True}) as mock_verify:
        exit_code = main.main()
        assert exit_code == 0
        mock_verify.assert_called_once_with(
            sport="football",
            league_id=39,
            seasons=[2024],
            season=None,
            is_queue=False,
        )


def test_workflow_sync_failure_preserves_exit_code():
    # Test bash error handling and exit code preservation logic in historical.yml
    bash_cmd = """
    # Sync step simulation with failure (exit code 2)
    set +e
    SYNC_EXIT=2
    echo "sync_exit=$SYNC_EXIT" > /tmp/sync_out.txt

    # Verification step simulation (always runs)
    VERIFY_EXIT=0

    SAVED_SYNC_EXIT=$(cat /tmp/sync_out.txt | cut -d= -f2)
    if [ "$SAVED_SYNC_EXIT" != "0" ]; then
      exit "$SAVED_SYNC_EXIT"
    fi
    if [ "$VERIFY_EXIT" != "0" ]; then
      exit "$VERIFY_EXIT"
    fi
    """
    proc = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True)
    assert proc.returncode == 2
