"""
Historical football sync tests.

Football historical acquisition uses the approved provider order only:
direct Football-Data CSV -> SoccerData FBref -> SoccerData MatchHistory ->
SoccerData Sofascore. API-Football / football-data.org are NOT historical
football fixture providers, so these tests drive acquisition through the direct
CSV provider (and assert that no API provider is consulted).
"""

import pytest
from unittest.mock import patch, MagicMock
import config
import storage
import historical_sync
import data_resolver
import team_identity


# Number of teams in the mocked competition universe. A small, self-consistent
# double round robin keeps the integrity assertions (exact expected coverage)
# meaningful without generating a full 380-fixture league.
MOCK_TEAM_COUNT = 2


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "predictions.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    storage.init_db()
    return db_path


@pytest.fixture(autouse=True)
def mock_team_universe(monkeypatch):
    """
    Provide a deterministic, authoritative team universe for the mocked league.

    The expected league size is patched to match so structural coverage is judged
    against the same authority for both the provider response and the persisted
    dataset.
    """
    cid1 = team_identity.bootstrap_historical_team_identity("Team A", "api_football", 1)
    cid2 = team_identity.bootstrap_historical_team_identity("Team B", "api_football", 2)
    canonical_ids = [cid1 or "football_team_team_a", cid2 or "football_team_team_b"]

    def _mock_get_team_ids(league_id, season, *args, **kwargs):
        return list(canonical_ids)

    monkeypatch.setattr("data_resolver._get_authoritative_historical_team_ids", _mock_get_team_ids)
    monkeypatch.setattr(data_resolver, "get_expected_league_team_count", lambda league_id, season=None: MOCK_TEAM_COUNT)


def make_fixture(fid, h_name, a_name, h_id, a_id, status="FT", season=2024, league_id=39,
                 date="2025-01-10T15:00:00+00:00", h_goals=2, a_goals=1):
    fixture = {
        "fixture": {"id": fid, "date": date, "status": {"short": status, "long": "Finished" if status in ("FT", "AET", "PEN") else "Not Started"}},
        "league": {"id": league_id, "season": season, "name": f"League {league_id}"},
        "teams": {
            "home": {"id": h_id, "name": h_name},
            "away": {"id": a_id, "name": a_name},
        },
        "goals": {"home": h_goals, "away": a_goals},
        "provider_provenance": {"provider": "football_data_csv", "provider_type": "primary"},
    }
    return fixture


def complete_two_team_season(season=2024, league_id=39):
    return [
        make_fixture(5001, "Team A", "Team B", 1, 2, season=season, league_id=league_id, date=f"{season}-09-10T15:00:00+00:00"),
        make_fixture(5002, "Team B", "Team A", 2, 1, season=season, league_id=league_id, date=f"{season + 1}-01-17T15:00:00+00:00"),
    ]


def csv_patch(fixtures, **kwargs):
    """Patch the direct CSV provider (first approved historical provider)."""
    return patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=fixtures, **kwargs)


def no_other_sources():
    """All other approved providers report that they have no data."""
    return [
        patch("soccerdata_provider.get_fbref_games", return_value=("SOURCE_NOT_AVAILABLE", [], {})),
        patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_NOT_AVAILABLE", [], {})),
        patch("soccerdata_provider.get_sofascore_historical_games", return_value=("SOURCE_NOT_AVAILABLE", [], {})),
    ]


def test_historical_sync_saves_fixtures(temp_db):
    fixtures_sample = complete_two_team_season(season=2024)

    with csv_patch(fixtures_sample), \
         patch("api_football.get_league_fixtures_page") as mock_api_fixtures, \
         patch("football_data_api.get_competition_matches") as mock_fd_matches:
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in no_other_sources():
                stack.enter_context(p)
            report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

        # The retired secondary API route must never be consulted.
        mock_api_fixtures.assert_not_called()
        mock_fd_matches.assert_not_called()

    assert report["league_id"] == 39
    assert report["season"] == 2024
    assert report["existing_before"] == 0
    assert report["fixtures_received"] == 2
    assert report["newly_stored"] == 2
    assert report["final_stored_count"] == 2
    assert report["status"] == "COMPLETE"
    assert report["lifecycle_state"] == "COMPLETED"
    assert report["acquisition_state"] == "VALIDATED"

    stored = storage.get_historical_fixtures(39, 2024)
    assert len(stored) == 2


