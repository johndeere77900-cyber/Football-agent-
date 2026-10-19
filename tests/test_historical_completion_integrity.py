"""
Unit tests for historical dataset completion revalidation integrity (Phase 1).
"""

from unittest.mock import patch
import pytest

import config
import data_resolver
import historical_sync
import storage
import team_identity


@pytest.fixture(autouse=True)
def init_test_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_historical_completion_integrity.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()


@pytest.fixture(autouse=True)
def mock_team_universe(monkeypatch):
    def _mock_get_team_ids(league_id, season, *args, **kwargs):
        if league_id in (39, 140, 135, 78, 2):
            raw_ids = list(range(1, 21))
        elif league_id in (61, 88, 94):
            raw_ids = list(range(1, 19))
        else:
            return []
        canonical_ids = []
        for tid in raw_ids:
            cid = team_identity.bootstrap_historical_team_identity(
                f"Team {tid}", "api_football", tid, league_id=league_id
            )
            if cid:
                canonical_ids.append(cid)
        return canonical_ids
    monkeypatch.setattr("data_resolver._get_authoritative_historical_team_ids", _mock_get_team_ids)


def make_valid_complete_fixtures(league_id=39, season=2024, num_teams=20):
    teams = list(range(1, num_teams + 1))
    pairings = [(h, a) for h in teams for a in teams if h != a]
    fixtures = []
    for i, (h_id, a_id) in enumerate(pairings):
        c_home = team_identity.bootstrap_historical_team_identity(f"Team {h_id}", "api_football", h_id, league_id=league_id)
        c_away = team_identity.bootstrap_historical_team_identity(f"Team {a_id}", "api_football", a_id, league_id=league_id)
        fixtures.append({
            "fixture": {
                "id": 1000 + i,
                "date": "2024-08-17T15:00:00Z",
                "status": {"short": "FT", "long": "Match Finished"},
            },
            "league": {"id": league_id, "season": season, "name": f"League {league_id}"},
            "teams": {
                "home": {"id": h_id, "name": f"Team {h_id}"},
                "away": {"id": a_id, "name": f"Team {a_id}"},
            },
            "canonical_home_id": c_home,
            "canonical_away_id": c_away,
            "goals": {"home": 2, "away": 1},
            "provider_provenance": {
                "provider": "api_football",
                "provider_type": "primary",
                "provider_fixture_id": 1000 + i,
            },
        })
    return fixtures


@patch("data_resolver.DataResolver.get_league_fixtures_page")
def test_1_valid_complete_dataset_skips_acquisition(mock_page):
    """
    1. A genuinely valid COMPLETE football dataset skips acquisition and consumes zero API requests.
    """
    fixtures = make_valid_complete_fixtures(league_id=39, season=2024, num_teams=20)
    storage.save_historical_fixtures(fixtures, 39, 2024, source="api_football", require_completed=True)
    storage.mark_historical_dataset_complete(39, 2024, fixture_count=380, source="api_football", expected_pages=2, pages_completed=2)

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "COMPLETE"
    assert report["api_requests_consumed"] == 0
    assert mock_page.call_count == 0


@patch("data_resolver.DataResolver.get_league_fixtures_page")
def test_2_complete_manifest_with_missing_fixtures_not_trusted(mock_page):
    """
    2. A COMPLETE football manifest with missing persisted fixtures is not trusted.
    """
    # Store only 10 fixtures out of 380, but mark as COMPLETE
    fixtures = make_valid_complete_fixtures(league_id=39, season=2024, num_teams=20)[:10]
    storage.save_historical_fixtures(fixtures, 39, 2024, source="api_football", require_completed=True)
    storage.mark_historical_dataset_complete(39, 2024, fixture_count=380, source="api_football", expected_pages=2, pages_completed=2)

    mock_page.return_value = {
        "fixtures": [],
        "expected_pages": 2,
        "current_page": 1,
        "secondary_failed": True,
    }

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"
    assert mock_page.call_count >= 1


@patch("data_resolver.DataResolver.get_league_fixtures_page")
def test_3_complete_manifest_with_wrong_season_or_league_rejected(mock_page):
    """
    3. A COMPLETE football manifest with wrong-season or wrong-competition fixtures is rejected.
    """
    fixtures = make_valid_complete_fixtures(league_id=39, season=2024, num_teams=20)
    # Tamper season in stored fixtures
    for f in fixtures:
        f["league"]["season"] = 2023

    storage.save_historical_fixtures(fixtures, 39, 2024, source="api_football", require_completed=True)
    storage.mark_historical_dataset_complete(39, 2024, fixture_count=380, source="api_football", expected_pages=2, pages_completed=2)

    mock_page.return_value = {
        "fixtures": [],
        "expected_pages": 2,
        "current_page": 1,
        "secondary_failed": True,
    }

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"


