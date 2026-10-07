import pytest
import storage

def test_prediction_snapshot_immutability_and_coexistence(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "is_neon", lambda: False)
    monkeypatch.setattr(storage.config, "DB_PATH", str(tmp_path / "test_immutability.db"))
    storage.init_db()

    fixture_id = 101

    # 1. Create initial prediction snapshot for fixture 101
    snap_id1 = storage.save_prediction_snapshot(
        fixture_id=fixture_id,
        prediction_timestamp="2026-03-30T10:00:00Z",
        kickoff_at="2026-03-30T15:00:00Z",
        sport="football",
        league_id=39,
        season=2024,
        home_team="Arsenal",
        away_team="Chelsea",
        prediction_context="PRE_MATCH",
        model_version="v3.0.0",
        markets={"match_result": {"home_win": 0.55, "draw": 0.25, "away_win": 0.20}},
        quality_gate="PASS",
    )

    # 2. Store original values from legacy prediction as well
    storage.save_prediction(
        fixture_id=fixture_id,
        match_date="2026-03-30T15:00:00Z",
        home_team="Arsenal",
        away_team="Chelsea",
        league="Premier League",
        markets={"match_result": {"home_win": 0.55, "draw": 0.25, "away_win": 0.20}},
        confidence={"label": "Moderate", "top_pick": "home_win", "top_probability": 0.55},
        home_team_id=42,
        away_team_id=49,
    )

    snapshots_before_grading = storage.get_prediction_snapshots(fixture_id=fixture_id)
    assert len(snapshots_before_grading) == 1
    orig_snap = snapshots_before_grading[0]
    assert orig_snap["id"] == snap_id1
    assert orig_snap["markets"]["match_result"]["home_win"] == 0.55

    # 3. Create a second (live) snapshot for the same fixture 101
    snap_id2 = storage.save_prediction_snapshot(
        fixture_id=fixture_id,
        prediction_timestamp="2026-03-30T15:30:00Z",
        kickoff_at="2026-03-30T15:00:00Z",
        sport="football",
        league_id=39,
        season=2024,
        home_team="Arsenal",
        away_team="Chelsea",
        prediction_context="LIVE",
        model_version="v3.0.0",
        markets={"match_result": {"home_win": 0.70, "draw": 0.20, "away_win": 0.10}},
        quality_gate="PASS",
    )

    assert snap_id2 != snap_id1

    # Verify both snapshots coexist and order is newest first
    coexist_snaps = storage.get_prediction_snapshots(fixture_id=fixture_id)
    assert len(coexist_snaps) == 2
    assert coexist_snaps[0]["id"] == snap_id2
    assert coexist_snaps[1]["id"] == snap_id1
    # Verify earlier snapshot remains unchanged
    assert coexist_snaps[1]["markets"]["match_result"]["home_win"] == 0.55

    # 4. Run grading / result recording for fixture 101
    storage.record_result(fixture_id=fixture_id, home_goals=2, away_goals=1)
    storage.update_elo_ratings(home_id=42, home_name="Arsenal", away_id=49, away_name="Chelsea", home_goals=2, away_goals=1)

    # 5. Run cleanup routine
    storage.cleanup_non_target_leagues(keep_keywords=["Serie A"])

    # 6. Read snapshots again and verify original snapshot values are completely unchanged
    after_snaps = storage.get_prediction_snapshots(fixture_id=fixture_id)
    assert len(after_snaps) == 2

    earliest_snap = [s for s in after_snaps if s["id"] == snap_id1][0]
    assert earliest_snap["prediction_timestamp"] == "2026-03-30T10:00:00Z"
    assert earliest_snap["prediction_context"] == "PRE_MATCH"
    assert earliest_snap["markets"]["match_result"]["home_win"] == 0.55
    assert earliest_snap["quality_gate"] == "PASS"

    latest_snap = [s for s in after_snaps if s["id"] == snap_id2][0]
    assert latest_snap["prediction_timestamp"] == "2026-03-30T15:30:00Z"
    assert latest_snap["prediction_context"] == "LIVE"
    assert latest_snap["markets"]["match_result"]["home_win"] == 0.70


def test_get_prediction_snapshots_filtering(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "is_neon", lambda: False)
    monkeypatch.setattr(storage.config, "DB_PATH", str(tmp_path / "test_filtering.db"))
    storage.init_db()

    storage.save_prediction_snapshot(fixture_id=201, sport="football", markets={"match_result": {"home_win": 0.6}})
    storage.save_prediction_snapshot(fixture_id=202, sport="basketball", markets={"moneyline": {"home_win": 0.8}})

    fb_snaps = storage.get_prediction_snapshots(sport="football")
    assert len(fb_snaps) == 1
    assert fb_snaps[0]["fixture_id"] == 201

    bb_snaps = storage.get_prediction_snapshots(sport="basketball")
    assert len(bb_snaps) == 1
    assert bb_snaps[0]["fixture_id"] == 202

    f201_snaps = storage.get_prediction_snapshots(fixture_id=201)
    assert len(f201_snaps) == 1
    assert f201_snaps[0]["sport"] == "football"
