import os
import pytest
from unittest.mock import patch, MagicMock

import config
import data_resolver
import storage
from tests import completion_helpers
import api_football
import basketball_api
import espn_basketball_provider
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


@pytest.fixture(autouse=True)
def synthetic_nba_season_expectation(monkeypatch):
    """State the scale of this module's synthetic basketball fixtures.

    Production keeps the published NBA regular-season totals in
    config.NBA_EXPECTED_MIN_REGULAR_SEASON_GAMES; these tests persist a handful of
    games, so they declare their own explicit expectation instead of the
    production minimum being loosened.
    """
    expected = dict(getattr(config, "NBA_EXPECTED_MIN_REGULAR_SEASON_GAMES", {}) or {})
    for season in (2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026):
        expected[season] = 1
    monkeypatch.setattr(config, "NBA_EXPECTED_MIN_REGULAR_SEASON_GAMES", expected, raising=False)


@pytest.fixture(autouse=True)
def mock_team_universe(isolated_db, monkeypatch):
    import team_identity
    for lid in (39, 61, 78, 88, 90, 140):
        for tid in range(1, 21):
            p_id = lid * 100 + tid
            cid = f"football_team_{lid}_team_{tid}"
            team_identity.register_canonical_team_identity(cid, f"Team {tid}", "api_football", p_id, league_id=lid)

    def _mock_get_team_ids(league_id, season, *args, **kwargs):
        return [f"football_team_{league_id}_team_1", f"football_team_{league_id}_team_2"]
    monkeypatch.setattr("data_resolver._get_authoritative_historical_team_ids", _mock_get_team_ids)
    # The mocked competition universe is authoritative and self-consistent: the
    # expected competition size matches the two registered teams, so structural
    # coverage is judged against the same authority for provider responses and
    # for the persisted dataset.
    monkeypatch.setattr(data_resolver, "get_expected_league_team_count", lambda league_id, season=None: 2)


