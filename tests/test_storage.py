import sqlite3

import pytest

import config
import storage


@pytest.fixture
def temp_database(tmp_path, monkeypatch):
    db_path = tmp_path / "predictions.db"

    monkeypatch.setattr(
        config,
        "DB_PATH",
        str(db_path),
    )

    storage.init_db()

    return db_path


def football_markets():
    return {
        "match_result": {
            "home_win": 0.60,
            "draw": 0.20,
            "away_win": 0.20,
        }
    }


def football_confidence():
    return {
        "label": "High",
        "top_pick": "Home Win",
        "top_probability": 0.60,
    }


def basketball_markets():
    return {
        "moneyline": {
            "home_win": 0.65,
            "away_win": 0.35,
        },
        "total_points": {
            "over_220_5": 0.55,
            "under_220_5": 0.45,
        },
    }


def basketball_confidence():
    return {
        "label": "Moderate",
        "top_pick": "Home Win",
        "top_probability": 0.65,
    }


def test_save_prediction_inserts_once(temp_database):
    first = storage.save_prediction(
        fixture_id=1001,
        match_date="2026-09-28",
        home_team="Home FC",
        away_team="Away FC",
        league="Premier League",
        markets=football_markets(),
        confidence=football_confidence(),
        home_team_id=10,
        away_team_id=20,
    )

    second = storage.save_prediction(
        fixture_id=1001,
        match_date="2026-09-28",
        home_team="Changed Home",
        away_team="Changed Away",
        league="Changed League",
        markets={
            "match_result": {
                "home_win": 0.10,
            }
        },
        confidence={
            "label": "Toss-up",
            "top_pick": "Away Win",
            "top_probability": 0.90,
        },
        home_team_id=99,
        away_team_id=98,
    )

    assert first is True
    assert second is False

    conn = sqlite3.connect(temp_database)

    try:
        row = conn.execute(
            """
            SELECT
                home_team,
                away_team,
                league,
                markets_json,
                confidence_label,
                top_pick,
                top_probability,
                home_team_id,
                away_team_id
            FROM predictions
            WHERE fixture_id = ?
            """,
            (1001,),
        ).fetchone()
    finally:
        conn.close()

    assert row[0] == "Home FC"
    assert row[1] == "Away FC"
    assert row[2] == "Premier League"
    assert row[4] == "High"
    assert row[5] == "Home Win"
    assert row[6] == pytest.approx(0.60)
    assert row[7] == 10
    assert row[8] == 20


def test_record_result_is_idempotent(temp_database):
    storage.save_prediction(
        fixture_id=1002,
        match_date="2026-09-28",
        home_team="Home FC",
        away_team="Away FC",
        league="Premier League",
        markets=football_markets(),
        confidence=football_confidence(),
        home_team_id=10,
        away_team_id=20,
    )

    first = storage.record_result(
        fixture_id=1002,
        home_goals=2,
        away_goals=1,
    )

    second = storage.record_result(
        fixture_id=1002,
        home_goals=2,
        away_goals=1,
    )

    assert first is True
    assert second is True

    conn = sqlite3.connect(temp_database)

    try:
        row = conn.execute(
            """
            SELECT
                actual_home_goals,
                actual_away_goals,
                top_pick_correct
            FROM predictions
            WHERE fixture_id = ?
            """,
            (1002,),
        ).fetchone()
    finally:
        conn.close()

    assert row[0] == 2
    assert row[1] == 1
    assert row[2] == 1


def test_record_result_rejects_conflicting_result(
    temp_database,
):
    storage.save_prediction(
        fixture_id=1003,
        match_date="2026-09-28",
        home_team="Home FC",
        away_team="Away FC",
        league="Premier League",
        markets=football_markets(),
        confidence=football_confidence(),
    )

    storage.record_result(
        fixture_id=1003,
        home_goals=2,
        away_goals=1,
    )

    with pytest.raises(ValueError):
        storage.record_result(
            fixture_id=1003,
            home_goals=1,
            away_goals=2,
        )


def test_record_result_unknown_fixture_returns_false(
    temp_database,
):
    assert (
        storage.record_result(
            fixture_id=999999,
            home_goals=1,
            away_goals=0,
        )
        is False
    )


def test_accuracy_summary_empty_database(
    temp_database,
):
    result = storage.accuracy_summary()

    assert result["total_graded"] == 0
    assert result["overall_accuracy"] == 0.0
    assert result["by_confidence"] == {}


def test_basketball_prediction_is_immutable(
    temp_database,
):
    storage.init_basketball_db()

    first = storage.save_basketball_prediction(
        game_id=2001,
        game_date="2026-09-28",
        home_team="Home",
        away_team="Away",
        league="NBA",
        markets=basketball_markets(),
        confidence=basketball_confidence(),
    )

    second = storage.save_basketball_prediction(
        game_id=2001,
        game_date="2026-09-28",
        home_team="Changed Home",
        away_team="Changed Away",
        league="Changed League",
        markets={
            "moneyline": {
                "home_win": 0.10,
            }
        },
        confidence={
            "label": "High",
            "top_pick": "Away Win",
            "top_probability": 0.90,
        },
    )

    assert first is True
    assert second is False

    conn = sqlite3.connect(temp_database)

    try:
        row = conn.execute(
            """
            SELECT
                home_team,
                away_team,
                league,
                confidence_label,
                top_pick,
                top_probability
            FROM basketball_predictions
            WHERE game_id = ?
            """,
            (2001,),
        ).fetchone()
    finally:
        conn.close()

    assert row[0] == "Home"
    assert row[1] == "Away"
    assert row[2] == "NBA"
    assert row[3] == "Moderate"
    assert row[4] == "Home Win"
    assert row[5] == pytest.approx(0.65)


