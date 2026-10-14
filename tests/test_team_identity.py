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


def test_alias_normalization_handles_punctuation_ampersands_hyphens_and_diacritics():
    """
    Verify that alias normalization strips accents, punctuation, ampersands, and hyphens to match configured aliases.
    """
    import config
    with patch.object(config, "CANONICAL_TEAM_ALIASES", {
        ("Atlético Madrid", 140): "atletico_madrid",
        ("Brighton & Hove Albion", 39): "brighton",
        ("Paris Saint-Germain", 61): "psg",
    }):
        cid_atletico = team_identity.resolve_canonical_team_id("Atletico Madrid", "api_football", 30, league_id=140)
        assert cid_atletico == "football_team_atletico_madrid"

        cid_brighton = team_identity.resolve_canonical_team_id("Brighton and Hove Albion", "api_football", 31, league_id=39)
        assert cid_brighton == "football_team_brighton"

        cid_psg = team_identity.resolve_canonical_team_id("Paris Saint Germain", "api_football", 32, league_id=61)
        assert cid_psg == "football_team_psg"


def test_ambiguous_bootstrap_fails_closed_and_existing_mapping_cannot_be_reassigned():
    """
    Verify that empty or invalid team names fail closed during identity bootstrap,
    and existing provider mappings cannot be overwritten or reassigned.
    """
    assert team_identity.bootstrap_historical_team_identity("", "api_football", 100) is None
    assert team_identity.bootstrap_historical_team_identity("   ", "api_football", 100) is None
    assert team_identity.bootstrap_historical_team_identity("Arsenal", "mixed", 100) is None

    # Save mapping for provider ID 50 -> "football_team_arsenal"
    storage.save_team_identity("football", "football_team_arsenal", "api_football", "50", "arsenal", "Arsenal", league_id=39)

    # Attempting to bootstrap same provider ID 50 with a different name returns existing canonical ID
    resolved = team_identity.resolve_canonical_team_id("Chelsea FC", "api_football", 50, league_id=39)
    assert resolved == "football_team_arsenal"  # Existing mapping preserved!
