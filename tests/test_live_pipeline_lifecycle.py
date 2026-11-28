"""Integration coverage for the lifecycle-backed discovery, Telegram and result paths."""

import datetime as dt

import lifecycle_storage as lstore
import live_lifecycle
import live_providers
import provider_registry
import storage
import telegram_bot
import team_identity


def _iso(hours: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).isoformat()


def _seed(**overrides):
    record = {
        "lifecycle_id": "football:api_football:9001",
        "sport": "football",
        "canonical_fixture_key": "api_football:9001",
        "provider": "api_football",
        "provider_fixture_id": "9001",
        "league_id": 39,
        "season": 2026,
        "home_team_id": "arsenal",
        "away_team_id": "chelsea",
        "kickoff_at": _iso(3),
        "stage": "TRACKED",
        "provider_status": "NS",
    }
    record.update(overrides)
    return lstore.upsert_lifecycle(record)


def _save_snapshot(row, key_suffix="PRE_MATCH"):
    return storage.save_prediction_snapshot(
        fixture_id=int(row["provider_fixture_id"]),
        sport=row["sport"],
        league_id=row["league_id"],
        season=row["season"],
        canonical_home_id=row["home_team_id"],
        canonical_away_id=row["away_team_id"],
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff_at=row["kickoff_at"],
        prediction_context="PRE_MATCH",
        data_cutoff_timestamp=row["kickoff_at"],
        markets={"match_result": {"home_win": 0.55, "draw": 0.25, "away_win": 0.20}},
        confidence={"label": "Moderate"},
        quality_gate="PASS",
        reason_codes=[],
        prediction_record={
            "fixture_id": int(row["provider_fixture_id"]),
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "markets": {"match_result": {"home_win": 0.55, "draw": 0.25, "away_win": 0.20}},
            "confidence": {"label": "Moderate"},
        },
        idempotency_key=f"{row['lifecycle_id']}:{key_suffix}:{row['kickoff_at']}",
        lifecycle_id=row["lifecycle_id"],
        model_inputs={"features": {"elo_diff": 30}},
    )


