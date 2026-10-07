import json
import pytest
from unittest.mock import patch, MagicMock
import config
import storage
from data_resolver import DataResolver, LEAGUE_TO_SD_MH_CODE, LEAGUE_TO_SD_SOFASCORE_CODE, _validate_and_filter_tertiary_matches
import soccerdata_provider
import historical_sync
import team_identity


@pytest.fixture(autouse=True)
def init_test_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_predictions.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    storage.init_db()


# Assertion 1: Real numeric MatchHistory & Sofascore fixture ID is retained unchanged (BIGINT int)
def test_1_real_numeric_fixture_id_retained():
    raw_mh = {
        "game_id": 500101,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 2,
        "away_score": 1,
        "league": "Premier League",
        "season": 2024,
    }
    norm_mh = soccerdata_provider._normalize_match_history_row(raw_mh, league_id=39, season=2024)
    assert norm_mh is not None
    assert norm_mh["fixture"]["id"] == 500101
    assert isinstance(norm_mh["fixture"]["id"], int)

    raw_ss = {
        "game_id": 12345678,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 2,
        "away_score": 1,
        "league": "Premier League",
        "season": 2024,
    }
    norm_ss = soccerdata_provider._normalize_sofascore_row(raw_ss, league_id=39, season=2024)
    assert norm_ss is not None
    assert norm_ss["fixture"]["id"] == 12345678
    assert isinstance(norm_ss["fixture"]["id"], int)