def sample_football_fixture(fid, date="2025-01-10T15:00:00+00:00", status="FT", home_goals=2, away_goals=1, league_id=39, home_id=1, away_id=2):
    c_home = f"football_team_{league_id}_team_{home_id}"
    c_away = f"football_team_{league_id}_team_{away_id}"
    h_pid = league_id * 100 + home_id
    a_pid = league_id * 100 + away_id
    return {
        "fixture": {"id": fid, "date": date, "status": {"short": status}},
        "league": {"id": league_id, "season": 2024},
        "teams": {"home": {"id": h_pid, "name": f"Team {home_id}"}, "away": {"id": a_pid, "name": f"Team {away_id}"}},
        "canonical_home_id": c_home,
        "canonical_away_id": c_away,
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


def espn_no_coverage_patch():
    """Force the documented basketball route deterministically and offline.

    ESPN reports a failed acquisition for every monthly scoreboard, so no ESPN
    coverage is validated and acquisition proceeds to the API-Basketball
    fallback (the paginated historical basketball route).
    """
    months = espn_basketball_provider.NBA_SEASON_MONTH_COUNT
    meta = {
        "months_attempted": months,
        "months_succeeded": 0,
        "months_failed": months,
        "all_months_succeeded": False,
        "monthly_details": [],
    }
    return patch("espn_basketball_provider.fetch_espn_nba_season_games", return_value=([], meta))


def espn_full_coverage_patch(games):
    """ESPN primary fully covers all nine monthly scoreboards."""
    months = espn_basketball_provider.NBA_SEASON_MONTH_COUNT
    meta = {
        "months_attempted": months,
        "months_succeeded": months,
        "months_failed": 0,
        "all_months_succeeded": True,
        "monthly_details": [{"month": "202411", "status": "SUCCESS", "event_count": len(games)}],
    }
    return patch("espn_basketball_provider.fetch_espn_nba_season_games", return_value=(games, meta))


@pytest.fixture
def espn_enabled(monkeypatch):
    """Enable the ESPN primary route so the ESPN-first basketball architecture
    (ESPN first, API-Basketball fallback) is the flow exercised by the test."""
    monkeypatch.setattr(config, "ENABLE_ESPN_BASKETBALL", True)


def no_soccerdata_sources():
    """Every SoccerData historical football provider reports no usable data."""
    return [
        patch("soccerdata_provider.get_fbref_games", return_value=("SOURCE_NOT_AVAILABLE", [], {})),
        patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_NOT_AVAILABLE", [], {})),
        patch("soccerdata_provider.get_sofascore_historical_games", return_value=("SOURCE_NOT_AVAILABLE", [], {})),
    ]


# ============================================================================
# 1. FOOTBALL PAGINATION + RESUMABILITY (A-G)
# ============================================================================


def test_A_to_G_pagination_failure_persistence_and_resumption(isolated_db, espn_enabled):
    """A-G on the paginated historical route.

    Football historical acquisition is single-shot (direct CSV -> SoccerData) and
    consumes no API pagination, so football page-level failure/resumption is
    retired. The identical requirement chain is exercised on the API-Basketball
    fallback, which is the paginated historical route that remains:
      A/B: a failed later page keeps the already-acquired games persisted,
      C:   the dataset stays INCOMPLETE,
      G:   the quota stop is recorded as the stopping reason,
      D/E/F: the next run completes the dataset without duplicating games.
    """
    bg1 = sample_basketball_game(7001)
    bg2 = sample_basketball_game(7002)

    def mock_p1_ok_p2_quota(league_id, season, page, max_budget=None):
        if page == 1:
            return {"games": [bg1], "page": 1, "expected_pages": 2}
        raise basketball_api.APIBasketballQuotaExhaustedError("Quota exhausted on page 2")

    # A, B, C, G: page 1 succeeds, page 2 fails with quota exhaustion
    with espn_no_coverage_patch(), \
         patch("basketball_api.get_league_games_page", side_effect=mock_p1_ok_p2_quota):
        rep1 = historical_sync.sync_historical_basketball_games(league_id=12, season=2024)

    assert rep1["status"] == "INCOMPLETE"  # C
    assert rep1["coverage_unit"] == "api_pages"
    assert rep1["completed_coverage_units"] == 1
    assert rep1["expected_coverage_units"] == 2
    assert rep1["quota_budget_stopped"] is True  # G

    # B: page 1 games remain persisted
    stored = storage.get_historical_basketball_games(12, 2024)
    assert len(stored) == 1
    assert stored[0]["id"] == 7001

    # D, E, F: the next run completes the dataset without duplicating games
    def mock_p2_resume(league_id, season, page, max_budget=None):
        if page == 1:
            return {"games": [bg1], "page": 1, "expected_pages": 2}
        if page == 2:
            return {"games": [bg1, bg2], "page": 2, "expected_pages": 2}  # bg1 duplicate
        raise RuntimeError("Unexpected page")

    with espn_no_coverage_patch(), \
         patch("basketball_api.get_league_games_page", side_effect=mock_p2_resume):
        rep2 = historical_sync.sync_historical_basketball_games(league_id=12, season=2024)

    assert rep2["status"] == "COMPLETE"  # F
    assert rep2["completed_coverage_units"] == 2
    stored_final = storage.get_historical_basketball_games(12, 2024)
    assert len(stored_final) == 2  # E: no duplicates


# ============================================================================
# 2. BASKETBALL PAGINATION + COMPLETENESS (H-M)
# ============================================================================


def test_H_to_M_basketball_pagination_deduplication_and_completeness(isolated_db, espn_enabled):
    bg1 = sample_basketball_game(8001)
    bg2 = sample_basketball_game(8002)
    bg_malformed = {"id": 8003, "date": "2024-11-12T20:00:00+00:00"} # missing teams

    def mock_b_p1(league_id, season, page=1, max_budget=None):
        if page == 1:
            return {"games": [bg1], "page": 1, "expected_pages": 2}
        return {"games": [bg1, bg2, bg_malformed], "page": 2, "expected_pages": 2}

    with espn_no_coverage_patch(), \
         patch("basketball_api.get_league_games_page", side_effect=mock_b_p1):
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


def test_consumer_level_settlement_outcomes_football():
    import market_grading

    # 1. Normal FT (2-1)
    ft_fixture = {
        "fixture": {"id": 101, "status": {"short": "FT"}},
        "goals": {"home": 2, "away": 1},
        "score": {"fulltime": {"home": 2, "away": 1}},
    }
    graded_ft = market_grading.grade_fixture_markets(ft_fixture)
    gm_ft = graded_ft["goal_markets"]
    assert gm_ft["match_result"]["outcome"] == "home_win"
    assert gm_ft["double_chance"]["home_or_draw"]["won"] is True
    assert gm_ft["btts"]["yes"]["won"] is True
    assert gm_ft["over_under"]["over_2_5"]["won"] is True

    # 2. AET fixture with fulltime present (regulation 1-1, extra-time 2-1 final)
    aet_fixture = {
        "fixture": {"id": 102, "status": {"short": "AET"}},
        "goals": {"home": 2, "away": 1},
        "score": {
            "fulltime": {"home": 1, "away": 1},
            "extratime": {"home": 1, "away": 0},
        },
    }
    graded_aet = market_grading.grade_fixture_markets(aet_fixture)
    gm_aet = graded_aet["goal_markets"]
    # 1X2 uses regulation 1-1 -> draw!
    assert gm_aet["match_result"]["outcome"] == "draw"
    assert gm_aet["double_chance"]["home_or_draw"]["won"] is True
    assert gm_aet["double_chance"]["away_or_draw"]["won"] is True
    # Totals & BTTS use match goals (2-1) -> BTTS yes, Over 2.5 over
    assert gm_aet["btts"]["yes"]["won"] is True
    assert gm_aet["over_under"]["over_2_5"]["won"] is True

    # 3. PEN fixture with fulltime present (regulation 1-1, shootout 4-3)
    pen_fixture = {
        "fixture": {"id": 103, "status": {"short": "PEN"}},
        "goals": {"home": 1, "away": 1},
        "score": {
            "fulltime": {"home": 1, "away": 1},
            "penalty": {"home": 4, "away": 3},
        },
    }
    graded_pen = market_grading.grade_fixture_markets(pen_fixture)
    gm_pen = graded_pen["goal_markets"]
    # 1X2 uses regulation 1-1 -> draw! (shootout kicks excluded)
    assert gm_pen["match_result"]["outcome"] == "draw"
    assert gm_pen["btts"]["yes"]["won"] is True
    assert gm_pen["over_under"]["under_2_5"]["won"] is True

    # 4. AET fixture missing score.fulltime -> fails closed for 1X2 and DC!
    aet_missing_fulltime = {
        "fixture": {"id": 104, "status": {"short": "AET"}},
        "goals": {"home": 2, "away": 1},
        "score": {"extratime": {"home": 1, "away": 0}}, # missing fulltime block
    }
    graded_missing = market_grading.grade_fixture_markets(aet_missing_fulltime)
    gm_missing = graded_missing["goal_markets"]
    assert gm_missing["match_result"] is None
    assert gm_missing["double_chance"] is None
    # Totals and BTTS still grade safely from match goals (2-1)
    assert gm_missing["btts"]["yes"]["won"] is True
    assert gm_missing["over_under"]["over_2_5"]["won"] is True


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
    prior = historical_features.prior_completed_fixtures(fixtures, "2026-01-01T00:00:00+00:00")
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
    completion_helpers.force_manifest(12, 2024, fixture_count=1230, sport="basketball")

    # Verify coexistence without primary key collision
    status_bb = storage.get_historical_dataset_status(12, 2024, sport="basketball")
    assert status_bb["status"] == "COMPLETE"
    assert status_bb["fixture_count"] == 1230

    status_fb_recheck = storage.get_historical_dataset_status(12, 2024, sport="football")
    assert status_fb_recheck["fixture_count"] == 380


def test_pre_acquisition_quota_exhaustion_preserves_manifest_fields_football_and_basketball(isolated_db, espn_enabled):
    # Football pre-population. Football historical acquisition now consumes no API
    # quota (direct CSV / SoccerData), so there is no football quota gate; the
    # football half below proves a run that acquires nothing preserves every
    # previously recorded manifest counter instead of wiping progress.
    storage.mark_historical_dataset_incomplete(
        league_id=88, season=2024, fixture_count=50, sport="football",
        expected_pages=10, pages_completed=4, acquisition_complete=False,
        enrichment_status="PARTIAL", rejected_count=3, empty_pages_count=1,
    )
    # Basketball pre-population (same league_id=88, season=2024 - sport separation)
    storage.mark_historical_dataset_incomplete(
        league_id=88, season=2024, fixture_count=60, sport="basketball",
        expected_pages=12, pages_completed=5, acquisition_complete=False,
        enrichment_status="PARTIAL", rejected_count=2, empty_pages_count=2,
    )

    # Exhaust the basketball historical quota ledger
    for _ in range(50):
        storage.record_api_request("api_basketball", "games")

    # Football: approved providers supply nothing, and the retired API route is
    # never consulted.
    with patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[]), \
         patch("api_football.get_league_fixtures_page") as mock_fb_http:
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in no_soccerdata_sources():
                stack.enter_context(p)
            rep_fb = historical_sync.sync_historical_fixtures(league_id=88, season=2024, historical_budget=50)
        mock_fb_http.assert_not_called()

    assert rep_fb["status"] == "INCOMPLETE"

    # Assert Football manifest preserved
    st_fb = storage.get_historical_dataset_status(88, 2024, sport="football")
    assert st_fb["status"] == "INCOMPLETE"
    assert st_fb["expected_pages"] == 10
    assert st_fb["pages_completed"] == 4
    assert st_fb["acquisition_complete"] is False
    assert st_fb["enrichment_status"] == "PARTIAL"
    assert st_fb["rejected_count"] == 3
    # Empty-page occurrences are cumulative and never reset by a later run
    assert st_fb["empty_pages_count"] >= 1

    # Basketball: quota already exhausted before acquisition -> the API provider is
    # never called and the existing manifest fields are preserved.
    with espn_no_coverage_patch(), patch("basketball_api.get_league_games_page") as mock_bb_http:
        rep_bb = historical_sync.sync_historical_basketball_games(league_id=88, season=2024, historical_budget=50)
        mock_bb_http.assert_not_called()

    assert rep_bb["status"] == "INCOMPLETE"
    assert rep_bb["quota_budget_stopped"] is True

    # Assert Basketball manifest preserved
    st_bb = storage.get_historical_dataset_status(88, 2024, sport="basketball")
    assert st_bb["status"] == "INCOMPLETE"
    assert st_bb["expected_pages"] == 12
    assert st_bb["pages_completed"] == 5
    assert st_bb["acquisition_complete"] is False
    assert st_bb["enrichment_status"] == "PARTIAL"
    assert st_bb["rejected_count"] == 2
    assert st_bb["empty_pages_count"] == 2
    assert st_bb["error_reason"] == "quota_budget_exhausted_before_acquisition"


