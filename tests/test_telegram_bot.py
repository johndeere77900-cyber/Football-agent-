import os
import telegram_bot


def test_classify_greeting():
    assert (
        telegram_bot.classify_intent("hello")
        == "GREETING"
    )


def test_classify_prediction_command():
    assert (
        telegram_bot.classify_intent(
            "predict football tomorrow"
        )
        == "COMMAND"
    )


def test_classify_prediction_question():
    assert (
        telegram_bot.classify_intent(
            "what football games are today?"
        )
        == "QUESTION"
    )


def test_classify_unknown_text():
    assert (
        telegram_bot.classify_intent(
            "the weather feels strange"
        )
        == "UNKNOWN"
    )


def test_parse_quantity_does_not_treat_iso_date_as_quantity():
    assert (
        telegram_bot.parse_quantity(
            "predict football 2026-09-28"
        )
        == 1
    )


def test_parse_quantity_reads_explicit_quantity():
    assert (
        telegram_bot.parse_quantity(
            "predict 5 football games"
        )
        == 5
    )


def test_safest_schema_uses_nested_probability():
    prediction = {
        "safest": {
            "label": "Home Win",
            "probability": 0.72,
        }
    }

    safest = prediction["safest"]

    assert safest["label"] == "Home Win"
    assert safest["probability"] == 0.72

    # Regression guard against the old nonexistent key.
    assert "safest_probability" not in prediction


def test_save_football_prediction_persists(monkeypatch):
    calls = []

    monkeypatch.setattr(
        telegram_bot.storage,
        "save_prediction",
        lambda **kwargs: calls.append(kwargs) or True,
    )

    item = {
        "fixture": {
            "fixture": {
                "id": 123,
                "date": "2026-09-28T15:00:00+00:00",
            },
            "league": {
                "name": "Premier League",
            },
            "teams": {
                "home": {
                    "id": 1,
                    "name": "Home FC",
                },
                "away": {
                    "id": 2,
                    "name": "Away FC",
                },
            },
        },
        "prediction": {
            "fixture_id": 123,
            "date": "2026-09-28T15:00:00+00:00",
            "home_team": "Home FC",
            "away_team": "Away FC",
            "league": "Premier League",
            "markets": {
                "match_result": {
                    "home_win": 0.60,
                    "draw": 0.20,
                    "away_win": 0.20,
                }
            },
            "confidence": {
                "label": "High",
                "top_pick": "Home Win",
                "top_probability": 0.60,
            },
            "home_team_id": 1,
            "away_team_id": 2,
            "odds_comparison": None,
            "insufficient_data": False,
        },
    }

    assert telegram_bot._save_football_prediction(item) is True
    assert len(calls) == 1
    assert calls[0]["fixture_id"] == 123
    assert calls[0]["home_team_id"] == 1
    assert calls[0]["away_team_id"] == 2


def test_save_football_prediction_skips_insufficient_data(monkeypatch):
    called = []

    monkeypatch.setattr(
        telegram_bot.storage,
        "save_prediction",
        lambda **kwargs: called.append(kwargs),
    )

    item = {
        "fixture": {},
        "prediction": {
            "insufficient_data": True,
        },
    }

    assert telegram_bot._save_football_prediction(item) is False
    assert called == []


def test_save_basketball_prediction_persists(monkeypatch):
    calls = []

    monkeypatch.setattr(
        telegram_bot.storage,
        "save_basketball_prediction",
        lambda **kwargs: calls.append(kwargs) or True,
    )

    item = {
        "game": {
            "id": 500,
            "date": "2026-09-28T19:00:00+00:00",
        },
        "prediction": {
            "game_id": 500,
            "date": "2026-09-28T19:00:00+00:00",
            "home_team": "Home",
            "away_team": "Away",
            "league": "NBA",
            "markets": {
                "moneyline": {
                    "home_win": 0.65,
                    "away_win": 0.35,
                }
            },
            "confidence": {
                "label": "Moderate",
                "top_pick": "Home Win",
                "top_probability": 0.65,
            },
            "insufficient_data": False,
        },
    }

    assert telegram_bot._save_basketball_prediction(item) is True
    assert len(calls) == 1
    assert calls[0]["game_id"] == 500