def test_basketball_result_is_idempotent(
    temp_database,
):
    storage.init_basketball_db()

    storage.save_basketball_prediction(
        game_id=2002,
        game_date="2026-09-28",
        home_team="Home",
        away_team="Away",
        league="NBA",
        markets=basketball_markets(),
        confidence=basketball_confidence(),
    )

    first = storage.record_basketball_result(
        game_id=2002,
        home_points=110,
        away_points=100,
    )

    second = storage.record_basketball_result(
        game_id=2002,
        home_points=110,
        away_points=100,
    )

    assert first is True
    assert second is True


def test_basketball_result_rejects_conflict(
    temp_database,
):
    storage.init_basketball_db()

    storage.save_basketball_prediction(
        game_id=2003,
        game_date="2026-09-28",
        home_team="Home",
        away_team="Away",
        league="NBA",
        markets=basketball_markets(),
        confidence=basketball_confidence(),
    )

    storage.record_basketball_result(
        game_id=2003,
        home_points=110,
        away_points=100,
    )

    with pytest.raises(ValueError):
        storage.record_basketball_result(
            game_id=2003,
            home_points=100,
            away_points=110,
      )


def test_historical_fixtures_storage(temp_database):
    sample_fixtures = [
        {
            "fixture": {"id": 3002, "date": "2025-02-01T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {
                "home": {"id": 101, "name": "Team B"},
                "away": {"id": 102, "name": "Team C"},
            },
            "goals": {"home": 1, "away": 0},
        },
        {
            "fixture": {"id": 3001, "date": "2025-01-01T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {
                "home": {"id": 100, "name": "Team A"},
                "away": {"id": 101, "name": "Team B"},
            },
            "goals": {"home": 2, "away": 1},
        },
    ]

    # Test saving fixtures
    save_res = storage.save_historical_fixtures(sample_fixtures, league_id=39, season=2024)
    assert save_res["total"] == 2
    assert save_res["valid"] == 2
    assert save_res["inserted"] == 2

    # Idempotent insert test
    save_res_dup = storage.save_historical_fixtures(sample_fixtures, league_id=39, season=2024)
    assert save_res_dup["inserted"] == 0

    # Count test
    assert storage.get_historical_fixture_count(39, 2024) == 2
    assert storage.get_historical_fixture_count(39, 2025) == 0

    # Deterministic ordering test (kickoff_at ASC, fixture_id ASC)
    fetched = storage.get_historical_fixtures(39, 2024)
    assert len(fetched) == 2
    assert fetched[0]["fixture"]["id"] == 3001
    assert fetched[1]["fixture"]["id"] == 3002


def test_historical_fixture_enrichment_storage(temp_database):
    # Test A — initial insert
    initial = {
        "fixture": {"id": 3001},
        "statistics": {
            "shots": {"home": 5, "away": 3}
        }
    }
    inserted = storage.save_historical_enrichment([initial], source="api_football")
    assert inserted == 1

    fetched_init = storage.get_historical_enrichment([3001], source="api_football")
    assert 3001 in fetched_init
    assert fetched_init[3001]["statistics"]["shots"]["home"] == 5

    # Test B — same source improved record must replace the old row
    improved = {
        "fixture": {"id": 3001},
        "statistics": {
            "shots": {"home": 10, "away": 7},
            "corners": {"home": 6, "away": 4}
        }
    }
    updated = storage.save_historical_enrichment([improved], source="api_football")
    assert updated == 1

    fetched_improved = storage.get_historical_enrichment([3001], source="api_football")
    assert fetched_improved[3001]["statistics"]["shots"]["home"] == 10
    assert fetched_improved[3001]["statistics"]["corners"]["home"] == 6

    # Test C — source isolation must remain intact
    fallback_record = {
        "fixture": {"id": 3001},
        "statistics": {
            "shots": {"home": 8, "away": 6}
        }
    }
    fd_inserted = storage.save_historical_enrichment([fallback_record], source="football_data_org")
    assert fd_inserted == 1

    api_record = storage.get_historical_enrichment([3001], source="api_football")
    fd_record = storage.get_historical_enrichment([3001], source="football_data_org")

    assert api_record[3001]["statistics"]["shots"]["home"] == 10
    assert fd_record[3001]["statistics"]["shots"]["home"] == 8

    # Test D — source=None priority must remain intact
    reconciled_rec = {
        "fixture": {"id": 3001},
        "statistics": {
            "shots": {"home": 12, "away": 8}
        },
        "enrichment_provenance": {"record_type": "reconciled"}
    }
    rec_inserted = storage.save_historical_enrichment([reconciled_rec], source="reconciled")
    assert rec_inserted == 1

    priority_fetched = storage.get_historical_enrichment([3001])
    assert priority_fetched[3001]["statistics"]["shots"]["home"] == 12

    # Test E — identical payload remains idempotent
    dup_inserted = storage.save_historical_enrichment([improved], source="api_football")
    assert dup_inserted == 0

    fetched_dup = storage.get_historical_enrichment([3001], source="api_football")
    assert fetched_dup[3001]["statistics"]["shots"]["home"] == 10


