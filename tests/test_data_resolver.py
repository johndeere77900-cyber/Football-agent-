from unittest.mock import MagicMock, patch
import pytest

from data_resolver import DataResolver
import storage


@pytest.fixture(autouse=True)
def init_test_db():
    storage.init_db()


@patch("api_football.get_fixtures_by_date")
def test_data_resolver_primary_success(mock_api_fb):
    mock_api_fb.return_value = [
        {
            "fixture": {"id": 1, "date": "2025-01-15T15:00:00Z", "status": {"short": "NS"}},
            "league": {"id": 39, "season": 2024, "name": "Premier League"},
            "teams": {
                "home": {"id": 10, "name": "Arsenal"},
                "away": {"id": 20, "name": "Chelsea"},
            },
        }
    ]

    resolver = DataResolver()
    data, meta = resolver.get_fixtures_for_date("2025-01-15", league_id=39)

    assert len(data) == 1
    assert meta["data_source"] == "api_football"
    assert meta["primary_attempted"] is True
    assert meta["fallback_used"] is False
    assert meta["resolver_status"] == "PRIMARY_SUCCESS"


@patch("api_football.get_fixtures_by_date")
@patch("football_data_api.get_competition_matches")
def test_data_resolver_fallback_success(mock_fd_matches, mock_api_fb):
    mock_api_fb.side_effect = RuntimeError("API Football Unavailable")

    mock_fd_matches.return_value = [
        {
            "id": 999,
            "utcDate": "2025-01-15T15:00:00Z",
            "status": "SCHEDULED",
            "competition": {"name": "Premier League"},
            "season": {"startDate": "2024-08-01"},
            "homeTeam": {"id": 101, "name": "Arsenal FC", "shortName": "Arsenal"},
            "awayTeam": {"id": 102, "name": "Chelsea FC", "shortName": "Chelsea"},
            "score": {"fullTime": {"home": None, "away": None}},
        }
    ]

    resolver = DataResolver()
    data, meta = resolver.get_fixtures_for_date("2025-01-15", league_id=39)

    assert len(data) == 1
    assert data[0]["fixture"]["id"] == 999
    assert data[0]["teams"]["home"]["name"] == "Arsenal"
    assert meta["data_source"] == "football_data_org"
    assert meta["primary_attempted"] is True
    assert meta["fallback_used"] is True
    assert meta["resolver_status"] in ("SECONDARY_SUCCESS", "FALLBACK_SUCCESS")


@patch("api_football.get_league_standings")
@patch("football_data_api.get_competition_standings")
def test_data_resolver_standings_fallback(mock_fd_standings, mock_api_fb_standings):
    mock_api_fb_standings.side_effect = RuntimeError("Primary failure")
    mock_fd_standings.return_value = [
        {
            "position": 1,
            "team": {"id": 10, "name": "Arsenal", "shortName": "Arsenal"},
            "playedGames": 20,
            "won": 15,
            "draw": 3,
            "lost": 2,
            "points": 48,
            "goalsFor": 45,
            "goalsAgainst": 15,
            "goalDifference": 30,
        }
    ]

    resolver = DataResolver()
    standings, meta = resolver.get_standings(39, season=2024)

    assert len(standings) == 1
    assert standings[0]["rank"] == 1
    assert standings[0]["points"] == 48
    assert meta["data_source"] == "football_data_org"
    assert meta["fallback_used"] is True


@patch("api_football.get_fixtures_by_date")
@patch("football_data_api.get_competition_matches")
def test_primary_valid_empty_returns_primary_success_zero_secondary_calls(mock_fd_matches, mock_api_fb):
    """
    Prove VALID_EMPTY sufficiency:
    When primary provider legitimately returns [] (confirmed 0 scheduled matches),
    DataResolver returns PRIMARY_SUCCESS immediately and makes ZERO secondary API calls.
    """
    mock_api_fb.return_value = []

    resolver = DataResolver()
    data, meta = resolver.get_fixtures_for_date("2025-01-15", league_id=39)

    assert data == []
    assert meta["resolver_status"] == "PRIMARY_SUCCESS"
    assert meta["fallback_used"] is False
    assert mock_fd_matches.call_count == 0  # Zero secondary calls!