def _finished_payload():
    return {
        "fixture": {"id": 9001, "date": _iso(-4), "status": {"short": "FT", "long": "Full Time"}},
        "league": {"id": 39, "season": 2026, "name": "Premier League"},
        "teams": {"home": {"id": 42, "name": "Arsenal"}, "away": {"id": 49, "name": "Chelsea"}},
        "goals": {"home": 2, "away": 1},
        "score": {
            "halftime": {"home": 1, "away": 0},
            "fulltime": {"home": 2, "away": 1},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
        "provider_provenance": {"provider": "api_football"},
    }


def _canonical_resolver(monkeypatch):
    canon = {"Arsenal": "arsenal", "Chelsea": "chelsea"}

    def fake_resolve(name=None, provider=None, provider_id=None, league_id=None, raw_name=None, **kwargs):
        return canon.get(raw_name or name)

    monkeypatch.setattr(team_identity, "resolve_canonical_team_id", fake_resolve)


def test_finished_without_live_window_verifies_grades_persists_and_recovers(monkeypatch):
    # A credentialed provider is only routable when its secret is present.
    monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
    row = _seed(stage="PREMATCH_SNAPSHOT_SAVED", kickoff_at=_iso(-4))
    _save_snapshot(row)
    payload = _finished_payload()
    _canonical_resolver(monkeypatch)
    monkeypatch.setattr(live_lifecycle, "_fetch_result", lambda *args, **kwargs: payload)
    # No live payload ever arrives; the match is already past its 3h window.
    monkeypatch.setattr(live_lifecycle, "_live_index", lambda sport, provider: {})

    live_lifecycle.monitor()
    row = lstore.get_lifecycle(row["lifecycle_id"])
    assert row["stage"] == "FINAL_RESULT_PENDING"
    assert lstore.get_observations(row["lifecycle_id"]) == []

    real_save = storage.save_verified_live_result
    monkeypatch.setattr(
        storage, "save_verified_live_result", lambda **kwargs: {"saved": False, "reason": "transient_write"}
    )
    first = live_lifecycle.verify_results()
    row = lstore.get_lifecycle(row["lifecycle_id"])
    assert first["verified"] == 1
    assert row["stage"] == "PREDICTION_GRADED"
    assert row["error_class"] == "historical_persistence_failed"
    graded_after_first = _grading_keys()
    assert graded_after_first, "grading must happen before historical persistence"
    assert storage.get_historical_fixtures(39, 2026) == []

    monkeypatch.setattr(storage, "save_verified_live_result", real_save)
    second = live_lifecycle.verify_results()
    row = lstore.get_lifecycle(row["lifecycle_id"])
    assert second["persisted"] == 1
    assert row["stage"] == "HISTORICAL_RECORD_UPDATED"
    assert row["error_state"] is None
    assert _grading_keys() == graded_after_first, "retries must not duplicate grading rows"
    snapshots = storage.get_prediction_snapshots(fixture_id=9001, sport="football")
    assert len(snapshots) == 1, "retries must not duplicate prediction snapshots"
    history = storage.get_historical_fixtures(39, 2026)
    assert len(history) == 1
    assert history[0]["fixture"]["id"] == 9001
    assert history[0]["goals"] == {"home": 2, "away": 1}


def _grading_keys():
    rows = lstore.list_gradings_for_lifecycle("football:api_football:9001") if hasattr(
        lstore, "list_gradings_for_lifecycle"
    ) else []
    if rows:
        return [row["grading_key"] for row in rows]
    conn, _ = storage._connect()
    try:
        return [r[0] for r in conn.execute("SELECT grading_key FROM prediction_grading_ledger").fetchall()]
    finally:
        conn.close()


def test_telegram_fixture_listing_reads_persisted_lifecycle(monkeypatch):
    row = _seed(stage="TRACKED")
    _seed(
        lifecycle_id="football:api_football:9002",
        canonical_fixture_key="api_football:9002",
        provider_fixture_id="9002",
        stage="DISCOVERED",
        home_team_id=None,
        away_team_id=None,
    )
    _save_snapshot(row)

    import api_football

    def forbidden(*args, **kwargs):
        raise AssertionError("fixture listing must not call a provider")

    monkeypatch.setattr(api_football, "get_fixtures_by_date", forbidden)

    fixtures = telegram_bot.get_tracked_fixtures_for_date(row["kickoff_at"][:10])
    assert len(fixtures) == 1
    assert fixtures[0]["teams"]["home"]["name"] == "Arsenal"
    assert fixtures[0]["lifecycle"]["stage"] == "TRACKED"

    params = telegram_bot.parse_operation_parameters(
        "fixtures", f"fixtures Premier League {row['kickoff_at'][:10]}"
    )
    message = telegram_bot.handle_fixtures_op(params)
    assert "Arsenal vs Chelsea" in message


def test_empty_listing_explains_missing_provider_credential(monkeypatch):
    monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
    message = telegram_bot.handle_fixtures_op(
        {"sport": "football", "date": _iso(0)[:10], "date_label": "today", "quantity": 10}
    )
    assert "Lifecycle diagnostics" in message
    assert "Persisted football fixtures" in message
    assert "API_FOOTBALL_KEY" in message


def test_telegram_prediction_uses_lifecycle_snapshot(monkeypatch):
    row = _seed(stage="PREMATCH_SNAPSHOT_SAVED")
    _save_snapshot(row)
    monkeypatch.setattr(
        live_lifecycle,
        "discover",
        lambda *a, **k: {"discovered": 1, "tracked": 1, "unresolved": 0, "identity_failures": 0, "errors": []},
    )
    monkeypatch.setattr(
        live_lifecycle,
        "prematch",
        lambda *a, **k: {"considered": 1, "processed": 1, "snapshots_saved": 1},
    )

    params = telegram_bot.parse_operation_parameters(
        "predict", f"predict Premier League {row['kickoff_at'][:10]}"
    )
    message = telegram_bot.handle_predict_op(params)
    assert "PREDICTION" in message
    assert "Arsenal vs Chelsea" in message
    assert "Home Win" in message


def test_prediction_failure_reports_lifecycle_diagnostics(monkeypatch):
    _seed(
        lifecycle_id="football:api_football:9003",
        canonical_fixture_key="api_football:9003",
        provider_fixture_id="9003",
        stage="TRACKED",
        # Diagnostics scope by the fixture's UTC calendar date; a fixed +4h
        # offset crosses midnight late in the day and made this test flaky.
        kickoff_at=_iso(0),
    )
    lstore.update_lifecycle(
        "football:api_football:9003",
        error_state="season_team_stats unavailable key=secret_12345",
        error_class="model_error",
    )
    monkeypatch.setattr(
        live_lifecycle,
        "discover",
        lambda *a, **k: {"discovered": 1, "tracked": 1, "unresolved": 0, "identity_failures": 0, "errors": []},
    )
    monkeypatch.setattr(
        live_lifecycle,
        "prematch",
        lambda *a, **k: {"considered": 1, "processed": 1, "snapshots_saved": 0},
    )

    params = telegram_bot.parse_operation_parameters("predict", "predict Premier League today")
    message = telegram_bot.handle_predict_op(params)

    assert "No football predictions could be produced" in message
    assert "Diagnostic Breakdown" in message
    assert "Fixtures Discovered: 1" in message
    assert "Snapshots Saved: 0" in message
    assert "season_team_stats unavailable" in message
    assert "secret_12345" not in message
    assert "REDACTED" in message


def test_registry_only_advertises_wired_capabilities():
    assert provider_registry.supports("espn_soccer", "discovery", "fixtures", 39)
    assert provider_registry.supports("espn_soccer", "prematch", "statistics", 39)
    assert provider_registry.supports("espn_soccer", "final_result", "score", 39)
    assert not provider_registry.supports("espn_soccer", "live", "score", 39)

    assert provider_registry.supports("fotmob", "discovery", "fixtures", 39)
    assert provider_registry.supports("fotmob", "final_result", "score", 39)
    assert not provider_registry.supports("fotmob", "live", "score", 39)

    # ESPN NBA is wired for discovery, pre-match data and final results; in-play
    # polling is still unimplemented, so live must never be routed.
    assert provider_registry.supports("espn_nba", "discovery", "fixtures", 12)
    assert provider_registry.supports("espn_nba", "prematch", "statistics", 12)
    assert provider_registry.supports("espn_nba", "final_result", "score", 12)
    assert not provider_registry.supports("espn_nba", "live", "score", 12)
    assert not provider_registry.supports("football_data_csv", "final_result", "score", 39)


def test_final_result_never_borrows_another_providers_identity(monkeypatch):
    _seed(
        lifecycle_id="football:football_data_csv:9004",
        canonical_fixture_key="football_data_csv:9004",
        provider="football_data_csv",
        provider_fixture_id="9004",
        stage="FINAL_RESULT_PENDING",
        kickoff_at=_iso(-5),
    )

    import api_football

    def forbidden(*args, **kwargs):
        raise AssertionError("historical-only provider must not supply a live result")

    monkeypatch.setattr(api_football, "get_fixture_result", forbidden)
    live_lifecycle.verify_results()

    row = lstore.get_lifecycle("football:football_data_csv:9004")
    assert row["stage"] == "FINAL_RESULT_PENDING"
    assert row["error_class"] == "provider_not_eligible"


def test_provider_native_ids_are_namespaced(monkeypatch):
    _canonical_resolver(monkeypatch)
    base = {
        "sport": "football",
        "league_id": 39,
        "season": 2026,
        "kickoff_at": _iso(6),
        "home_provider_id": 42,
        "away_provider_id": 49,
        "home_name": "Arsenal",
        "away_name": "Chelsea",
        "status": "NS",
    }
    first = live_lifecycle._persist_identity(
        {**base, "provider": "api_football", "provider_fixture_id": "777"}
    )
    second = live_lifecycle._persist_identity({**base, "provider": "fotmob", "provider_fixture_id": "777"})
    assert first["lifecycle_id"] != second["lifecycle_id"]
    assert first["canonical_fixture_key"] == "api_football:777"
    assert second["canonical_fixture_key"] == "fotmob:777"


def test_discovery_queries_every_eligible_provider_and_merges(monkeypatch):
    fallback = {
        "fixture": {"id": 6112357, "date": _iso(5), "status": {"short": "NS"}},
        "league": {"id": 39, "season": 2026, "name": "Premier League"},
        "teams": {"home": {"id": 11, "name": "Arsenal"}, "away": {"id": 12, "name": "Chelsea"}},
        "goals": {"home": None, "away": None},
        "provider_provenance": {"provider": "fotmob", "provider_fixture_id": "6112357"},
    }
    monkeypatch.setattr(live_providers, "fotmob_fixtures", lambda date_str: [fallback])
    espn = {
        "fixture": {"id": 401879999, "date": fallback["fixture"]["date"], "status": {"short": "NS"}},
        "league": fallback["league"],
        "teams": {"home": {"id": 357, "name": "Arsenal"}, "away": {"id": 388, "name": "Chelsea"}},
        "goals": {"home": 0, "away": 0},
        "provider_provenance": {"provider": "espn_soccer", "provider_fixture_id": "401879999"},
    }
    monkeypatch.setattr(live_providers, "espn_soccer_fixtures", lambda date_str: [espn])
    _canonical_resolver(monkeypatch)

    from data_resolver import DataResolver

    monkeypatch.setattr(DataResolver, "get_fixtures_for_date", lambda self, date_str, league_id=None: ([], {}))
    result = live_lifecycle.discover("football", horizon_days=0)
    # Both keyless providers were queried and their observations merged into a
    # single canonical fixture; neither provider was skipped.
    attempted = {a["provider"] for a in result["provider_attempts"]}
    assert {"espn_soccer", "fotmob"} <= attempted
    assert result["provider_counts"] == {"fotmob": 1, "espn_soccer": 1}
    assert result["merged_fixtures"] == 1
    rows = lstore.list_lifecycles(sport="football")
    assert len(rows) == 1
    assert rows[0]["provider"] in ("fotmob", "espn_soccer")
    assert rows[0]["canonical_fixture_key"] in ("fotmob:6112357", "espn_soccer:401879999")


def test_prematch_selection_scopes_sport_date_and_excludes_expired(monkeypatch):
    """Stale global records must not consume a scoped, eligible batch."""
    day = _iso(30)[:10]
    for index in range(250):
        _seed(
            lifecycle_id=f"football:api_football:stale{index}",
            canonical_fixture_key=f"api_football:stale{index}",
            provider_fixture_id=str(7000 + index),
            stage="TRACKED",
            kickoff_at=_iso(-24 - index),
        )
    _seed(
        lifecycle_id="football:api_football:unresolved",
        canonical_fixture_key="api_football:unresolved",
        provider_fixture_id="7100",
        stage="DISCOVERED",
        home_team_id=None,
        away_team_id=None,
        kickoff_at=f"{day}T18:00:00+00:00",
    )
    target = _seed(
        lifecycle_id="football:api_football:target",
        canonical_fixture_key="api_football:target",
        provider_fixture_id="7200",
        stage="TRACKED",
        kickoff_at=f"{day}T20:00:00+00:00",
    )
    basketball = _seed(
        lifecycle_id="basketball:espn_nba:target",
        canonical_fixture_key="espn_nba:5200",
        sport="basketball",
        provider="espn_nba",
        provider_fixture_id="5200",
        league_id=12,
        stage="TRACKED",
        kickoff_at=f"{day}T22:00:00+00:00",
    )
    selected = []

    def fake_prematch(row):
        selected.append(row["lifecycle_id"])
        return {**row, "stage": "PREMATCH_SNAPSHOT_SAVED"}

    monkeypatch.setattr(live_lifecycle, "_prematch_football", fake_prematch)
    monkeypatch.setattr(live_lifecycle, "_prematch_basketball", fake_prematch)

    result = live_lifecycle.prematch(limit=1, sport="football", date_str=day)
    assert selected == ["football:api_football:target"]
    assert target["lifecycle_id"] in selected
    assert basketball["lifecycle_id"] not in selected
    assert result["snapshots_saved"] == 1
    assert result["expired"] >= 250
    assert result["unresolved_skipped"] == 1
    stale = lstore.get_lifecycle("football:api_football:stale0")
    assert stale["error_class"] == "kickoff_passed"
    assert stale["stage"] == "TRACKED", "expiry must preserve lifecycle history"


def test_prematch_basketball_scope_does_not_touch_football(monkeypatch):
    day = _iso(30)[:10]
    _seed(
        lifecycle_id="football:api_football:other",
        canonical_fixture_key="api_football:other",
        provider_fixture_id="7300",
        kickoff_at=f"{day}T18:00:00+00:00",
    )
    _seed(
        lifecycle_id="basketball:espn_nba:chosen",
        canonical_fixture_key="espn_nba:5300",
        sport="basketball",
        provider="espn_nba",
        provider_fixture_id="5300",
        league_id=12,
        kickoff_at=f"{day}T22:00:00+00:00",
    )
    selected = []
    monkeypatch.setattr(
        live_lifecycle,
        "_prematch_basketball",
        lambda row: selected.append(row["lifecycle_id"]) or {**row, "stage": "PREMATCH_SNAPSHOT_SAVED"},
    )

    result = live_lifecycle.prematch(limit=5, sport="basketball", date_str=day, league_id=12)
    assert selected == ["basketball:espn_nba:chosen"]
    assert result["sport"] == "basketball"
    assert result["date"] == day


def test_prematch_exhausted_retry_stops_consuming_batch(monkeypatch):
    day = _iso(30)[:10]
    _seed(
        lifecycle_id="football:api_football:exhausted",
        canonical_fixture_key="api_football:exhausted",
        provider_fixture_id="7400",
        kickoff_at=f"{day}T18:00:00+00:00",
    )
    lstore.update_lifecycle(
        "football:api_football:exhausted",
        retry_count=live_lifecycle.MAX_PREMATCH_RETRIES,
        error_class="model_error",
    )
    called = []
    monkeypatch.setattr(
        live_lifecycle, "_prematch_football", lambda row: called.append(row["lifecycle_id"])
    )
    result = live_lifecycle.prematch(limit=5, sport="football", date_str=day)
    assert called == []
    assert result["retry_exhausted"] == 1


def test_football_snapshot_stores_exact_model_inputs(monkeypatch):
    row = _seed(stage="TRACKED")
    engineered = {
        "home_attack": 1.72,
        "away_defence": 1.11,
        "elo_diff": 31.5,
        "h2h_home_win_rate": 0.5,
    }
    raw_feature_snapshot = {
        "season_home": {"matches": 12},
        "season_away": {"matches": 12},
        "recent_home": {"matches": 5},
        "h2h": {"matches": 6},
    }
    provider_provenance = {"provider": "espn_soccer", "as_of": row["kickoff_at"]}
    prediction = {
        "fixture_id": 9001,
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "markets": {"match_result": {"home_win": 0.51, "draw": 0.26, "away_win": 0.23}},
        "feature_snapshot": raw_feature_snapshot,
        "provenance": {"provider_statistics": provider_provenance},
        "prediction_record": {
            "model_inputs": {
                "features": dict(engineered),
                "elo_probabilities": {"home_win": 0.48, "draw": 0.27, "away_win": 0.25},
                "elo_weight": 0.2,
                "prediction_timestamp": "2026-11-27T10:00:00+00:00",
                "data_cutoff_timestamp": "2026-11-27T10:00:00+00:00",
                "model_version": "v3.0.0",
                "feature_version": "v3.0.0",
            }
        },
    }
    monkeypatch.setattr(live_lifecycle, "_provider_eligible", lambda *a, **k: ("espn_soccer", None))
    monkeypatch.setattr(
        live_lifecycle,
        "_fetch_result",
        lambda *a, **k: {
            "fixture": {"id": 9001, "date": row["kickoff_at"], "status": {"short": "NS"}},
            "league": {"id": 39, "season": 2026},
            "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 2, "name": "Chelsea"}},
        },
    )
    import main as app_main

    monkeypatch.setattr(app_main, "predict_fixture", lambda *a, **k: prediction)
    captured = {}

    def fake_save(**kwargs):
        captured.update(kwargs)
        return 1

    import storage as storage_module

    monkeypatch.setattr(storage_module, "save_prediction_snapshot", fake_save)

    result = live_lifecycle._prematch_football(row)
    assert result["stage"] == "PREMATCH_SNAPSHOT_SAVED"
    assert captured["features"] == engineered
    assert captured["model_inputs"]["features"] == engineered
    assert captured["model_inputs"]["elo_weight"] == 0.2
    assert captured["model_inputs"]["feature_snapshot"] == raw_feature_snapshot
    assert captured["model_inputs"]["feature_provenance"]["provider_statistics"] == provider_provenance
    assert captured["model_inputs"]["provider"] == "api_football"
    assert captured["prediction_context"] == "PRE_MATCH"


def test_football_snapshot_refuses_missing_model_inputs(monkeypatch):
    row = _seed(stage="TRACKED")
    monkeypatch.setattr(live_lifecycle, "_provider_eligible", lambda *a, **k: ("espn_soccer", None))
    monkeypatch.setattr(
        live_lifecycle,
        "_fetch_result",
        lambda *a, **k: {
            "fixture": {"id": 9001, "date": row["kickoff_at"], "status": {"short": "NS"}},
            "league": {"id": 39, "season": 2026},
            "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 2, "name": "Chelsea"}},
        },
    )
    import main as app_main

    monkeypatch.setattr(
        app_main,
        "predict_fixture",
        lambda *a, **k: {"fixture_id": 9001, "prediction_record": {}, "feature_snapshot": {}},
    )
    saved = []
    import storage as storage_module

    monkeypatch.setattr(storage_module, "save_prediction_snapshot", lambda **k: saved.append(k) or 1)

    result = live_lifecycle._prematch_football(row)
    assert result["error_class"] == "model_input_fidelity_error"
    assert saved == []
    updated = lstore.get_lifecycle(row["lifecycle_id"])
    assert updated["stage"] == "TRACKED"


def test_telegram_prematch_is_scoped_to_request(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        live_lifecycle,
        "discover",
        lambda *a, **k: {"discovered": 0, "tracked": 0, "unresolved": 0, "identity_failures": 0, "errors": []},
    )

    def fake_prematch(**kwargs):
        captured.update(kwargs)
        return {"eligible": 0, "processed": 0, "snapshots_saved": 0}

    monkeypatch.setattr(live_lifecycle, "prematch", fake_prematch)
    monkeypatch.setattr(live_lifecycle, "diagnostics", lambda *a, **k: {})
    monkeypatch.setattr(live_lifecycle, "list_tracked", lambda *a, **k: [])

    telegram_bot._lifecycle_predictions("basketball", "2026-11-28", 1, 12)
    assert captured == {
        "limit": 200,
        "sport": "basketball",
        "date_str": "2026-11-28",
        "league_id": 12,
    }


def test_autonomous_prematch_is_not_limited_to_one_sport(monkeypatch):
    day = _iso(30)[:10]
    _seed(
        lifecycle_id="football:api_football:auto",
        canonical_fixture_key="api_football:auto",
        provider_fixture_id="7500",
        kickoff_at=f"{day}T18:00:00+00:00",
    )
    _seed(
        lifecycle_id="basketball:espn_nba:auto",
        canonical_fixture_key="espn_nba:5500",
        sport="basketball",
        provider="espn_nba",
        provider_fixture_id="5500",
        league_id=12,
        kickoff_at=f"{day}T20:00:00+00:00",
    )
    selected = []
    monkeypatch.setattr(
        live_lifecycle,
        "_prematch_football",
        lambda row: selected.append(row["sport"]) or {**row, "stage": "PREMATCH_SNAPSHOT_SAVED"},
    )
    monkeypatch.setattr(
        live_lifecycle,
        "_prematch_basketball",
        lambda row: selected.append(row["sport"]) or {**row, "stage": "PREMATCH_SNAPSHOT_SAVED"},
    )
    result = live_lifecycle.prematch(limit=5)
    assert set(selected) == {"football", "basketball"}
    assert result["sport"] is None


def test_fail_exhausts_bounded_prematch_retries():
    row = _seed(
        lifecycle_id="football:api_football:cap",
        canonical_fixture_key="api_football:cap",
        provider_fixture_id="7600",
    )
    row = lstore.update_lifecycle(row["lifecycle_id"], retry_count=live_lifecycle.MAX_PREMATCH_RETRIES)
    result = live_lifecycle._fail(
        row, RuntimeError("persistent"), "model_error", max_retries=live_lifecycle.MAX_PREMATCH_RETRIES
    )
    assert result["error_class"] == "model_error_exhausted"
    assert result["next_retry_at"] is None


def test_prematch_pages_through_more_than_one_queue_page(monkeypatch):
    """Ineligible rows inside the window must not cap the eligible batch.

    The pre-fix selector read at most one 1000-row page of candidates, so a
    backlog of identity-unresolved rows sorted ahead of the valid fixtures
    silently hid them. Selection now pages until the batch is full or the
    window is exhausted.
    """
    day = _iso(30 * 24)[:10]
    for index in range(1100):
        _seed(
            lifecycle_id=f"football:api_football:noident{index}",
            canonical_fixture_key=f"api_football:noident{index}",
            provider_fixture_id=str(8000 + index),
            home_team_id=None,
            away_team_id=None,
            kickoff_at=f"{day}T06:00:00+00:00",
        )
    targets = []
    for index, hour in enumerate((18, 20)):
        targets.append(
            _seed(
                lifecycle_id=f"football:api_football:valid{index}",
                canonical_fixture_key=f"api_football:valid{index}",
                provider_fixture_id=str(9000 + index),
                kickoff_at=f"{day}T{hour:02d}:00:00+00:00",
            )["lifecycle_id"]
        )
    selected = []
    monkeypatch.setattr(
        live_lifecycle,
        "_prematch_football",
        lambda row: selected.append(row["lifecycle_id"]) or {**row, "stage": "PREMATCH_SNAPSHOT_SAVED"},
    )

    result = live_lifecycle.prematch(limit=5, sport="football", date_str=day)
    assert selected == targets
    assert result["eligible"] == 2
    assert result["snapshots_saved"] == 2
    assert result["scanned_pages"] > 5
    assert result["considered"] >= 1100


def test_kickoff_date_filters_use_utc_calendar_date(monkeypatch):
    """Kickoff filters select the UTC calendar date of the instant."""
    day = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=30)
    prev = day - dt.timedelta(days=1)
    nxt = day + dt.timedelta(days=1)

    def seed(name, kickoff):
        return _seed(
            lifecycle_id=f"football:api_football:{name}",
            canonical_fixture_key=f"api_football:{name}",
            provider_fixture_id=name,
            kickoff_at=kickoff,
        )["lifecycle_id"]

    before_midnight = seed("before", f"{prev}T23:59:59+00:00")
    midnight = seed("midnight", f"{day}T00:00:00+00:00")
    late = seed("late", f"{day}T23:59:59+00:00")
    # Local calendar date is ``day`` but the instant belongs to the previous
    # UTC day: 01:00+03:00 == 22:00Z the day before.
    local_ahead = seed("ahead", f"{day}T01:00:00+03:00")
    # Local calendar date is ``day`` but the instant belongs to the next UTC
    # day: 23:00-03:00 == 02:00Z the day after.
    local_behind = seed("behind", f"{day}T23:00:00-03:00")

    stored = lstore.get_lifecycle(local_ahead)
    assert stored["kickoff_at"] == f"{prev}T22:00:00+00:00", "kickoff must be stored in UTC"
    assert lstore.get_lifecycle(local_behind)["kickoff_at"] == f"{nxt}T02:00:00+00:00"
    assert [r["lifecycle_id"] for r in lstore.list_lifecycles(kickoff_on=prev.isoformat())] == [
        local_ahead,
        before_midnight,
    ]
    assert [r["lifecycle_id"] for r in lstore.list_lifecycles(kickoff_on=day.isoformat())] == [
        midnight,
        late,
    ]
    assert [r["lifecycle_id"] for r in lstore.list_lifecycles(kickoff_on=nxt.isoformat())] == [
        local_behind
    ]

    monkeypatch.setattr(
        live_lifecycle,
        "_prematch_football",
        lambda row: {**row, "stage": "PREMATCH_SNAPSHOT_SAVED"},
    )
    for date, expected in (
        (day.isoformat(), [midnight, late]),
        (prev.isoformat(), [local_ahead, before_midnight]),
        (nxt.isoformat(), [local_behind]),
    ):
        selected = []
        monkeypatch.setattr(
            live_lifecycle,
            "_prematch_football",
            lambda row: selected.append(row["lifecycle_id"]) or {**row, "stage": "PREMATCH_SNAPSHOT_SAVED"},
        )
        live_lifecycle.prematch(limit=10, sport="football", date_str=date)
        assert selected == expected, f"UTC date scoping failed for {date}"


