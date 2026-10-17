import pytest
from unittest.mock import patch, MagicMock
import config
import storage
import historical_sync
import team_identity


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "predictions.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    storage.init_db()
    return db_path


@pytest.fixture(autouse=True)
def mock_team_universe(monkeypatch):
    import team_identity
    cid1 = team_identity.bootstrap_historical_team_identity("Team A", "api_football", 1)
    cid2 = team_identity.bootstrap_historical_team_identity("Team B", "api_football", 2)
    def _mock_get_team_ids(league_id, season, *args, **kwargs):
        return [cid1 or "football_team_team_a", cid2 or "football_team_team_b"]
    monkeypatch.setattr("data_resolver._get_authoritative_historical_team_ids", _mock_get_team_ids)


def test_historical_sync_saves_fixtures(temp_db, monkeypatch):
    fixtures_sample = [
        {
            "fixture": {"id": 5001, "date": "2025-01-10T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {
                "home": {"id": 1, "name": "Team A"},
                "away": {"id": 2, "name": "Team B"},
            },
            "goals": {"home": 2, "away": 1},
        },
        {
            "fixture": {"id": 5002, "date": "2025-01-17T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {
                "home": {"id": 2, "name": "Team B"},
                "away": {"id": 1, "name": "Team A"},
            },
            "goals": {"home": 0, "away": 0},
        },
    ]

    meta_return = {
        "fixtures": fixtures_sample,
        "page": 1,
        "expected_pages": 1,
    }

    with patch("api_football.get_league_fixtures_page", return_value=meta_return), \
         patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[]), \
         patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_NOT_AVAILABLE", [], {})), \
         patch("soccerdata_provider.get_sofascore_historical_games", return_value=("SOURCE_NOT_AVAILABLE", [], {})):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["league_id"] == 39
    assert report["season"] == 2024
    assert report["existing_before"] == 0
    assert report["fixtures_received"] == 2
    assert report["newly_stored"] == 2
    assert report["final_stored_count"] == 2
    assert report["status"] == "COMPLETE"

    # Verify database contents
    stored = storage.get_historical_fixtures(39, 2024)
    assert len(stored) == 2


def test_historical_sync_repeated_runs_are_idempotent(temp_db):
    fixtures_sample = [
        {
            "fixture": {"id": 5001, "date": "2025-01-10T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
            "goals": {"home": 2, "away": 1},
        },
        {
            "fixture": {"id": 5002, "date": "2025-01-17T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 2, "name": "Team B"}, "away": {"id": 1, "name": "Team A"}},
            "goals": {"home": 0, "away": 0},
        },
    ]

    meta_return = {
        "fixtures": fixtures_sample,
        "page": 1,
        "expected_pages": 1,
    }

    with patch("api_football.get_league_fixtures_page", return_value=meta_return) as mock_get, \
         patch("api_football.get_league_standings") as mock_standings, \
         patch("football_data_api.get_competition_teams") as mock_fd_teams, \
         patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[]), \
         patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_NOT_AVAILABLE", [], {})), \
         patch("soccerdata_provider.get_sofascore_historical_games", return_value=("SOURCE_NOT_AVAILABLE", [], {})):
        report1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

        # Reset mocks after run 1
        mock_get.reset_mock()
        mock_standings.reset_mock()
        mock_fd_teams.reset_mock()

        # Run 2 on COMPLETE dataset must trigger ZERO network requests across all providers and helpers
        report2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

        mock_get.assert_not_called()
        mock_standings.assert_not_called()
        mock_fd_teams.assert_not_called()

    assert report1["newly_stored"] == 2
    assert report1["status"] == "COMPLETE"
    assert report2["status"] == "COMPLETE"
    assert report2["api_requests_consumed"] == 0
    assert report2["final_stored_count"] == 2


def test_historical_sync_refresh_forces_reacquisition(temp_db):
    fixtures_sample = [
        {
            "fixture": {"id": 5001, "date": "2025-01-10T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
            "goals": {"home": 2, "away": 1},
        }
    ]

    meta_return = {
        "fixtures": fixtures_sample,
        "page": 1,
        "expected_pages": 1,
    }

    with patch("api_football.get_league_fixtures_page", return_value=meta_return) as mock_get, \
         patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[]):
        report1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        report2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024, refresh=True)
        assert mock_get.call_count == 2

    assert report2["already_existing_skipped"] == 1


