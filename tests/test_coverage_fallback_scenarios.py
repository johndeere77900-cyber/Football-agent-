"""
Focused Unit Tests for Historical Football Acquisition Coverage Fallback Hierarchy.

Verifies the 15 explicit acceptance scenarios:
1. football-data.org returns 0 fixtures -> SoccerData MatchHistory is attempted.
2. football-data.org returns partial fixtures -> SoccerData MatchHistory is attempted.
3. MatchHistory returns partial/unusable data -> Sofascore is attempted.
4. football-data.org returns partial data and Sofascore returns additional valid fixtures -> merged result with source="mixed".
5. A provider returning len(fixtures) > 0 without completeness metadata -> NOT automatically considered COMPLETE.
6. A provider explicitly reporting incomplete/partial -> tertiary fallback is attempted.
7. Valid SoccerData fixtures retain real numeric fixture IDs.
8. Valid SoccerData fixtures retain real numeric team IDs.
9. Missing provider IDs remain rejected by the storage boundary.
10. Wrong-season fixtures remain rejected.
11. Non-completed fixtures remain rejected when completed_only=True.
12. Existing API-Football -> football-data.org behavior remains intact when primary is genuinely sufficient.
13. Existing COMPLETE datasets still cause zero re-fetch requests unless refresh=True.
14. Existing 3-year season guard remains exactly 2024, 2025, 2026.
15. No changes to basketball behavior.
"""

from unittest.mock import patch, MagicMock
import pytest

import api_football
import football_data_api
import soccerdata_provider
import data_resolver
from data_resolver import DataResolver, _validate_and_filter_tertiary_matches, _provider_result_is_sufficient_for_historical_acquisition, _validate_historical_structural_coverage
import historical_sync
import storage
import config


@pytest.fixture(autouse=True)
def setup_db():
    storage.init_db()


def make_test_fixture(fid, h_name="Arsenal", a_name="Chelsea", h_id=1, a_id=2, status="FT", season=2024, league_id=39, h_goals=2, a_goals=1):
    return {
        "fixture": {
            "id": fid,
            "date": "2024-08-17T15:00:00Z",
            "status": {"short": status, "long": "Finished" if status in ("FT", "AET", "PEN") else "Not Started"},
        },
        "league": {
            "id": league_id,
            "season": season,
            "name": f"League_{league_id}",
        },
        "teams": {
            "home": {"id": h_id, "name": h_name},
            "away": {"id": a_id, "name": a_name},
        },
        "goals": {
            "home": h_goals,
            "away": a_goals,
        },
        "statistics": {
            "shots": {"home": 10, "away": 5},
            "shots_on_target": {"home": 5, "away": 2},
            "corners": {"home": 4, "away": 3},
            "yellow_cards": {"home": 1, "away": 2},
            "red_cards": {"home": 0, "away": 0},
            "possession": {"home": "50%", "away": "50%"},
            "xG": {"home": 1.5, "away": 1.0},
        },
        "events": [{"type": "Goal"}],
        "provider_provenance": {
            "provider": "soccerdata_sofascore",
            "provider_type": "tertiary",
            "provider_fixture_id": fid,
            "provider_team_ids": {"home": h_id, "away": a_id},
        },
    }


# 1. football-data.org returns 0 fixtures -> SoccerData MatchHistory is attempted.
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_scenario_1_fd_zero_fixtures_triggers_match_history(mock_api_fb, mock_fd, mock_sd_mh):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary failure")
    mock_fd.return_value = {"matches": [], "metadata": {"count": 0}}
    mock_sd_mh.return_value = (
        "SOURCE_AVAILABLE",
        [make_test_fixture(7001, h_name="Arsenal", a_name="Chelsea", h_id=10, a_id=20)],
        {"status": "SOURCE_AVAILABLE", "is_complete": True},
    )

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert mock_sd_mh.call_count == 1
    assert res["source"] == "soccerdata"
    assert len(res["fixtures"]) == 1
    assert res["fixtures"][0]["fixture"]["id"] == 7001