def test_later_page_quota_interruption_and_resume_basketball_fallback(isolated_db, espn_enabled):
    """Quota interruption on a later page keeps recorded coverage and resumes.

    Exercised on the API-Basketball fallback: football historical acquisition is
    single-shot and consumes no API quota.
    """
    bg1 = sample_basketball_game(1501)
    bg2 = sample_basketball_game(1502)

    def mock_p1_ok_p2_quota(league_id, season, page, max_budget=None):
        if page == 1:
            return {"games": [bg1], "page": 1, "expected_pages": 2}
        raise basketball_api.APIBasketballQuotaExhaustedError("Quota exhausted on page 2")

    with espn_no_coverage_patch(), \
         patch("basketball_api.get_league_games_page", side_effect=mock_p1_ok_p2_quota):
        rep = historical_sync.sync_historical_basketball_games(league_id=12, season=2024, historical_budget=50)

    assert rep["status"] == "INCOMPLETE"
    assert rep["completed_coverage_units"] == 1
    assert rep["expected_coverage_units"] == 2
    assert rep["quota_budget_stopped"] is True
    st = storage.get_historical_dataset_status(12, 2024, sport="basketball")
    assert st["pages_completed"] == 1
    assert st["expected_pages"] == 2
    assert st["coverage_unit"] == "api_pages"
    assert st["error_reason"] == "quota_budget_exhausted_during_acquisition"

    # Resume from the recorded coverage
    def mock_p2_resume(league_id, season, page, max_budget=None):
        if page == 1:
            return {"games": [bg1], "page": 1, "expected_pages": 2}
        if page == 2:
            return {"games": [bg2], "page": 2, "expected_pages": 2}
        raise RuntimeError(f"Unexpected page fetch for page {page}")

    with espn_no_coverage_patch(), \
         patch("basketball_api.get_league_games_page", side_effect=mock_p2_resume):
        rep_res = historical_sync.sync_historical_basketball_games(league_id=12, season=2024, historical_budget=50)

    assert rep_res["status"] == "COMPLETE"
    assert rep_res["completed_coverage_units"] == 2
    assert len(storage.get_historical_basketball_games(12, 2024)) == 2


