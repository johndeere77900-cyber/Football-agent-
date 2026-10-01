import os
import pytest
from unittest.mock import patch, MagicMock

import config
import storage
import api_football
import basketball_api
import historical_sync
import historical_match_policy
import historical_features
import historical_elo
import historical_h2h
import backtest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_p0_p1.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()
    return db_file


def sample_football_fixture(fid, date="2025-01-10T15:00:00+00:00", status="FT", home_goals=2, away_goals=1, league_id=39):
    return {
        "fixture": {"id": fid, "date": date, "status": {"short": status}},
        "league": {"id": league_id, "season": 2024},
        "teams": {"home": {"id": 1, "name": "Team A"}, "away": {"id": 2, "name": "Team B"}},
        "goals": {"home": home_goals, "away": away_goals},
        "score": {
            "fulltime": {"home": 1, "away": 1},
            "extratime": {"home": 2, "away": 1},
            "penalty": {"home": 4, "away": 3},
        },
    }


def sample_basketball_game(gid, date="2024-11-10T20:00:00+00:00", status="FT", home_pts=105, away_pts=98, league_id=12):
    return {
        "id": gid,
        "date": date,
        "status": {"short": status},
        "league": {"id": league_id, "season": 2024, "name": "NBA"},
        "teams": {
            "home": {"id": 1, "name": "Lakers"},
            "away": {"id": 2, "name": "Celtics"},
        },
        "scores": {
            "home": {"total": home_pts},
            "away": {"total": away_pts},
        },
    }


# ============================================================================
# 1. FOOTBALL PAGINATION + RESUMABILITY (A-G)
# ============================================================================


def test_A_to_G_football_pagination_failure_persistence_and_resumption(isolated_db):
    fix1 = sample_football_fixture(7001)
    fix2 = sample_football_fixture(7002)

    def mock_p1_ok_p2_fail(league_id, season, page, max_budget=None):
        if page == 1:
            return {"fixtures": [fix1], "page": 1, "expected_pages": 2}
        raise api_football.APIFootballQuotaExhaustedError("Quota exhausted on page 2")

    # A, B, C, G: page 1 succeeds, page 2 fails with quota exhaustion
    with patch("api_football.get_league_fixtures_page", side_effect=mock_p1_ok_p2_fail):
        rep1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert rep1["status"] == "INCOMPLETE" # C
    assert rep1["pages_completed"] == 1
    assert rep1["expected_pages"] == 2
    assert rep1["quota_budget_stopped"] is True # G

    # B: page 1 fixtures remain persisted
    stored = storage.get_historical_fixtures(39, 2024)
    assert len(stored) == 1
    assert stored[0]["fixture"]["id"] == 7001

    # D, E, F: next run resumes from page 2 and completes dataset without duplicates
    def mock_p2_resume(league_id, season, page, max_budget=None):
        if page == 2:
            return {"fixtures": [fix1, fix2], "page": 2, "expected_pages": 2} # fix1 duplicate
        raise RuntimeError("Unexpected page")

    with patch("api_football.get_league_fixtures_page", side_effect=mock_p2_resume):
        rep2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert rep2["status"] == "COMPLETE" # F
    assert rep2["pages_completed"] == 2
    stored_final = storage.get_historical_fixtures(39, 2024)
    assert len(stored_final) == 2 # E: no duplicates


# ============================================================================
# 2. BASKETBALL PAGINATION + COMPLETENESS (H-M)
# ============================================================================


def test_H_to_M_basketball_pagination_deduplication_and_completeness(isolated_db):
    bg1 = sample_basketball_game(8001)
    bg2 = sample_basketball_game(8002)
    bg_malformed = {"id": 8003, "date": "2024-11-12T20:00:00+00:00"} # missing teams

    def mock_b_p1(league_id, season, page=1, max_budget=None):
        if page == 1:
            return {"games": [bg1], "page": 1, "expected_pages": 2}
        return {"games": [bg1, bg2, bg_malformed], "page": 2, "expected_pages": 2}

    with patch("basketball_api.get_league_games_page", side_effect=mock_b_p1):
        rep = historical_sync.sync_historical_basketball_games(league_id=12, season=2024)

    # L: malformed game rejected keeps dataset INCOMPLETE
    assert rep["rejected_count"] == 1
    assert rep["status"] == "INCOMPLETE" # M

    # K: deduplicated valid games stored
    stored = storage.get_historical_basketball_games(12, 2024)
    assert len(stored) == 2