# 2. football-data.org returns partial fixtures -> SoccerData MatchHistory is attempted.
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_scenario_2_fd_partial_fixtures_triggers_match_history(mock_api_fb, mock_fd, mock_sd_mh):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary failure")
    fd_match = {
        "id": 8001,
        "utcDate": "2024-08-17T15:00:00Z",
        "status": "FINISHED",
        "competition": {"name": "Premier League", "code": "PL"},
        "season": {"startDate": "2024-08-01"},
        "homeTeam": {"id": 101, "name": "Arsenal FC", "shortName": "Arsenal"},
        "awayTeam": {"id": 102, "name": "Chelsea FC", "shortName": "Chelsea"},
        "score": {"fullTime": {"home": 2, "away": 1}},
    }
    mock_fd.return_value = {
        "matches": [fd_match],
        "metadata": {"count": 380, "played": 10, "is_partial": True},
    }

    sd_match = make_test_fixture(8002, h_name="Liverpool", a_name="Everton", h_id=30, a_id=40)
    mock_sd_mh.return_value = (
        "SOURCE_AVAILABLE",
        [sd_match],
        {"status": "SOURCE_AVAILABLE", "is_complete": True},
    )

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert mock_sd_mh.call_count == 1
    assert res["source"] == "mixed"
    assert len(res["fixtures"]) == 2


# 3. MatchHistory returns partial/unusable data -> Sofascore is attempted.
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_scenario_3_match_history_partial_triggers_sofascore(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary failure")
    mock_fd.return_value = {"matches": [], "metadata": {"is_partial": True}}
    mock_sd_mh.return_value = ("SOURCE_RETURNED_PARTIAL_DATA", [], {"is_partial": True})

    ss_match = make_test_fixture(9001, h_name="Benfica", a_name="Porto", h_id=50, a_id=60, league_id=94)
    mock_sd_ss.return_value = ("SOURCE_AVAILABLE", [ss_match], {"status": "SOURCE_AVAILABLE", "is_complete": True})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=94, season=2024, page=1)

    assert mock_sd_ss.call_count == 1
    assert len(res["fixtures"]) == 1
    assert res["fixtures"][0]["fixture"]["id"] == 9001


