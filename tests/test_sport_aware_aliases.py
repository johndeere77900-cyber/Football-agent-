"""
Unit tests for sport-aware canonical team aliases and final coverage metadata.
"""

import pytest
import config
import team_identity
from data_resolver import _validate_historical_structural_coverage, DataResolver


def test_alias_resolution_for_all_configured_football_and_basketball_leagues():
    """Verify team identity resolution works across all configured football and basketball leagues."""
    # Test Football competitions
    football_test_cases = [
        ("Arsenal FC", 39, "football_team_arsenal"),
        ("RCD Espanyol", 140, "football_team_espanyol"),
        ("AC Milan", 135, "football_team_ac_milan"),
        ("FC Bayern München", 78, "football_team_bayern_munich"),
        ("PSG", 61, "football_team_paris_saint_germain"),
        ("AFC Ajax", 88, "football_team_ajax"),
        ("SL Benfica", 94, "football_team_benfica"),
    ]

    for raw_name, league_id, expected_cid in football_test_cases:
        cid = team_identity.resolve_canonical_team_id(
            raw_name=raw_name,
            provider="api_football",
            provider_team_id=None,
            league_id=league_id,
            sport="football",
            auto_register=False,
        )
        assert cid == expected_cid, f"Failed resolving {raw_name} for football league {league_id}"

    # Test Basketball league 12
    basketball_test_cases = [
        ("Boston Celtics", 12, "basketball_team_boston_celtics"),
        ("LA Lakers", 12, "basketball_team_la_lakers"),
        ("Warriors", 12, "basketball_team_golden_state_warriors"),
        ("Bulls", 12, "basketball_team_chicago_bulls"),
    ]

    for raw_name, league_id, expected_cid in basketball_test_cases:
        cid = team_identity.resolve_canonical_team_id(
            raw_name=raw_name,
            provider="api_basketball",
            provider_team_id=None,
            league_id=league_id,
            sport="basketball",
            auto_register=False,
        )
        assert cid == expected_cid, f"Failed resolving {raw_name} for basketball league {league_id}"


def test_same_club_retains_identical_canonical_id_across_competitions():
    """The same football club retains identical canonical ID across domestic and continental competitions."""
    # Real Madrid in La Liga (140) vs Champions League (2)
    cid_domestic = team_identity.resolve_canonical_team_id("Real Madrid CF", "api_football", 541, league_id=140, sport="football")
    cid_ucl = team_identity.resolve_canonical_team_id("Real Madrid", "api_football", 541, league_id=2, sport="football")

    assert cid_domestic == "football_team_real_madrid"
    assert cid_ucl == "football_team_real_madrid"
    assert cid_domestic == cid_ucl


def test_identity_isolation_between_football_and_basketball():
    """Football and basketball team identities remain strictly isolated by sport."""
    fb_cid = team_identity.resolve_canonical_team_id("Celtics", "api_football", None, league_id=39, sport="football")
    bb_cid = team_identity.resolve_canonical_team_id("Celtics", "api_basketball", None, league_id=12, sport="basketball")

    assert fb_cid is None  # 'Celtics' is not a football team
    assert bb_cid == "basketball_team_boston_celtics"


def test_unresolved_team_name_fails_closed():
    """Unmapped or unknown team names fail closed and return None."""
    cid = team_identity.resolve_canonical_team_id(
        "Completely Unknown FC 12345",
        "api_football",
        None,
        league_id=39,
        sport="football",
        auto_register=False,
    )
    assert cid is None


def test_empty_authoritative_team_list_fails_closed_when_network_disallowed():
    """Coverage validation with empty authoritative team list and allow_network=False fails closed."""
    fixtures = [
        {
            "fixture": {"id": 1, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Team A"}, "away": {"id": 20, "name": "Team B"}},
            "goals": {"home": 2, "away": 1},
            "canonical_home_id": "football_team_39_team_a",
            "canonical_away_id": "football_team_39_team_b",
        }
    ]

    res = _validate_historical_structural_coverage(
        fixtures,
        season=2024,
        league_id=39,
        provider_metadata={"expected_team_ids": [], "allow_network": False},
    )

    assert res["verified"] is False
    assert res["reason"] == "authoritative_team_universe_unavailable"
