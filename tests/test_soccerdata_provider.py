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


# 1. Real Sofascore fixture ID required & synthetic/missing fixture ID rejected
def test_real_sofascore_fixture_id_required():
    # Valid record with real game_id
    valid_row = {
        "game_id": 123456,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 2,
        "away_score": 1,
        "league": "Premier League",
        "season": 2024,
    }
    norm_valid = soccerdata_provider._normalize_sofascore_row(valid_row, league_id=39, season=2024)
    assert norm_valid is not None
    assert norm_valid["fixture"]["id"] == "sd_ss_123456"

    # Missing game_id / synthetic fixture ID rejected
    missing_id_row = {
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 2,
        "away_score": 1,
        "league": "Premier League",
        "season": 2024,
    }
    norm_missing = soccerdata_provider._normalize_sofascore_row(missing_id_row, league_id=39, season=2024)
    assert norm_missing is None


# 2. Season filtering authoritative: missing season rejected, wrong season rejected, exact season accepted
def test_season_filtering_authoritative():
    raw_row_2024 = {
        "game_id": 1001,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 2,
        "away_score": 1,
        "season": 2024,
        "status": "FT",
    }
    raw_row_2025 = {
        "game_id": 1002,
        "date": "2025-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 3,
        "away_score": 0,
        "season": 2025,
        "status": "FT",
    }
    raw_row_no_season = {
        "game_id": 1003,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 1,
        "away_score": 1,
        "season": None,
        "status": "FT",
    }

    norm_2024 = soccerdata_provider._normalize_sofascore_row(raw_row_2024, league_id=39, season=2024)
    norm_2025 = soccerdata_provider._normalize_sofascore_row(raw_row_2025, league_id=39, season=2024)
    norm_no_season = soccerdata_provider._normalize_sofascore_row(raw_row_no_season, league_id=39, season=2024)

    assert norm_2024["league"]["season"] == 2024
    assert norm_2025["league"]["season"] == 2025
    assert norm_no_season["league"]["season"] is None

    from data_resolver import _validate_and_filter_tertiary_matches
    matches = [norm_2024, norm_2025, norm_no_season]
    res = _validate_and_filter_tertiary_matches(matches, league_id=39, season=2024, completed_only=True)

    assert len(res) == 1
    assert res[0]["fixture"]["id"] == "sd_ss_1001"
    assert res[0]["league"]["season"] == 2024


# 3. Verified SoccerData mappings for all 12 configured leagues
def test_12_configured_leagues_checked_against_verified_mappings():
    configured_leagues = getattr(config, "ALLOWED_LEAGUE_IDS", [])
    assert len(configured_leagues) == 12

    for lid in configured_leagues:
        has_mh = lid in LEAGUE_TO_SD_MH_CODE
        has_ss = lid in LEAGUE_TO_SD_SOFASCORE_CODE

        assert has_mh or has_ss, f"League ID {lid} lacks a tertiary SoccerData strategy!"


# 4. Fallback hierarchy: MatchHistory -> Sofascore (Sofascore is NOT called if MatchHistory returns usable data)
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
                "fixture": {"id": "sd_mh_88", "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
                "league": {"id": 39, "season": 2024, "name": "Premier League"},
                "teams": {"home": {"id": None, "name": "Arsenal"}, "away": {"id": None, "name": "Chelsea"}},
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
    # Sofascore must NOT have been called!
    mock_sd_ss.assert_not_called()


# 5. Completed match protection: FT/AET/PEN accepted, NS/PST/CANC/SUSP & missing goals rejected
@pytest.mark.parametrize("short_code", ["FT", "AET", "PEN"])
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_completed_statuses_accepted(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss, short_code):
    mock_api_fb.side_effect = Exception("Primary error")
    mock_fd.side_effect = Exception("Secondary error")
    mock_sd_mh.return_value = ("SOURCE_NOT_AVAILABLE", [], {})

    record = [
        {
            "fixture": {"id": f"sd_ss_{short_code}", "date": "2024-09-01T15:00:00Z", "status": {"short": short_code}},
            "league": {"id": 39, "season": 2024, "name": "Premier League"},
            "teams": {"home": {"id": None, "name": "Arsenal"}, "away": {"id": None, "name": "Chelsea"}},
            "goals": {"home": 1, "away": 0},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        }
    ]
    mock_sd_ss.return_value = ("SOURCE_AVAILABLE", record, {"status": "SOURCE_AVAILABLE"})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1, completed_only=True)

    assert len(res["fixtures"]) == 1
    assert res["fixtures"][0]["fixture"]["status"]["short"] == short_code


