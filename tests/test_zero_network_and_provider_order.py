"""
Tests for zero-network COMPLETE dataset revalidation and strict provider execution ordering.
"""

from tests import completion_helpers
from unittest.mock import patch, MagicMock
import pytest
import storage
import config
import data_resolver
from data_resolver import DataResolver
import historical_sync

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_zero_network.db")
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    storage.init_db()
    yield

def test_complete_dataset_zero_network_calls():
    """Verify that a COMPLETE and structurally valid dataset does NOT invoke any external network endpoints or resolvers."""
    league_id = 39
    season = 2024

    # Store a complete 380 fixture dataset for double round robin
    teams = [f"football_team_39_team_{i}" for i in range(1, 21)]
    for i, t in enumerate(teams, start=1):
        storage.save_team_identity("football", t, "football_data_csv", str(i), f"Team {i}", f"Team {i}", league_id=league_id)

    fixtures = []
    fid = 1
    for i, h in enumerate(teams, start=1):
        for j, a in enumerate(teams, start=1):
            if i == j:
                continue
            fixtures.append({
                "fixture": {"id": fid, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
                "league": {"id": league_id, "season": season},
                "teams": {"home": {"id": i, "name": f"Team {i}"}, "away": {"id": j, "name": f"Team {j}"}},
                "goals": {"home": 1, "away": 0},
                "canonical_home_id": h,
                "canonical_away_id": a,
                "provider_provenance": {"provider": "football_data_csv"}
            })
            fid += 1

    storage.save_historical_fixtures(fixtures, league_id, season, source="football_data_csv")
    completion_helpers.mark_complete(
        league_id, season, fixture_count=len(fixtures), source="football_data_csv", expected_pages=1, pages_completed=1, acquisition_complete=True, expected_team_ids=teams
    )

    # Patch all network entry points to raise RuntimeError if called
    with patch("requests.get", side_effect=RuntimeError("Network call requested!")), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("Network call requested!")), \
         patch("api_football._get", side_effect=RuntimeError("Network call requested!")), \
         patch("api_football.get_league_standings", side_effect=RuntimeError("Network call requested!")), \
         patch("football_data_api.get_competition_matches", side_effect=RuntimeError("Network call requested!")), \
         patch("football_data_api.get_competition_teams", side_effect=RuntimeError("Network call requested!")), \
         patch("soccerdata_provider.get_match_history_games", side_effect=RuntimeError("Network call requested!")), \
         patch("soccerdata_provider.get_sofascore_historical_games", side_effect=RuntimeError("Network call requested!")):

        # 1. Test sync_historical_fixtures
        res = historical_sync.sync_historical_fixtures(league_id, season)
        assert res["status"] == "COMPLETE"
        assert res["api_requests_consumed"] == 0

        # 2. Test DataResolver.get_league_fixtures_page page 1 and page 2
        resolver = DataResolver()
        page_res = resolver.get_league_fixtures_page(league_id, season, page=1)
        assert page_res["source"] == "internal_db"
        assert len(page_res["fixtures"]) == 380

        page_res_p2 = resolver.get_league_fixtures_page(league_id, season, page=2)
        assert page_res_p2["source"] == "internal_db"
        assert len(page_res_p2["fixtures"]) == 0

        # 3. Test verify_persisted_historical_datasets offline audit
        audit_res = historical_sync.verify_persisted_historical_datasets(league_id=league_id, season=season)
        assert audit_res["success"] is True


def test_verification_audit_future_season_zero_records_fails():
    """Verify that a future season dataset with zero records fails verification."""
    future_season = 2028
    audit_res = historical_sync.verify_persisted_historical_datasets(league_id=39, season=future_season)
    assert audit_res["success"] is False
    assert audit_res["failed_count"] == 1


def test_api_football_normalization_provenance_is_secondary():
    """Verify that API-Football fixture normalization classifies provider_type as 'secondary'."""
    raw_fixture = {
        "fixture": {"id": 101, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 2, "name": "Chelsea"}},
        "goals": {"home": 2, "away": 1},
    }
    norm = data_resolver._normalize_api_football_fixture(raw_fixture, league_id=39, season=2024)
    assert norm["provider_provenance"]["provider"] == "api_football"
    assert norm["provider_provenance"]["provider_type"] == "secondary"