def test_historical_sync_repeated_runs_are_idempotent(temp_db):
    fixtures_sample = complete_two_team_season(season=2024)

    with csv_patch(fixtures_sample) as mock_csv:
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in no_other_sources():
                stack.enter_context(p)
            report1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
            calls_after_run1 = mock_csv.call_count
            report2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report1["newly_stored"] == 2
    assert report1["status"] == "COMPLETE"
    assert report2["status"] == "COMPLETE"
    assert report2["api_requests_consumed"] == 0
    assert report2["newly_stored"] == 0
    assert report2["final_stored_count"] == 2
    # A COMPLETE and independently revalidated dataset is not re-fetched.
    assert mock_csv.call_count == calls_after_run1

    stored = storage.get_historical_fixtures(39, 2024)
    assert len(stored) == 2


def test_historical_sync_refresh_forces_reacquisition(temp_db):
    fixtures_sample = complete_two_team_season(season=2024)

    with csv_patch(fixtures_sample) as mock_csv:
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in no_other_sources():
                stack.enter_context(p)
            report1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
            report2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024, refresh=True)
        assert mock_csv.call_count == 2

    assert report1["status"] == "COMPLETE"
    assert report2["already_existing_skipped"] == 2


def test_sync_historical_fixtures_season_guards(temp_db):
    fixtures_2019 = complete_two_team_season(season=2019)

    with csv_patch(fixtures_2019):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in no_other_sources():
                stack.enter_context(p)
            report = historical_sync.sync_historical_fixtures(league_id=39, season=2019)

    assert report["season"] == 2019
    assert report["newly_stored"] == 2
    assert report["final_stored_count"] == 2

    with pytest.raises(ValueError, match="Football historical acquisition only supports seasons"):
        historical_sync.sync_historical_fixtures(league_id=39, season=2027)


def test_sync_historical_fixtures_unconfigured_league_guard(temp_db):
    with pytest.raises(ValueError, match="configured leagues"):
        historical_sync.sync_historical_fixtures(league_id=999999, season=2024)


def test_unknown_provenance_fails_closed():
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


def test_unplayed_fixtures_not_persisted(temp_db):
    """Unplayed/upcoming fixtures must never be persisted as completed history."""
    mixed_fixtures = [
        make_fixture(8001, "Team A", "Team B", 1, 2, status="FT", date="2025-01-10T15:00:00+00:00"),
        make_fixture(8002, "Team A", "Team B", 1, 2, status="NS", date="2025-02-10T15:00:00+00:00", h_goals=None, a_goals=None),
        make_fixture(8004, "Team B", "Team A", 2, 1, status="AET", date="2025-01-12T15:00:00+00:00", h_goals=3, a_goals=2),
        make_fixture(8005, "Team A", "Team B", 1, 2, status="PEN", date="2025-01-14T15:00:00+00:00", h_goals=1, a_goals=1),
    ]

    with csv_patch(mixed_fixtures):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in no_other_sources():
                stack.enter_context(p)
            report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    stored = storage.get_historical_fixtures(39, 2024)
    stored_ids = {f["fixture"]["id"] for f in stored}
    assert stored_ids == {8001, 8004, 8005}
    assert 8002 not in stored_ids
    assert report["final_stored_count"] == 3


