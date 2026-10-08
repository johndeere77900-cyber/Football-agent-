"""
Regression tests for provider identity isolation and secondary completeness logic.

Covers requirement 4:
1. Same numeric team ID across two providers does not cross-match.
2. Same numeric fixture ID across providers remains isolated.
3. H2H cannot cross-match providers by numeric ID.
4. Partial football-data.org data cannot become COMPLETE.
5. Verified canonical mappings still work.
"""

from unittest.mock import patch
import pytest

import config
import historical_features
import historical_h2h
import historical_sync
import storage
import team_identity


@pytest.fixture(autouse=True)
def init_test_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_provider_identity_regression.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()


def test_1_same_numeric_team_id_across_providers_does_not_cross_match():
    """
    Team ID 50 in API-Football is Team Alpha.
    Team ID 50 in football-data.org is Team Beta.
    With canonical IDs provided, historical_features functions must not mix history across providers.
    """
    fixtures = [
        {
            "fixture": {"id": 1001, "date": "2024-01-01T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 50, "name": "Team Alpha (API-Football)"}, "away": {"id": 20, "name": "Opponent A"}},
            "goals": {"home": 3, "away": 0},
            "canonical_home_id": "football_team_alpha",
            "canonical_away_id": "football_team_opponent_a",
            "source": "api_football",
        },
        {
            "fixture": {"id": 2002, "date": "2024-01-02T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 50, "name": "Team Beta (football-data.org)"}, "away": {"id": 30, "name": "Opponent B"}},
            "goals": {"home": 0, "away": 5},
            "canonical_home_id": "football_team_beta",
            "canonical_away_id": "football_team_opponent_b",
            "source": "football_data_org",
        },
    ]

    cutoff = "2024-02-01T00:00:00+00:00"

    # Query history for Team Alpha (canonical_team_id="football_team_alpha") using numeric team_id=50
    history_alpha = historical_features.team_match_history(
        fixtures, team_id=50, cutoff=cutoff, canonical_team_id="football_team_alpha"
    )
    assert len(history_alpha) == 1
    assert history_alpha[0]["fixture"]["id"] == 1001

    # Query goal averages for Team Alpha
    avg_alpha = historical_features.team_goal_averages(
        fixtures, team_id=50, cutoff=cutoff, canonical_team_id="football_team_alpha"
    )
    assert avg_alpha is not None
    assert avg_alpha["matches"] == 1
    assert avg_alpha["goals_for"] == 3.0

    # Query goal averages for Team Beta using same numeric team_id=50 but different canonical_team_id
    avg_beta = historical_features.team_goal_averages(
        fixtures, team_id=50, cutoff=cutoff, canonical_team_id="football_team_beta"
    )
    assert avg_beta is not None
    assert avg_beta["matches"] == 1
    assert avg_beta["goals_for"] == 0.0


def test_2_same_numeric_fixture_id_across_providers_remains_isolated():
    """
    Fixture ID 888 exists in both API-Football and football-data.org with different records/goals.
    DB storage isolates them by composite primary key (source, fixture_id).
    """
    f_api = [{
        "fixture": {"id": 888, "date": "2024-05-01T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
        "goals": {"home": 2, "away": 1},
    }]

    f_fd = [{
        "fixture": {"id": 888, "date": "2024-05-01T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 57, "name": "Arsenal FC"}, "away": {"id": 61, "name": "Chelsea FC"}},
        "goals": {"home": 0, "away": 0},
    }]

    res_api = storage.save_historical_fixtures(f_api, 39, 2024, source="api_football")
    assert res_api["inserted"] == 1

    res_fd = storage.save_historical_fixtures(f_fd, 39, 2024, source="football_data_org")
    assert res_fd["inserted"] == 1

    stored = storage.get_historical_fixtures(39, 2024)
    assert len(stored) == 2

    goals = {item["goals"]["home"] for item in stored}
    assert goals == {2, 0}


def test_3_h2h_cannot_cross_match_providers_by_numeric_id():
    """
    Fixture 1 uses numeric IDs home=10, away=20 (Provider A, canonicals alpha and beta).
    Fixture 2 uses numeric IDs home=10, away=20 (Provider B, canonicals gamma and delta).
    When querying H2H for alpha and beta, fixture 2 must be excluded.
    """
    fixtures = [
        {
            "fixture": {"id": 101, "date": "2024-01-01T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Alpha"}, "away": {"id": 20, "name": "Beta"}},
            "goals": {"home": 1, "away": 0},
            "canonical_home_id": "football_team_alpha",
            "canonical_away_id": "football_team_beta",
            "source": "api_football",
        },
        {
            "fixture": {"id": 102, "date": "2024-01-10T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Gamma"}, "away": {"id": 20, "name": "Delta"}},
            "goals": {"home": 4, "away": 4},
            "canonical_home_id": "football_team_gamma",
            "canonical_away_id": "football_team_delta",
            "source": "football_data_org",
        },
    ]

    cutoff = "2024-02-01T00:00:00+00:00"

    h2h_matches = historical_h2h.historical_h2h_matches(
        fixtures,
        home_team_id=10,
        away_team_id=20,
        cutoff=cutoff,
        canonical_home_id="football_team_alpha",
        canonical_away_id="football_team_beta",
    )
    assert len(h2h_matches) == 1
    assert h2h_matches[0]["fixture"]["id"] == 101

    snapshot = historical_h2h.historical_h2h_snapshot(
        fixtures,
        home_team_id=10,
        away_team_id=20,
        cutoff=cutoff,
        canonical_home_id="football_team_alpha",
        canonical_away_id="football_team_beta",
    )
    assert snapshot is not None
    assert snapshot["meetings"] == 1
    assert snapshot["wins"] == 1
    assert snapshot["goals_for"] == 1.0


def test_4_football_data_org_data_can_become_complete():
    """
    When primary provider API-Football is unavailable and secondary provider
    football-data.org returns valid complete dataset, historical_sync allows
    the dataset to become COMPLETE with provider-neutral completion.
    """
    teams = list(range(1, 21))
    pairings = [(h, a) for h in teams for a in teams if h != a]
    fd_matches = [
        {
            "id": 99900 + i,
            "utcDate": "2024-08-15T19:00:00Z",
            "status": "FINISHED",
            "homeTeam": {"id": h_id, "name": f"Team {h_id}"},
            "awayTeam": {"id": a_id, "name": f"Team {a_id}"},
            "score": {"fullTime": {"home": 2, "away": 1}},
            "competition": {"code": "PL"},
            "season": {"startDate": "2024-08-01", "endDate": "2025-05-31"},
        }
        for i, (h_id, a_id) in enumerate(pairings)
    ]

    fd_response = {
        "matches": fd_matches,
        "metadata": {
            "count": 380,
            "played": 380,
            "first": "2024-08-15",
            "last": "2025-05-25",
            "competition_code": "PL",
            "season": 2024,
        },
    }

    with patch("main.check_competition_coverage", return_value=("season_not_available", "Season 2024 unavailable")), \
         patch("football_data_api.get_competition_matches", return_value=fd_response):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "COMPLETE"
    st = storage.get_historical_dataset_status(39, 2024)
    assert st["status"] == "COMPLETE"
    assert st["acquisition_complete"] is True


def test_4_b_partial_football_data_org_data_cannot_become_complete():
    """
    When secondary provider football-data.org returns partial/insufficient data (is_partial=True),
    historical_sync must fail closed and keep dataset status INCOMPLETE.
    """
    fd_match = {
        "id": 99911,
        "utcDate": "2024-08-15T19:00:00Z",
        "status": "FINISHED",
        "homeTeam": {"id": 57, "name": "Arsenal FC"},
        "awayTeam": {"id": 61, "name": "Chelsea FC"},
        "score": {"fullTime": {"home": 2, "away": 1}},
        "competition": {"code": "PL"},
        "season": {"startDate": "2024-08-01", "endDate": "2025-05-31"},
    }

    fd_response = {
        "matches": [fd_match],
        "metadata": {
            "count": 1,
            "played": 1,
            "first": "2024-08-15",
            "last": "2024-08-15",
            "competition_code": "PL",
            "season": 2024,
            "is_partial": True,
        },
    }

    with patch("main.check_competition_coverage", return_value=("season_not_available", "Season 2024 unavailable")), \
         patch("football_data_api.get_competition_matches", return_value=fd_response):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"
    st = storage.get_historical_dataset_status(39, 2024)
    assert st["status"] == "INCOMPLETE"
    assert st["acquisition_complete"] is False


def test_5_verified_canonical_mappings_still_work():
    """
    Verify that canonical mapping resolutions continue to succeed for known teams.
    """
    cid_epl = team_identity.resolve_canonical_team_id("Arsenal", "api_football", 42, league_id=39)
    assert cid_epl == "football_team_arsenal"

    cid_fd = team_identity.resolve_canonical_team_id("Arsenal FC", "football_data_org", 57, league_id=39)
    assert cid_fd == "football_team_arsenal"

    cid_inter = team_identity.resolve_canonical_team_id("Inter", "api_football", 108, league_id=135)
    assert cid_inter == "football_team_inter_milan"
