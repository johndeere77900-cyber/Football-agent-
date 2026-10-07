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


# Test A — Real numeric Sofascore ID (no prefix, strict int)
def test_A_real_numeric_sofascore_id():
    raw_row = {
        "game_id": 12345678,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 2,
        "away_score": 1,
        "league": "Premier League",
        "season": 2024,
    }
    norm = soccerdata_provider._normalize_sofascore_row(raw_row, league_id=39, season=2024)
    assert norm is not None
    assert norm["fixture"]["id"] == 12345678
    assert isinstance(norm["fixture"]["id"], int)
    assert norm["provider_provenance"]["provider_fixture_id"] == 12345678


# Test B — Synthetic / missing / invalid ID rejected
@pytest.mark.parametrize("invalid_id", [None, "", "NaN", "invalid_str", -5, 0])
def test_B_invalid_or_missing_game_id_rejected(invalid_id):
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


# Test C — No fake team IDs produced
def test_C_no_fake_team_ids_when_missing():
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


# Test D — Canonical bootstrap with missing provider team IDs
def test_D_canonical_bootstrap_with_missing_provider_ids():
    # Calling bootstrap with None as provider_team_id
    c_home = team_identity.bootstrap_historical_team_identity("Arsenal FC", "soccerdata_sofascore", None, league_id=39)
    assert c_home is not None
    assert c_home.startswith("football_team_")

    # Confirm no fake "None" string mapping was persisted in team_identities
    mapping = storage.get_team_identity_by_provider("football", "soccerdata_sofascore", "None")
    assert mapping is None


# Test E — Real storage integration with normalized Sofascore fixture
def test_E_real_storage_integration_with_normalized_sofascore_fixture():
    raw_row = {
        "game_id": 88776655,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 2,
        "away_score": 1,
        "league": "Premier League",
        "season": 2024,
        "status": "FT",
    }
    norm = soccerdata_provider._normalize_sofascore_row(raw_row, league_id=39, season=2024)
    assert norm is not None

    from data_resolver import _validate_and_filter_tertiary_matches
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
    assert item["fixture"]["status"]["short"] in ("FT", "AET", "PEN")
    assert item["goals"]["home"] == 2
    assert item["goals"]["away"] == 1


# Test F — Invalid completed data rejected
def test_F_invalid_completed_data_rejected():
    # FT missing home score
    raw_no_hscore = {
        "game_id": 111111,
        "date": "2024-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": None,
        "away_score": 1,
        "league": "Premier League",
        "season": 2024,
        "status": "FT",
    }
    norm_no_hscore = soccerdata_provider._normalize_sofascore_row(raw_no_hscore, league_id=39, season=2024)
    assert norm_no_hscore is not None

    from data_resolver import _validate_and_filter_tertiary_matches
    filtered_no_hscore = _validate_and_filter_tertiary_matches([norm_no_hscore], league_id=39, season=2024, completed_only=True)
    assert len(filtered_no_hscore) == 0

    # Uncompleted status (NS, PST, CANC, SUSP)
    for st_code in ["NS", "PST", "CANC", "SUSP"]:
        raw_uncompleted = {
            "game_id": 222222,
            "date": "2024-09-01T15:00:00Z",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": None,
            "away_score": None,
            "league": "Premier League",
            "season": 2024,
            "status": st_code,
        }
        norm_uncomp = soccerdata_provider._normalize_sofascore_row(raw_uncompleted, league_id=39, season=2024)
        assert norm_uncomp is not None
        filtered_uncomp = _validate_and_filter_tertiary_matches([norm_uncomp], league_id=39, season=2024, completed_only=True)
        assert len(filtered_uncomp) == 0

    # Wrong season
    raw_wrong_season = {
        "game_id": 333333,
        "date": "2025-09-01T15:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 2,
        "away_score": 1,
        "league": "Premier League",
        "season": 2025,
        "status": "FT",
    }
    norm_wrong_s = soccerdata_provider._normalize_sofascore_row(raw_wrong_season, league_id=39, season=2024)
    assert norm_wrong_s is not None
    assert norm_wrong_s["league"]["season"] == 2025

    filtered_wrong_season = _validate_and_filter_tertiary_matches([norm_wrong_s], league_id=39, season=2024, completed_only=True)
    assert len(filtered_wrong_season) == 0


# Test G — Fallback ordering: MatchHistory -> Sofascore
@patch("soccerdata_provider.get_sofascore_historical_games")
@patch("soccerdata_provider.get_match_history_games")
@patch("football_data_api.get_competition_matches")
@patch("api_football.get_league_fixtures_page")
def test_G_fallback_ordering_sofascore_only_used_when_match_history_fails(mock_api_fb, mock_fd, mock_sd_mh, mock_sd_ss):
    mock_api_fb.side_effect = Exception("Primary error")
    mock_fd.side_effect = Exception("Secondary error")

    # MatchHistory returns usable data
    mock_sd_mh.return_value = (
        "SOURCE_AVAILABLE",
        [
            {
                "fixture": {"id": "sd_mh_101", "date": "2024-09-01T15:00:00Z", "status": {"short": "FT"}},
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


# Test 12 configured leagues mapping verification
def test_all_12_configured_leagues_have_tertiary_strategy():
    configured_leagues = getattr(config, "ALLOWED_LEAGUE_IDS", [])
    assert len(configured_leagues) == 12

    for lid in configured_leagues:
        has_mh = lid in LEAGUE_TO_SD_MH_CODE
        has_ss = lid in LEAGUE_TO_SD_SOFASCORE_CODE
        assert has_mh or has_ss, f"League ID {lid} lacks a tertiary SoccerData mapping!"
