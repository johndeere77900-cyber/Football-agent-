"""
Adversarial tests for completeness verification and cross-provider fixture identity.
"""

import pytest
import storage
import config
from data_resolver import _validate_historical_structural_coverage

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_adversarial.db")
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    storage.init_db()
    yield

def test_round_robin_subset_of_teams_fails_closed():
    """A perfectly complete round robin among only 18 teams (out of 20 real teams) must fail closed."""
    from team_identity import register_canonical_team_identity
    real_20_teams = [f"football_team_39_team_{i}" for i in range(1, 21)]
    for i, t in enumerate(real_20_teams):
        register_canonical_team_identity(
            canonical_id=t, display_name=f"Team {i+1}", provider="api_football", provider_team_id=str(i+1), league_id=39, sport="football"
        )
    subset_18_teams = real_20_teams[:18]

    fixtures = []
    fid = 1
    for i, h in enumerate(subset_18_teams):
        for j, a in enumerate(subset_18_teams):
            if i == j:
                continue
            fixtures.append({
                "fixture": {"id": fid, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
                "league": {"id": 39, "season": 2024},
                "teams": {"home": {"id": i+1, "name": h}, "away": {"id": j+1, "name": a}},
                "goals": {"home": 1, "away": 0},
                "canonical_home_id": h,
                "canonical_away_id": a,
                "provider_provenance": {"provider": "api_football"}
            })
            fid += 1

    # 18 * 17 = 306 fixtures, perfectly complete among the 18 subset teams
    assert len(fixtures) == 306

    # Structural coverage with 20 real teams as authoritative universe
    res = _validate_historical_structural_coverage(
        fixtures, season=2024, league_id=39, provider_metadata={"expected_team_ids": real_20_teams}
    )
    assert res["verified"] is False
    assert res["reason"] == "team_universe_mismatch"
    assert len(res["missing_team_ids"]) == 2

def test_missing_authoritative_team_universe_fails_closed():
    """Double round robin without authoritative team universe must fail closed."""
    teams = [f"football_team_39_team_{i}" for i in range(1, 21)]
    fixtures = []
    fid = 1
    for i, h in enumerate(teams):
        for j, a in enumerate(teams):
            if i == j:
                continue
            fixtures.append({
                "fixture": {"id": fid, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
                "league": {"id": 39, "season": 2024},
                "teams": {"home": {"id": i+1, "name": h}, "away": {"id": j+1, "name": a}},
                "goals": {"home": 1, "away": 0},
                "canonical_home_id": h,
                "canonical_away_id": a,
                "provider_provenance": {"provider": "api_football"}
            })
            fid += 1

    res = _validate_historical_structural_coverage(
        fixtures, season=2024, league_id=39, provider_metadata={"expected_team_ids": [], "allow_network": False}
    )
    assert res["verified"] is False
    assert res["reason"] == "authoritative_team_universe_unavailable"

def test_stale_team_identities_from_different_season_fails():
    """Team universe containing a relegated team from a different season must fail closed."""
    from team_identity import register_canonical_team_identity
    real_2024_teams = [f"football_team_39_team_{i}" for i in range(1, 21)]
    for i, t in enumerate(real_2024_teams):
        register_canonical_team_identity(
            canonical_id=t, display_name=f"Team {i+1}", provider="api_football", provider_team_id=str(i+1), league_id=39, sport="football"
        )
    # Replace one team with a stale team from 2020
    stale_teams = list(real_2024_teams)
    stale_teams[0] = "football_team_39_relegated_2020"
    register_canonical_team_identity(
        canonical_id=stale_teams[0], display_name="Relegated 2020", provider="api_football", provider_team_id="999", league_id=39, sport="football"
    )

    fixtures = []
    fid = 1
    for i, h in enumerate(real_2024_teams):
        for j, a in enumerate(real_2024_teams):
            if i == j:
                continue
            fixtures.append({
                "fixture": {"id": fid, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
                "league": {"id": 39, "season": 2024},
                "teams": {"home": {"id": i+1, "name": h}, "away": {"id": j+1, "name": a}},
                "goals": {"home": 1, "away": 0},
                "canonical_home_id": h,
                "canonical_away_id": a,
                "provider_provenance": {"provider": "api_football"}
            })
            fid += 1

    res = _validate_historical_structural_coverage(
        fixtures, season=2024, league_id=39, provider_metadata={"expected_team_ids": stale_teams}
    )
    assert res["verified"] is False
    assert res["reason"] == "team_universe_mismatch"
    assert "football_team_39_relegated_2020" in res["missing_team_ids"]
    assert "football_team_39_team_1" in res["unexpected_team_ids"]

def test_duplicate_matches_different_provider_ids():
    """Same match from Provider A (id 101) and Provider B (id 999) with same score is deduplicated."""
    from team_identity import register_canonical_team_identity
    teams = [f"football_team_39_team_{i}" for i in range(1, 21)]
    for i, t in enumerate(teams):
        register_canonical_team_identity(
            canonical_id=t, display_name=f"Team {i+1}", provider="api_football", provider_team_id=str(i+1), league_id=39, sport="football"
        )
    fixtures = []
    fid = 1
    for i, h in enumerate(teams):
        for j, a in enumerate(teams):
            if i == j:
                continue
            fixtures.append({
                "fixture": {"id": fid, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
                "league": {"id": 39, "season": 2024},
                "teams": {"home": {"id": i+1, "name": h}, "away": {"id": j+1, "name": a}},
                "goals": {"home": 1, "away": 0},
                "canonical_home_id": h,
                "canonical_away_id": a,
                "provider_provenance": {"provider": "api_football"}
            })
            fid += 1

    # Add duplicate match from provider B with different fixture ID 99999
    fixtures.append({
        "fixture": {"id": 99999, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 1, "name": teams[0]}, "away": {"id": 2, "name": teams[1]}},
        "goals": {"home": 1, "away": 0},
        "canonical_home_id": teams[0],
        "canonical_away_id": teams[1],
        "provider_provenance": {"provider": "football_data_org"}
    })

    res = _validate_historical_structural_coverage(
        fixtures, season=2024, league_id=39, provider_metadata={"expected_team_ids": teams}
    )
    assert res["verified"] is True
    assert res["distinct_fixture_count"] == 380

def test_conflicting_cross_provider_scores_fails():
    """Same match from Provider A (goals 2-1) and Provider B (goals 0-0) fails with conflicting_cross_provider_match."""
    from team_identity import register_canonical_team_identity
    teams = [f"football_team_39_team_{i}" for i in range(1, 21)]
    for i, t in enumerate(teams):
        register_canonical_team_identity(
            canonical_id=t, display_name=f"Team {i+1}", provider="api_football", provider_team_id=str(i+1), league_id=39, sport="football"
        )
    fixtures = []
    fid = 1
    for i, h in enumerate(teams):
        for j, a in enumerate(teams):
            if i == j:
                continue
            fixtures.append({
                "fixture": {"id": fid, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
                "league": {"id": 39, "season": 2024},
                "teams": {"home": {"id": i+1, "name": h}, "away": {"id": j+1, "name": a}},
                "goals": {"home": 1, "away": 0},
                "canonical_home_id": h,
                "canonical_away_id": a,
                "provider_provenance": {"provider": "api_football"}
            })
            fid += 1

    # Add conflicting match from provider B
    fixtures.append({
        "fixture": {"id": 99999, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 1, "name": teams[0]}, "away": {"id": 2, "name": teams[1]}},
        "goals": {"home": 3, "away": 3},  # Conflicting score!
        "canonical_home_id": teams[0],
        "canonical_away_id": teams[1],
        "provider_provenance": {"provider": "football_data_org"}
    })

    res = _validate_historical_structural_coverage(
        fixtures, season=2024, league_id=39, provider_metadata={"expected_team_ids": teams}
    )
    assert res["verified"] is False
    assert res["reason"] == "conflicting_cross_provider_match"

def test_duplicate_fixture_id_same_provider_conflicting_fails():
    """Duplicate fixture ID within same provider with conflicting scores fails closed."""
    from team_identity import register_canonical_team_identity
    register_canonical_team_identity(canonical_id="football_team_39_team_1", display_name="Team 1", provider="api_football", provider_team_id="1", league_id=39, sport="football")
    register_canonical_team_identity(canonical_id="football_team_39_team_2", display_name="Team 2", provider="api_football", provider_team_id="2", league_id=39, sport="football")
    fixtures = [
        {
            "fixture": {"id": 100, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
            "goals": {"home": 1, "away": 0},
            "canonical_home_id": "football_team_39_team_1",
            "canonical_away_id": "football_team_39_team_2",
            "provider_provenance": {"provider": "api_football"}
        },
        {
            "fixture": {"id": 100, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
            "goals": {"home": 2, "away": 2},  # Conflicting!
            "canonical_home_id": "football_team_39_team_1",
            "canonical_away_id": "football_team_39_team_2",
            "provider_provenance": {"provider": "api_football"}
        }
    ]
    res = _validate_historical_structural_coverage(
        fixtures, season=2024, league_id=39, provider_metadata={"expected_team_ids": ["football_team_39_team_1", "football_team_39_team_2"]}
    )
    assert res["verified"] is False
    assert res["reason"] == "duplicate_same_provider_fixture"

def test_unknown_competition_format_unverifiable():
    """Unknown competition format without authoritative evidence fails closed."""
    fixtures = [
        {
            "fixture": {"id": 100, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
            "league": {"id": 999, "season": 2024},
            "teams": {"home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
            "goals": {"home": 1, "away": 0},
            "canonical_home_id": "football_team_999_team_1",
            "canonical_away_id": "football_team_999_team_2",
            "provider_provenance": {"provider": "api_football"}
        }
    ]
    res = _validate_historical_structural_coverage(
        fixtures, season=2024, league_id=999, provider_metadata={"competition_format": "unknown"}
    )
    assert res["verified"] is False
    assert res["reason"] == "structural_coverage_unverifiable"

def test_uncompleted_match_status_rejected():
    """Fixtures with uncompleted statuses (e.g. NS, PST, HT) cannot contribute to historical completion."""
    from team_identity import register_canonical_team_identity
    teams = [f"football_team_39_team_{i}" for i in range(1, 21)]
    for i, t in enumerate(teams):
        register_canonical_team_identity(
            canonical_id=t, display_name=f"Team {i+1}", provider="api_football", provider_team_id=str(i+1), league_id=39, sport="football"
        )
    fixtures = []
    fid = 1
    for i, h in enumerate(teams):
        for j, a in enumerate(teams):
            if i == j:
                continue
            status_str = "FT" if fid < 380 else "PST"  # 1 fixture postponed!
            fixtures.append({
                "fixture": {"id": fid, "date": "2024-09-01T15:00:00Z", "status": {"short": status_str}},
                "league": {"id": 39, "season": 2024},
                "teams": {"home": {"id": i+1, "name": h}, "away": {"id": j+1, "name": a}},
                "goals": {"home": 1, "away": 0} if status_str == "FT" else {"home": None, "away": None},
                "canonical_home_id": h,
                "canonical_away_id": a,
                "provider_provenance": {"provider": "api_football"}
            })
            fid += 1

    res = _validate_historical_structural_coverage(
        fixtures, season=2024, league_id=39, provider_metadata={"expected_team_ids": teams}
    )
    assert res["verified"] is False
    assert res["reason"] in ("uncompleted_fixture_status", "completed_fixture_missing_goals")

def test_missing_or_malformed_status_object_rejected():
    """Missing or empty status short must fail closed with explicit reason."""
    from team_identity import register_canonical_team_identity
    register_canonical_team_identity(canonical_id="football_team_39_team_1", display_name="Team 1", provider="api_football", provider_team_id="1", league_id=39, sport="football")
    register_canonical_team_identity(canonical_id="football_team_39_team_2", display_name="Team 2", provider="api_football", provider_team_id="2", league_id=39, sport="football")
    meta = {"expected_team_ids": ["football_team_39_team_1", "football_team_39_team_2"]}
    fixture_missing_status = {
        "fixture": {"id": 1, "date": "2024-09-01T15:00:00Z"},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
        "goals": {"home": 1, "away": 0},
        "canonical_home_id": "football_team_39_team_1",
        "canonical_away_id": "football_team_39_team_2",
    }
    res_missing = _validate_historical_structural_coverage([fixture_missing_status], season=2024, league_id=39, provider_metadata=meta)
    assert res_missing["verified"] is False
    assert res_missing["reason"] == "missing_or_invalid_status_object"

    fixture_empty_short = {
        "fixture": {"id": 1, "date": "2024-09-01T15:00:00Z", "status": {"short": ""}},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
        "goals": {"home": 1, "away": 0},
        "canonical_home_id": "football_team_39_team_1",
        "canonical_away_id": "football_team_39_team_2",
    }
    res_empty = _validate_historical_structural_coverage([fixture_empty_short], season=2024, league_id=39, provider_metadata=meta)
    assert res_empty["verified"] is False
    assert res_empty["reason"] == "missing_or_empty_status_short"

    fixture_null_short = {
        "fixture": {"id": 1, "date": "2024-09-01T15:00:00Z", "status": {"short": None}},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 1, "name": "Team 1"}, "away": {"id": 2, "name": "Team 2"}},
        "goals": {"home": 1, "away": 0},
        "canonical_home_id": "football_team_39_team_1",
        "canonical_away_id": "football_team_39_team_2",
    }
    res_null = _validate_historical_structural_coverage([fixture_null_short], season=2024, league_id=39, provider_metadata=meta)
    assert res_null["verified"] is False
    assert res_null["reason"] == "missing_or_empty_status_short"


def test_malformed_or_duplicate_expected_team_ids_manifest_rejected():
    """Manifests with duplicate team IDs, empty strings, None, or unresolvable teams are rejected."""
    from data_resolver import _validate_and_canonicalize_team_manifest

    # Duplicate team IDs
    assert _validate_and_canonicalize_team_manifest(["football_team_39_team_1", "football_team_39_team_1"], league_id=39) == []

    # Empty string or None in manifest
    assert _validate_and_canonicalize_team_manifest(["football_team_39_team_1", ""], league_id=39) == []
    assert _validate_and_canonicalize_team_manifest(["football_team_39_team_1", None], league_id=39) == []

    # Unresolvable team name
    assert _validate_and_canonicalize_team_manifest(["completely_unknown_unmapped_team_name_xyz"], league_id=39) == []


def test_untrusted_provider_metadata_cannot_bypass_manifest_validation():
    """Untrusted provider_metadata with duplicate or unresolvable team IDs fails closed."""
    meta = {"expected_team_ids": ["football_team_39_team_1", "football_team_39_team_1"]}
    res = _validate_historical_structural_coverage([], season=2024, league_id=39, provider_metadata=meta)
    assert res["verified"] is False
    assert res["reason"] == "authoritative_team_universe_unavailable"
