"""Phase 1 reliability + sports knowledge foundation regression tests."""

import json

import pytest

import build_phase1_inventory as inventory_builder
import config
import data_resolver
import historical_sync
import sports_knowledge
import storage


def _bb_game(game_id, **overrides):
    game = {
        "id": game_id,
        "date": "2024-11-01T00:00:00+00:00",
        "league": {"id": 12, "season": 2024},
        "teams": {"home": {"id": 1, "name": "Home Team"}, "away": {"id": 2, "name": "Away Team"}},
        "scores": {"home": {"total": 100}, "away": {"total": 90}},
        "status": {"short": "FT"},
    }
    game.update(overrides)
    return game


# ---------------------------------------------------------------------------
# Basketball enrichment persistence
# ---------------------------------------------------------------------------

def test_basketball_resave_enriches_existing_row_and_is_idempotent():
    first = storage.save_historical_basketball_games([_bb_game(5001)], league_id=12, season=2024)
    assert first["inserted"] == 1

    enriched_input = _bb_game(5001)
    enriched_input["provider_provenance"] = {
        "provider": "espn_url",
        "season_type": "regular_season",
        "season_type_id": 2,
    }
    second = storage.save_historical_basketball_games([enriched_input], league_id=12, season=2024)
    assert second["inserted"] == 0
    assert second["enriched"] == 1
    assert second["already_existing"] == 0

    stored = storage.get_historical_basketball_games(12, 2024)
    assert len(stored) == 1
    record = stored[0]
    # Verified metadata was added.
    assert record["provider_provenance"]["season_type"] == "regular_season"
    # Identity, date and valid scores were preserved.
    assert record["id"] == 5001
    assert record["date"].startswith("2024-11-01")
    assert record["scores"]["home"]["total"] == 100
    assert record["scores"]["away"]["total"] == 90
    assert record["enrichment_history"][-1]["fields"]

    # Re-applying the same verified metadata is a no-op, not a second enrichment.
    third = storage.save_historical_basketball_games([enriched_input], league_id=12, season=2024)
    assert third["inserted"] == 0
    assert third["enriched"] == 0
    assert third["already_existing"] == 1


def test_basketball_enrichment_upgrades_unclassified_phase_but_never_downgrades():
    unclassified = _bb_game(5002, provider_provenance={"provider": "espn_url", "season_type": "unclassified"})
    storage.save_historical_basketball_games([unclassified], league_id=12, season=2024)

    verified = _bb_game(5002, provider_provenance={"provider": "espn_url", "season_type": "regular_season"})
    upgraded = storage.save_historical_basketball_games([verified], league_id=12, season=2024)
    assert upgraded["enriched"] == 1
    assert storage.get_historical_basketball_games(12, 2024)[0]["provider_provenance"]["season_type"] == "regular_season"

    # A later unverified/unclassified re-acquisition must not erase the verified
    # phase, and the row is not rewritten at all.
    downgrade_attempt = _bb_game(5002, provider_provenance={"provider": "espn_url", "season_type": "unclassified"})
    result = storage.save_historical_basketball_games([downgrade_attempt], league_id=12, season=2024)
    assert result["enriched"] == 0
    assert storage.get_historical_basketball_games(12, 2024)[0]["provider_provenance"]["season_type"] == "regular_season"


def test_basketball_conflicting_score_is_preserved_and_recorded():
    storage.save_historical_basketball_games([_bb_game(5003)], league_id=12, season=2024)

    conflicting = _bb_game(5003, scores={"home": {"total": 88}, "away": {"total": 90}})
    result = storage.save_historical_basketball_games([conflicting], league_id=12, season=2024)

    record = storage.get_historical_basketball_games(12, 2024)[0]
    assert record["scores"]["home"]["total"] == 100
    assert result["metadata_conflicts"].get("scores.home.total") == 1


def test_basketball_missing_metadata_is_filled_from_verified_provider_payload():
    incomplete = _bb_game(5004)
    incomplete["status"] = {"short": "FT"}  # no long status persisted yet
    storage.save_historical_basketball_games([incomplete], league_id=12, season=2024)

    complete = _bb_game(5004)
    complete["status"] = {"short": "FT", "long": "Final"}
    result = storage.save_historical_basketball_games([complete], league_id=12, season=2024)

    assert result["enriched"] == 1
    record = storage.get_historical_basketball_games(12, 2024)[0]
    assert record["status"]["long"] == "Final"
    assert record["status"]["short"] == "FT"