def test_api_exception_checkpoint_and_resume_basketball_fallback(isolated_db, espn_enabled):
    """A transient API error on a later page checkpoints progress and resumes."""
    bg1 = sample_basketball_game(8501, league_id=12)
    bg2 = sample_basketball_game(8502, league_id=12)

    def mock_bb_p1_ok_p2_err(league_id, season, page, max_budget=None):
        if page == 1:
            return {"games": [bg1], "page": 1, "expected_pages": 2}
        raise RuntimeError("API network timeout on page 2")

    with espn_no_coverage_patch(), \
         patch("basketball_api.get_league_games_page", side_effect=mock_bb_p1_ok_p2_err):
        rep = historical_sync.sync_historical_basketball_games(league_id=12, season=2024)

    assert rep["status"] == "INCOMPLETE"
    assert rep["completed_coverage_units"] == 1
    assert rep["expected_coverage_units"] == 2
    st = storage.get_historical_dataset_status(12, 2024, sport="basketball")
    assert st["pages_completed"] == 1
    assert st["error_reason"] == "api_error_during_acquisition"

    # Resume from the recorded coverage
    def mock_bb_p2_resume(league_id, season, page, max_budget=None):
        if page == 1:
            return {"games": [bg1], "page": 1, "expected_pages": 2}
        if page == 2:
            return {"games": [bg2], "page": 2, "expected_pages": 2}
        raise RuntimeError(f"Unexpected page fetch for page {page}")

    with espn_no_coverage_patch(), \
         patch("basketball_api.get_league_games_page", side_effect=mock_bb_p2_resume):
        rep_res = historical_sync.sync_historical_basketball_games(league_id=12, season=2024)

    assert rep_res["status"] == "COMPLETE"
    assert rep_res["completed_coverage_units"] == 2