@patch("api_football.get_fixtures_by_date")
@patch("football_data_api.get_competition_matches")
def test_primary_insufficient_malformed_triggers_secondary_fallback(mock_fd_matches, mock_api_fb):
    """
    Prove INSUFFICIENT_MALFORMED sufficiency:
    When primary provider returns malformed records (e.g. missing fixture or teams dicts),
    DataResolver considers secondary fallback.
    """
    mock_api_fb.return_value = [{"malformed_item_no_fixture_key": True}]

    mock_fd_matches.return_value = [
        {
            "id": 888,
            "utcDate": "2025-01-15T15:00:00Z",
            "status": "SCHEDULED",
            "competition": {"name": "Premier League"},
            "season": {"startDate": "2024-08-01"},
            "homeTeam": {"id": 101, "name": "Arsenal FC", "shortName": "Arsenal"},
            "awayTeam": {"id": 102, "name": "Chelsea FC", "shortName": "Chelsea"},
            "score": {"fullTime": {"home": None, "away": None}},
        }
    ]

    resolver = DataResolver()
    data, meta = resolver.get_fixtures_for_date("2025-01-15", league_id=39)

    assert len(data) == 1
    assert data[0]["fixture"]["id"] == 888
    assert meta["resolver_status"] == "SECONDARY_SUCCESS"
    assert meta["fallback_used"] is True


import api_football
import football_data_api
import soccerdata_provider


# Test A — API-Football multi-page acquisition COMPLETE when all pages processed
@patch("api_football.get_league_fixtures_page")
def test_A_api_football_multipage_complete(mock_api_fb):
    import historical_sync

    def mock_page_fetch(league_id, season, page, max_budget=None):
        if page == 1:
            return {
                "fixtures": [
                    {
                        "fixture": {"id": 8001, "date": "2024-08-17T14:00:00Z", "status": {"short": "FT"}},
                        "league": {"id": 39, "season": 2024},
                        "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
                        "goals": {"home": 1, "away": 0},
                    }
                ],
                "expected_pages": 2,
                "current_page": 1,
            }
        elif page == 2:
            return {
                "fixtures": [
                    {
                        "fixture": {"id": 8002, "date": "2024-08-24T14:00:00Z", "status": {"short": "FT"}},
                        "league": {"id": 39, "season": 2024},
                        "teams": {"home": {"id": 2, "name": "Team B"}, "away": {"id": 3, "name": "Team C"}},
                        "goals": {"home": 2, "away": 2},
                    }
                ],
                "expected_pages": 2,
                "current_page": 2,
            }

    mock_api_fb.side_effect = mock_page_fetch
    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "COMPLETE"
    assert report["pages_completed"] == 2
    assert report["expected_pages"] == 2


# Test B — API-Football page 1 succeeds but page 2 fails -> INCOMPLETE
@patch("api_football.get_league_fixtures_page")
def test_B_api_football_partial_multipage_incomplete(mock_api_fb):
    import historical_sync

    def mock_page_fetch(league_id, season, page, max_budget=None):
        if page == 1:
            return {
                "fixtures": [
                    {
                        "fixture": {"id": 8001, "date": "2024-08-17T14:00:00Z", "status": {"short": "FT"}},
                        "league": {"id": 39, "season": 2024},
                        "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
                        "goals": {"home": 1, "away": 0},
                    }
                ],
                "expected_pages": 2,
                "current_page": 1,
            }
        raise RuntimeError("API-Football network timeout on page 2")

    mock_api_fb.side_effect = mock_page_fetch
    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"
    assert report["pages_completed"] == 1
    assert report["expected_pages"] == 2


# Test C — football-data.org partial response -> INCOMPLETE
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_C_football_data_org_partial_response_incomplete(mock_api_fb, mock_fd):
    import historical_sync

    mock_api_fb.side_effect = api_football.APIFootballQuotaExhaustedError("Quota exhausted")
    mock_fd.return_value = {
        "matches": [
            {
                "id": 1001,
                "utcDate": "2024-08-17T14:00:00Z",
                "status": "FINISHED",
                "competition": {"name": "Premier League", "code": "PL"},
                "season": {"startDate": "2024-08-01"},
                "homeTeam": {"id": 10, "name": "Arsenal FC", "shortName": "Arsenal"},
                "awayTeam": {"id": 20, "name": "Chelsea FC", "shortName": "Chelsea"},
                "score": {"fullTime": {"home": 2, "away": 1}},
            }
        ],
        "metadata": {"count": 1, "played": 1, "is_partial": True},
    }

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"
    assert report["acquisition_complete"] is False