def test_basketball_validation_still_rejects_unclassified_regular_season_coverage():
    """Enrichment is a persistence repair, not a validation weakening."""
    dataset_info = {
        "fixture_count": 1,
        "expected_pages": 9,
        "pages_completed": 9,
        "rejected_count": 0,
        "empty_pages_count": 0,
        "provider_coverage": {
            "espn_url": {
                "coverage_unit": "months",
                "expected": 9,
                "completed": 9,
                "valid_empty_units": 0,
                "competition_verified": True,
                "status": historical_sync.ACQUISITION_STATE_VALIDATED,
            }
        },
    }
    unclassified_game = _bb_game(5005, provider_provenance={"provider": "espn_url"})
    assert (
        historical_sync._validate_historical_basketball_dataset([unclassified_game], 12, 2024, dataset_info) is False
    )


# ---------------------------------------------------------------------------
# Provider attribution
# ---------------------------------------------------------------------------

def test_provider_contributors_are_derived_from_reconciled_records():
    reconciled = {
        "fixture": {"id": 1},
        "provider_provenance": {"provider": "football_data_csv"},
        "field_provenance": {"goals": "football_data_csv", "stats_shots": "soccerdata_fbref"},
        "reconciliation_metadata": {
            "providers_used": ["football_data_csv", "soccerdata_fbref"],
            "provider_count": 2,
            "is_reconciled": True,
        },
    }
    assert data_resolver._providers_contributed_from_records([reconciled]) == [
        "football_data_csv",
        "soccerdata_fbref",
    ]

    # Records carrying no provenance fall back to the attempted providers rather
    # than reporting no attribution at all.
    assert data_resolver._providers_contributed_from_records(
        [{"fixture": {"id": 2}}],
        attempted=[("football_data_csv", [{"fixture": {"id": 2}}]), ("soccerdata_fbref", [])],
    ) == ["football_data_csv"]

    # Reconciliation labels are not providers.
    assert data_resolver._providers_contributed_from_records(
        [{"provider_provenance": {"provider": "mixed"}, "reconciliation_metadata": {"providers_used": ["mixed"]}}]
    ) == []


def test_fbref_is_reported_when_it_only_contributed_to_merged_rows():
    reconciled = {
        "provider_provenance": {"provider": "football_data_csv"},
        "reconciliation_metadata": {"providers_used": ["football_data_csv", "soccerdata_fbref"]},
    }
    contributors = data_resolver._providers_contributed_from_records(
        [reconciled],
        attempted=[("football_data_csv", [reconciled]), ("soccerdata_fbref", [{"x": 1}])],
    )
    assert "soccerdata_fbref" in contributors


# ---------------------------------------------------------------------------
# Competition lifecycle / edition knowledge
# ---------------------------------------------------------------------------

def test_knowledge_catalogue_is_valid_and_agrees_with_config():
    assert sports_knowledge.validate() == []


def test_edition_windows_match_documented_evidence():
    world_cup_2022 = sports_knowledge.edition("football", 1, 2022)
    assert world_cup_2022["status"] == "KNOWN"
    assert world_cup_2022["start"].startswith("2022-11-20")
    assert world_cup_2022["end"].startswith("2022-12-18")

    euro_2020 = sports_knowledge.edition("football", 4, 2020)
    assert euro_2020["status"] == "KNOWN"
    assert euro_2020["start"].startswith("2021-06-11")
    assert euro_2020["end"].startswith("2021-07-11")

    # The Nations League is biennial: 2019/2021/2023/2025 never existed.
    for season in (2019, 2021, 2023, 2025):
        assert sports_knowledge.edition("football", 5, season)["exists"] is False
    for season in (2018, 2020, 2022, 2024, 2026):
        assert sports_knowledge.edition("football", 5, season)["exists"] is True

    # An edition without a documented window stays UNKNOWN and fails closed.
    assert sports_knowledge.edition("football", 5, 2022)["status"] == "UNKNOWN"
    assert sports_knowledge.edition("football", 5, 2026)["status"] == "KNOWN"


def test_world_cup_and_euro_are_not_treated_as_annual_competitions():
    world_cup_seasons = sports_knowledge.edition_years("football", 1)
    assert world_cup_seasons == [2018, 2022, 2026]
    selection = sports_knowledge.select_seasons("football", 1, list(config.TARGET_SEASONS))
    assert selection["supported"] == [2022, 2026]
    assert 2019 in selection["not_applicable"]


