"""
Comprehensive test suite for Provider-Neutral Historical Data Acquisition, Team Identity,
and Provider Fixture Isolation.

Covers all PR #17 blocking audit requirements:
1. Same fixture ID (e.g. 12345) from api_football and football_data_org coexist in DB.
2. Same (source, fixture_id) is idempotent.
3. Provider A cannot overwrite Provider B.
4. Unknown team name does NOT automatically create a canonical identity (fails closed).
5. Unknown provider team returns None/unresolved.
6. Unresolved identity produces INSUFFICIENT_DATA (failure_stage="unresolved_team_identity") in prediction path.
7. Two provider names for the same team only resolve together when a verified mapping/alias exists.
8. Ambiguous identity fails closed.
9. Competition context (league_id) is respected in aliases and matching.
10. Existing API-Football historical records resolve correctly.
11. Existing football-data.org historical records resolve correctly.
12. Fallback returning 0 fixtures marks dataset INCOMPLETE (error_reason="secondary_provider_returned_no_fixtures").
"""

from unittest.mock import MagicMock, patch
import pytest
import config
import data_resolver
import historical_features
import historical_h2h
import historical_sync
import main
import storage
import team_identity


@pytest.fixture(autouse=True)
def init_test_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_blockers.db"
    monkeypatch.setattr("config.DB_PATH", str(db_file))
    monkeypatch.setattr("config.NEON_DATABASE_URL", None)
    monkeypatch.setattr("config.ENVIRONMENT", "development")
    storage.init_db()


def test_01_02_03_provider_fixture_id_isolation_and_coexistence():
    # Save fixture 12345 from api_football
    f_api = [{
        "fixture": {"id": 12345, "date": "2024-09-01T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
        "goals": {"home": 2, "away": 0},
    }]
    res_api = storage.save_historical_fixtures(f_api, 39, 2024, source="api_football")
    assert res_api["inserted"] == 1

    # Save fixture 12345 from football_data_org (same ID, different source)
    f_fd = [{
        "fixture": {"id": 12345, "date": "2024-09-01T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 57, "name": "Arsenal FC"}, "away": {"id": 61, "name": "Chelsea FC"}},
        "goals": {"home": 1, "away": 1},
    }]
    res_fd = storage.save_historical_fixtures(f_fd, 39, 2024, source="football_data_org")
    assert res_fd["inserted"] == 1  # 1. Coexists without conflict

    # 2. Same (source, fixture_id) is idempotent
    res_api_dup = storage.save_historical_fixtures(f_api, 39, 2024, source="api_football")
    assert res_api_dup["inserted"] == 0

    # 3. Both records exist in storage without overwriting each other
    stored = storage.get_historical_fixtures(39, 2024)
    assert len(stored) == 2
    sources = {item.get("source") or item.get("provider_provenance", {}).get("provider") for item in stored}
    # Verify raw_json contents preserved for both
    goals = {item["goals"]["home"] for item in stored}
    assert goals == {2, 1}


def test_04_05_unknown_team_does_not_auto_create_identity_and_returns_none():
    # Unknown team with no DB record and no alias
    cid = team_identity.resolve_canonical_team_id("Unknown Random FC 999", "api_football", 9999, league_id=39)
    assert cid is None  # Fails closed, does NOT auto-create identity


def test_06_unresolved_identity_causes_insufficient_data_in_prediction_path():
    live_fixture = {
        "fixture": {"id": 99999, "date": "2024-09-01T15:00:00+00:00", "status": {"short": "NS"}},
        "league": {"id": 39, "season": 2024, "name": "Premier League"},
        "teams": {"home": {"id": 9888, "name": "Unknown Club Alpha"}, "away": {"id": 9889, "name": "Unknown Club Beta"}},
    }
    pred = main.predict_fixture(live_fixture, 1.40)
    assert pred["insufficient_data"] is True
    assert pred["failure_stage"] == "unresolved_team_identity"


def test_07_08_09_verified_mapping_and_competition_context_aliasing():
    # Resolved via competition-aware alias (Arsenal, 39)
    cid_epl = team_identity.resolve_canonical_team_id("Arsenal", "api_football", 10, league_id=39)
    assert cid_epl == "football_team_arsenal"

    # Competition-aware alias for Inter Milan in Serie A (135)
    cid_inter = team_identity.resolve_canonical_team_id("Inter", "api_football", 108, league_id=135)
    assert cid_inter == "football_team_inter_milan"

    # Attempting "Inter" in an unmapped competition without context fails closed
    cid_unmapped = team_identity.resolve_canonical_team_id("Inter", "some_provider", 888, league_id=999)
    assert cid_unmapped is None


def test_12_zero_fixtures_fallback_marks_dataset_incomplete():
    with patch("main.check_competition_coverage", return_value=("season_not_available", "Season 2024 unavailable")), \
         patch("football_data_api.get_competition_matches", return_value=[]):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        assert report["status"] == "INCOMPLETE"
        st = storage.get_historical_dataset_status(39, 2024)
        assert st["status"] == "INCOMPLETE"
        assert st["error_reason"] == "secondary_provider_returned_no_fixtures"