# 4. football-data.org partial data + Sofascore valid fixtures -> merged result with source="mixed".
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_scenario_4_partial_fd_plus_sofascore_yields_mixed_source(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary failure")
    fd_match = {
        "id": 1001,
        "utcDate": "2024-08-17T15:00:00Z",
        "status": "FINISHED",
        "competition": {"name": "Premier League", "code": "PL"},
        "season": {"startDate": "2024-08-01"},
        "homeTeam": {"id": 101, "name": "Arsenal FC", "shortName": "Arsenal"},
        "awayTeam": {"id": 102, "name": "Chelsea FC", "shortName": "Chelsea"},
        "score": {"fullTime": {"home": 1, "away": 0}},
    }
    mock_fd.return_value = {"matches": [fd_match], "metadata": {"is_partial": True}}
    mock_sd_mh.return_value = ("SOURCE_NOT_AVAILABLE", [], {})

    ss_match = make_test_fixture(1002, h_name="Manchester City", a_name="Manchester United", h_id=70, a_id=80)
    mock_sd_ss.return_value = ("SOURCE_AVAILABLE", [ss_match], {"status": "SOURCE_AVAILABLE", "is_complete": True})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "mixed"
    assert len(res["fixtures"]) == 2


# 5. A provider returning len(fixtures) > 0 without completeness metadata -> must NOT automatically be considered COMPLETE in historical_sync.
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_scenario_5_fixtures_without_completeness_metadata_remains_incomplete(mock_api_fb, mock_fd, mock_sd):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary error")
    mock_fd.side_effect = football_data_api.FootballDataAPIError("Secondary error")
    mock_sd.return_value = (
        "SOURCE_AVAILABLE",
        [make_test_fixture(1101)],
        {"status": "SOURCE_AVAILABLE", "count": 1},  # No is_complete metadata
    )

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"
    assert report["acquisition_complete"] is False


# 6. A provider explicitly reporting incomplete/partial -> tertiary fallback is attempted.
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_scenario_6_explicit_partial_reports_triggers_tertiary_fallback(mock_api_fb, mock_fd, mock_sd_mh):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary error")
    mock_fd.return_value = {
        "matches": [{
            "id": 1201,
            "utcDate": "2024-08-17T15:00:00Z",
            "status": "FINISHED",
            "competition": {"name": "Premier League", "code": "PL"},
            "season": {"startDate": "2024-08-01"},
            "homeTeam": {"id": 1, "name": "Arsenal"},
            "awayTeam": {"id": 2, "name": "Chelsea"},
            "score": {"fullTime": {"home": 1, "away": 0}},
        }],
        "metadata": {"is_partial": True},  # Explicit partial
    }
    mock_sd_mh.return_value = (
        "SOURCE_AVAILABLE",
        [make_test_fixture(1202, h_name="Liverpool", a_name="Tottenham", h_id=3, a_id=4)],
        {"status": "SOURCE_AVAILABLE", "is_complete": True},
    )

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert mock_sd_mh.call_count == 1
    assert len(res["fixtures"]) == 2


# 7. Valid SoccerData fixtures retain real numeric fixture IDs.
def test_scenario_7_soccerdata_retains_real_numeric_fixture_id():
    raw_fix = make_test_fixture(987654)
    filtered = _validate_and_filter_tertiary_matches([raw_fix], league_id=39, season=2024)

    assert len(filtered) == 1
    assert filtered[0]["fixture"]["id"] == 987654
    assert isinstance(filtered[0]["fixture"]["id"], int)


# 8. Valid SoccerData fixtures retain real numeric team IDs.
def test_scenario_8_soccerdata_retains_real_numeric_team_ids():
    raw_fix = make_test_fixture(987655, h_id=123, a_id=456)
    filtered = _validate_and_filter_tertiary_matches([raw_fix], league_id=39, season=2024)

    assert len(filtered) == 1
    assert filtered[0]["teams"]["home"]["id"] == 123
    assert filtered[0]["teams"]["away"]["id"] == 456
    assert isinstance(filtered[0]["teams"]["home"]["id"], int)
    assert isinstance(filtered[0]["teams"]["away"]["id"], int)


# 9. Missing provider IDs remain rejected by the storage boundary.
def test_scenario_9_missing_provider_team_ids_rejected():
    raw_fix = make_test_fixture(987656, h_id=None, a_id=456)
    filtered = _validate_and_filter_tertiary_matches([raw_fix], league_id=39, season=2024)

    assert len(filtered) == 0  # Rejected due to missing real provider team ID!


# 10. Wrong-season fixtures remain rejected.
def test_scenario_10_wrong_season_fixtures_rejected():
    raw_fix = make_test_fixture(987657, season=2022)  # Requested 2024, received 2022
    filtered = _validate_and_filter_tertiary_matches([raw_fix], league_id=39, season=2024)

    assert len(filtered) == 0  # Rejected due to season mismatch!


# 11. Non-completed fixtures remain rejected when completed_only=True.
def test_scenario_11_non_completed_fixtures_rejected():
    raw_fix = make_test_fixture(987658, status="NS")  # Scheduled match
    filtered = _validate_and_filter_tertiary_matches([raw_fix], league_id=39, season=2024, completed_only=True)

    assert len(filtered) == 0  # Rejected due to non-completed status!


# 12. Existing API-Football -> football-data.org behavior remains intact when primary dataset is genuinely sufficient.
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_scenario_12_primary_sufficient_remains_intact(mock_api_fb, mock_fd):
    mock_api_fb.return_value = {
        "fixtures": [make_test_fixture(1001)],
        "expected_pages": 1,
        "current_page": 1,
    }

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "api_football"
    assert len(res["fixtures"]) == 1
    assert mock_fd.call_count == 0  # Zero secondary calls when primary is sufficient!


# 13. Existing COMPLETE datasets still cause zero re-fetch requests unless refresh=True.
@patch("api_football.get_league_fixtures_page")
def test_scenario_13_complete_dataset_bypasses_refetch(mock_api_fb):
    # Mark dataset COMPLETE in DB
    storage.mark_historical_dataset_complete(
        league_id=39,
        season=2024,
        fixture_count=380,
        source="api_football",
        sport="football",
        expected_pages=1,
        pages_completed=1,
    )

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024, refresh=False)

    assert report["status"] == "COMPLETE"
    assert report["api_requests_consumed"] == 0
    assert mock_api_fb.call_count == 0  # Zero API calls made!