def test_sufficient_primary_coverage_prevents_secondary_network_calls(monkeypatch):
    """
    Verify that when primary sources (CSV / SoccerData) return sufficient, structurally complete coverage,
    ZERO secondary network endpoints are called.
    """
    league_id = 39
    season = 2024

    teams = [f"football_team_39_team_{i}" for i in range(1, 21)]
    for i, t in enumerate(teams, start=1):
        storage.save_team_identity("football", t, "football_data_csv", str(i), f"Team {i}", f"Team {i}", league_id=league_id)

    # Persist expected team manifest into DB
    storage.mark_historical_dataset_incomplete(league_id, season, fixture_count=0, expected_team_ids=teams)

    # Generate 380 complete CSV fixtures
    csv_fixtures = []
    fid = 1
    for i, h in enumerate(teams, start=1):
        for j, a in enumerate(teams, start=1):
            if i == j:
                continue
            csv_fixtures.append({
                "fixture": {"id": fid, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
                "league": {"id": league_id, "season": season},
                "teams": {"home": {"id": i, "name": f"Team {i}"}, "away": {"id": j, "name": f"Team {j}"}},
                "goals": {"home": 1, "away": 0},
                "canonical_home_id": h,
                "canonical_away_id": a,
                "provider_provenance": {"provider": "football_data_csv"}
            })
            fid += 1

    monkeypatch.setattr("data_resolver._fetch_direct_csv_league_fixtures", lambda lid, ssn, *args, **kwargs: csv_fixtures)

    # Instrument all secondary network entry points to fail if called
    with patch("api_football.get_league_fixtures_page", side_effect=RuntimeError("Secondary API-Football fixtures called!")), \
         patch("api_football.get_league_standings", side_effect=RuntimeError("Secondary API-Football standings called!")), \
         patch("football_data_api.get_competition_matches", side_effect=RuntimeError("Secondary football-data.org matches called!")), \
         patch("football_data_api.get_competition_teams", side_effect=RuntimeError("Secondary football-data.org teams called!")):

        resolver = DataResolver()
        res = resolver.get_league_fixtures_page(league_id, season, page=1)
        assert res["source"] == "football_data_csv"
        assert len(res["fixtures"]) == 380


def test_strict_provider_execution_ordering(monkeypatch):
    """
    Verify the approved historical football provider order:
    direct CSV -> SoccerData FBref -> SoccerData MatchHistory -> SoccerData Sofascore,
    and confirm the retired secondary API providers are never consulted.
    """
    league_id = 39
    season = 2024

    calls = []

    def mock_csv(lid, ssn, *args, **kwargs):
        calls.append("football_data_csv")
        return []

    def mock_sd_fb(code, ssn, *args, **kwargs):
        calls.append("soccerdata_fbref")
        return ("SOURCE_UNAVAILABLE", [], {})

    def mock_sd_mh(code, ssn, *args, **kwargs):
        calls.append("soccerdata_match_history")
        return ("SOURCE_UNAVAILABLE", [], {})

    def mock_sd_ss(code, ssn, *args, **kwargs):
        calls.append("soccerdata_sofascore")
        return ("SOURCE_UNAVAILABLE", [], {})

    def mock_af_page(*args, **kwargs):
        calls.append("api_football_fixtures")
        return {"fixtures": [], "expected_pages": 1, "page": 1}

    def mock_fd_matches(*args, **kwargs):
        calls.append("football_data_org_matches")
        return []

    def mock_fd_teams(*args, **kwargs):
        calls.append("football_data_org_teams")
        return []

    monkeypatch.setattr("data_resolver._fetch_direct_csv_league_fixtures", mock_csv)
    monkeypatch.setattr("soccerdata_provider.get_fbref_games", mock_sd_fb)
    monkeypatch.setattr("soccerdata_provider.get_match_history_games", mock_sd_mh)
    monkeypatch.setattr("soccerdata_provider.get_sofascore_historical_games", mock_sd_ss)
    monkeypatch.setattr("api_football.get_league_fixtures_page", mock_af_page)
    monkeypatch.setattr("football_data_api.get_competition_matches", mock_fd_matches)
    monkeypatch.setattr("football_data_api.get_competition_teams", mock_fd_teams)

    resolver = DataResolver()
    resolver.get_league_fixtures_page(league_id, season, page=1)

    expected_order = ["football_data_csv", "soccerdata_fbref", "soccerdata_match_history", "soccerdata_sofascore"]
    assert calls[:4] == expected_order

    # The retired secondary API providers must never be used for historical fixtures.
    assert "api_football_fixtures" not in calls
    assert "football_data_org_matches" not in calls
    assert "football_data_org_teams" not in calls