def test_past_kickoff_with_foreign_offset_is_expired_not_selected(monkeypatch):
    """A passed kickoff stops consuming capacity regardless of stored offset."""
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
    local = past.astimezone(dt.timezone(dt.timedelta(hours=-11))).isoformat()
    row = _seed(
        lifecycle_id="football:api_football:passed",
        canonical_fixture_key="api_football:passed",
        provider_fixture_id="9100",
        kickoff_at=local,
    )
    assert row["kickoff_at"] == past.isoformat()
    selected = []
    monkeypatch.setattr(
        live_lifecycle,
        "_prematch_football",
        lambda item: selected.append(item["lifecycle_id"]) or item,
    )
    result = live_lifecycle.prematch(limit=5, sport="football", date_str=past.date().isoformat())
    assert selected == []
    assert result["expired"] == 1
    stored = lstore.get_lifecycle("football:api_football:passed")
    assert stored["error_class"] == "kickoff_passed"
    assert stored["stage"] == "TRACKED"


def test_telegram_scope_passes_utc_date_to_prematch(monkeypatch):
    """The requested Telegram date is a UTC calendar date end to end."""
    day = (dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=3)).isoformat()
    assert telegram_bot.resolve_date(day)[0] == day
    captured = {}
    monkeypatch.setattr(live_lifecycle, "discover", lambda *a, **k: {"discovered": 0})
    monkeypatch.setattr(
        live_lifecycle,
        "prematch",
        lambda **kwargs: captured.update(kwargs) or {"eligible": 0, "processed": 0, "snapshots_saved": 0},
    )
    monkeypatch.setattr(live_lifecycle, "diagnostics", lambda *a, **k: {})
    monkeypatch.setattr(live_lifecycle, "list_tracked", lambda *a, **k: [])

    telegram_bot._lifecycle_predictions("football", day, 1, None)
    assert captured["date_str"] == day
    assert captured["sport"] == "football"
    assert captured["league_id"] is None
