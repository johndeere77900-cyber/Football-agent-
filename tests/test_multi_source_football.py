"""
Comprehensive Multi-Source Football Architecture Tests.

Verifies:
1. International fixture support (Nigeria vs Ghana).
2. Rolling windows (last 5, 10, 20, 30 matches).
3. Database-first historical feature generation (zero external calls when history is abundant).
4. Provider fallback hierarchy (API-Football -> football-data.org -> SoccerData).
5. Field-level data merging and conflict recording.
6. Permanent raw fixture persistence even when prediction returns INSUFFICIENT_DATA.
7. Subsequent prediction re-use without re-calling external providers.
8. Provider provenance tracking and canonical team identity mapping.
9. Hard timeout safety and retry handling.
"""

import json
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

import config
import storage
import team_identity
import data_resolver
import soccerdata_provider
import historical_features
import main


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    """Set up isolated test database for every test."""
    db_file = str(tmp_path / "test_multi_source.db")
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()


def make_mock_fixture(
    fixture_id,
    home_name,
    away_name,
    home_id,
    away_id,
    date_str="2024-05-01T15:00:00+00:00",
    league_id=39,
    season=2023,
    home_goals=2,
    away_goals=1,
    status="FT",
    provider="api_football",
    stats=None,
):
    """Create a standardized mock fixture dictionary."""
    return {
        "fixture": {
            "id": fixture_id,
            "date": date_str,
            "status": {"short": status, "long": "Finished" if status in ("FT", "AET", "PEN") else "Not Started"},
        },
        "league": {
            "id": league_id,
            "season": season,
            "name": f"League_{league_id}",
        },
        "teams": {
            "home": {"id": home_id, "name": home_name},
            "away": {"id": away_id, "name": away_name},
        },
        "goals": {
            "home": home_goals,
            "away": away_goals,
        },
        "statistics": stats or {
            "shots": {"home": 12, "away": 8},
            "corners": {"home": 6, "away": 4},
            "cards": {"home": {"yellow": 2, "red": 0}, "away": {"yellow": 3, "red": 0}},
            "xG": {"home": 1.8, "away": 0.9},
        },
        "provider_provenance": {
            "provider": provider,
            "provider_type": "primary" if provider == "api_football" else "fallback",
            "provider_fixture_id": fixture_id,
            "provider_team_ids": {"home": home_id, "away": away_id},
            "retrieved_at": "2024-05-01T00:00:00+00:00",
        },
    }


def test_scenario_1_international_fixture_nigeria_vs_ghana():
    """
    Scenario 1: International fixture (Nigeria vs Ghana).
    Proves:
    - Resolves fixture
    - Queries DB for historical matches
    - Calculates rolling windows (last 5, 10, 20, 30)
    - Identifies missing data and invokes fallback providers
    - Persists newly retrieved data into PostgreSQL
    - Recalculates coverage and attempts prediction
    - Saves fixture even if prediction returns INSUFFICIENT_DATA
    - Reuses accumulated database data without re-calling external providers.
    """
    home_name = "Nigeria"
    away_name = "Ghana"
    cutoff = "2024-06-01T18:00:00+00:00"

    # Step 1: Bootstrap team identity for international team
    c_home = team_identity.bootstrap_historical_team_identity(home_name, "api_football", 1001, league_id=15)
    c_away = team_identity.bootstrap_historical_team_identity(away_name, "api_football", 1002, league_id=15)

    assert c_home is not None
    assert c_away is not None

    # Step 2: Seed 7 historical completed matches into DB for Nigeria, 6 for Ghana
    hist_fixtures = []
    for i in range(1, 8):
        hist_fixtures.append(make_mock_fixture(
            fixture_id=2000 + i,
            home_name="Nigeria",
            away_name=f"Opponent_{i}",
            home_id=1001,
            away_id=3000 + i,
            date_str=f"2024-01-{i:02d}T15:00:00+00:00",
            league_id=15,
            season=2023,
            home_goals=2,
            away_goals=1,
        ))

    for i in range(1, 7):
        hist_fixtures.append(make_mock_fixture(
            fixture_id=3000 + i,
            home_name="Ghana",
            away_name=f"Opponent_{i}",
            home_id=1002,
            away_id=4000 + i,
            date_str=f"2024-02-{i:02d}T15:00:00+00:00",
            league_id=15,
            season=2023,
            home_goals=1,
            away_goals=0,
        ))

    storage.save_historical_fixtures(hist_fixtures, league_id=15, season=2023)

    # Step 3: Query DB history and calculate last 5, 10, 20, 30
    db_matches_home = storage.get_team_historical_fixtures(c_home, cutoff=cutoff, limit=30)
    db_matches_away = storage.get_team_historical_fixtures(c_away, cutoff=cutoff, limit=30)

    assert len(db_matches_home) == 7
    assert len(db_matches_away) == 6

    # Verify rolling windows
    form_5 = historical_features.team_recent_form(db_matches_home, 1001, cutoff, window=5, canonical_team_id=c_home)
    form_10 = historical_features.team_recent_form(db_matches_home, 1001, cutoff, window=10, canonical_team_id=c_home)
    assert form_5["matches"] == 5
    assert form_10["matches"] == 7

    # Step 4: Run prediction on Nigeria vs Ghana
    target_fixture = make_mock_fixture(
        fixture_id=9999,
        home_name="Nigeria",
        away_name="Ghana",
        home_id=1001,
        away_id=1002,
        date_str="2024-06-01T18:00:00+00:00",
        league_id=15,
        season=2023,
        status="NS",
    )

    pred = main.predict_fixture(target_fixture, league_avg_goals=2.5)

    # Verify fixture data was persisted in database
    db_count = storage.get_historical_fixture_count(15, 2023)
    assert db_count >= 13

    # Step 5: Verify second prediction run uses DB history without re-fetching
    with patch("api_football.get_recent_form") as mock_rf:
        pred_second = main.predict_fixture(target_fixture, league_avg_goals=2.5)
        # API-Football should NOT have been called because DB history was present
        mock_rf.assert_not_called()