# 14. Existing 3-year season guard remains exactly 2024, 2025, 2026.
def test_scenario_14_three_year_season_guard_raises_value_error():
    with pytest.raises(ValueError, match="only supports seasons"):
        historical_sync.sync_historical_fixtures(league_id=39, season=2023)

    with pytest.raises(ValueError, match="only supports seasons"):
        historical_sync.sync_historical_fixtures(league_id=39, season=2027)


# 15. No changes to basketball behavior.
@patch("basketball_api.get_league_games_page")
def test_scenario_15_basketball_sync_behavior_unaffected(mock_bb_page):
    mock_bb_page.return_value = {
        "games": [
            {
                "id": 555,
                "date": "2024-11-01T20:00:00Z",
                "status": {"short": "FT"},
                "league": {"id": 12, "season": 2024},
                "teams": {
                    "home": {"id": 10, "name": "Lakers"},
                    "away": {"id": 20, "name": "Celtics"},
                },
                "scores": {"home": {"total": 105}, "away": {"total": 102}},
            }
        ],
        "expected_pages": 1,
    }

    report = historical_sync.sync_historical_basketball_games(league_id=12, season=2024)

    assert report["sport"] == "basketball"
    assert report["status"] == "COMPLETE"
    assert report["final_stored_count"] == 1