def test_historical_sync_respects_quota_budget(temp_db, monkeypatch):
    monkeypatch.setattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 5)

    # Pre-fill api_request_counts to simulate 5 requests already used today
    for i in range(5):
        storage.record_api_request("api_football", "fixtures")

    with patch("api_football.get_league_fixtures_page") as mock_get, \
         patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[]), \
         patch("football_data_api.get_competition_matches", return_value=[]), \
         patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_UNAVAILABLE", [], {})), \
         patch("soccerdata_provider.get_sofascore_historical_games", return_value=("SOURCE_UNAVAILABLE", [], {})):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        mock_get.assert_not_called()

    assert report["quota_budget_stopped"] is True
    assert report["fixtures_received"] == 0


def test_historical_sync_page_by_page_resumability(temp_db):
    page1_fixtures = [
        {
            "fixture": {"id": 6001, "date": "2025-01-01T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
            "goals": {"home": 1, "away": 0},
        }
    ]
    page2_fixtures = [
        {
            "fixture": {"id": 6002, "date": "2025-01-08T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 2, "name": "Team B"}, "away": {"id": 1, "name": "Team A"}},
            "goals": {"home": 2, "away": 2},
        }
    ]

    def mock_page_fetch(league_id, season, page, max_budget=None):
        if page == 1:
            return {"fixtures": page1_fixtures, "page": 1, "expected_pages": 2}
        elif page == 2:
            raise RuntimeError("Network error on page 2")
        raise RuntimeError("Unexpected page")

    # Run 1: Page 1 succeeds, Page 2 fails
    with patch("api_football.get_league_fixtures_page", side_effect=mock_page_fetch), \
         patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[]), \
         patch("football_data_api.get_competition_matches", return_value=[]), \
         patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_UNAVAILABLE", [], {})), \
         patch("soccerdata_provider.get_sofascore_historical_games", return_value=("SOURCE_UNAVAILABLE", [], {})):
        report1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report1["status"] == "INCOMPLETE"
    assert report1["pages_completed"] == 1
    assert report1["expected_pages"] == 2
    assert report1["final_stored_count"] == 1

    # Verify Page 1 fixtures were safely persisted in storage
    stored_after_p1 = storage.get_historical_fixtures(39, 2024)
    assert len(stored_after_p1) == 1
    assert stored_after_p1[0]["fixture"]["id"] == 6001

    # Run 2: Resume sync starting from page 2
    def mock_page_fetch_resume(league_id, season, page, max_budget=None):
        if page == 2:
            return {"fixtures": page2_fixtures, "page": 2, "expected_pages": 2}
        raise RuntimeError(f"Unexpected page fetch for page {page}")

    with patch("api_football.get_league_fixtures_page", side_effect=mock_page_fetch_resume) as mock_resume, \
         patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[]), \
         patch("football_data_api.get_competition_matches", return_value=[]), \
         patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_UNAVAILABLE", [], {})), \
         patch("soccerdata_provider.get_sofascore_historical_games", return_value=("SOURCE_UNAVAILABLE", [], {})):
        report2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report2["status"] == "COMPLETE"
    assert report2["pages_completed"] == 2
    assert report2["final_stored_count"] == 2
    stored_final = storage.get_historical_fixtures(39, 2024)
    assert len(stored_final) == 2


