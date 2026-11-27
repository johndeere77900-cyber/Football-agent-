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
        kickoff_at=_iso(4),
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

    # No adapter is wired for ESPN NBA, so it must not be routed.
    assert not provider_registry.supports("espn_nba", "discovery", "fixtures", 12)
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


def test_discovery_falls_back_only_when_primary_is_empty(monkeypatch):
    fallback = {
        "fixture": {"id": 6112357, "date": _iso(5), "status": {"short": "NS"}},
        "league": {"id": 39, "season": 2026, "name": "Premier League"},
        "teams": {"home": {"id": 11, "name": "Arsenal"}, "away": {"id": 12, "name": "Chelsea"}},
        "goals": {"home": None, "away": None},
        "provider_provenance": {"provider": "fotmob", "provider_fixture_id": "6112357"},
    }
    monkeypatch.setattr(live_providers, "fotmob_fixtures", lambda date_str: [fallback])
    monkeypatch.setattr(live_providers, "espn_soccer_fixtures", lambda date_str: [])
    _canonical_resolver(monkeypatch)

    from data_resolver import DataResolver

    monkeypatch.setattr(DataResolver, "get_fixtures_for_date", lambda self, date_str, league_id=None: ([], {}))
    result = live_lifecycle.discover("football", horizon_days=0)
    assert result["provider_counts"] == {"fotmob": 1}
    rows = lstore.list_lifecycles(sport="football")
    assert len(rows) == 1
    assert rows[0]["provider"] == "fotmob"
    assert rows[0]["canonical_fixture_key"] == "fotmob:6112357"
