"""
Focused Unit Tests for Historical Football Acquisition Coverage Fallback Hierarchy.

Verifies the 15 explicit acceptance scenarios and prompt regression tests A-F / 1-5.
"""

from unittest.mock import patch, MagicMock
import pytest

import api_football
import football_data_api
import soccerdata_provider
import data_resolver
from data_resolver import (
    DataResolver,
    _validate_and_filter_tertiary_matches,
    _provider_result_is_sufficient_for_historical_acquisition,
    _validate_historical_structural_coverage,
    _get_authoritative_historical_team_ids,
)
import historical_sync
import storage
import config


@pytest.fixture(autouse=True)
def setup_db():
    storage.init_db()


@pytest.fixture(autouse=True)
def mock_team_universe(monkeypatch):
    def _mock_get_team_ids(league_id, season):
        if league_id in (39, 140, 135, 78, 2):
            return list(range(1, 21))  # 20 teams for PL/PD/SA/BL1
        elif league_id in (61, 88, 94):
            return list(range(1, 19))  # 18 teams for FL1/DED/PPD
        return []
    monkeypatch.setattr("data_resolver._get_authoritative_historical_team_ids", _mock_get_team_ids)


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


# TEST 1 — Primary API-Football structural gap triggers fallback
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_1_primary_api_football_structural_gap_triggers_fallback(mock_api_fb, mock_fd, mock_sd_mh):
    primary_305 = [
        make_test_fixture(610000 + i, h_name=f"Team {h_id}", a_name=f"Team {a_id}", h_id=h_id, a_id=a_id, league_id=61, season=2025)
        for i, (h_id, a_id) in enumerate([(h, a) for h in range(1, 19) for a in range(1, 19) if h != a and (h, a) != (17, 18)])
    ]
    mock_api_fb.return_value = {"fixtures": primary_305, "expected_pages": 1, "current_page": 1}
    mock_fd.return_value = {"matches": []}

    missing_sd_fixture = make_test_fixture(610305, h_name="Team 17", a_name="Team 18", h_id=17, a_id=18, league_id=61, season=2025)
    mock_sd_mh.return_value = ("SOURCE_AVAILABLE", [missing_sd_fixture], {"status": "SOURCE_AVAILABLE"})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=61, season=2025, page=1)

    assert mock_fd.call_count == 1 or mock_sd_mh.call_count == 1
    assert res["source"] == "mixed"
    assert len(res["fixtures"]) == 306
    assert res["provider_metadata"]["structural_coverage"]["verified"] is True


# TEST 2 — Entire team missing
def test_2_entire_team_missing():
    provider_metadata = {
        "competition_format": "double_round_robin",
        "expected_team_ids": list(range(1, 19)),
    }
    fixtures_17_teams = [
        make_test_fixture(100 + i, h_name=f"Team {h_id}", a_name=f"Team {a_id}", h_id=h_id, a_id=a_id, league_id=61, season=2025)
        for i, (h_id, a_id) in enumerate([(h, a) for h in range(1, 18) for a in range(1, 18) if h != a])
    ]

    result = _validate_historical_structural_coverage(
        fixtures_17_teams,
        season=2025,
        provider_metadata=provider_metadata,
        league_id=61,
    )

    assert result["verified"] is False
    assert result["reason"] == "team_universe_mismatch"
    assert 18 in result["missing_team_ids"]


# TEST 3 — Complete 18-team double round robin
def test_3_complete_18_team_double_round_robin():
    provider_metadata = {
        "competition_format": "double_round_robin",
        "expected_team_ids": list(range(1, 19)),
    }
    fixtures_306 = [
        make_test_fixture(100 + i, h_name=f"Team {h_id}", a_name=f"Team {a_id}", h_id=h_id, a_id=a_id, league_id=61, season=2025)
        for i, (h_id, a_id) in enumerate([(h, a) for h in range(1, 19) for a in range(1, 19) if h != a])
    ]

    result = _validate_historical_structural_coverage(
        fixtures_306,
        season=2025,
        provider_metadata=provider_metadata,
        league_id=61,
    )

    assert result["verified"] is True
    assert result["fixture_count"] == 306
    assert result["distinct_fixture_count"] == 306
    assert result["expected_team_count"] == 18
    assert result["missing_team_ids"] == []
    assert result["unexpected_team_ids"] == []


# TEST 4 — Primary API-Football 380 complete dataset
@patch("api_football.get_league_fixtures_page")
def test_4_primary_api_football_380_complete_dataset(mock_api_fb):
    fixtures_380 = [
        make_test_fixture(1000 + i, h_name=f"Team {h_id}", a_name=f"Team {a_id}", h_id=h_id, a_id=a_id, league_id=39, season=2024)
        for i, (h_id, a_id) in enumerate([(h, a) for h in range(1, 21) for a in range(1, 21) if h != a])
    ]
    mock_api_fb.return_value = {"fixtures": fixtures_380, "expected_pages": 1, "current_page": 1}

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "api_football"
    assert res["provider_metadata"]["structural_coverage"]["verified"] is True
    assert res["provider_metadata"]["structural_coverage"]["fixture_count"] == 380
    assert res["provider_metadata"]["structural_coverage"]["expected_fixture_count"] == 380


# TEST 5 — Unknown competition format
def test_5_unknown_competition_format():
    cl_fixtures = [
        make_test_fixture(2001, h_name="Real Madrid", a_name="Stuttgart", h_id=1, a_id=2, league_id=2, season=2025)
    ]

    cov = _validate_historical_structural_coverage(cl_fixtures, season=2025, league_id=2)

    assert cov["verified"] is False
    assert cov["reason"] == "structural_coverage_unverifiable"
    assert cov["expected_fixture_count"] is None


# Additional fallback scenario tests
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_scenario_1_fd_zero_fixtures_triggers_match_history(mock_api_fb, mock_fd, mock_sd_mh):
    mock_api_fb.side_effect = api_football.APIFootballError("Primary failure")
    mock_fd.return_value = {"matches": [], "metadata": {"count": 0}}

    sd_fixtures = [
        make_test_fixture(7000 + i, h_name=f"Team {h}", a_name=f"Team {a}", h_id=h, a_id=a)
        for i, (h, a) in enumerate([(h, a) for h in range(1, 21) for a in range(1, 21) if h != a])
    ]
    mock_sd_mh.return_value = ("SOURCE_AVAILABLE", sd_fixtures, {"status": "SOURCE_AVAILABLE"})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert mock_sd_mh.call_count == 1
    assert res["source"] == "soccerdata"
    assert len(res["fixtures"]) == 380


@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_scenario_12_primary_sufficient_remains_intact(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    fixtures_380 = [
        make_test_fixture(1000 + i, h_name=f"Team {h_id}", a_name=f"Team {a_id}", h_id=h_id, a_id=a_id, league_id=39, season=2024)
        for i, (h_id, a_id) in enumerate([(h, a) for h in range(1, 21) for a in range(1, 21) if h != a])
    ]
    mock_api_fb.return_value = {
        "fixtures": fixtures_380,
        "expected_pages": 1,
        "current_page": 1,
    }

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "api_football"
    assert len(res["fixtures"]) == 380
    assert mock_fd.call_count == 0


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