def test_historical_sync_empty_pages_count_persisted_on_resume(temp_db):
    page1_fixtures = []  # empty page!
    page2_fixtures = [
        {
            "fixture": {"id": 7002, "date": "2025-01-08T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 2, "name": "Team B"}, "away": {"id": 3, "name": "Team C"}},
            "goals": {"home": 2, "away": 2},
        }
    ]

    def mock_page_fetch(league_id, season, page, max_budget=None):
        if page == 1:
            return {"fixtures": page1_fixtures, "page": 1, "expected_pages": 2}
        elif page == 2:
            raise RuntimeError("Network error on page 2")
        raise RuntimeError("Unexpected page")

    with patch("api_football.get_league_fixtures_page", side_effect=mock_page_fetch), \
         patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[]), \
         patch("football_data_api.get_competition_matches", return_value=[]), \
         patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_UNAVAILABLE", [], {})), \
         patch("soccerdata_provider.get_sofascore_historical_games", return_value=("SOURCE_UNAVAILABLE", [], {})):
        report1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report1["status"] == "INCOMPLETE"
    status_p1 = storage.get_historical_dataset_status(39, 2024)
    assert status_p1["empty_pages_count"] == 1

    def mock_page_fetch_resume(league_id, season, page, max_budget=None):
        if page == 2:
            return {"fixtures": page2_fixtures, "page": 2, "expected_pages": 2}
        raise RuntimeError(f"Unexpected page fetch for page {page}")

    with patch("api_football.get_league_fixtures_page", side_effect=mock_page_fetch_resume), \
         patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[]), \
         patch("football_data_api.get_competition_matches", return_value=[]), \
         patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_UNAVAILABLE", [], {})), \
         patch("soccerdata_provider.get_sofascore_historical_games", return_value=("SOURCE_UNAVAILABLE", [], {})):
        report2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report2["status"] == "INCOMPLETE"
    status_p2 = storage.get_historical_dataset_status(39, 2024)
    assert status_p2["empty_pages_count"] == 1


def test_sync_historical_fixtures_season_guards(temp_db):
    """
    Verify strict football historical-season guard in sync_historical_fixtures.
    """
    with pytest.raises(ValueError, match="Football historical acquisition only supports seasons"):
        historical_sync.sync_historical_fixtures(league_id=39, season=2019)

    with pytest.raises(ValueError, match="Football historical acquisition only supports seasons"):
        historical_sync.sync_historical_fixtures(league_id=39, season=2027)


def test_unknown_provenance_fails_closed():
    """
    Verify that an unknown, empty, None, or malformed provider provenance fails closed.
    """
    assert historical_sync._is_historical_provider_complete(
        page_source=None,
        page_meta={"provider_metadata": {"structural_coverage": {"verified": True}}},
        valid_fixtures_count=100,
        expected_pages=1,
        pages_completed=1,
        quota_budget_stopped=False,
        acquisition_failed=False,
        rejected_count=0,
        empty_pages_count=0,
        final_stored_count=100,
    ) is False


