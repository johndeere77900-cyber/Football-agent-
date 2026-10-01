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