def test_early_quota_exhaustion_preserves_persisted_manifest_state_basketball_fallback(isolated_db, espn_enabled):
    """A pre-acquisition quota stop must never wipe recorded coverage progress."""
    bg_ok = sample_basketball_game(8401)
    bg_bad = {"id": 8402}  # malformed completed game

    def mock_bb_p1(league_id, season, page=1, max_budget=None):
        if page == 1:
            return {"games": [bg_ok, bg_bad], "page": 1, "expected_pages": 2}
        raise basketball_api.APIBasketballQuotaExhaustedError("Quota exhausted")

    with espn_no_coverage_patch(), \
         patch("basketball_api.get_league_games_page", side_effect=mock_bb_p1):
        rep_b1 = historical_sync.sync_historical_basketball_games(league_id=12, season=2024, historical_budget=50)

    assert rep_b1["completed_coverage_units"] == 1
    assert rep_b1["expected_coverage_units"] == 2
    assert rep_b1["rejected_count"] == 1

    # Simulate the basketball daily budget being exhausted before the next run starts
    for _ in range(50):
        storage.record_api_request("api_basketball", "games")

    with espn_no_coverage_patch(), patch("basketball_api.get_league_games_page") as mock_bb_http:
        rep_b_quota = historical_sync.sync_historical_basketball_games(league_id=12, season=2024, historical_budget=50)
        mock_bb_http.assert_not_called()

    # Early exit must NOT wipe out completed units, expected units, or rejected count
    assert rep_b_quota["status"] == "INCOMPLETE"
    assert rep_b_quota["quota_budget_stopped"] is True
    st_bb = storage.get_historical_dataset_status(12, 2024, sport="basketball")
    assert st_bb["pages_completed"] == 1
    assert st_bb["expected_pages"] == 2
    assert st_bb["rejected_count"] == 1
    assert st_bb["status"] == "INCOMPLETE"