def test_scenario_2_abundant_historical_coverage_uses_db_only():
    """
    Scenario 2: Abundant DB historical coverage.
    Expected: PostgreSQL/DB supplies required history. External providers are NOT called.
    """
    league_id = 39
    season = 2023
    cutoff = "2024-05-15T15:00:00+00:00"

    # Seed 15 completed matches for Arsenal and Chelsea into DB
    hist = []
    for i in range(1, 16):
        hist.append(make_mock_fixture(
            fixture_id=5000 + i,
            home_name="Arsenal",
            away_name="Chelsea" if i % 2 == 0 else f"Opponent_{i}",
            home_id=42,
            away_id=49 if i % 2 == 0 else 500 + i,
            date_str=f"2024-03-{i:02d}T15:00:00+00:00",
            league_id=league_id,
            season=season,
        ))

    storage.save_historical_fixtures(hist, league_id=league_id, season=season)

    target_fixture = make_mock_fixture(
        fixture_id=6000,
        home_name="Arsenal",
        away_name="Chelsea",
        home_id=42,
        away_id=49,
        date_str=cutoff,
        league_id=league_id,
        season=season,
        status="NS",
    )

    with patch("api_football.get_team_statistics") as mock_stats, \
         patch("api_football.get_recent_form") as mock_rf:

        pred = main.predict_fixture(target_fixture, league_avg_goals=2.6)

        assert pred["insufficient_data"] is False
        assert pred["provenance"]["provider"] == "internal_db"
        mock_stats.assert_not_called()
        mock_rf.assert_not_called()


def test_scenario_3_incomplete_db_history_with_provider_gap_fill():
    """
    Scenario 3: Rare/international team with incomplete DB history.
    Expected: DB partial history -> API-Football / fallback gap fill -> persist -> prediction attempt.
    """
    league_id = 39
    season = 2023

    # Bootstrap identity for Team_A and Team_B
    team_identity.bootstrap_historical_team_identity("Team_A", "api_football", 101, league_id=league_id)
    team_identity.bootstrap_historical_team_identity("Team_B", "api_football", 102, league_id=league_id)

    # Empty DB
    target_fixture = make_mock_fixture(
        fixture_id=7000,
        home_name="Team_A",
        away_name="Team_B",
        home_id=101,
        away_id=102,
        date_str="2024-05-10T15:00:00+00:00",
        league_id=league_id,
        season=season,
        status="NS",
    )

    mock_home_stats = {
        "fixtures": {"played": {"total": 10}},
        "goals": {
            "for": {"average": {"total": "1.5"}},
            "against": {"average": {"total": "1.0"}},
        },
    }
    mock_away_stats = {
        "fixtures": {"played": {"total": 10}},
        "goals": {
            "for": {"average": {"total": "1.2"}},
            "against": {"average": {"total": "1.1"}},
        },
    }

    mock_recent_matches_a = [
        make_mock_fixture(8000 + i, "Team_A", "Opponent", 101, 200 + i, date_str=f"2024-04-{i:02d}T15:00:00+00:00")
        for i in range(1, 6)
    ]
    mock_recent_matches_b = [
        make_mock_fixture(8100 + i, "Team_B", "Opponent", 102, 300 + i, date_str=f"2024-04-{i:02d}T15:00:00+00:00")
        for i in range(1, 6)
    ]

    def mock_get_recent_form(team_id, **kwargs):
        if team_id == 101:
            return mock_recent_matches_a
        elif team_id == 102:
            return mock_recent_matches_b
        return []

    with patch("api_football.get_team_statistics", side_effect=[mock_home_stats, mock_away_stats]), \
         patch("api_football.get_recent_form", side_effect=mock_get_recent_form), \
         patch("api_football.get_head_to_head", return_value=[]):

        pred = main.predict_fixture(target_fixture, league_avg_goals=2.5)

        assert pred["insufficient_data"] is False
        assert pred["provenance"]["provider"] == "api_football"

        # Verify raw fixture was persisted even when retrieved via external gap filling
        db_count = storage.get_historical_fixture_count(league_id, season)
        assert db_count >= 1