# PR #29 Fix Test 1: football-data.org partial + SoccerData complete -> merged schedule complete -> final is_complete=True
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_fd_partial_plus_soccerdata_complete_yields_is_complete_true(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary error")

    teams = list(range(1, 21))
    pairings = [(h, a) for h in teams for a in teams if h != a]
    fd_pairings = pairings[:200]
    ss_pairings = pairings[200:]

    fd_matches = [
        {
            "id": 2000 + i,
            "utcDate": "2024-08-17T15:00:00Z",
            "status": "FINISHED",
            "competition": {"name": "Premier League", "code": "PL"},
            "season": {"startDate": "2024-08-01"},
            "homeTeam": {"id": h_id, "name": f"Team {h_id}"},
            "awayTeam": {"id": a_id, "name": f"Team {a_id}"},
            "score": {"fullTime": {"home": 1, "away": 0}},
        }
        for i, (h_id, a_id) in enumerate(fd_pairings)
    ]
    mock_fd.return_value = {
        "matches": fd_matches,
        "metadata": {"is_partial": True},
    }
    mock_sd_mh.return_value = ("SOURCE_NOT_AVAILABLE", [], {})

    ss_fixtures = [
        make_test_fixture(3000 + i, h_name=f"Team {h_id}", a_name=f"Team {a_id}", h_id=h_id, a_id=a_id)
        for i, (h_id, a_id) in enumerate(ss_pairings)
    ]
    mock_sd_ss.return_value = (
        "SOURCE_AVAILABLE",
        ss_fixtures,
        {"status": "SOURCE_AVAILABLE", "is_complete": True},
    )

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "mixed"
    assert len(res["fixtures"]) == 380
    assert res["provider_metadata"]["is_complete"] is True
    assert res["provider_metadata"]["is_partial"] is False


# PR #29 Fix Test 2: football-data.org partial + SoccerData incomplete -> final is_complete=False
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_fd_partial_plus_soccerdata_incomplete_yields_is_complete_false(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary error")
    mock_fd.return_value = {
        "matches": [{
            "id": 2001,
            "utcDate": "2024-08-17T15:00:00Z",
            "status": "FINISHED",
            "competition": {"name": "Premier League", "code": "PL"},
            "season": {"startDate": "2024-08-01"},
            "homeTeam": {"id": 1, "name": "Arsenal"},
            "awayTeam": {"id": 2, "name": "Chelsea"},
            "score": {"fullTime": {"home": 1, "away": 0}},
        }],
        "metadata": {"is_partial": True},
    }
    mock_sd_mh.return_value = ("SOURCE_NOT_AVAILABLE", [], {})
    mock_sd_ss.return_value = (
        "SOURCE_AVAILABLE",
        [make_test_fixture(2002, h_name="Liverpool", a_name="Everton", h_id=3, a_id=4)],
        {"status": "SOURCE_AVAILABLE", "is_partial": True},
    )

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "mixed"
    assert res["provider_metadata"]["is_complete"] is False
    assert res["provider_metadata"]["is_partial"] is True


def make_18_team_schedule(league_id=61, season=2025, missing_pair=None):
    teams = list(range(1, 19))
    pairings = [(h, a) for h in teams for a in teams if h != a]
    if missing_pair:
        pairings = [p for p in pairings if p != missing_pair]
    fixtures = []
    for i, (h_id, a_id) in enumerate(pairings):
        fixtures.append({
            "fixture": {"id": 610000 + i, "date": "2025-08-15T20:00:00Z", "status": {"short": "FT"}},
            "league": {"id": league_id, "season": season, "name": "Ligue 1"},
            "teams": {
                "home": {"id": h_id, "name": f"Team {h_id}"},
                "away": {"id": a_id, "name": f"Team {a_id}"},
            },
            "goals": {"home": 1, "away": 0},
            "canonical_home_id": f"team_{h_id}",
            "canonical_away_id": f"team_{a_id}",
            "provider_provenance": {"provider": "football_data_org", "provider_type": "secondary"},
        })
    return fixtures


# TEST A — Provider falsely claims complete
def test_A_provider_falsely_claims_complete():
    # 305 fixtures (1 missing from 18-team Ligue 1 schedule)
    fixtures_305 = make_18_team_schedule(league_id=61, season=2025, missing_pair=(17, 18))
    meta = {"count": 305, "played": 305, "is_complete": True, "is_partial": False}

    is_suff = _provider_result_is_sufficient_for_historical_acquisition(
        "football_data_org", fixtures_305, provider_metadata=meta, season=2025, league_id=61
    )
    cov = _validate_historical_structural_coverage(fixtures_305, season=2025, provider_metadata=meta, league_id=61)

    assert is_suff is False
    assert cov["verified"] is False
    assert cov["reason"] in ("fixture_count_mismatch", "team_participation_imbalance")


# TEST B — Missing fixture detected by team balance
def test_B_missing_fixture_detected_by_team_balance():
    # 18 teams, missing pair (17, 18) -> team 17 missing 1 home match, team 18 missing 1 away match
    fixtures_305 = make_18_team_schedule(league_id=61, season=2025, missing_pair=(17, 18))
    cov = _validate_historical_structural_coverage(fixtures_305, season=2025, league_id=61)

    assert cov["verified"] is False
    assert cov["fixture_count"] == 305
    assert cov["expected_fixture_count"] == 306
    assert cov["team_count"] == 18
    # Team 17 home match count is 16 (instead of 17)
    assert cov["team_match_counts"]["team_17"]["home"] == 16
    assert cov["team_match_counts"]["team_17"]["total"] == 33
    # Team 18 away match count is 16 (instead of 17)
    assert cov["team_match_counts"]["team_18"]["away"] == 16
    assert cov["team_match_counts"]["team_18"]["total"] == 33


# TEST C — Complete 18-team double round robin
def test_C_complete_18_team_double_round_robin():
    fixtures_306 = make_18_team_schedule(league_id=61, season=2025)
    cov = _validate_historical_structural_coverage(fixtures_306, season=2025, league_id=61)

    assert cov["verified"] is True
    assert cov["reason"] is None
    assert cov["fixture_count"] == 306
    assert cov["distinct_fixture_count"] == 306
    assert cov["expected_fixture_count"] == 306
    assert cov["team_count"] == 18
    for t_id, counts in cov["team_match_counts"].items():
        assert counts["home"] == 17
        assert counts["away"] == 17
        assert counts["total"] == 34


# TEST D — Mixed provider completion
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_D_mixed_provider_completion(mock_api_fb, mock_fd, mock_sd_mh):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary error")

    # Provider A (football-data.org) supplies 305 fixtures
    fd_raw = [
        {
            "id": 610000 + i,
            "utcDate": "2025-08-15T20:00:00Z",
            "status": "FINISHED",
            "competition": {"name": "Ligue 1", "code": "FL1"},
            "season": {"startDate": "2025-08-01"},
            "homeTeam": {"id": h_id, "name": f"Team {h_id}"},
            "awayTeam": {"id": a_id, "name": f"Team {a_id}"},
            "score": {"fullTime": {"home": 1, "away": 0}},
        }
        for i, (h_id, a_id) in enumerate([(h, a) for h in range(1, 19) for a in range(1, 19) if h != a and (h, a) != (17, 18)])
    ]
    mock_fd.return_value = {
        "matches": fd_raw,
        "metadata": {"count": 305, "played": 305, "is_complete": True},
    }

    # Provider B (SoccerData) supplies missing fixture (17 vs 18)
    missing_sd_fixture = {
        "fixture": {"id": 610305, "date": "2025-08-15T20:00:00Z", "status": {"short": "FT"}},
        "league": {"id": 61, "season": 2025, "name": "Ligue 1"},
        "teams": {"home": {"id": 17, "name": "Team 17"}, "away": {"id": 18, "name": "Team 18"}},
        "goals": {"home": 2, "away": 1},
        "provider_provenance": {"provider": "soccerdata_match_history"},
    }
    mock_sd_mh.return_value = ("SOURCE_AVAILABLE", [missing_sd_fixture], {"status": "SOURCE_AVAILABLE"})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=61, season=2025, page=1)

    assert res["source"] == "mixed"
    assert len(res["fixtures"]) == 306
    assert res["provider_metadata"]["is_complete"] is True
    assert res["provider_metadata"]["structural_coverage"]["verified"] is True
    assert res["provider_metadata"]["structural_coverage"]["expected_fixture_count"] == 306


# TEST E — Provider claims complete but structure is incomplete (Ligue 1 305 vs 306 failure)
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_E_provider_claims_complete_but_structure_incomplete_triggers_fallback(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary error")

    # football-data.org returns 305 fixtures and claims complete
    fd_raw = [
        {
            "id": 610000 + i,
            "utcDate": "2025-08-15T20:00:00Z",
            "status": "FINISHED",
            "competition": {"name": "Ligue 1", "code": "FL1"},
            "season": {"startDate": "2025-08-01"},
            "homeTeam": {"id": h_id, "name": f"Team {h_id}"},
            "awayTeam": {"id": a_id, "name": f"Team {a_id}"},
            "score": {"fullTime": {"home": 1, "away": 0}},
        }
        for i, (h_id, a_id) in enumerate([(h, a) for h in range(1, 19) for a in range(1, 19) if h != a and (h, a) != (17, 18)])
    ]
    mock_fd.return_value = {
        "matches": fd_raw,
        "metadata": {"count": 305, "played": 305, "is_complete": True},  # Claims complete
    }

    mock_sd_mh.return_value = ("SOURCE_NOT_AVAILABLE", [], {})
    mock_sd_ss.return_value = ("SOURCE_NOT_AVAILABLE", [], {})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=61, season=2025, page=1)

    # Must NOT stop at football-data.org's false claim; must trigger fallbacks
    assert mock_sd_mh.call_count == 1
    assert mock_sd_ss.call_count == 1
    assert res["provider_metadata"]["is_complete"] is False
    assert res["provider_metadata"]["structural_coverage"]["verified"] is False


# TEST F — Unknown competition format fails closed
def test_F_unknown_competition_format_fails_closed():
    # Champions League (league_id=2) is not a double round robin league
    cl_fixtures = [
        {
            "fixture": {"id": 2001, "date": "2025-09-16T20:00:00Z", "status": {"short": "FT"}},
            "league": {"id": 2, "season": 2025, "name": "Champions League"},
            "teams": {"home": {"id": 1, "name": "Real Madrid"}, "away": {"id": 2, "name": "Stuttgart"}},
            "goals": {"home": 3, "away": 1},
            "canonical_home_id": "real_madrid",
            "canonical_away_id": "stuttgart",
        }
    ]

    cov = _validate_historical_structural_coverage(cl_fixtures, season=2025, league_id=2)

    assert cov["verified"] is False
    assert cov["reason"] == "structural_coverage_unverifiable"
    assert cov["expected_fixture_count"] is None


# PR #29 Fix Test 3: football-data.org partial + SoccerData completeness unknown -> final is_complete=False
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_fd_partial_plus_soccerdata_unknown_yields_is_complete_false(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary error")
    mock_fd.return_value = {
        "matches": [{
            "id": 2001,
            "utcDate": "2024-08-17T15:00:00Z",
            "status": "FINISHED",
            "competition": {"name": "Premier League", "code": "PL"},
            "season": {"startDate": "2024-08-01"},
            "homeTeam": {"id": 1, "name": "Arsenal"},
            "awayTeam": {"id": 2, "name": "Chelsea"},
            "score": {"fullTime": {"home": 1, "away": 0}},
        }],
        "metadata": {"is_partial": True},
    }
    mock_sd_mh.return_value = ("SOURCE_NOT_AVAILABLE", [], {})
    mock_sd_ss.return_value = (
        "SOURCE_AVAILABLE",
        [make_test_fixture(2002, h_name="Liverpool", a_name="Everton", h_id=3, a_id=4)],
        {"status": "SOURCE_AVAILABLE"},  # No is_complete=True
    )

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "mixed"
    assert res["provider_metadata"]["is_complete"] is False
    assert res["provider_metadata"]["is_partial"] is True


# PR #29 Fix Test 4: football-data.org partial + MatchHistory partial + Sofascore complete -> merged schedule complete -> final is_complete=True
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_fd_partial_plus_mh_partial_plus_sofascore_complete_yields_is_complete_true(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary error")

    teams = list(range(1, 21))
    pairings = [(h, a) for h in teams for a in teams if h != a]
    fd_pairings = pairings[:100]
    mh_pairings = pairings[100:200]
    ss_pairings = pairings[200:]

    fd_matches = [
        {
            "id": 2000 + i,
            "utcDate": "2024-08-17T15:00:00Z",
            "status": "FINISHED",
            "competition": {"name": "Premier League", "code": "PL"},
            "season": {"startDate": "2024-08-01"},
            "homeTeam": {"id": h_id, "name": f"Team {h_id}"},
            "awayTeam": {"id": a_id, "name": f"Team {a_id}"},
            "score": {"fullTime": {"home": 1, "away": 0}},
        }
        for i, (h_id, a_id) in enumerate(fd_pairings)
    ]
    mock_fd.return_value = {
        "matches": fd_matches,
        "metadata": {"is_partial": True},
    }

    mh_fixtures = [
        make_test_fixture(3000 + i, h_name=f"Team {h_id}", a_name=f"Team {a_id}", h_id=h_id, a_id=a_id)
        for i, (h_id, a_id) in enumerate(mh_pairings)
    ]
    mock_sd_mh.return_value = (
        "SOURCE_AVAILABLE",
        mh_fixtures,
        {"status": "SOURCE_AVAILABLE", "is_partial": True},
    )

    ss_fixtures = [
        make_test_fixture(4000 + i, h_name=f"Team {h_id}", a_name=f"Team {a_id}", h_id=h_id, a_id=a_id)
        for i, (h_id, a_id) in enumerate(ss_pairings)
    ]
    mock_sd_ss.return_value = (
        "SOURCE_AVAILABLE",
        ss_fixtures,
        {"status": "SOURCE_AVAILABLE", "is_complete": True},
    )

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "mixed"
    assert len(res["fixtures"]) == 380
    assert res["provider_metadata"]["is_complete"] is True
    assert res["provider_metadata"]["is_partial"] is False


# PR #29 Fix Test 5: Non-empty mixed result without explicit completeness evidence -> is_complete=False
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_non_empty_mixed_without_completeness_evidence_yields_is_complete_false(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary error")
    mock_fd.return_value = {
        "matches": [{
            "id": 2001,
            "utcDate": "2024-08-17T15:00:00Z",
            "status": "FINISHED",
            "competition": {"name": "Premier League", "code": "PL"},
            "season": {"startDate": "2024-08-01"},
            "homeTeam": {"id": 1, "name": "Arsenal"},
            "awayTeam": {"id": 2, "name": "Chelsea"},
            "score": {"fullTime": {"home": 1, "away": 0}},
        }],
        "metadata": {},
    }
    mock_sd_mh.return_value = (
        "SOURCE_AVAILABLE",
        [make_test_fixture(2002, h_name="Liverpool", a_name="Everton", h_id=3, a_id=4)],
        {"status": "SOURCE_AVAILABLE"},
    )
    mock_sd_ss.return_value = ("SOURCE_NOT_AVAILABLE", [], {})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "mixed"
    assert res["provider_metadata"]["is_complete"] is False
    assert res["provider_metadata"]["is_partial"] is True
