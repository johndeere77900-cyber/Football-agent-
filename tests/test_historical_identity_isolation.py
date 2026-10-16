"""
Unit tests for historical team identity isolation across feature calculations, Elo reconstruction, and backtesting (Phase 6).
"""

from unittest.mock import patch
import pytest

import config
import historical_elo
import historical_features
import historical_h2h
import team_identity


def test_1_equal_numeric_ids_from_two_providers_with_different_canonicals_do_not_mix():
    """
    Team ID 10 in Provider A is Alpha (football_team_alpha).
    Team ID 10 in Provider B is Beta (football_team_beta).
    Feature queries for Alpha must not include Provider B matches.
    """
    fixtures = [
        {
            "fixture": {"id": 101, "date": "2024-01-01T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Alpha"}, "away": {"id": 20, "name": "Opponent X"}},
            "goals": {"home": 3, "away": 0},
            "canonical_home_id": "football_team_alpha",
            "canonical_away_id": "football_team_opponent_x",
            "source": "api_football",
        },
        {
            "fixture": {"id": 102, "date": "2024-01-02T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Beta"}, "away": {"id": 30, "name": "Opponent Y"}},
            "goals": {"home": 0, "away": 4},
            "canonical_home_id": "football_team_beta",
            "canonical_away_id": "football_team_opponent_y",
            "source": "football_data_org",
        },
    ]

    cutoff = "2024-02-01T00:00:00+00:00"

    alpha_history = historical_features.team_match_history(fixtures, 10, cutoff, canonical_team_id="football_team_alpha")
    assert len(alpha_history) == 1
    assert alpha_history[0]["fixture"]["id"] == 101

    beta_history = historical_features.team_match_history(fixtures, 10, cutoff, canonical_team_id="football_team_beta")
    assert len(beta_history) == 1
    assert beta_history[0]["fixture"]["id"] == 102


def test_2_target_fixture_excluded_from_own_features():
    """
    The match on 2024-01-10 must not be included in feature calculations at cutoff 2024-01-10T15:00:00.
    """
    fixtures = [
        {
            "fixture": {"id": 201, "date": "2024-01-01T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Alpha"}, "away": {"id": 20, "name": "Beta"}},
            "goals": {"home": 1, "away": 0},
            "canonical_home_id": "football_team_alpha",
            "canonical_away_id": "football_team_beta",
        },
        {
            "fixture": {"id": 202, "date": "2024-01-10T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Alpha"}, "away": {"id": 20, "name": "Beta"}},
            "goals": {"home": 5, "away": 5},
            "canonical_home_id": "football_team_alpha",
            "canonical_away_id": "football_team_beta",
        },
    ]

    cutoff = "2024-01-10T15:00:00+00:00"

    alpha_avg = historical_features.team_goal_averages(fixtures, 10, cutoff, canonical_team_id="football_team_alpha")
    assert alpha_avg["matches"] == 1
    assert alpha_avg["goals_for"] == 1.0


def test_3_chronological_elo_reconstruction_with_canonical_ids():
    """
    Elo ratings are reconstructed using canonical IDs across providers.
    """
    fixtures = [
        {
            "fixture": {"id": 301, "date": "2024-01-01T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Alpha"}, "away": {"id": 20, "name": "Beta"}},
            "goals": {"home": 2, "away": 0},
            "canonical_home_id": "football_team_alpha",
            "canonical_away_id": "football_team_beta",
        },
    ]

    cutoff = "2024-01-05T00:00:00+00:00"

    elo_snap = historical_elo.fixture_elo_snapshot(
        fixtures,
        home_team_id=10,
        away_team_id=20,
        cutoff=cutoff,
        canonical_home_id="football_team_alpha",
        canonical_away_id="football_team_beta",
    )

    assert elo_snap["home_rating"] > 1500.0
    assert elo_snap["away_rating"] < 1500.0


def test_4_equal_numeric_ids_different_providers_independent_elo_history():
    """
    Team ID 10 in Provider A is Alpha (football_team_alpha).
    Team ID 10 in Provider B is Beta (football_team_beta).
    Provider A win updates football_team_alpha, NOT football_team_beta.
    """
    fixtures = [
        {
            "fixture": {"id": 401, "date": "2024-01-01T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Alpha"}, "away": {"id": 20, "name": "Opponent X"}},
            "goals": {"home": 3, "away": 0},
            "canonical_home_id": "football_team_alpha",
            "canonical_away_id": "football_team_opponent_x",
            "source": "api_football",
        },
    ]

    cutoff = "2024-01-05T00:00:00+00:00"

    ratings = historical_elo.reconstruct_ratings(fixtures, cutoff)

    assert ratings.get("football_team_alpha", 1500.0) > 1500.0
    assert ratings.get("football_team_beta") is None

    snap_alpha = historical_elo.fixture_elo_snapshot(
        fixtures, home_team_id=10, away_team_id=20, cutoff=cutoff, canonical_home_id="football_team_alpha", canonical_away_id="football_team_opponent_x"
    )
    snap_beta = historical_elo.fixture_elo_snapshot(
        fixtures, home_team_id=10, away_team_id=30, cutoff=cutoff, canonical_home_id="football_team_beta", canonical_away_id="football_team_opponent_y"
    )

    assert snap_alpha["home_rating"] > 1500.0
    assert snap_beta["home_rating"] == 1500.0