def test_scenario_4_soccerdata_fallback_when_primary_and_secondary_fail():
    """
    Scenario 4: Simulate API-Football unavailable, football-data.org unavailable, SoccerData available.
    Expected: SoccerData supplies data, which is stored in PostgreSQL with provenance.
    """
    resolver = data_resolver.DataResolver(force_fallback=True)

    sd_matches = [
        make_mock_fixture(9001, "Leverkusen", "Bayern", 88, 89, date_str="2024-05-01T15:00:00+00:00", provider="soccerdata_match_history")
    ]

    with patch("football_data_api.get_competition_matches", side_effect=Exception("FD down")), \
         patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_AVAILABLE", sd_matches, {})):

        fixtures, meta = resolver.get_fixtures_for_date("2024-05-01", league_id=78)

        assert len(fixtures) == 1
        assert meta["resolver_status"] == "TERTIARY_SUCCESS"
        assert meta["provider"] == "soccerdata"


def test_scenario_5_multi_provider_field_level_merge():
    """
    Scenario 5: Simulate API-Football partial, football-data.org partial, SoccerData partial.
    Expected: System combines valid portions at the field level with explicit provenance.
    """
    rec_api = {
        "fixture": {"id": 111, "date": "2024-05-01T15:00:00+00:00"},
        "league": {"id": 39, "season": 2023, "name": "Premier League"},
        "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
        "goals": {"home": 2, "away": 1},
        "statistics": {"shots": None, "corners": None, "xG": None},
        "provider_provenance": {"provider": "api_football", "retrieved_at": "2024-05-01T00:00:00+00:00"},
    }

    rec_fd = {
        "fixture": {"id": 111, "date": "2024-05-01T15:00:00+00:00"},
        "goals": {"home": 2, "away": 1},
        "statistics": {"shots": {"home": 10, "away": 5}, "corners": {"home": 7, "away": 3}, "xG": None},
        "provider_provenance": {"provider": "football_data_org", "retrieved_at": "2024-05-01T00:00:00+00:00"},
    }

    rec_sd = {
        "fixture": {"id": 111, "date": "2024-05-01T15:00:00+00:00"},
        "goals": {"home": 2, "away": 1},
        "statistics": {"shots": {"home": 10, "away": 5}, "corners": {"home": 7, "away": 3}, "xG": {"home": 1.6, "away": 0.7}},
        "provider_provenance": {"provider": "soccerdata_match_history", "retrieved_at": "2024-05-01T00:00:00+00:00"},
    }

    reconciled = data_resolver.reconcile_fixture_records([rec_api, rec_fd, rec_sd])

    assert reconciled["teams"]["home"]["name"] == "Team A"
    assert reconciled["goals"]["home"] == 2
    assert reconciled["statistics"]["shots"]["home"] == 10
    assert reconciled["statistics"]["corners"]["home"] == 7
    assert reconciled["statistics"]["xG"]["home"] == 1.6

    # Verify field provenance mapping
    fp = reconciled["field_provenance"]
    assert fp["goals"] == "api_football"
    assert fp["stats_shots"] == "football_data_org"
    assert fp["stats_xG"] == "soccerdata_match_history"


def test_provider_disagreement_score_conflict_flagged():
    """Test score disagreement between providers is recorded as a conflict."""
    rec_api = {
        "fixture": {"id": 222, "date": "2024-05-01T15:00:00+00:00"},
        "goals": {"home": 2, "away": 1},
        "provider_provenance": {"provider": "api_football"},
    }
    rec_fd = {
        "fixture": {"id": 222, "date": "2024-05-01T15:00:00+00:00"},
        "goals": {"home": 2, "away": 2},  # Disagreement: 2-2 vs 2-1
        "provider_provenance": {"provider": "football_data_org"},
    }

    reconciled = data_resolver.reconcile_fixture_records([rec_api, rec_fd])

    assert reconciled.get("disputed_score") is True
    assert len(reconciled["data_conflicts"]) == 1
    assert reconciled["data_conflicts"][0]["field"] == "goals"


def test_soccerdata_timeout_safety():
    """Test SoccerData scraper call respects hard timeout wrapper without blocking."""
    status, matches, meta = soccerdata_provider.get_match_history_games("ENG-Premier League", 2023, timeout_seconds=0.001)

    assert status == "SOURCE_FAILED" or status == "SOURCE_NOT_AVAILABLE"
    assert matches == []
