import data_resolver
import sports_knowledge
import team_identity
import config


def test_edition_catalogue_carries_verified_participants_and_formats():
    assert sports_knowledge.validate() == []

    premier_2023 = sports_knowledge.edition("football", 39, 2023)
    assert premier_2023["participant_status"] == sports_knowledge.PARTICIPANT_STATUS_VERIFIED
    assert len(premier_2023["participants"]) == 20
    assert premier_2023["fixture_total"] == 380
    assert premier_2023["coverage_requirements"]["expected_participant_count"] == 20
    assert [stage["id"] for stage in premier_2023["stages"]] == ["regular_season"]

    world_cup = sports_knowledge.edition("football", 1, 2022)
    assert world_cup["participant_status"] == "VERIFIED"
    assert len(world_cup["participants"]) == 32
    assert world_cup["fixture_total"] == 64
    assert [stage["id"] for stage in world_cup["stages"]] == [
        "group_stage",
        "round_of_16",
        "quarter_finals",
        "semi_finals",
        "third_place",
        "final",
    ]

    nba = sports_knowledge.edition("basketball", 12, 2024)
    assert nba["participant_status"] == "VERIFIED"
    assert len(nba["participants"]) == 30
    assert nba["fixture_total"] is None
    assert nba["coverage_requirements"]["expected_regular_season_games"] == 1230


def test_season_specific_promotion_and_relegation_changes_are_not_current_season_copies():
    promoted_2023 = set(sports_knowledge.edition("football", 39, 2023)["promoted"])
    promoted_2024 = set(sports_knowledge.edition("football", 39, 2024)["promoted"])
    assert promoted_2023 == {"Burnley", "Sheffield United", "Luton Town"}
    assert promoted_2024 == {"Leicester City", "Ipswich Town", "Southampton"}
    assert promoted_2023 != promoted_2024


def test_catalogue_aliases_are_competition_scoped():
    aliases = team_identity._get_sport_alias_map("football")
    assert aliases[("AZ", 88)] == "az_alkmaar"
    assert aliases[("Korea Republic", 1)] == "south_korea"
    assert aliases[("Nottingham Forest", 39)] == "39_nottingham"
    # No unqualified global alias was added for a club name.
    assert "AZ" not in aliases


def test_knowledge_resolver_returns_the_verified_edition_universe():
    premier_ids = data_resolver._knowledge_historical_team_ids(39, 2023)
    assert len(premier_ids) == 20
    assert len(set(premier_ids)) == 20
    assert "football_team_arsenal" in premier_ids

    world_cup_ids = data_resolver._knowledge_historical_team_ids(1, 2022)
    assert len(world_cup_ids) == 32
    assert "football_team_argentina" in world_cup_ids


def test_structural_validation_consumes_catalogue_participants_when_provider_declares_none():
    result = data_resolver._validate_historical_structural_coverage(
        [],
        season=2024,
        league_id=39,
        provider_metadata={},
    )
    assert result["verified"] is False
    assert result["reason"] == "team_universe_mismatch"
    assert result["expected_team_count"] == 20
    assert "team_universe_mismatch" not in ("authoritative_team_universe_unavailable",)


def test_future_editions_are_provisional_and_refreshable():
    future = sports_knowledge.edition("football", 39, 2027)
    assert future["exists"] is True
    assert future["is_future"] is True
    assert future["status"] == "UNKNOWN"
    assert future["evidence_status"] == "PROVISIONAL"
    assert "future_edition_details_not_refreshed" in future["evidence_gaps"]

    # Quadrennial competitions do not get a fabricated 2027 edition.
    assert sports_knowledge.edition("football", 1, 2027)["exists"] is False
    assert 2027 in sports_knowledge.edition_years("football", 39, include_future=True, future_to=2028)