# ============================================================================
# 3. MATCH STATUS + SCORE SEMANTICS POLICY (N-T)
# ============================================================================


def test_N_to_T_match_policy_semantics():
    ft = sample_football_fixture(9001, status="FT", home_goals=1, away_goals=1)
    aet = sample_football_fixture(9002, status="AET", home_goals=2, away_goals=1)
    pen = sample_football_fixture(9003, status="PEN", home_goals=1, away_goals=1)
    pst = sample_football_fixture(9004, status="PST", home_goals=0, away_goals=0)

    # N, O, P, Q
    assert historical_match_policy.is_finished_match(ft) is True
    assert historical_match_policy.is_finished_match(aet) is True
    assert historical_match_policy.is_finished_match(pen) is True
    assert historical_match_policy.is_finished_match(pst) is False
    assert historical_match_policy.is_postponed_or_cancelled(pst) is True

    # R, S: penalty shootout kicks excluded from regular goals
    assert historical_match_policy.get_football_match_goals(pen) == (1, 1)
    bd = historical_match_policy.get_score_breakdown(pen)
    assert bd["fulltime"] == (1, 1)
    assert bd["penalty"] == (4, 3)

    # T: modules use authoritative policy
    fixtures = [ft, aet, pen]
    prior = historical_features.prior_completed_fixtures(fixtures, "2026-01-01")
    assert len(prior) == 3


# ============================================================================
# 4. SEPARATE BASKETBALL HISTORICAL QUOTA CONFIGURATION
# ============================================================================


def test_separate_basketball_quota_configuration(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 10)
    monkeypatch.setattr(config, "API_BASKETBALL_HISTORICAL_DAILY_BUDGET", 25)

    assert config.API_FOOTBALL_HISTORICAL_DAILY_BUDGET == 10
    assert config.API_BASKETBALL_HISTORICAL_DAILY_BUDGET == 25


# ============================================================================
# 5. BACKTEST PERSISTENCE MACHINE-DETECTABLE STATUS (U-Y)
# ============================================================================