@pytest.mark.parametrize("short_code", ["NS", "PST", "CANC", "SUSP"])
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_uncompleted_statuses_rejected(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss, short_code):
    mock_api_fb.side_effect = Exception("Primary error")
    mock_fd.side_effect = Exception("Secondary error")
    mock_sd_mh.return_value = ("SOURCE_NOT_AVAILABLE", [], {})

    record = [
        {
            "fixture": {"id": f"sd_ss_{short_code}", "date": "2024-09-01T15:00:00Z", "status": {"short": short_code}},
            "league": {"id": 39, "season": 2024, "name": "Premier League"},
            "teams": {"home": {"id": None, "name": "Arsenal"}, "away": {"id": None, "name": "Chelsea"}},
            "goals": {"home": None, "away": None},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        }
    ]
    mock_sd_ss.return_value = ("SOURCE_AVAILABLE", record, {"status": "SOURCE_AVAILABLE"})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1, completed_only=True)

    assert len(res["fixtures"]) == 0


@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_missing_goals_rejected_for_completed_status(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = Exception("Primary error")
    mock_fd.side_effect = Exception("Secondary error")
    mock_sd_mh.return_value = ("SOURCE_NOT_AVAILABLE", [], {})

    record = [
        {
            "fixture": {"id": "sd_ss_missinggoals", "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024, "name": "Premier League"},
            "teams": {"home": {"id": None, "name": "Arsenal"}, "away": {"id": None, "name": "Chelsea"}},
            "goals": {"home": None, "away": None},
            "provider_provenance": {"provider": "soccerdata_sofascore"},
        }
    ]
    mock_sd_ss.return_value = ("SOURCE_AVAILABLE", record, {"status": "SOURCE_AVAILABLE"})

    resolver = DataResolver()
    res = resolver.get_league_fixtures_page(league_id=39, season=2024, page=1, completed_only=True)

    assert len(res["fixtures"]) == 0


# 6. Canonical identity resolution during ingestion vs auto_register=False during analysis
def test_canonical_identity_ingestion_and_analysis_boundaries():
    # Ingestion path: bootstrap_historical_team_identity
    c_home = team_identity.bootstrap_historical_team_identity("Arsenal FC", "soccerdata", 101, league_id=39)
    assert c_home is not None

    record = {
        "fixture": {"id": 900101, "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024, "name": "Premier League"},
        "teams": {"home": {"id": 101, "name": "Arsenal FC"}, "away": {"id": 102, "name": "Chelsea FC"}},
        "goals": {"home": 2, "away": 1},
        "provider_provenance": {"provider": "soccerdata_sofascore"},
    }
    save_res = storage.save_historical_fixtures([record], league_id=39, season=2024, source="soccerdata", require_completed=True)
    assert save_res["inserted"] == 1

    # Analysis path: resolve_canonical_team_id with auto_register=False
    c_unknown = team_identity.resolve_canonical_team_id(
        raw_name="Totally Unknown Team 9999",
        provider="soccerdata",
        provider_team_id=None,
        league_id=39,
        sport="football",
        auto_register=False,
    )
    assert c_unknown is None


# 7. Failed tertiary source leaves dataset "INCOMPLETE"
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_failed_tertiary_source_leaves_dataset_incomplete(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = Exception("Primary error")
    mock_fd.side_effect = Exception("Secondary error")
    mock_sd_mh.return_value = ("SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": "Crash"})
    mock_sd_ss.return_value = ("SOURCE_FAILED", [], {"status": "SOURCE_FAILED", "error": "Crash"})

    report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert report["status"] == "INCOMPLETE"
    assert report["acquisition_complete"] is False


# 8. Target seasons 2024, 2025, 2026 passed explicitly into SoccerData adapter
@pytest.mark.parametrize("target_season", [2024, 2025, 2026])
@patch("soccerdata_provider.subprocess.run")
def test_target_seasons_passed_explicitly_to_adapter(mock_subproc, target_season):
    mock_subproc.return_value = MagicMock(returncode=0, stdout="[]")

    soccerdata_provider.get_sofascore_historical_games("ENG-Premier League", season=target_season)

    assert mock_subproc.call_count == 1
    call_args = mock_subproc.call_args[0][0]
    code_passed = call_args[2]
    assert f"seasons={target_season!r}" in code_passed or f"seasons={target_season}" in code_passed