def test_ongoing_season_not_marked_complete(temp_db):
    """
    An ongoing season's acquired schedule is persisted but the dataset must not be
    promoted to COMPLETE on the strength of a date boundary plus records.
    """
    fixtures_2026 = [
        make_fixture(202601, "Team A", "Team B", 1, 2, status="FT", season=2026, date="2026-03-01T15:00:00+00:00"),
        make_fixture(202602, "Team B", "Team A", 2, 1, status="NS", season=2026, date="2026-04-01T15:00:00+00:00", h_goals=None, a_goals=None),
    ]

    with csv_patch(fixtures_2026):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in no_other_sources():
                stack.enter_context(p)
            report = historical_sync.sync_historical_fixtures(league_id=39, season=2026)

    stored = storage.get_historical_fixtures(39, 2026)
    assert {f["fixture"]["id"] for f in stored} == {202601}
    assert report["status"] == "INCOMPLETE"
    assert report["lifecycle_state"] in ("ONGOING", "UNKNOWN", "UPCOMING")


def test_provider_failure_falls_through_without_secondary_api(temp_db):
    """
    When the approved providers fail or return nothing, the dataset stays
    INCOMPLETE and the retired secondary API providers are never used.
    """
    with patch("data_resolver._fetch_direct_csv_league_fixtures", side_effect=RuntimeError("CSV down")), \
         patch("soccerdata_provider.get_fbref_games", return_value=("SOURCE_FAILED", [], {"status": "SOURCE_FAILED"})), \
         patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_FAILED", [], {"status": "SOURCE_FAILED"})), \
         patch("soccerdata_provider.get_sofascore_historical_games", return_value=("SOURCE_FAILED", [], {"status": "SOURCE_FAILED"})), \
         patch("api_football.get_league_fixtures_page") as mock_api_fixtures, \
         patch("football_data_api.get_competition_matches") as mock_fd_matches:
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

        mock_api_fixtures.assert_not_called()
        mock_fd_matches.assert_not_called()

    assert report["status"] == "INCOMPLETE"
    assert report["final_stored_count"] == 0


def test_insufficient_data_preserves_records_and_stays_incomplete(temp_db):
    """
    An insufficient dataset (missing expected fixtures) is persisted but remains
    INCOMPLETE, and a later failed run never erases the valid records.
    """
    partial = [make_fixture(9001, "Team A", "Team B", 1, 2, date="2025-01-10T15:00:00+00:00")]

    with csv_patch(partial):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in no_other_sources():
                stack.enter_context(p)
            report1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report1["status"] == "INCOMPLETE"
    assert report1["final_stored_count"] == 1

    with csv_patch([]):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in no_other_sources():
                stack.enter_context(p)
            report2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report2["status"] == "INCOMPLETE"
    stored = storage.get_historical_fixtures(39, 2024)
    assert len(stored) == 1
    assert stored[0]["fixture"]["id"] == 9001


def test_ongoing_2026_fixtures_handled_by_status():
    """
    Ongoing 2026 season fixtures are handled according to status: unplayed
    fixtures (NS, PST) are never persisted, while completed fixtures (FT, AET,
    PEN) are persisted. Because the 2026 season is still in progress the
    competition lifecycle is ONGOING, so the dataset is never marked COMPLETE no
    matter how many fixtures were acquired successfully.
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

    from contextlib import ExitStack

    with patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=fixtures_2026), \
         patch("api_football.get_league_fixtures_page") as mock_api_fixtures:
        with ExitStack() as stack:
            for provider_patch in no_other_sources():
                stack.enter_context(provider_patch)
            report = historical_sync.sync_historical_fixtures(league_id=39, season=2026)

        mock_api_fixtures.assert_not_called()

    assert report["final_stored_count"] == 1
    stored = storage.get_historical_fixtures(39, 2026)
    assert len(stored) == 1
    assert stored[0]["fixture"]["id"] == 202601
    assert report["status"] == "INCOMPLETE"
    assert report["lifecycle_state"] == "ONGOING"