@patch("data_resolver.DataResolver.get_league_fixtures_page")
def test_4_complete_manifest_with_duplicate_identities_rejected(mock_page):
    """
    4. A COMPLETE football manifest with duplicate or conflicting fixture identities is rejected.
    """
    fixtures = make_valid_complete_fixtures(league_id=39, season=2024, num_teams=20)
    # Duplicate fixture ID 1000
    fixtures[1]["fixture"]["id"] = 1000

    storage.save_historical_fixtures(fixtures, 39, 2024, source="api_football", require_completed=True)
    storage.mark_historical_dataset_complete(39, 2024, fixture_count=380, source="api_football", expected_pages=2, pages_completed=2)

    mock_page.return_value = {
        "fixtures": [],
        "expected_pages": 2,
        "current_page": 1,
        "secondary_failed": True,
    }

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"


@patch("basketball_api.get_league_games_page")
def test_5_complete_basketball_manifest_with_invalid_persisted_data(mock_page):
    """
    5. A COMPLETE basketball manifest with invalid persisted data is not trusted.
    """
    storage.mark_historical_dataset_complete(12, 2024, fixture_count=100, sport="basketball", expected_pages=2, pages_completed=2)

    mock_page.return_value = {
        "games": [],
        "expected_pages": 1,
        "current_page": 1,
    }

    report = historical_sync.sync_historical_basketball_games(league_id=12, season=2024)

    assert report["status"] == "INCOMPLETE"


@patch("data_resolver.DataResolver.get_league_fixtures_page")
def test_6_quota_exhaustion_cannot_promote_invalid_dataset_to_complete(mock_page, monkeypatch):
    """
    6. Quota exhaustion cannot turn an invalid persisted dataset into COMPLETE.
    """
    # 0 stored fixtures, manifest marked COMPLETE
    storage.mark_historical_dataset_complete(39, 2024, fixture_count=380, source="api_football", expected_pages=2, pages_completed=2)

    # Force request count to historical budget limit
    monkeypatch.setattr("storage.get_api_request_count", lambda *args, **kwargs: 100)

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024, historical_budget=50)

    assert report["status"] == "INCOMPLETE"
    assert report["quota_budget_stopped"] is True


def test_7_valid_complete_dataset_remains_idempotent():
    """
    7. A valid COMPLETE dataset remains idempotent and does not refetch.
    """
    fixtures = make_valid_complete_fixtures(league_id=39, season=2024, num_teams=20)
    storage.save_historical_fixtures(fixtures, 39, 2024, source="api_football", require_completed=True)
    storage.mark_historical_dataset_complete(39, 2024, fixture_count=380, source="api_football", expected_pages=2, pages_completed=2)

    with patch("data_resolver.DataResolver.get_league_fixtures_page") as mock_page:
        report1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        report2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report1["status"] == "COMPLETE"
    assert report2["status"] == "COMPLETE"
    assert mock_page.call_count == 0


@patch("data_resolver.DataResolver.get_league_fixtures_page")
def test_8_failed_validation_does_not_erase_existing_records(mock_page):
    """
    8. Failed validation does not erase valid existing records or reset resumable progress.
    """
    fixtures = make_valid_complete_fixtures(league_id=39, season=2024, num_teams=20)[:100]
    storage.save_historical_fixtures(fixtures, 39, 2024, source="api_football", require_completed=True)
    storage.mark_historical_dataset_complete(39, 2024, fixture_count=380, source="api_football", expected_pages=2, pages_completed=2)

    mock_page.return_value = {
        "fixtures": [],
        "expected_pages": 2,
        "current_page": 2,
        "secondary_failed": True,
    }

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"
    stored = storage.get_historical_fixtures(39, 2024)
    assert len(stored) == 100


def test_missing_authoritative_team_universe_cannot_trigger_unbudgeted_repeat_request(monkeypatch):
    """
    Missing authoritative team universe cannot trigger an unbudgeted repeat request.
    """
    called_budget = []

    def _mock_get_team_ids_budget(league_id, season, max_budget=None, *args, **kwargs):
        called_budget.append(max_budget)
        return []

    monkeypatch.setattr("data_resolver._get_authoritative_historical_team_ids", _mock_get_team_ids_budget)

    cov = data_resolver._validate_historical_structural_coverage(
        [], season=2024, provider_metadata={"max_budget": 5, "competition_format": "double_round_robin"}, league_id=39
    )

    assert cov["verified"] is False
    assert cov["reason"] == "authoritative_team_universe_unavailable"
    assert 5 in called_budget


@patch("data_resolver.DataResolver.get_league_fixtures_page")
def test_failed_revalidation_never_returns_complete(mock_page):
    """
    A failed revalidation never returns COMPLETE.
    """
    # Fixtures marked COMPLETE but failing structural revalidation due to missing fixtures
    fixtures = make_valid_complete_fixtures(league_id=39, season=2024, num_teams=20)[:5]
    storage.save_historical_fixtures(fixtures, 39, 2024, source="api_football", require_completed=True)
    storage.mark_historical_dataset_complete(39, 2024, fixture_count=380, source="api_football", expected_pages=2, pages_completed=2)

    mock_page.return_value = {
        "fixtures": [],
        "expected_pages": 2,
        "current_page": 1,
        "secondary_failed": True,
    }

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
    assert report["status"] != "COMPLETE"
    assert report["status"] == "INCOMPLETE"