def test_historical_enrichment_source_isolation_between_provider_and_reconciled(
    temp_database,
):
    provider_record = {
        "fixture": {"id": 3010},
        "statistics": {
            "shots": {"home": 5, "away": 3}
        },
    }

    reconciled_record = {
        "fixture": {"id": 3010},
        "statistics": {
            "shots": {"home": 9, "away": 7}
        },
        "reconciliation_metadata": {
            "providers_used": [
                "api_football",
                "football_data_org",
            ],
            "provider_count": 2,
            "is_reconciled": True,
        },
    }

    assert (
        storage.save_historical_enrichment(
            [provider_record],
            source="api_football",
        )
        == 1
    )

    assert (
        storage.save_historical_enrichment(
            [reconciled_record],
            source="reconciled",
        )
        == 1
    )

    api_result = storage.get_historical_enrichment(
        [3010],
        source="api_football",
    )
    reconciled_result = storage.get_historical_enrichment(
        [3010],
        source="reconciled",
    )

    assert api_result[3010]["statistics"]["shots"]["home"] == 5
    assert reconciled_result[3010]["statistics"]["shots"]["home"] == 9


def test_save_historical_fixtures_season_guards(temp_database):
    sample_fixture = [{
        "fixture": {"id": 3001, "date": "2025-01-01T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2024},
        "teams": {
            "home": {"id": 100, "name": "Team A"},
            "away": {"id": 101, "name": "Team B"},
        },
        "goals": {"home": 2, "away": 1},
    }]

    # Seasons 2024, 2025, 2026 are allowed
    res_2024 = storage.save_historical_fixtures(sample_fixture, league_id=39, season=2024)
    assert res_2024["inserted"] == 1

    sample_fixture_2025 = [{
        "fixture": {"id": 3002, "date": "2025-01-01T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2025},
        "teams": {
            "home": {"id": 100, "name": "Team A"},
            "away": {"id": 101, "name": "Team B"},
        },
        "goals": {"home": 2, "away": 1},
    }]
    res_2025 = storage.save_historical_fixtures(sample_fixture_2025, league_id=39, season=2025)
    assert res_2025["inserted"] == 1

    sample_fixture_2026 = [{
        "fixture": {"id": 3003, "date": "2026-01-01T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": 39, "season": 2026},
        "teams": {
            "home": {"id": 100, "name": "Team A"},
            "away": {"id": 101, "name": "Team B"},
        },
        "goals": {"home": 2, "away": 1},
    }]
    res_2026 = storage.save_historical_fixtures(sample_fixture_2026, league_id=39, season=2026)
    assert res_2026["inserted"] == 1

    # Season 2027 or 2023 raises ValueError
    with pytest.raises(ValueError, match="Football historical storage only supports seasons"):
        storage.save_historical_fixtures(sample_fixture, league_id=39, season=2027)

    with pytest.raises(ValueError, match="Football historical storage only supports seasons"):
        storage.save_historical_fixtures(sample_fixture, league_id=39, season=2023)

    # Basketball historical storage is unaffected for other seasons (e.g. 2023, 2027)
    basketball_game = [{
        "id": 4001,
        "date": "2023-01-01T15:00:00+00:00",
        "league": {"id": 12, "season": 2023},
        "teams": {
            "home": {"id": 200, "name": "Team X"},
            "away": {"id": 201, "name": "Team Y"},
        },
        "scores": {
            "home": {"total": 100},
            "away": {"total": 95},
        },
        "status": {"short": "FT"},
    }]
    bb_res_2023 = storage.save_historical_basketball_games(basketball_game, league_id=12, season=2023)
    assert bb_res_2023["inserted"] == 1


def test_historical_dataset_status_storage(temp_database):
    # Initial status is INCOMPLETE
    init_status = storage.get_historical_dataset_status(39, 2024)
    assert init_status["status"] == "INCOMPLETE"
    assert init_status["fixture_count"] == 0

    # Mark complete
    storage.mark_historical_dataset_complete(39, 2024, fixture_count=380)
    status = storage.get_historical_dataset_status(39, 2024)
    assert status["status"] == "COMPLETE"
    assert status["fixture_count"] == 380
    assert status["completed_at"] is not None

    # Mark incomplete
    storage.mark_historical_dataset_incomplete(39, 2024, fixture_count=100)
    status_inc = storage.get_historical_dataset_status(39, 2024)
    assert status_inc["status"] == "INCOMPLETE"
    assert status_inc["fixture_count"] == 100