# Assertion 2 & 3: "sd_mh_<id>" and "sd_ss_<id>" string prefixes are rejected for permanent fixture.id
def test_2_3_prefixed_fixture_ids_rejected():
    record_prefixed_mh = {
        "fixture": {"id": "sd_mh_101", "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024, "name": "Premier League"},
        "teams": {"home": {"id": 101, "name": "Arsenal"}, "away": {"id": 102, "name": "Chelsea"}},
        "goals": {"home": 2, "away": 1},
        "provider_provenance": {"provider": "soccerdata_match_history"},
    }
    res_mh = _validate_and_filter_tertiary_matches([record_prefixed_mh], league_id=39, season=2024)
    assert len(res_mh) == 0

    record_prefixed_ss = {
        "fixture": {"id": "sd_ss_101", "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024, "name": "Premier League"},
        "teams": {"home": {"id": 101, "name": "Arsenal"}, "away": {"id": 102, "name": "Chelsea"}},
        "goals": {"home": 2, "away": 1},
        "provider_provenance": {"provider": "soccerdata_sofascore"},
    }
    res_ss = _validate_and_filter_tertiary_matches([record_prefixed_ss], league_id=39, season=2024)
    assert len(res_ss) == 0


# Assertion 4 & 5: Missing or non-numeric fixture ID is rejected
@pytest.mark.parametrize("invalid_id", [None, "", "NaN", "invalid_str", -5, 0])
def test_4_5_missing_or_non_numeric_fixture_id_rejected(invalid_id):
    raw_row = {
        "game_id": invalid_id,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 2,
        "away_score": 1,
        "league": "Premier League",
        "season": 2024,
    }
    norm = soccerdata_provider._normalize_sofascore_row(raw_row, league_id=39, season=2024)
    assert norm is None


# Assertion 6: Missing provider team IDs are NOT converted into generated/hash IDs
def test_6_missing_provider_team_ids_not_converted_into_generated_ids():
    raw_row = {
        "game_id": 99887766,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_team_id": None,
        "away_team_id": None,
        "home_score": 2,
        "away_score": 1,
        "league": "Premier League",
        "season": 2024,
    }
    norm = soccerdata_provider._normalize_sofascore_row(raw_row, league_id=39, season=2024)
    assert norm is not None
    assert norm["teams"]["home"]["id"] is None
    assert norm["teams"]["away"]["id"] is None
    assert norm["provider_provenance"]["provider_team_ids"]["home"] is None
    assert norm["provider_provenance"]["provider_team_ids"]["away"] is None


# Assertion 7: Tertiary fixture without real numeric team IDs is rejected by _validate_and_filter_tertiary_matches
def test_7_tertiary_fixture_without_real_numeric_team_ids_rejected():
    raw_row_no_team_ids = {
        "game_id": 99887766,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_team_id": None,
        "away_team_id": None,
        "home_score": 2,
        "away_score": 1,
        "league": "Premier League",
        "season": 2024,
    }
    norm = soccerdata_provider._normalize_sofascore_row(raw_row_no_team_ids, league_id=39, season=2024)
    assert norm is not None

    filtered = _validate_and_filter_tertiary_matches([norm], league_id=39, season=2024, completed_only=True)
    assert len(filtered) == 0


# Assertion 8 & 9: Wrong season or missing season rejected
def test_8_9_wrong_or_missing_season_rejected():
    raw_2025 = {
        "game_id": 1002,
        "date": "2025-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_team_id": 101,
        "away_team_id": 102,
        "home_score": 3,
        "away_score": 0,
        "season": 2025,
        "status": "FT",
    }
    raw_no_season = {
        "game_id": 1003,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_team_id": 101,
        "away_team_id": 102,
        "home_score": 1,
        "away_score": 1,
        "season": None,
        "status": "FT",
    }

    norm_2025 = soccerdata_provider._normalize_sofascore_row(raw_2025, league_id=39, season=2024)
    norm_no_s = soccerdata_provider._normalize_sofascore_row(raw_no_season, league_id=39, season=2024)

    assert norm_2025 is None
    assert norm_no_s is not None
    assert norm_no_s["league"]["season"] == 2024

    res_no_s = _validate_and_filter_tertiary_matches([norm_no_s], league_id=39, season=2024, completed_only=True)
    assert len(res_no_s) == 1


# Assertion 10: Uncompleted statuses (NS/PST/CANC/SUSP) rejected
@pytest.mark.parametrize("short_code", ["NS", "PST", "CANC", "SUSP"])
def test_10_uncompleted_statuses_rejected(short_code):
    raw_uncomp = {
        "game_id": 222222,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_team_id": 101,
        "away_team_id": 102,
        "home_score": None,
        "away_score": None,
        "league": "Premier League",
        "season": 2024,
        "status": short_code,
    }
    norm = soccerdata_provider._normalize_sofascore_row(raw_uncomp, league_id=39, season=2024)
    assert norm is not None
    filtered = _validate_and_filter_tertiary_matches([norm], league_id=39, season=2024, completed_only=True)
    assert len(filtered) == 0


# Assertion 11: Completed FT/AET/PEN fixtures without scores rejected
def test_11_completed_fixtures_without_scores_rejected():
    raw_no_score = {
        "game_id": 111111,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_team_id": 101,
        "away_team_id": 102,
        "home_score": None,
        "away_score": 1,
        "league": "Premier League",
        "season": 2024,
        "status": "FT",
    }
    norm = soccerdata_provider._normalize_sofascore_row(raw_no_score, league_id=39, season=2024)
    assert norm is not None
    filtered = _validate_and_filter_tertiary_matches([norm], league_id=39, season=2024, completed_only=True)
    assert len(filtered) == 0


# Assertion 12: Valid completed fixture with REAL numeric fixture/team IDs and canonical IDs passes storage.save_historical_fixtures
def test_12_valid_completed_fixture_passes_real_storage_integration():
    raw_row = {
        "game_id": 88776655,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_team_id": 101,
        "away_team_id": 102,
        "home_score": 2,
        "away_score": 1,
        "league": "Premier League",
        "season": 2024,
        "status": "FT",
    }
    norm = soccerdata_provider._normalize_sofascore_row(raw_row, league_id=39, season=2024)
    assert norm is not None

    filtered = _validate_and_filter_tertiary_matches([norm], league_id=39, season=2024, completed_only=True)
    assert len(filtered) == 1

    save_res = storage.save_historical_fixtures(filtered, league_id=39, season=2024, source="soccerdata", require_completed=True)
    assert save_res["inserted"] == 1

    stored = storage.get_historical_fixtures(39, 2024)
    assert len(stored) == 1
    item = stored[0]
    assert item["fixture"]["id"] == 88776655
    assert isinstance(item["fixture"]["id"], int)
    assert item["canonical_home_id"] is not None
    assert item["canonical_away_id"] is not None
    assert item["teams"]["home"]["id"] == 101
    assert item["teams"]["away"]["id"] == 102
    assert item["fixture"]["status"]["short"] in ("FT", "AET", "PEN")
    assert item["goals"]["home"] == 2
    assert item["goals"]["away"] == 1


# Test: Sofascore provider status contract returns SOURCE_RETURNED_PARTIAL_DATA when team IDs cannot be obtained
@patch("subprocess.run")
def test_sofascore_status_partial_data_when_team_ids_missing(mock_subproc):
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    # Return records without home_team_id / away_team_id
    payload = {
        "records": [
            {
                "game_id": 99887766,
                "date": "2024-09-01T15:00:00Z",
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "home_score": 2,
                "away_score": 1,
                "league": "Premier League",
                "season": 2024,
            }
        ],
        "team_id_map_size": 0,
        "enrichment_error": None,
    }
    mock_proc.stdout = json.dumps(payload)
    mock_subproc.return_value = mock_proc

    status, matches, meta = soccerdata_provider.get_sofascore_historical_games("ENG-Premier League", 2024, league_id=39)

    assert status == "SOURCE_RETURNED_PARTIAL_DATA"
    assert len(matches) == 0
    assert meta["storage_ready_count"] == 0


# Test: Sofascore provider status contract returns SOURCE_AVAILABLE when team IDs are successfully enriched
@patch("subprocess.run")
def test_sofascore_status_available_when_team_ids_present(mock_subproc):
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    payload = {
        "records": [
            {
                "game_id": 99887766,
                "date": "2024-09-01T15:00:00Z",
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "home_team_id": 101,
                "away_team_id": 102,
                "home_score": 2,
                "away_score": 1,
                "league": "Premier League",
                "season": 2024,
            }
        ],
        "team_id_map_size": 1,
        "enrichment_error": None,
    }
    mock_proc.stdout = json.dumps(payload)
    mock_subproc.return_value = mock_proc

    status, matches, meta = soccerdata_provider.get_sofascore_historical_games("ENG-Premier League", 2024, league_id=39)

    assert status == "SOURCE_AVAILABLE"
    assert len(matches) == 1
    assert matches[0]["fixture"]["id"] == 99887766
    assert matches[0]["teams"]["home"]["id"] == 101
    assert matches[0]["teams"]["away"]["id"] == 102


# Fallback hierarchy check: Sofascore is NOT called if MatchHistory returns usable data
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_sofascore_not_used_when_match_history_returns_usable_data(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = Exception("Primary error")
    mock_fd.side_effect = Exception("Secondary error")

    mock_sd_mh.return_value = (
        "SOURCE_AVAILABLE",
        [
            {
                "fixture": {"id": 500101, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
                "league": {"id": 39, "season": 2024, "name": "Premier League"},
                "teams": {"home": {"id": 101, "name": "Arsenal"}, "away": {"id": 102, "name": "Chelsea"}},
                "goals": {"home": 2, "away": 1},
                "provider_provenance": {"provider": "soccerdata_match_history"},
            }
        ],
        {"status": "SOURCE_AVAILABLE"},
    )

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "soccerdata"
    assert len(res["fixtures"]) == 1
    assert res["fixtures"][0]["provider_provenance"]["provider"] == "soccerdata_match_history"
    mock_sd_ss.assert_not_called()