# Test D — football-data.org trustworthy complete metadata without quota exhaustion -> COMPLETE
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_D_football_data_org_complete_metadata_complete(mock_api_fb, mock_fd):
    import historical_sync

    mock_api_fb.side_effect = api_football.APIFootballError("Primary error")
    teams = list(range(1, 21))
    pairings = [(h, a) for h in teams for a in teams if h != a]
    matches = [
        {
            "id": 2000 + i,
            "utcDate": "2024-08-17T14:00:00Z",
            "status": "FINISHED",
            "competition": {"name": "Premier League", "code": "PL"},
            "season": {"startDate": "2024-08-01"},
            "homeTeam": {"id": h_id, "name": f"Team {h_id}"},
            "awayTeam": {"id": a_id, "name": f"Team {a_id}"},
            "score": {"fullTime": {"home": 1, "away": 0}},
        }
        for i, (h_id, a_id) in enumerate(pairings)
    ]
    mock_fd.return_value = {
        "matches": matches,
        "metadata": {
            "count": 380,
            "played": 380,
            "first": "2024-08-17T14:00:00Z",
            "last": "2025-05-25T16:00:00Z",
            "competition_code": "PL",
            "season": 2024,
        },
    }

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "COMPLETE"
    assert report["acquisition_complete"] is True
    assert report["valid_fixtures"] == 380


@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_quota_exhaustion_with_fallback_fixtures_remains_incomplete(mock_api_fb, mock_fd, mock_sd):
    import historical_sync

    # 1. API-Football raises quota exhaustion
    mock_api_fb.side_effect = api_football.APIFootballQuotaExhaustedError("Quota exhausted")

    # 2. DataResolver returns valid football-data.org fallback fixtures
    matches = [
        {
            "id": 5000 + i,
            "utcDate": "2024-08-17T14:00:00Z",
            "status": "FINISHED",
            "competition": {"name": "Premier League", "code": "PL"},
            "season": {"startDate": "2024-08-01"},
            "homeTeam": {"id": (i % 20) + 1, "name": f"Team {(i % 20) + 1}"},
            "awayTeam": {"id": ((i + 1) % 20) + 1, "name": f"Team {((i + 1) % 20) + 1}"},
            "score": {"fullTime": {"home": 1, "away": 0}},
        }
        for i in range(380)
    ]
    mock_fd.return_value = {
        "matches": matches,
        "metadata": {
            "count": 380,
            "played": 380,
            "first": "2024-08-17T14:00:00Z",
            "last": "2025-05-25T16:00:00Z",
            "competition_code": "PL",
            "season": 2024,
        },
    }

    # 3. sync_historical_fixtures() receives those fixtures
    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    # 4. Final dataset status is INCOMPLETE
    assert report["status"] == "INCOMPLETE"
    # 5. quota_budget_stopped is True
    assert report["quota_budget_stopped"] is True
    # 6. acquisition_complete is False
    assert report["acquisition_complete"] is False

    # 7. Fallback fixtures remain stored in database
    stored = storage.get_historical_fixtures(39, 2024)
    assert len(stored) == 380
    assert stored[0]["provider_provenance"]["provider"] == "football_data_org"

    status = storage.get_historical_dataset_status(39, 2024)
    assert status["status"] == "INCOMPLETE"
    assert status["acquisition_complete"] is False
    assert status["error_reason"] == "quota_budget_exhausted_during_acquisition"


# Test E — SoccerData with fixtures but no trustworthy completeness metadata -> INCOMPLETE
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_E_soccerdata_no_completeness_metadata_incomplete(mock_api_fb, mock_fd, mock_sd):
    import historical_sync

    mock_api_fb.side_effect = api_football.APIFootballError("Primary error")
    mock_fd.side_effect = football_data_api.FootballDataAPIError("Secondary error")
    mock_sd.return_value = (
        "SOURCE_AVAILABLE",
        [
            {
                "fixture": {"id": 900101, "date": "2024-08-17T14:00:00Z", "status": {"short": "FT"}},
                "league": {"id": 39, "season": 2024, "name": "Premier League"},
                "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 2, "name": "Chelsea"}},
                "goals": {"home": 1, "away": 0},
                "provider_provenance": {"provider": "soccerdata", "provider_type": "tertiary"},
            }
        ],
        {"status": "SOURCE_AVAILABLE", "count": 1},  # No is_complete metadata
    )

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"
    assert report["acquisition_complete"] is False