def test_save_basketball_prediction_skips_insufficient_data(monkeypatch):
    called = []

    monkeypatch.setattr(
        telegram_bot.storage,
        "save_basketball_prediction",
        lambda **kwargs: called.append(kwargs),
    )

    item = {
        "game": {},
        "prediction": {
            "insufficient_data": True,
        },
    }

    assert telegram_bot._save_basketball_prediction(item) is False
    assert called == []


def test_accuracy_question_uses_accuracy_summary(monkeypatch):
    called = []

    monkeypatch.setattr(
        telegram_bot.storage,
        "accuracy_summary",
        lambda: called.append(True) or {
            "total_graded": 10,
            "overall_accuracy": 0.70,
            "by_confidence": {},
        },
    )

    result = telegram_bot.handle_accuracy_question("what is the accuracy?")
    assert called == [True]
    assert result is not None


def test_count_question_does_not_claim_unrelated_text():
    result = telegram_bot.handle_count_question("tell me something about football")
    assert result is None


def test_schedule_question_returns_none_when_not_handled():
    result = telegram_bot.handle_schedule_question("tell me something unrelated")
    assert result is None


# ============================================================================
# PHASE 5 CONTROL CENTER TESTS
# ============================================================================

def test_resolve_operation_slash_commands():
    ops = [
        ("/predict", "predict"),
        ("/fixtures", "fixtures"),
        ("/football", "football"),
        ("/basketball", "basketball"),
        ("/backtest", "backtest"),
        ("/backtest_status", "backtest_status"),
        ("/evaluate", "evaluate"),
        ("/health", "health"),
        ("/model_status", "model_status"),
        ("/data_status", "data_status"),
        ("/logs", "logs"),
        ("/errors", "errors"),
        ("/retrain", "retrain"),
        ("/config", "config"),
    ]
    for cmd, expected_op in ops:
        op, _ = telegram_bot.resolve_operation(cmd)
        assert op == expected_op, f"Expected {expected_op} for {cmd}, got {op}"


def test_resolve_operation_natural_language():
    assert telegram_bot.resolve_operation("predict Arsenal tomorrow")[0] == "predict"
    assert telegram_bot.resolve_operation("run a Premier League 2024 backtest")[0] == "backtest"
    assert telegram_bot.resolve_operation("show calibration")[0] == "evaluate"
    assert telegram_bot.resolve_operation("what is model status?")[0] == "model_status"
    assert telegram_bot.resolve_operation("system health check")[0] == "health"


def test_unauthorized_chat_rejection(monkeypatch):
    monkeypatch.setattr(telegram_bot, "CHAT_ID", "authorized_123")
    res = telegram_bot.process_telegram_update("hello", chat_id="unauthorized_999")
    assert "Unauthorized" in res


def test_telegram_update_idempotency(monkeypatch):
    monkeypatch.setattr(telegram_bot, "CHAT_ID", "chat_100")
    telegram_bot.storage.init_db()

    # First update
    res1 = telegram_bot.process_telegram_update("/health", update_id="upd_999", chat_id="chat_100")
    assert "CONTROL CENTER HEALTH" in res1

    # Duplicate update
    res2 = telegram_bot.process_telegram_update("/health", update_id="upd_999", chat_id="chat_100")
    assert res2 == res1


def test_durable_operation_job_creation():
    telegram_bot.storage.init_db()

    req_id = "req_test_123"
    telegram_bot.storage.save_operation_request(
        request_id=req_id,
        chat_id="chat_100",
        operation="backtest",
        sport="football",
        parameters={"league_id": 39, "season": 2024},
        status="RUNNING",
    )

    req = telegram_bot.storage.get_operation_request(req_id)
    assert req is not None
    assert req["operation"] == "backtest"
    assert req["status"] == "RUNNING"
    assert req["parameters"]["league_id"] == 39

    telegram_bot.storage.update_operation_request(
        req_id,
        status="COMPLETED",
        result_summary={"accuracy": 0.75},
    )

    req_updated = telegram_bot.storage.get_operation_request(req_id)
    assert req_updated["status"] == "COMPLETED"
    assert req_updated["result_summary"]["accuracy"] == 0.75