def test_rejected_count_survives_resumed_sync_basketball_fallback(isolated_db, espn_enabled):
    """Rejected records survive a resumed run and keep the dataset INCOMPLETE."""
    bg_ok = sample_basketball_game(1101)
    bg_bad = {"id": 1102}  # malformed completed game

    def mock_p1(league_id, season, page=1, max_budget=None):
        if page == 1:
            return {"games": [bg_ok, bg_bad], "page": 1, "expected_pages": 2}
        raise basketball_api.APIBasketballQuotaExhaustedError("Quota exhausted")

    with espn_no_coverage_patch(), \
         patch("basketball_api.get_league_games_page", side_effect=mock_p1):
        rep1 = historical_sync.sync_historical_basketball_games(league_id=12, season=2024)

    assert rep1["rejected_count"] == 1
    assert rep1["status"] == "INCOMPLETE"

    # Resumed run: the remaining page is acquired, but the rejected record was
    # recorded and must survive the resume and block a COMPLETE promotion.
    bg_ok2 = sample_basketball_game(1103)

    def mock_p2(league_id, season, page=1, max_budget=None):
        if page == 1:
            return {"games": [bg_ok, bg_bad], "page": 1, "expected_pages": 2}
        return {"games": [bg_ok2], "page": 2, "expected_pages": 2}

    with espn_no_coverage_patch(), \
         patch("basketball_api.get_league_games_page", side_effect=mock_p2):
        rep2 = historical_sync.sync_historical_basketball_games(league_id=12, season=2024)

    # rejected_count MUST survive resumption and prevent the dataset becoming COMPLETE
    assert rep2["rejected_count"] >= 1
    assert rep2["status"] == "INCOMPLETE"
    st = storage.get_historical_dataset_status(12, 2024, sport="basketball")
    assert st["rejected_count"] >= 1
    assert st["status"] == "INCOMPLETE"


