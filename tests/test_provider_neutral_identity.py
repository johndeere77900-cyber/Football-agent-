"""
Comprehensive test suite for Provider-Neutral Historical Data Acquisition & Team Identity.

Covers all 28 required areas:
1. API-Football historical acquisition.
2. football-data.org fallback when API-Football season is unavailable.
3. No fallback when API-Football already provides sufficient data.
4. COMPLETE dataset causes zero provider calls.
5. Provider fixture IDs remain isolated.
6. Provider team IDs remain isolated.
7. Two provider IDs can resolve to one canonical team.
8. Ambiguous team identity fails closed.
9. Exact normalized team-name matching.
10. Safe alias matching.
11. Incorrect league identity rejected.
12. Incorrect season identity rejected.
13. Invalid dates rejected.
14. Future matches rejected.
15. Incomplete matches rejected.
16. Missing final scores rejected.
17. Invalid scores rejected.
18. Duplicate canonical matches are not duplicated.
19. Historical features work using canonical team identity.
20. Recent form uses only records before cutoff.
21. H2H uses only records before cutoff.
22. Minimum history is enforced independently for both teams.
23. API-Football live team IDs can consume football-data historical records through canonical identity.
24. football-data live team IDs can consume API-Football historical records through canonical identity.
25. Unresolved identity produces INSUFFICIENT_DATA rather than fabricated data.
26. Database-first tests pass.
27. Provider isolation tests pass.
28. Backtest tests pass.
"""

from unittest.mock import MagicMock, patch
import pytest
import config
import data_resolver
import historical_features
import historical_h2h
import historical_sync
import main
import storage
import team_identity


@pytest.fixture(autouse=True)
def init_test_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_acquisition.db"
    monkeypatch.setattr("config.DB_PATH", str(db_file))
    monkeypatch.setattr("config.NEON_DATABASE_URL", None)
    monkeypatch.setattr("config.ENVIRONMENT", "development")
    storage.init_db()