def test_legacy_sqlite_primary_key_migration(tmp_path, monkeypatch):
    import sqlite3
    legacy_db = tmp_path / "legacy.db"
    monkeypatch.setattr(config, "DB_PATH", str(legacy_db))
    monkeypatch.setattr(config, "ENVIRONMENT", "development")

    # Initialize legacy schema with primary key (league_id, season)
    conn = sqlite3.connect(str(legacy_db))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE historical_datasets (
            league_id INTEGER NOT NULL,
            season INTEGER NOT NULL,
            status TEXT NOT NULL,
            fixture_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (league_id, season)
        )
    """)
    cur.execute("""
        INSERT INTO historical_datasets (league_id, season, status, fixture_count)
        VALUES (12, 2024, 'COMPLETE', 380)
    """)
    conn.commit()
    conn.close()

    # Now run storage.init_db() which triggers legacy primary key migration
    storage.init_db()

    # Verify composite PK (sport, league_id, season)
    conn = sqlite3.connect(str(legacy_db))
    tbl_info = conn.execute("PRAGMA table_info(historical_datasets)").fetchall()
    pk_cols = [row[1] for row in tbl_info if row[5] > 0]
    conn.close()
    assert set(pk_cols) == {"sport", "league_id", "season"}

    # Verify football row preserved
    status_fb = storage.get_historical_dataset_status(12, 2024, sport="football")
    assert status_fb["status"] == "COMPLETE"
    assert status_fb["fixture_count"] == 380

    # Insert basketball row with identical league_id=12 and season=2024
    storage.mark_historical_dataset_complete(12, 2024, fixture_count=1230, sport="basketball")

    # Verify coexistence without primary key collision
    status_bb = storage.get_historical_dataset_status(12, 2024, sport="basketball")
    assert status_bb["status"] == "COMPLETE"
    assert status_bb["fixture_count"] == 1230

    status_fb_recheck = storage.get_historical_dataset_status(12, 2024, sport="football")
    assert status_fb_recheck["fixture_count"] == 380


def test_rejected_count_survives_resumed_sync_football_and_basketball(isolated_db):
    # Football: page 1 has 1 valid fixture + 1 rejected malformed fixture
    fix_ok = sample_football_fixture(1101)
    fix_bad = {"fixture": {"id": 1102}} # missing dates/teams

    def mock_fb_p1(league_id, season, page=1, max_budget=None):
        if page == 1:
            return {"fixtures": [fix_ok, fix_bad], "page": 1, "expected_pages": 2}
        raise api_football.APIFootballQuotaExhaustedError("Quota exhausted")

    with patch("api_football.get_league_fixtures_page", side_effect=mock_fb_p1):
        rep1 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert rep1["rejected_count"] == 1
    assert rep1["status"] == "INCOMPLETE"

    # Page 2 resumed run: valid fixture
    fix_ok2 = sample_football_fixture(1103)

    def mock_fb_p2(league_id, season, page=1, max_budget=None):
        if page == 2:
            return {"fixtures": [fix_ok2], "page": 2, "expected_pages": 2}
        raise RuntimeError("Unexpected page")

    with patch("api_football.get_league_fixtures_page", side_effect=mock_fb_p2):
        rep2 = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    # rejected_count MUST survive resumption and prevent dataset from becoming COMPLETE!
    assert rep2["rejected_count"] == 1
    assert rep2["status"] == "INCOMPLETE"
    st = storage.get_historical_dataset_status(39, 2024, sport="football")
    assert st["rejected_count"] == 1
    assert st["status"] == "INCOMPLETE"


def test_empty_page_acquisition_scenarios_football_and_basketball(isolated_db):
    # A. Football Empty first page -> INCOMPLETE
    def mock_empty_p1(league_id, season, page=1, max_budget=None):
        return {"fixtures": [], "page": 1, "expected_pages": 1}

    with patch("api_football.get_league_fixtures_page", side_effect=mock_empty_p1):
        rep_fb_empty = historical_sync.sync_historical_fixtures(league_id=301, season=2024)
    assert rep_fb_empty["status"] == "INCOMPLETE"

    # B. Football Empty middle page (Page 1 valid, Page 2 empty, Page 3 valid) -> INCOMPLETE
    fix1 = sample_football_fixture(1301, league_id=302)
    fix2 = sample_football_fixture(1302, league_id=302)

    def mock_empty_middle_p2(league_id, season, page=1, max_budget=None):
        if page == 1:
            return {"fixtures": [fix1], "page": 1, "expected_pages": 3}
        if page == 2:
            return {"fixtures": [], "page": 2, "expected_pages": 3}
        return {"fixtures": [fix2], "page": 3, "expected_pages": 3}

    with patch("api_football.get_league_fixtures_page", side_effect=mock_empty_middle_p2):
        rep_fb_mid = historical_sync.sync_historical_fixtures(league_id=302, season=2024)
    assert rep_fb_mid["status"] == "INCOMPLETE"

    # C. Football Empty final page -> INCOMPLETE
    fix303 = sample_football_fixture(1303, league_id=303)
    def mock_empty_final_p2(league_id, season, page=1, max_budget=None):
        if page == 1:
            return {"fixtures": [fix303], "page": 1, "expected_pages": 2}
        return {"fixtures": [], "page": 2, "expected_pages": 2}

    with patch("api_football.get_league_fixtures_page", side_effect=mock_empty_final_p2):
        rep_fb_fin = historical_sync.sync_historical_fixtures(league_id=303, season=2024)
    assert rep_fb_fin["status"] == "INCOMPLETE"

    # D. Football Valid 1-page dataset -> COMPLETE
    fix304 = sample_football_fixture(1304, league_id=304)
    def mock_valid_1p(league_id, season, page=1, max_budget=None):
        return {"fixtures": [fix304], "page": 1, "expected_pages": 1}

    with patch("api_football.get_league_fixtures_page", side_effect=mock_valid_1p):
        rep_fb_valid = historical_sync.sync_historical_fixtures(league_id=304, season=2024)
    assert rep_fb_valid["status"] == "COMPLETE"
    assert rep_fb_valid["final_stored_count"] == 1

    # E. Basketball Empty middle page -> INCOMPLETE
    bg101 = sample_basketball_game(8301, league_id=101)
    bg102 = sample_basketball_game(8302, league_id=101)

    def mock_b_empty_mid(league_id, season, page=1, max_budget=None):
        if page == 1:
            return {"games": [bg101], "page": 1, "expected_pages": 3}
        if page == 2:
            return {"games": [], "page": 2, "expected_pages": 3}
        return {"games": [bg102], "page": 3, "expected_pages": 3}

    with patch("basketball_api.get_league_games_page", side_effect=mock_b_empty_mid):
        rep_bb_mid = historical_sync.sync_historical_basketball_games(league_id=101, season=2024)
    assert rep_bb_mid["status"] == "INCOMPLETE"

    # F. Basketball valid multi-page dataset -> COMPLETE
    bg102_a = sample_basketball_game(8303, league_id=102)
    bg102_b = sample_basketball_game(8304, league_id=102)

    def mock_b_multi(league_id, season, page=1, max_budget=None):
        if page == 1:
            return {"games": [bg102_a], "page": 1, "expected_pages": 2}
        return {"games": [bg102_b], "page": 2, "expected_pages": 2}

    with patch("basketball_api.get_league_games_page", side_effect=mock_b_multi):
        rep_bb_multi = historical_sync.sync_historical_basketball_games(league_id=102, season=2024)
    assert rep_bb_multi["status"] == "COMPLETE"
    assert rep_bb_multi["final_stored_count"] == 2


def test_changing_pagination_totals_fails_closed(isolated_db):
    fix1 = sample_football_fixture(1201)

    # Page 1 claims expected_pages = 2, page 2 claims expected_pages = 3
    def mock_changing_pages(league_id, season, page=1, max_budget=None):
        if page == 1:
            return {"fixtures": [fix1], "page": 1, "expected_pages": 2}
        return {"fixtures": [fix1], "page": 2, "expected_pages": 3}

    with patch("api_football.get_league_fixtures_page", side_effect=mock_changing_pages):
        rep = historical_sync.sync_historical_fixtures(league_id=39, season=2024)

    assert rep["status"] == "INCOMPLETE"
    st = storage.get_historical_dataset_status(39, 2024, sport="football")
    assert st["status"] == "INCOMPLETE"


def test_U_to_Y_backtest_persistence_status_and_errors(isolated_db, monkeypatch):
    # Setup complete dataset
    games = [sample_basketball_game(1000 + i, date=f"2024-11-{i:02d}T20:00:00+00:00") for i in range(1, 10)]
    storage.save_historical_basketball_games(games, league_id=12, season=2024)
    storage.mark_historical_dataset_complete(12, 2024, fixture_count=len(games), sport="basketball")

    # U: successful backtest + successful persistence
    res_ok = backtest.run_basketball_backtest(league_id=12, season=2024, sample_size=5, min_prior_matches=2)
    assert res_ok["status"] == "COMPLETED"
    assert res_ok["persisted"] is True
    assert res_ok["persistence_error"] is None

    # V: successful backtest + persistence failure
    def mock_save_error(*args, **kwargs):
        raise RuntimeError("Database disk full error")

    monkeypatch.setattr(storage, "save_backtest_run", mock_save_error)

    res_fail = backtest.run_basketball_backtest(league_id=12, season=2024, sample_size=5, min_prior_matches=2)
    assert res_fail["status"] == "PERSISTENCE_FAILED"
    assert res_fail["persisted"] is False
    assert "Database disk full error" in res_fail["persistence_error"]
    assert res_fail["graded"] > 0 # computation succeeded