def test_empty_page_acquisition_scenarios_football_and_basketball(isolated_db, espn_enabled):
    from contextlib import ExitStack

    # A. Football: approved providers return nothing -> INCOMPLETE (empty != complete)
    with patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[]), \
         patch("api_football.get_league_fixtures_page") as mock_fb_http:
        with ExitStack() as stack:
            for p in no_soccerdata_sources():
                stack.enter_context(p)
            rep_fb_empty = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        mock_fb_http.assert_not_called()
    assert rep_fb_empty["status"] == "INCOMPLETE"
    assert rep_fb_empty["final_stored_count"] == 0

    # B. Football: a failed run never erases records already acquired
    fix1 = sample_football_fixture(1301, league_id=61, home_id=1, away_id=2)
    fix1["provider_provenance"] = {"provider": "football_data_csv", "provider_type": "primary"}

    with patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[fix1]):
        with ExitStack() as stack:
            for p in no_soccerdata_sources():
                stack.enter_context(p)
            rep_fb_partial = historical_sync.sync_historical_fixtures(league_id=61, season=2024)
    assert rep_fb_partial["status"] == "INCOMPLETE"
    assert rep_fb_partial["final_stored_count"] == 1

    # C. Basketball: ESPN delivers no coverage and the API fallback page is empty
    def mock_empty_p1(league_id, season, page=1, max_budget=None):
        return {"games": [], "page": 1, "expected_pages": 1}

    with espn_no_coverage_patch(), \
         patch("basketball_api.get_league_games_page", side_effect=mock_empty_p1):
        rep_bb_empty = historical_sync.sync_historical_basketball_games(league_id=12, season=2024)
    assert rep_bb_empty["status"] == "INCOMPLETE"

    # D. Football: a verified complete approved-route dataset becomes COMPLETE
    fix_a = sample_football_fixture(1304, league_id=140, home_id=1, away_id=2)
    fix_b = sample_football_fixture(1305, league_id=140, home_id=2, away_id=1)
    for fx in (fix_a, fix_b):
        fx["provider_provenance"] = {"provider": "football_data_csv", "provider_type": "primary"}

    with patch("data_resolver._fetch_direct_csv_league_fixtures", return_value=[fix_a, fix_b]), \
         patch("api_football.get_league_fixtures_page") as mock_fb_http:
        with ExitStack() as stack:
            for p in no_soccerdata_sources():
                stack.enter_context(p)
            rep_fb_valid = historical_sync.sync_historical_fixtures(league_id=140, season=2024)
        mock_fb_http.assert_not_called()
    assert rep_fb_valid["status"] == "COMPLETE"
    assert rep_fb_valid["final_stored_count"] == 2

    # E. Basketball: an empty middle page keeps the dataset INCOMPLETE
    bg101 = sample_basketball_game(8301, league_id=12)
    bg102 = sample_basketball_game(8302, league_id=12)

    def mock_b_empty_mid(league_id, season, page=1, max_budget=None):
        if page == 1:
            return {"games": [bg101], "page": 1, "expected_pages": 3}
        if page == 2:
            return {"games": [], "page": 2, "expected_pages": 3}
        return {"games": [bg102], "page": 3, "expected_pages": 3}

    with espn_no_coverage_patch(), \
         patch("basketball_api.get_league_games_page", side_effect=mock_b_empty_mid):
        rep_bb_mid = historical_sync.sync_historical_basketball_games(league_id=12, season=2024)
    assert rep_bb_mid["status"] == "INCOMPLETE"
    st_bb = storage.get_historical_dataset_status(12, 2024, sport="basketball")
    # Empty-page occurrences are counted cumulatively and never promote the dataset
    assert st_bb["empty_pages_count"] >= 1
    assert st_bb["status"] == "INCOMPLETE"


def test_espn_full_month_coverage_completes_basketball_dataset(isolated_db, monkeypatch):
    """ESPN monthly scoreboards are the coverage unit for ESPN-acquired NBA data."""
    monkeypatch.setattr(config, "ENABLE_ESPN_BASKETBALL", True)
    espn_games = [
        sample_basketball_game(8303, league_id=12),
        sample_basketball_game(8304, league_id=12),
    ]

    with espn_full_coverage_patch(espn_games), \
         patch("basketball_api.get_league_games_page") as mock_api_http:
        rep = historical_sync.sync_historical_basketball_games(league_id=12, season=2024)
        # Fully validated ESPN coverage means the fallback is never consulted
        mock_api_http.assert_not_called()

    assert rep["status"] == "COMPLETE"
    assert rep["coverage_unit"] == "espn_months"
    assert rep["expected_coverage_units"] == espn_basketball_provider.NBA_SEASON_MONTH_COUNT
    assert rep["completed_coverage_units"] == espn_basketball_provider.NBA_SEASON_MONTH_COUNT
    assert rep["final_stored_count"] == 2
    assert rep["provider_coverage"]["espn_url"]["status"] == "VALIDATED"


