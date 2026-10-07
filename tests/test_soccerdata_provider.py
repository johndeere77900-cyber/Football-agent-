import pytest
from unittest.mock import patch, MagicMock
import config
import storage
from data_resolver import DataResolver, LEAGUE_TO_SD_MH_CODE, LEAGUE_TO_SD_SOFASCORE_CODE
import soccerdata_provider
import historical_sync
import team_identity


@pytest.fixture(autouse=True)
def init_test_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_predictions.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    storage.init_db()


# Test A — API-Football unavailable + football-data.org unavailable + SoccerData MatchHistory available -> tertiary data returned
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_A_match_history_tertiary_returned(mock_api_fb, mock_fd, mock_sd_mh):
    mock_api_fb.side_effect = Exception("Primary error")
    mock_fd.side_effect = Exception("Secondary error")

    mock_sd_mh.return_value = (
        "SOURCE_AVAILABLE",
        [
            {
                "fixture": {"id": "sd_mh_1", "date": "2024-09-01T15:00:00Z", "status": {"short": "FT", "long": "Finished"}},
                "league": {"id": 39, "season": 2024, "name": "Premier League"},
                "teams": {"home": {"id": "sd_Arsenal", "name": "Arsenal"}, "away": {"id": "sd_Chelsea", "name": "Chelsea"}},
                "goals": {"home": 2, "away": 1},
                "provider_provenance": {"provider": "soccerdata_match_history", "provider_type": "tertiary"},
            }
        ],
        {"status": "SOURCE_AVAILABLE", "count": 1},
    )

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert res["source"] == "soccerdata"
    assert len(res["fixtures"]) == 1
    assert res["fixtures"][0]["goals"] == {"home": 2, "away": 1}
    assert res["fixtures"][0]["provider_provenance"]["provider"] == "soccerdata_match_history"


# Test B — API-Football unavailable + football-data.org unavailable + MatchHistory empty + SoccerData Sofascore available -> Sofascore tertiary data returned
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_B_sofascore_tertiary_returned_when_mh_empty(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = Exception("Primary error")
    mock_fd.side_effect = Exception("Secondary error")
    mock_sd_mh.return_value = ("SOURCE_NOT_AVAILABLE", [], {"status": "SOURCE_NOT_AVAILABLE"})

    mock_sd_ss.return_value = (
        "SOURCE_AVAILABLE",
        [
            {
                "fixture": {"id": "sd_ss_1001", "date": "2024-09-01T15:00:00Z", "status": {"short": "FT", "long": "Finished"}},
                "league": {"id": 2, "season": 2024, "name": "UEFA Champions League"},
                "teams": {"home": {"id": "sd_Real_Madrid", "name": "Real Madrid"}, "away": {"id": "sd_Barcelona", "name": "Barcelona"}},
                "goals": {"home": 3, "away": 1},
                "provider_provenance": {"provider": "soccerdata_sofascore", "provider_type": "tertiary"},
            }
        ],
        {"status": "SOURCE_AVAILABLE", "count": 1},
    )

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=2, season=2024, page=1)

    assert res["source"] == "soccerdata"
    assert len(res["fixtures"]) == 1
    assert res["fixtures"][0]["goals"] == {"home": 3, "away": 1}
    assert res["fixtures"][0]["provider_provenance"]["provider"] == "soccerdata_sofascore"