def test_lifecycle_uses_the_edition_window_not_the_calendar_year():
    world_cup_2022 = sports_knowledge.edition("football", 1, 2022)
    from datetime import datetime, timezone

    state, reason, _ = historical_sync.get_competition_lifecycle(
        "football",
        1,
        2022,
        [],
        coverage=None,
        now_utc=datetime(2022, 10, 1, tzinfo=timezone.utc),
    )
    # Before the November edition the state is UPCOMING, not ONGOING/COMPLETED.
    assert state == historical_sync.COMPETITION_LIFECYCLE_UPCOMING
    assert world_cup_2022["start"].startswith("2022-11-20")


def test_nba_2026_has_no_fabricated_expectation():
    assert 2026 not in (config.NBA_EXPECTED_MIN_REGULAR_SEASON_GAMES or {})
    assert sports_knowledge.expected_coverage_units("basketball", 12, 2026)["value"] is None
    assert sports_knowledge.expected_coverage_units("basketball", 12, 2024)["value"] == 1230
    assert sports_knowledge.expected_coverage_units("basketball", 12, 2019)["value"] == 1059


def test_approved_provider_orders_are_unchanged():
    assert sports_knowledge.provider_order("football") == [
        "football_data_csv",
        "soccerdata_fbref",
        "soccerdata_match_history",
        "soccerdata_sofascore",
    ]
    assert sports_knowledge.provider_order("basketball") == ["espn_url", "api_basketball"]


def test_queue_skips_seasons_a_competition_never_staged(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_LEAGUE_IDS", [1, 39])
    monkeypatch.setattr(config, "ALLOWED_BASKETBALL_LEAGUE_IDS", [])

    processed = []

    def mock_sync(league_id, season, **kwargs):
        processed.append((league_id, season))
        return {
            "sport": "football",
            "league_id": league_id,
            "season": season,
            "status": "INCOMPLETE",
            "api_requests_consumed": 0,
            "quota_budget_stopped": False,
        }

    monkeypatch.setattr(historical_sync, "sync_historical_fixtures", mock_sync)

    summary = historical_sync.run_historical_queue()

    assert sorted(season for league_id, season in processed if league_id == 1) == [2022, 2026]
    assert sorted(season for league_id, season in processed if league_id == 39) == sorted(config.TARGET_SEASONS)

    skipped_world_cups = [
        item for item in summary["edition_selection"]["skipped"] if item["league_id"] == 1
    ]
    assert {item["season"] for item in skipped_world_cups} == set(config.TARGET_SEASONS) - {2022, 2026}
    assert all(item["reason"] == "unsupported_season_edition" for item in skipped_world_cups)


# ---------------------------------------------------------------------------
# Reproducible inventory
# ---------------------------------------------------------------------------

def test_inventory_is_reproducible_and_never_invents_persisted_counts():
    built = inventory_builder.build_inventory()
    assert built["database_inspected"] is False
    assert built["knowledge_validation_problems"] == []
    assert built["targets"]
    for target in built["targets"]:
        assert target["persisted_records"] == "UNKNOWN"
        assert target["unresolved_team_ids"] == "UNKNOWN"

    world_cup_states = {
        target["season"]: target["lifecycle_state"]
        for target in built["targets"]
        if target["sport"] == "football" and target["league_id"] == 1
    }
    assert world_cup_states[2019] == "NOT_APPLICABLE"
    assert world_cup_states[2022] == "COMPLETED"
    assert world_cup_states[2026] == "COMPLETED"

    # Regenerating twice from the same configuration is deterministic.
    assert json.dumps(built, sort_keys=True) == json.dumps(inventory_builder.build_inventory(), sort_keys=True)


def test_committed_inventory_matches_the_generator():
    with open("PHASE1_INVENTORY.json", "r", encoding="utf-8") as handle:
        committed = json.load(handle)
    generated = inventory_builder.build_inventory()
    assert committed["inventory_version"] == generated["inventory_version"]
    assert committed["database_inspected"] is False
    assert len(committed["targets"]) == len(generated["targets"])
    committed_keys = {
        (target["sport"], target["league_id"], target["season"], target["lifecycle_state"])
        for target in committed["targets"]
    }
    generated_keys = {
        (target["sport"], target["league_id"], target["season"], target["lifecycle_state"])
        for target in generated["targets"]
    }
    assert committed_keys == generated_keys
