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
    # Ongoing active season (e.g. 2026) in INCOMPLETE status with matching counts passes
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
    assert res["verified_count"] == 1
    assert res["results"][0]["issues"] == []

    # Count mismatch for active season still fails
    storage.mark_historical_dataset_incomplete(league_id=39, season=2026, fixture_count=99, sport="football")
    res_mismatch = historical_sync.verify_persisted_historical_datasets(sport="football", league_id=39, season=2026)
    assert res_mismatch["success"] is False
    assert any("count_mismatch" in str(i) for i in res_mismatch["results"][0]["issues"])


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