# Test C — SoccerData returns records from multiple seasons -> only requested season survives
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_C_multi_season_filtered_to_requested_season(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = Exception("Primary error")
    mock_fd.side_effect = Exception("Secondary error")
    mock_sd_mh.return_value = ("SOURCE_NOT_AVAILABLE", [], {})

    multi_season_records = [
        {
            "fixture": {"id": "sd_ss_2024", "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024, "name": "Premier League"},
            "teams": {"home": {"id": "sd_A", "name": "Team A"}, "away": {"id": "sd_B", "name": "Team B"}},
            "goals": {"home": 1, "away": 0},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        },
        {
            "fixture": {"id": "sd_ss_2025", "date": "2025-09-01T15:00:00Z", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2025, "name": "Premier League"},
            "teams": {"home": {"id": "sd_A", "name": "Team A"}, "away": {"id": "sd_C", "name": "Team C"}},
            "goals": {"home": 2, "away": 2},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        },
    ]

    mock_sd_ss.return_value = ("SOURCE_AVAILABLE", multi_season_records, {"status": "SOURCE_AVAILABLE"})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert len(res["fixtures"]) == 1
    assert res["fixtures"][0]["league"]["season"] == 2024
    assert res["fixtures"][0]["fixture"]["id"] == "sd_ss_2024"


# Test D — SoccerData returns a record without a score -> record rejected
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_D_record_without_score_rejected(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = Exception("Primary error")
    mock_fd.side_effect = Exception("Secondary error")
    mock_sd_mh.return_value = ("SOURCE_NOT_AVAILABLE", [], {})

    no_score_record = [
        {
            "fixture": {"id": "sd_ss_noscore", "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024, "name": "Premier League"},
            "teams": {"home": {"id": "sd_A", "name": "Team A"}, "away": {"id": "sd_B", "name": "Team B"}},
            "goals": {"home": None, "away": None},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        }
    ]

    mock_sd_ss.return_value = ("SOURCE_AVAILABLE", no_score_record, {"status": "SOURCE_AVAILABLE"})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1)

    assert len(res["fixtures"]) == 0


# Test E — SoccerData returns NS/PST/CANCELLED/SUSPENDED -> record rejected from completed persistence
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_E_uncompleted_statuses_rejected_from_completed_persistence(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = Exception("Primary error")
    mock_fd.side_effect = Exception("Secondary error")
    mock_sd_mh.return_value = ("SOURCE_NOT_AVAILABLE", [], {})

    uncompleted_records = [
        {
            "fixture": {"id": "sd_ss_ns", "date": "2024-09-01T15:00:00Z", "status": {"short": "NS"}},
            "league": {"id": 39, "season": 2024, "name": "Premier League"},
            "teams": {"home": {"id": "sd_A", "name": "Team A"}, "away": {"id": "sd_B", "name": "Team B"}},
            "goals": {"home": None, "away": None},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        },
        {
            "fixture": {"id": "sd_ss_pst", "date": "2024-09-01T15:00:00Z", "status": {"short": "PST"}},
            "league": {"id": 39, "season": 2024, "name": "Premier League"},
            "teams": {"home": {"id": "sd_A", "name": "Team A"}, "away": {"id": "sd_C", "name": "Team C"}},
            "goals": {"home": None, "away": None},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        },
        {
            "fixture": {"id": "sd_ss_canc", "date": "2024-09-01T15:00:00Z", "status": {"short": "CANC"}},
            "league": {"id": 39, "season": 2024, "name": "Premier League"},
            "teams": {"home": {"id": "sd_B", "name": "Team B"}, "away": {"id": "sd_C", "name": "Team C"}},
            "goals": {"home": None, "away": None},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        },
    ]

    mock_sd_ss.return_value = ("SOURCE_AVAILABLE", uncompleted_records, {"status": "SOURCE_AVAILABLE"})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1, completed_only=True)

    assert len(res["fixtures"]) == 0


# Test F, G, H — SoccerData returns valid FT, AET, PEN -> accepted
@pytest.mark.parametrize("short_code", ["FT", "AET", "PEN"])
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_F_G_H_valid_completed_statuses_accepted(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss, short_code):
    mock_api_fb.side_effect = Exception("Primary error")
    mock_fd.side_effect = Exception("Secondary error")
    mock_sd_mh.return_value = ("SOURCE_NOT_AVAILABLE", [], {})

    valid_completed_record = [
        {
            "fixture": {"id": f"sd_ss_{short_code}", "date": "2024-09-01T15:00:00Z", "status": {"short": short_code}},
            "league": {"id": 39, "season": 2024, "name": "Premier League"},
            "teams": {"home": {"id": "sd_A", "name": "Team A"}, "away": {"id": "sd_B", "name": "Team B"}},
            "goals": {"home": 2, "away": 1},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        }
    ]

    mock_sd_ss.return_value = ("SOURCE_AVAILABLE", valid_completed_record, {"status": "SOURCE_AVAILABLE"})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1, completed_only=True)

    assert len(res["fixtures"]) == 1
    assert res["fixtures"][0]["fixture"]["status"]["short"] == short_code


# Test I — SoccerData returns team names that map to existing canonical identities -> canonical IDs populated
def test_I_team_names_map_to_canonical_identities():
    c_home = team_identity.bootstrap_historical_team_identity("Arsenal", "soccerdata", 101, league_id=39)
    c_away = team_identity.bootstrap_historical_team_identity("Chelsea", "soccerdata", 102, league_id=39)

    record = {
        "fixture": {"id": 900101, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024, "name": "Premier League"},
        "teams": {"home": {"id": 101, "name": "Arsenal"}, "away": {"id": 102, "name": "Chelsea"}},
        "goals": {"home": 2, "away": 1},
        "provider_provenance": {"provider": "soccerdata_sofascore"},
    }

    save_res = storage.save_historical_fixtures([record], league_id=39, season=2024, source="soccerdata", require_completed=True)
    assert save_res["inserted"] == 1

    stored = storage.get_historical_fixtures(39, 2024)
    assert len(stored) == 1
    assert stored[0]["canonical_home_id"] == c_home
    assert stored[0]["canonical_away_id"] == c_away


# Test J — SoccerData returns an unknown team during a non-bootstrap analysis path -> do not create a new identity
def test_J_unknown_team_during_analysis_does_not_register_identity():
    c_id = team_identity.resolve_canonical_team_id(
        raw_name="Nonexistent FC 12345",
        provider="soccerdata",
        provider_team_id=None,
        league_id=39,
        sport="football",
        auto_register=False,
    )
    assert c_id is None


# Test K — Missing/failed SoccerData source -> existing error semantics preserved and dataset remains INCOMPLETE
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_K_failed_soccerdata_source_leaves_dataset_incomplete(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = Exception("Primary error")
    mock_fd.side_effect = Exception("Secondary error")
    mock_sd_mh.return_value = ("SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": "Subprocess crash"})
    mock_sd_ss.return_value = ("SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": "Subprocess crash"})

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"
    assert report["acquisition_complete"] is False


# Test L — Verify all 12 configured football leagues have a defined tertiary acquisition strategy
def test_L_all_12_leagues_have_tertiary_strategy():
    configured_leagues = getattr(config, "ALLOWED_LEAGUE_IDS", [])
    assert len(configured_leagues) == 12

    for lid in configured_leagues:
        has_mh = lid in LEAGUE_TO_SD_MH_CODE
        has_ss = lid in LEAGUE_TO_SD_SOFASCORE_CODE
        assert has_mh or has_ss, f"League ID {lid} lacks tertiary SoccerData mapping!"


# Test M — Verify 2024, 2025, and 2026 are all passed explicitly into SoccerData adapter
@pytest.mark.parametrize("target_season", [2024, 2025, 2026])
@patch("soccerdata_provider.subprocess.run")
def test_M_target_seasons_passed_explicitly_to_soccerdata_adapter(mock_subproc, target_season):
    mock_subproc.return_value = MagicMock(returncode=0, stdout="[]")

    soccerdata_provider.get_sofascore_historical_games("ENG-Premier League", season=target_season)

    assert mock_subproc.call_count == 1
    call_args = mock_subproc.call_args[0][0]
    code_passed = call_args[2]
    assert f"seasons={target_season!r}" in code_passed or f"seasons={target_season}" in code_passed