def test_format_prediction_contract_telegram():
    prediction = {
        "prediction_record": {
            "sport": "football",
            "fixture_id": 1234,
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "league_name": "Premier League",
            "data_cutoff_timestamp": "2026-10-01T12:00:00+00:00",
            "quality_gate": "SIGNAL",
            "reason_codes": [],
            "model_version": "v3.0.0",
            "feature_version": "v3.0.0",
            "calibration_version": "v3.0.0",
            "calibration_metadata": {"calibration_status": "APPLIED"},
            "calibrated_probabilities": {"match_result": {"home_win": 0.65, "draw": 0.20, "away_win": 0.15}},
            "raw_probabilities": {"match_result": {"home_win": 0.60, "draw": 0.22, "away_win": 0.18}},
            "confidence": {"label": "High", "top_pick": "Home Win"},
            "uncertainty": {"state": "LOW"},
            "edge": 0.08,
            "ev": 0.12,
        },
        "safest": {
            "by_market": {
                "match_result": {"label": "Home Win", "probability": 0.65},
                "double_chance": {"label": "Home or Draw", "probability": 0.85},
            }
        }
    }

    formatted = telegram_bot.format_prediction_contract_telegram(prediction, sport="football")

    assert "SIGNAL" in formatted
    assert "Arsenal vs Chelsea" in formatted
    assert "Premier League" in formatted
    assert "Home Win" in formatted
    assert "Calibrated:" in formatted
    assert "YES" in formatted
    assert "Informational market-scoped picks (non-authoritative):" in formatted
    assert "Double Chance: Home or Draw (85%)" in formatted


def test_config_secret_redaction(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "SECRET_API_KEY_12345")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET_TELEGRAM_TOKEN_999")
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)

    config_output = telegram_bot.handle_config_op()

    assert "SECRET_API_KEY" not in config_output
    assert "SECRET_TELEGRAM_TOKEN" not in config_output
    assert "REDACTED / SECURE" in config_output


def test_health_and_monitoring_ops():
    telegram_bot.storage.init_db()

    health = telegram_bot.handle_health_op()
    assert "PREDICTION CONTROL CENTER HEALTH" in health

    model_st = telegram_bot.handle_model_status_op()
    assert "MODEL & FEATURE STATUS" in model_st
    assert "v3.0.0" in model_st

    data_st = telegram_bot.handle_data_status_op({})
    assert "HISTORICAL DATASET STATUS" in data_st

    retrain = telegram_bot.handle_retrain_op()
    assert "Retraining is not currently available." in retrain

    greeting = telegram_bot.handle_greeting_op()
    assert "PREDICTION AGENT CONTROL CENTER" in greeting


def test_suggested_action_buttons():
    buttons = telegram_bot.get_suggested_action_buttons("predict")
    assert "inline_keyboard" in buttons
    assert len(buttons["inline_keyboard"]) >= 2
    assert buttons["inline_keyboard"][0][0]["callback_data"] == "cmd:fixtures"


def test_model_status_dynamic_calibration_status(monkeypatch):
    monkeypatch.setattr(telegram_bot, "get_dynamic_calibration_status", lambda: "UNAVAILABLE")
    res = telegram_bot.handle_model_status_op()
    assert "Calibration Status:" in res
    assert "UNAVAILABLE" in res

    monkeypatch.setattr(telegram_bot, "get_dynamic_calibration_status", lambda: "APPLIED")
    res2 = telegram_bot.handle_model_status_op()
    assert "Calibration Status:" in res2
    assert "APPLIED" in res2


def test_health_op_configured_data_api(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "test_key")
    res = telegram_bot.handle_health_op()
    assert "• *Football Data API:* CONFIGURED" in res


def test_parse_control_envelope():
    json_envelope = '{"version": "1.0", "request_id": "req_555", "telegram_update_id": "upd_555", "chat_id": "12345", "operation": "health", "text": "/health"}'
    parsed = telegram_bot.parse_control_envelope(json_envelope)
    assert parsed is not None
    assert parsed["request_id"] == "req_555"
    assert parsed["telegram_update_id"] == "upd_555"
    assert parsed["operation"] == "health"

    assert telegram_bot.parse_control_envelope("hello world") is None