# Test F — SoccerData with explicit trustworthy completeness metadata -> COMPLETE
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_F_soccerdata_with_complete_metadata_complete(mock_api_fb, mock_fd, mock_sd):
    import historical_sync

    mock_api_fb.side_effect = api_football.APIFootballError("Primary error")
    mock_fd.side_effect = football_data_api.FootballDataAPIError("Secondary error")
    teams = list(range(1, 21))
    pairings = [(h, a) for h in teams for a in teams if h != a]
    sd_fixtures = [
        {
            "fixture": {"id": 900000 + i, "date": "2024-08-17T14:00:00Z", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024, "name": "Premier League"},
            "teams": {"home": {"id": h_id, "name": f"Team {h_id}"}, "away": {"id": a_id, "name": f"Team {a_id}"}},
            "goals": {"home": 1, "away": 0},
            "provider_provenance": {"provider": "soccerdata", "provider_type": "tertiary"},
        }
        for i, (h_id, a_id) in enumerate(pairings)
    ]
    mock_sd.return_value = (
        "SOURCE_AVAILABLE",
        sd_fixtures,
        {"status": "SOURCE_AVAILABLE", "count": 380, "is_complete": True},
    )

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "COMPLETE"
    assert report["acquisition_complete"] is True


# Test G — API quota exhaustion -> INCOMPLETE and NO retry
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_G_api_quota_exhaustion_no_retry(mock_api_fb, mock_fd, mock_sd):
    mock_api_fb.side_effect = api_football.APIFootballQuotaExhaustedError("Quota exhausted")
    mock_fd.side_effect = football_data_api.FootballDataAPIError("Secondary unavailable")
    mock_sd.return_value = ("SOURCE_NOT_AVAILABLE", [], {})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert mock_api_fb.call_count == 1  # Called exactly once, no retry!
    assert res["primary_quota_exhausted"] is True


# Test H — Successful empty API-Football historical response falls through to fallback
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_H_api_empty_historical_falls_through(mock_api_fb, mock_fd, mock_sd):
    mock_api_fb.return_value = {"fixtures": [], "expected_pages": 1, "current_page": 1}
    mock_fd.return_value = {
        "matches": [
            {
                "id": 3001,
                "utcDate": "2024-08-17T14:00:00Z",
                "status": "FINISHED",
                "competition": {"name": "Premier League", "code": "PL"},
                "season": {"startDate": "2024-08-01"},
                "homeTeam": {"id": 10, "name": "Arsenal FC", "shortName": "Arsenal"},
                "awayTeam": {"id": 20, "name": "Chelsea FC", "shortName": "Chelsea"},
                "score": {"fullTime": {"home": 2, "away": 1}},
            }
        ],
        "metadata": {"count": 1, "played": 1},
    }

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "football_data_org"
    assert len(res["fixtures"]) == 1


# Test I — Normal date lookup VALID_EMPTY behavior remains unchanged
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_fixtures_by_date")
def test_I_normal_date_lookup_valid_empty_unchanged(mock_api_fb_date, mock_fd_matches):
    mock_api_fb_date.return_value = []  # Confirmed 0 matches on date

    resolver = DataResolver()
    data, meta = resolver.get_fixtures_for_date("2025-01-15", league_id=39)

    assert data == []
    assert meta["resolver_status"] == "PRIMARY_SUCCESS"
    assert meta["fallback_used"] is False
    assert mock_fd_matches.call_count == 0  # Zero secondary calls for date lookup!


# Test J — Prove arbitrary counts (1, 10, 11, 50, 100) alone cannot cause COMPLETE without explicit provider metadata
@pytest.mark.parametrize("arbitrary_count", [1, 10, 11, 50, 100, 380])
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_J_arbitrary_counts_alone_cannot_cause_complete(mock_api_fb, mock_fd, arbitrary_count):
    import historical_sync

    mock_api_fb.side_effect = api_football.APIFootballQuotaExhaustedError("Quota exhausted")
    matches = [
        {
            "id": 3000 + i,
            "utcDate": "2024-08-17T14:00:00Z",
            "status": "FINISHED",
            "competition": {"name": "Premier League", "code": "PL"},
            "season": {"startDate": "2024-08-01"},
            "homeTeam": {"id": (i % 20) + 1, "name": f"Team {(i % 20) + 1}"},
            "awayTeam": {"id": ((i + 1) % 20) + 1, "name": f"Team {((i + 1) % 20) + 1}"},
            "score": {"fullTime": {"home": 1, "away": 0}},
        }
        for i in range(arbitrary_count)
    ]
    # Response WITH NO TRUSTWORTHY COMPLETENESS METADATA (missing date range or count mismatch)
    mock_fd.return_value = {
        "matches": matches,
        "metadata": {"count": None, "played": None},  # No complete metadata!
    }

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"
    assert report["acquisition_complete"] is False