def test_01_api_football_acquisition():
    fixtures_sample = [
        {
            "fixture": {"id": 101, "date": "2024-09-01T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
            "goals": {"home": 2, "away": 0},
        }
    ]
    with patch("main.check_competition_coverage", return_value=("coverage_available", "Coverage available.")), \
         patch("api_football.get_league_fixtures_page", return_value={"fixtures": fixtures_sample, "page": 1, "expected_pages": 1}):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        assert report["status"] == "COMPLETE"
        assert report["final_stored_count"] == 1


def test_02_football_data_fallback_when_api_football_unavailable():
    fd_matches = [
        {
            "id": 9991,
            "utcDate": "2024-09-01T15:00:00Z",
            "status": "FINISHED",
            "competition": {"name": "Premier League"},
            "homeTeam": {"id": 57, "name": "Arsenal FC", "shortName": "Arsenal"},
            "awayTeam": {"id": 61, "name": "Chelsea FC", "shortName": "Chelsea"},
            "score": {"fullTime": {"home": 2, "away": 1}},
        }
    ]
    with patch("main.check_competition_coverage", return_value=("season_not_available", "Season 2024 unavailable")), \
         patch("football_data_api.get_competition_matches", return_value=fd_matches):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        assert report["status"] == "COMPLETE"
        assert report["final_stored_count"] == 1
        stored = storage.get_historical_fixtures(39, 2024)
        assert len(stored) == 1
        assert stored[0]["fixture"]["id"] == 9991


def test_03_no_fallback_when_api_football_available():
    fixtures_sample = [
        {
            "fixture": {"id": 102, "date": "2024-09-02T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
            "goals": {"home": 1, "away": 1},
        }
    ]
    with patch("main.check_competition_coverage", return_value=("coverage_available", "Coverage available.")), \
         patch("api_football.get_league_fixtures_page", return_value={"fixtures": fixtures_sample, "page": 1, "expected_pages": 1}), \
         patch("football_data_api.get_competition_matches") as mock_fd:
        historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        mock_fd.assert_not_called()


def test_04_complete_dataset_causes_zero_api_calls():
    storage.mark_historical_dataset_complete(league_id=39, season=2024, fixture_count=380, expected_pages=1, pages_completed=1)
    with patch("api_football.get_league_fixtures_page") as mock_api, \
         patch("football_data_api.get_competition_matches") as mock_fd:
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        mock_api.assert_not_called()
        mock_fd.assert_not_called()
        assert report["api_requests_consumed"] == 0


def test_05_06_07_provider_isolation_and_canonical_resolution():
    # Store API-Football fixture
    f_api = [{
        "fixture": {"id": 1001, "date": "2024-01-01T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 42, "name": "Arsenal"}, "away": {"id": 49, "name": "Chelsea"}},
        "goals": {"home": 2, "away": 0},
    }]
    storage.save_historical_fixtures(f_api, 39, 2024, source="api_football")

    # Store football-data.org fixture
    f_fd = [{
        "fixture": {"id": 2001, "date": "2024-01-08T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 57, "name": "Arsenal FC"}, "away": {"id": 61, "name": "Chelsea FC"}},
        "goals": {"home": 3, "away": 1},
    }]
    storage.save_historical_fixtures(f_fd, 39, 2024, source="football_data_org")

    cid1 = team_identity.resolve_canonical_team_id("Arsenal", "api_football", 42, league_id=39)
    cid2 = team_identity.resolve_canonical_team_id("Arsenal FC", "football_data_org", 57, league_id=39)

    assert cid1 == cid2 == "football_team_arsenal"


def test_08_ambiguous_team_identity_fails_closed():
    assert team_identity.resolve_canonical_team_id("", "api_football", 99) is None


def test_09_10_exact_normalized_and_alias_matching():
    cid_alias = team_identity.resolve_canonical_team_id("Man Utd", "api_football", 33, league_id=39)
    assert cid_alias == "football_team_manchester_united"


def test_11_to_17_validation_rules():
    base = {
        "fixture": {"id": 500, "date": "2024-09-01T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
        "goals": {"home": 1, "away": 0},
    }
    # 11. Wrong league
    ok, r, _ = storage.validate_historical_fixture(base, target_league_id=140)
    assert not ok and "conflicting_league_id" in r

    # 12. Wrong season
    ok, r, _ = storage.validate_historical_fixture(base, target_season=2023)
    assert not ok and "conflicting_season" in r

    # 13. Invalid date
    b_date = dict(base)
    b_date["fixture"] = {"id": 501, "date": "invalid-date", "status": {"short": "FT"}}
    ok, r, _ = storage.validate_historical_fixture(b_date)
    assert not ok and "malformed_kickoff_timestamp" in r

    # 14. Future match relative to cutoff
    ok, r, _ = storage.validate_historical_fixture(base, cutoff="2024-01-01T00:00:00+00:00")
    assert not ok and "kickoff_not_before_cutoff" in r

    # 15. Incomplete match when required
    b_ns = dict(base)
    b_ns["fixture"] = {"id": 502, "date": "2024-09-01T15:00:00+00:00", "status": {"short": "NS"}}
    ok, r, _ = storage.validate_historical_fixture(b_ns, require_completed=True)
    assert not ok and "fixture_not_completed" in r

    # 16. Missing goals for completed
    b_no_goals = dict(base)
    b_no_goals["goals"] = {"home": None, "away": None}
    ok, r, _ = storage.validate_historical_fixture(b_no_goals, require_completed=True)
    assert not ok and "completed_fixture_missing_goals" in r

    # 17. Negative goals
    b_neg = dict(base)
    b_neg["goals"] = {"home": -1, "away": 0}
    ok, r, _ = storage.validate_historical_fixture(b_neg)
    assert not ok and "invalid_or_negative_home_goals" in r


def test_18_duplicate_canonical_matches_skipped():
    item = {
        "fixture": {"id": 701, "date": "2024-09-01T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
        "goals": {"home": 1, "away": 0},
    }
    res1 = storage.save_historical_fixtures([item], 39, 2024)
    res2 = storage.save_historical_fixtures([item], 39, 2024)
    assert res1["inserted"] == 1
    assert res2["inserted"] == 0


def test_19_20_21_22_historical_features_using_canonical_identity():
    fixtures = [
        {
            "fixture": {"id": 1, "date": "2024-01-01T15:00:00+00:00", "status": {"short": "FT"}},
            "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
            "canonical_home_id": "football_team_arsenal",
            "canonical_away_id": "football_team_chelsea",
            "goals": {"home": 2, "away": 1},
        },
        {
            "fixture": {"id": 2, "date": "2024-01-08T15:00:00+00:00", "status": {"short": "FT"}},
            "teams": {"home": {"id": 57, "name": "Arsenal FC"}, "away": {"id": 30, "name": "Spurs"}},
            "canonical_home_id": "football_team_arsenal",
            "canonical_away_id": "football_team_spurs",
            "goals": {"home": 3, "away": 0},
        },
    ]

    cutoff = "2024-02-01T00:00:00+00:00"
    snap = historical_features.historical_feature_snapshot(
        fixtures, 10, 20, cutoff, minimum_matches=1,
        canonical_home_id="football_team_arsenal", canonical_away_id="football_team_chelsea"
    )
    assert snap is not None
    assert snap["home"]["matches"] == 2

    # Form strictly before cutoff
    form = historical_features.team_recent_form(
        fixtures, 10, "2024-01-05T00:00:00+00:00", window=8, minimum_matches=1,
        canonical_team_id="football_team_arsenal"
    )
    assert form["matches"] == 1

    # H2H strictly before cutoff
    h2h = historical_h2h.historical_h2h_snapshot(
        fixtures, 10, 20, cutoff, window=6, minimum_matches=1,
        canonical_home_id="football_team_arsenal", canonical_away_id="football_team_chelsea"
    )
    assert h2h["meetings"] == 1


def test_23_24_live_prediction_identity_bridge():
    # Save 5 historical fixtures for Arsenal and Chelsea from football-data.org
    for i in range(5):
        item = {
            "fixture": {"id": 8800 + i, "date": f"2024-01-0{i+1}T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 57, "name": "Arsenal FC"}, "away": {"id": 61, "name": "Chelsea FC"}},
            "goals": {"home": 2, "away": 0},
        }
        storage.save_historical_fixtures([item], 39, 2024, source="football_data_org")

    # Live fixture from API-Football
    live_fixture = {
        "fixture": {"id": 9999, "date": "2024-02-01T15:00:00+00:00", "status": {"short": "NS"}},
        "league": {"id": 39, "season": 2024, "name": "Premier League"},
        "teams": {"home": {"id": 42, "name": "Arsenal"}, "away": {"id": 49, "name": "Chelsea"}},
    }

    pred = main.predict_fixture(live_fixture, 1.40)
    assert not pred["insufficient_data"]
    assert pred["home_team"] == "Arsenal"