def test_changing_pagination_totals_fails_closed(isolated_db, espn_enabled):
    """Paginated acquisition fails closed when the provider changes its total.

    Football is single-shot (no pagination), so this contract is exercised on the
    API-Basketball fallback, which still reports paging metadata.
    """
    bg1 = sample_basketball_game(1201, league_id=12)

    # Page 1 claims expected_pages = 2, page 2 claims expected_pages = 3
    def mock_changing_pages(league_id, season, page=1, max_budget=None):
        if page == 1:
            return {"games": [bg1], "page": 1, "expected_pages": 2}
        return {"games": [bg1], "page": 2, "expected_pages": 3}

    with espn_no_coverage_patch(), \
         patch("basketball_api.get_league_games_page", side_effect=mock_changing_pages):
        rep = historical_sync.sync_historical_basketball_games(league_id=12, season=2024)

    assert rep["status"] == "INCOMPLETE"
    assert rep["acquisition_state"] == "FAILED"
    st = storage.get_historical_dataset_status(12, 2024, sport="basketball")
    assert st["status"] == "INCOMPLETE"
    assert st["error_reason"] == "pagination_metadata_mismatch"


def test_storage_level_complete_invariant_enforcement(isolated_db, monkeypatch):
    # 1. fixture_count <= 0 -> rejected
    with pytest.raises(ValueError, match="fixture_count is 0"):
        storage.mark_historical_dataset_complete(39, 2024, fixture_count=0, expected_pages=1, pages_completed=1, acquisition_complete=True)

    # 2. expected_pages <= 0 -> rejected
    with pytest.raises(ValueError, match="expected_pages"):
        storage.mark_historical_dataset_complete(39, 2024, fixture_count=10, expected_pages=0, pages_completed=0, acquisition_complete=True)

    # 3. pages_completed != expected_pages -> rejected
    with pytest.raises(ValueError, match="pages_completed"):
        storage.mark_historical_dataset_complete(39, 2024, fixture_count=10, expected_pages=2, pages_completed=1, acquisition_complete=True)

    # 4. acquisition_complete is False -> rejected
    with pytest.raises(ValueError, match="acquisition_complete is False"):
        storage.mark_historical_dataset_complete(39, 2024, fixture_count=10, expected_pages=2, pages_completed=2, acquisition_complete=False)

    # 5. rejected_count > 0 -> rejected
    with pytest.raises(ValueError, match="rejected_count"):
        storage.mark_historical_dataset_complete(39, 2024, fixture_count=10, expected_pages=1, pages_completed=1, acquisition_complete=True, rejected_count=1)

    # 6. empty_pages_count > 0 -> rejected
    with pytest.raises(ValueError, match="empty_pages_count"):
        storage.mark_historical_dataset_complete(39, 2024, fixture_count=10, expected_pages=1, pages_completed=1, acquisition_complete=True, empty_pages_count=1)

    # 7. Basketball valid dataset -> accepted
    bb_games = [sample_basketball_game(2000 + i, date=f"2024-11-{(i % 28) + 1:02d}T20:00:00+00:00") for i in range(100)]
    storage.save_historical_basketball_games(bb_games, league_id=12, season=2024)
    completion_helpers.allow_synthetic_nba_season(monkeypatch, 2024, len(bb_games))
    completion_helpers.mark_complete(12, 2024, fixture_count=100, sport="basketball", expected_pages=2, pages_completed=2, acquisition_complete=True)
    st = storage.get_historical_dataset_status(12, 2024, sport="basketball")
    assert st["status"] == "COMPLETE"


def test_U_to_Y_backtest_persistence_status_and_errors(isolated_db, monkeypatch):
    # Setup complete dataset
    games = [sample_basketball_game(1000 + i, date=f"2024-11-{i:02d}T20:00:00+00:00") for i in range(1, 10)]
    storage.save_historical_basketball_games(games, league_id=12, season=2024)
    completion_helpers.allow_synthetic_nba_season(monkeypatch, 2024, len(games))
    completion_helpers.mark_complete(12, 2024, fixture_count=len(games), sport="basketball")

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