def test_knowledge_gap_report_covers_configured_history_and_future_horizon():
    rows = sports_knowledge.knowledge_gap_report()
    configured = {("football", int(lid)) for lid in config.ALLOWED_LEAGUE_IDS}
    configured |= {("basketball", int(lid)) for lid in config.ALLOWED_BASKETBALL_LEAGUE_IDS}
    covered = {(row["sport"], row["league_id"]) for row in rows}
    assert configured == covered
    for row in rows:
        assert row["season"] >= 2018
        assert row["season"] <= sports_knowledge.KNOWLEDGE_FUTURE_HORIZON
        assert row["evidence_status"] in {
            sports_knowledge.EVIDENCE_CONFIRMED,
            sports_knowledge.EVIDENCE_PROVISIONAL,
            sports_knowledge.EVIDENCE_UNKNOWN,
            sports_knowledge.EVIDENCE_NOT_APPLICABLE,
        }
        if row["participant_status"] == sports_knowledge.PARTICIPANT_STATUS_UNKNOWN:
            assert row["expected_participant_count"] is None
            assert "authoritative_participant_universe" in row["evidence_gaps"]


def test_provider_route_filters_unsupported_competition_providers():
    premier = sports_knowledge.provider_route("football", 39, 2023)
    assert premier["eligible_providers"] == sports_knowledge.provider_order("football")
    assert premier["completion_allowed"] is True

    ucl = sports_knowledge.provider_route("football", 2, 2022)
    assert ucl["eligible_providers"] == ["soccerdata_sofascore"]
    assert ucl["unsupported_providers"]["football_data_csv"]
    assert ucl["completion_allowed"] is False


def test_lifecycle_requires_confirmed_knowledge_evidence_not_only_structural_coverage():
    from datetime import datetime, timezone
    import historical_sync

    records = [{"fixture": {"date": "2021-06-01T00:00:00+00:00"}}]
    state, reason, evidence = historical_sync.get_competition_lifecycle(
        "football", 140, 2020, records, coverage={"verified": True}
    )
    assert state == historical_sync.COMPETITION_LIFECYCLE_UNKNOWN
    assert reason == "knowledge_evidence_provisional"
    assert evidence["knowledge_evidence_status"] == sports_knowledge.EVIDENCE_PROVISIONAL

    state, reason, _ = historical_sync.get_competition_lifecycle(
        "football", 39, 2020, records, coverage={"verified": True}
    )
    assert state == historical_sync.COMPETITION_LIFECYCLE_COMPLETED
    assert reason == "season_concluded_with_verified_coverage"

    future_records = [{"fixture": {"date": "2028-06-01T00:00:00+00:00"}}]
    state, reason, _ = historical_sync.get_competition_lifecycle(
        "football",
        39,
        2027,
        future_records,
        coverage={"verified": True},
        now_utc=datetime(2028, 7, 1, tzinfo=timezone.utc),
    )
    assert state == historical_sync.COMPETITION_LIFECYCLE_UNKNOWN
    assert reason == "knowledge_evidence_provisional"


def test_prediction_context_never_blocks_on_unknown_evidence():
    future = sports_knowledge.prediction_context("football", 39, 2027, team_ids=["football_team_arsenal"])
    assert future["evidence_status"] == "PROVISIONAL"
    assert future["team_membership"] == {"football_team_arsenal": None}

    missing = sports_knowledge.prediction_context("football", 39, 2031, team_ids=[])
    assert missing["evidence_status"] == "UNKNOWN"


def test_inventory_exposes_participant_evidence_and_explicit_gaps():
    import build_phase1_inventory

    inventory = build_phase1_inventory.build_inventory()
    targets = {
        (target["sport"], target["league_id"], target["season"]): target
        for target in inventory["targets"]
    }

    premier = targets[("football", 39, 2023)]
    assert premier["participant_status"] == "VERIFIED"
    assert premier["expected_participant_count"] == 20
    assert premier["expected_coverage"]["expected_fixtures"] == 380
    assert premier["participant_evidence"]["source_url"]
    assert "authoritative_participant_universe" not in premier["evidence_gaps"]

    unverified = targets[("football", 140, 2020)]
    assert unverified["participant_status"] == "UNKNOWN"
    assert "authoritative_participant_universe" in unverified["evidence_gaps"]
    assert "roster" in unverified["next_safe_action"]