def test_resume_incomplete_dataset_single_page_retries_page_1(temp_db):
    """
    Regression test: An incomplete dataset with 380 stored fixtures, expected_pages=1,
    pages_completed=1, and acquisition_complete=false must retry from page 1, NOT page 2.
    """
    teams = list(range(1, 21))
    pairings = [(h, a) for h in teams for a in teams if h != a]
    fixtures = []
    for i, (h_id, a_id) in enumerate(pairings):
        c_home = team_identity.bootstrap_historical_team_identity(f"Team {h_id}", "api_football", h_id, league_id=39)
        c_away = team_identity.bootstrap_historical_team_identity(f"Team {a_id}", "api_football", a_id, league_id=39)
        fixtures.append({
            "fixture": {"id": 2000 + i, "date": "2023-09-15T15:00:00Z", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2023, "name": "Premier League"},
            "teams": {"home": {"id": h_id, "name": f"Team {h_id}"}, "away": {"id": a_id, "name": f"Team {a_id}"}},
            "canonical_home_id": c_home,
            "canonical_away_id": c_away,
            "goals": {"home": 2, "away": 1},
            "provider_provenance": {"provider": "api_football"},
        })
    storage.save_historical_fixtures(fixtures, 39, 2023, source="api_football", require_completed=True)

    storage.mark_historical_dataset_incomplete(
        league_id=39,
        season=2023,
        fixture_count=380,
        source="api_football",
        expected_pages=1,
        pages_completed=1,
        acquisition_complete=False,
    )

    meta_return = {
        "fixtures": fixtures,
        "page": 1,
        "expected_pages": 1,
        "provider_metadata": {
            "is_complete": True,
            "structural_coverage": {"verified": True},
        },
    }

    pages_requested = []

    def mock_fetch(league_id, season, page=1, max_budget=None):
        pages_requested.append(page)
        return meta_return

    with patch("api_football.get_league_fixtures_page", side_effect=mock_fetch), \
         patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[]), \
         patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_NOT_AVAILABLE", [], {})), \
         patch("soccerdata_provider.get_sofascore_historical_games", return_value=("SOURCE_NOT_AVAILABLE", [], {})), \
         patch("data_resolver._get_authoritative_historical_team_ids", return_value=list(range(1, 21))):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2023)

    assert pages_requested == [1]
    assert report["status"] == "COMPLETE"
    assert report["acquisition_complete"] is True


def test_resume_incomplete_dataset_stale_expected_pages_retries_page_1(temp_db):
    """
    Regression test: An incomplete dataset with stale expected_pages=2 and pages_completed=1
    must restart acquisition from page 1 to obtain fresh provider metadata rather than skipping page 1.
    """
    teams = list(range(1, 21))
    pairings = [(h, a) for h in teams for a in teams if h != a]
    fixtures = []
    for i, (h_id, a_id) in enumerate(pairings):
        c_home = team_identity.bootstrap_historical_team_identity(f"Team {h_id}", "api_football", h_id, league_id=39)
        c_away = team_identity.bootstrap_historical_team_identity(f"Team {a_id}", "api_football", a_id, league_id=39)
        fixtures.append({
            "fixture": {"id": 3000 + i, "date": "2023-09-15T15:00:00Z", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2023, "name": "Premier League"},
            "teams": {"home": {"id": h_id, "name": f"Team {h_id}"}, "away": {"id": a_id, "name": f"Team {a_id}"}},
            "canonical_home_id": c_home,
            "canonical_away_id": c_away,
            "goals": {"home": 1, "away": 0},
            "provider_provenance": {"provider": "api_football"},
        })
    storage.save_historical_fixtures(fixtures, 39, 2023, source="api_football", require_completed=True)

    # Stale manifest metadata: expected_pages=2, pages_completed=1, acquisition_complete=False
    storage.mark_historical_dataset_incomplete(
        league_id=39,
        season=2023,
        fixture_count=190,
        source="api_football",
        expected_pages=2,
        pages_completed=1,
        acquisition_complete=False,
    )

    meta_return = {
        "fixtures": fixtures,
        "page": 1,
        "expected_pages": 1,
        "provider_metadata": {
            "is_complete": True,
            "structural_coverage": {"verified": True},
        },
    }

    pages_requested = []

    def mock_fetch(league_id, season, page=1, max_budget=None):
        pages_requested.append(page)
        return meta_return

    with patch("api_football.get_league_fixtures_page", side_effect=mock_fetch), \
         patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[]), \
         patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_NOT_AVAILABLE", [], {})), \
         patch("soccerdata_provider.get_sofascore_historical_games", return_value=("SOURCE_NOT_AVAILABLE", [], {})), \
         patch("data_resolver._get_authoritative_historical_team_ids", return_value=list(range(1, 21))):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2023)

    assert pages_requested == [1]
    assert report["status"] == "COMPLETE"
    assert report["acquisition_complete"] is True

    assert historical_sync._is_historical_provider_complete(
        page_source="unsupported_provider",
        page_meta={"provider_metadata": {"structural_coverage": {"verified": True}}},
        valid_fixtures_count=100,
        expected_pages=1,
        pages_completed=1,
        quota_budget_stopped=False,
        acquisition_failed=False,
        rejected_count=0,
        empty_pages_count=0,
        final_stored_count=100,
    ) is False