def test_control_envelope_processing(monkeypatch):
    monkeypatch.setattr(telegram_bot, "CHAT_ID", "12345")
    telegram_bot.storage.init_db()

    json_envelope = '{"version": "1.0", "request_id": "req_env_01", "telegram_update_id": "upd_env_01", "chat_id": "12345", "operation": "health", "text": "/health"}'

    res = telegram_bot.process_telegram_update(json_envelope)
    assert "PREDICTION CONTROL CENTER HEALTH" in res

    req = telegram_bot.storage.get_operation_request("req_env_01")
    assert req is not None
    assert req["telegram_update_id"] == "upd_env_01"
    assert req["operation"] == "health"
    assert req["status"] == "COMPLETED"


def test_no_cross_market_safest_sorting(monkeypatch):
    monkeypatch.setattr(telegram_bot, "get_tracked_fixtures_for_date", lambda date: [
        {"fixture": {"id": 1, "status": {"short": "NS"}}, "league": {"id": 39, "season": 2024}, "teams": {"home": {"id": 10, "name": "A"}, "away": {"id": 20, "name": "B"}}},
        {"fixture": {"id": 2, "status": {"short": "NS"}}, "league": {"id": 39, "season": 2024}, "teams": {"home": {"id": 30, "name": "C"}, "away": {"id": 40, "name": "D"}}},
    ])
    monkeypatch.setattr(telegram_bot.agent, "get_league_avg_goals", lambda lid, ssn: 2.5)

    def mock_predict(fixture, avg, fetch_odds=False):
        fid = fixture["fixture"]["id"]
        prob = 0.50 if fid == 1 else 0.90
        return {
            "fixture_id": fid,
            "date": "2026-10-01",
            "home_team": "H",
            "away_team": "A",
            "league": "L",
            "markets": {"match_result": {"home_win": prob}},
            "confidence": {"label": "Moderate", "top_pick": "Home Win"},
            "safest": {"label": "Home Win", "probability": prob},
            "insufficient_data": False,
        }

    monkeypatch.setattr(telegram_bot.agent, "predict_fixture", mock_predict)
    monkeypatch.setattr(telegram_bot, "_save_football_prediction", lambda item: True)

    results = telegram_bot.research_football("2026-10-01", quantity=2)
    assert len(results) == 2
    assert results[0]["fixture"]["fixture"]["id"] == 1
    assert results[1]["fixture"]["fixture"]["id"] == 2


def test_unique_telegram_update_id_constraint():
    telegram_bot.storage.init_db()

    # Save first operation request
    telegram_bot.storage.save_operation_request(
        request_id="req_unique_1",
        chat_id="chat_1",
        operation="health",
        telegram_update_id="upd_uniq_100",
    )

    # Save second operation request with SAME telegram_update_id
    telegram_bot.storage.save_operation_request(
        request_id="req_unique_2",
        chat_id="chat_1",
        operation="health",
        telegram_update_id="upd_uniq_100",
    )

    # Re-running init_db triggers migration and unique index enforcement
    telegram_bot.storage.init_db()

    # The lookup by update_id returns the earliest stored request_id
    found = telegram_bot.storage.get_operation_request_by_update_id("upd_uniq_100")
    assert found is not None
    assert found["request_id"] in ("req_unique_1", "req_unique_2")


