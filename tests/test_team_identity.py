from unittest.mock import patch
import pytest
import storage
import team_identity


@pytest.fixture(autouse=True)
def init_test_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_identity.db"
    monkeypatch.setattr("config.DB_PATH", str(db_file))
    monkeypatch.setattr("config.NEON_DATABASE_URL", None)
    monkeypatch.setattr("config.ENVIRONMENT", "development")
    storage.init_db()


def test_canonical_alias_matching():
    # "Man Utd" vs "Manchester United FC"
    cid1 = team_identity.resolve_canonical_team_id("Man Utd", "api_football", "33", league_id=39)
    cid2 = team_identity.resolve_canonical_team_id("Manchester United FC", "football_data_org", "66", league_id=39)

    assert cid1 == "football_team_manchester_united"
    assert cid2 == "football_team_manchester_united"


def test_cross_provider_canonical_resolution():
    # API-Football ID 50 vs football-data.org ID 524 (Manchester City)
    cid_api = team_identity.resolve_canonical_team_id("Manchester City", "api_football", 50, league_id=39)
    cid_fd = team_identity.resolve_canonical_team_id("Manchester City FC", "football_data_org", 524, league_id=39)

    assert cid_api == cid_fd == "football_team_manchester_city"


def test_ambiguous_or_empty_name_fails_closed():
    assert team_identity.resolve_canonical_team_id("", "api_football", 123) is None
    assert team_identity.resolve_canonical_team_id("   ", "api_football", 124) is None


def test_explicit_team_identity_registration():
    cid = team_identity.register_canonical_team_identity("football_team_39_123", "Team 123", "api_football", 456, league_id=39)
    assert cid == "football_team_39_123"
    mapping = storage.get_team_identity_by_provider("football", "api_football", "456")
    assert mapping is not None
    assert mapping["canonical_id"] == "football_team_39_123"


def test_unknown_team_returns_unresolved_and_no_mapping_written():
    cid = team_identity.resolve_canonical_team_id("Unknown Team XYZ FC", "api_football", 9999, league_id=39, auto_register=False)
    assert cid is None
    mapping = storage.get_team_identity_by_provider("football", "api_football", "9999")
    assert mapping is None


def test_resolve_canonical_team_id_read_only_when_auto_register_false():
    """
    Verify that resolve_canonical_team_id with auto_register=False performs ZERO identity DB writes.
    """
    with patch("storage.save_team_identity") as mock_save:
        cid = team_identity.resolve_canonical_team_id(
            "Arsenal",
            "api_football",
            10,
            league_id=39,
            auto_register=False,
        )
        assert cid == "football_team_arsenal"
        mock_save.assert_not_called()


def test_four_pl_clubs_general_alias_resolution():
    # Brighton variants
    for b in ["Brighton & Hove Albion FC", "Brighton & Hove Albion", "Brighton"]:
        assert team_identity.resolve_canonical_team_id(b, "football_data_org", 107, league_id=39) == "football_team_brighton"

    # Luton variants (legacy & alias compatibility)
    for l in ["Luton Town FC", "Luton Town", "Luton", "39_luton"]:
        assert team_identity.resolve_canonical_team_id(l, "football_data_org", 389, league_id=39) == "football_team_39_luton_town"

    # Nottingham Forest variants (legacy & alias compatibility)
    for n in ["Nottingham Forest FC", "Nottingham Forest", "Nottingham", "39_nottingham_forest"]:
        assert team_identity.resolve_canonical_team_id(n, "football_data_org", 351, league_id=39) == "football_team_39_nottingham"

    # Wolves variants (legacy & alias compatibility)
    for w in ["Wolverhampton Wanderers FC", "Wolverhampton Wanderers", "Wolverhampton", "Wolves"]:
        assert team_identity.resolve_canonical_team_id(w, "football_data_org", 76, league_id=39) == "football_team_39_wolverhampton"


def test_distinct_clubs_never_merged():
    mancity = team_identity.resolve_canonical_team_id("Manchester City", "api_football", 50, league_id=39)
    manutd = team_identity.resolve_canonical_team_id("Manchester United", "api_football", 33, league_id=39)
    assert mancity != manutd
    assert mancity == "football_team_manchester_city"
    assert manutd == "football_team_manchester_united"


def test_repeated_bootstrap_creates_no_duplicate_identities():
    cid1 = team_identity.bootstrap_historical_team_identity("Brighton & Hove Albion FC", "football_data_org", 107, league_id=39)
    cid2 = team_identity.bootstrap_historical_team_identity("Brighton & Hove Albion", "football_data_org", 107, league_id=39)
    cid3 = team_identity.bootstrap_historical_team_identity("Brighton & Hove Albion FC", "api_football", 51, league_id=39)

    assert cid1 == cid2 == cid3 == "football_team_brighton"