def test_ongoing_2026_fixtures_handled_by_status():
    """
    Verify that ongoing 2026 season fixtures are handled according to status.
    Unplayed fixtures (NS, PST) are filtered out for historical storage,
    while completed fixtures (FT, AET, PEN) are persisted.
    """
    fixtures_2026 = [
        {
            "fixture": {"id": 202601, "date": "2026-03-01T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2026},
            "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
            "goals": {"home": 2, "away": 1},
        },
        {
            "fixture": {"id": 202602, "date": "2026-04-01T15:00:00+00:00", "status": {"short": "NS"}},
            "league": {"id": 39, "season": 2026},
            "teams": {"home": {"id": 2, "name": "Team B"}, "away": {"id": 1, "name": "Team A"}},
            "goals": {"home": None, "away": None},
        },
    ]

    meta_return = {
        "fixtures": fixtures_2026,
        "page": 1,
        "expected_pages": 1,
    }

    with patch("api_football.get_league_fixtures_page", return_value=meta_return), \
         patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[]):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2026)

    assert report["final_stored_count"] == 1
    stored = storage.get_historical_fixtures(39, 2026)
    assert len(stored) == 1
    assert stored[0]["fixture"]["id"] == 202601


def test_sync_historical_fixtures_unconfigured_league_guard(temp_db):
    """
    Verify unconfigured football league is rejected before acquisition.
    """
    with pytest.raises(ValueError, match="configured leagues"):
        historical_sync.sync_historical_fixtures(league_id=999999, season=2024)


def test_sync_historical_fixtures_unplayed_fixtures_not_persisted(temp_db):
    """
    Verify unplayed/upcoming fixtures from provider response are NOT persisted into historical DB.
    """
    mixed_fixtures = [
        {
            "fixture": {"id": 8001, "date": "2025-01-10T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
            "goals": {"home": 2, "away": 1},
        },
        {
            "fixture": {"id": 8002, "date": "2025-02-10T15:00:00+00:00", "status": {"short": "NS"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 3, "name": "Team C"}},
            "goals": {"home": None, "away": None},
        },
        {
            "fixture": {"id": 8003, "date": "2025-02-15T15:00:00+00:00", "status": {"short": "PST"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 2, "name": "Team B"}, "away": {"id": 3, "name": "Team C"}},
            "goals": {"home": None, "away": None},
        },
        {
            "fixture": {"id": 8004, "date": "2025-01-12T15:00:00+00:00", "status": {"short": "AET"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 3, "name": "Team C"}, "away": {"id": 1, "name": "Team A"}},
            "goals": {"home": 3, "away": 2},
        },
        {
            "fixture": {"id": 8005, "date": "2025-01-14T15:00:00+00:00", "status": {"short": "PEN"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 2, "name": "Team B"}, "away": {"id": 1, "name": "Team A"}},
            "goals": {"home": 1, "away": 1},
        },
    ]

    meta_return = {
        "fixtures": mixed_fixtures,
        "page": 1,
        "expected_pages": 1,
    }

    with patch("api_football.get_league_fixtures_page", return_value=meta_return), \
         patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[]):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["final_stored_count"] == 3
    stored = storage.get_historical_fixtures(39, 2024)
    stored_ids = {f["fixture"]["id"] for f in stored}
    assert stored_ids == {8001, 8004, 8005}
    assert 8002 not in stored_ids
    assert 8003 not in stored_ids