def test_deterministic_migration_same_timestamp():
    telegram_bot.storage.init_db()

    conn, db_type = telegram_bot.storage._connect()
    try:
        same_ts = "2026-10-01T12:00:00+00:00"
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute("DROP INDEX IF EXISTS idx_operation_requests_update_id_unique")
                cur.execute(
                    """
                    INSERT INTO operation_requests (request_id, telegram_update_id, chat_id, operation, status, created_at)
                    VALUES ('req_dup_a', 'upd_same_ts_100', 'chat_1', 'health', 'COMPLETED', %s),
                           ('req_dup_b', 'upd_same_ts_100', 'chat_1', 'health', 'COMPLETED', %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (same_ts, same_ts)
                )
            conn.commit()
        else:
            conn.execute("DROP INDEX IF EXISTS idx_operation_requests_update_id_unique")
            conn.execute(
                """
                INSERT OR IGNORE INTO operation_requests (request_id, telegram_update_id, chat_id, operation, status, created_at)
                VALUES ('req_dup_a', 'upd_same_ts_100', 'chat_1', 'health', 'COMPLETED', ?),
                       ('req_dup_b', 'upd_same_ts_100', 'chat_1', 'health', 'COMPLETED', ?)
                """,
                (same_ts, same_ts)
            )
            conn.commit()
    finally:
        conn.close()

    telegram_bot.storage.init_db()

    conn2, db_type2 = telegram_bot.storage._connect()
    try:
        if db_type2 == "postgres":
            with conn2.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM operation_requests WHERE telegram_update_id = %s", ("upd_same_ts_100",))
                count = cur.fetchone()[0]
        else:
            count = conn2.execute("SELECT COUNT(*) FROM operation_requests WHERE telegram_update_id = ?", ("upd_same_ts_100",)).fetchone()[0]
        assert count == 1
    finally:
        conn2.close()


def test_db_error_propagation_get_operation_request(monkeypatch):
    def bad_connect():
        import sqlite3
        raise sqlite3.OperationalError("Simulated database connection failure")

    monkeypatch.setattr(telegram_bot.storage, "_connect", bad_connect)

    import sqlite3
    import pytest

    with pytest.raises(sqlite3.OperationalError):
        telegram_bot.storage.get_operation_request("req_123")

    with pytest.raises(sqlite3.OperationalError):
        telegram_bot.storage.get_operation_request_by_update_id("upd_123")


def test_logs_and_errors_chat_scoping():
    telegram_bot.storage.init_db()

    # User A requests
    telegram_bot.storage.save_operation_request(
        request_id="req_user_a_1",
        chat_id="chat_A",
        operation="health",
        status="COMPLETED",
        result_summary={"message": "User A Health Log"},
    )
    telegram_bot.storage.save_operation_request(
        request_id="req_user_a_err",
        chat_id="chat_A",
        operation="backtest",
        status="FAILED",
        error_code="USER_A_FAIL",
        error_message="User A Error Message",
    )

    # User B requests
    telegram_bot.storage.save_operation_request(
        request_id="req_user_b_1",
        chat_id="chat_B",
        operation="health",
        status="COMPLETED",
        result_summary={"message": "User B Secret Log"},
    )
    telegram_bot.storage.save_operation_request(
        request_id="req_user_b_err",
        chat_id="chat_B",
        operation="backtest",
        status="FAILED",
        error_code="USER_B_FAIL",
        error_message="User B Secret Error",
    )

    logs_a = telegram_bot.handle_logs_op("chat_A")
    assert "User A" in logs_a
    assert "User B" not in logs_a

    errors_a = telegram_bot.handle_errors_op("chat_A")
    assert "User A Error Message" in errors_a
    assert "User B Secret Error" not in errors_a


def test_backtest_status_exact_request_id_lookup():
    telegram_bot.storage.init_db()

    # Save target request
    telegram_bot.storage.save_operation_request(
        request_id="req_bt_target_999",
        chat_id="chat_1",
        operation="backtest",
        sport="basketball",
        parameters={"league_id": 12, "season": 2024},
        status="COMPLETED",
        result_summary={"accuracy": 0.88, "correct": 88, "graded": 100},
    )

    # Save later request
    telegram_bot.storage.save_operation_request(
        request_id="req_bt_newer_000",
        chat_id="chat_1",
        operation="backtest",
        sport="football",
        parameters={"league_id": 39, "season": 2024},
        status="FAILED",
        error_message="Quota exceeded",
    )

    # Exact request lookup
    res_exact = telegram_bot.handle_backtest_status_op({"request_id": "req_bt_target_999"})
    assert "req_bt_target_999" in res_exact
    assert "Basketball" in res_exact
    assert "88.0%" in res_exact

    # Non-existent request lookup
    res_missing = telegram_bot.handle_backtest_status_op({"request_id": "req_nonexistent_000"})
    assert "was not found" in res_missing

    # Fallback to latest when no ID given
    res_latest = telegram_bot.handle_backtest_status_op({})
    assert "req_bt_newer_000" in res_latest
