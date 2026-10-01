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
            "by_market": {
                "match_result": {
                    "label": "Home Win",
                    "probability": 0.72,
                }
            }
        }
    }

    safest = prediction["safest"]

    assert safest["by_market"]["match_result"]["label"] == "Home Win"
    assert safest["by_market"]["match_result"]["probability"] == 0.72

    # Regression guard against the old nonexistent key.
    assert "safest_probability" not in prediction


def test_save_football_prediction_persists(
    monkeypatch,
):
    calls = []

    monkeypatch.setattr(
        telegram_bot.storage,
        "save_prediction",
        lambda **kwargs: (
            calls.append(kwargs)
            or True
        ),
    )

    item = {
        "fixture": {
            "fixture": {
                "id": 123,
                "date": (
                    "2026-09-28T15:00:00+00:00"
                ),
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
            "date": (
                "2026-09-28T15:00:00+00:00"
            ),
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

    assert (
        telegram_bot._save_football_prediction(
            item
        )
        is True
    )

    assert len(calls) == 1
    assert calls[0]["fixture_id"] == 123
    assert calls[0]["home_team_id"] == 1
    assert calls[0]["away_team_id"] == 2


def test_save_football_prediction_skips_insufficient_data(
    monkeypatch,
):
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

    assert (
        telegram_bot._save_football_prediction(
            item
        )
        is False
    )

    assert called == []


def test_save_basketball_prediction_persists(
    monkeypatch,
):
    calls = []

    monkeypatch.setattr(
        telegram_bot.storage,
        "save_basketball_prediction",
        lambda **kwargs: (
            calls.append(kwargs)
            or True
        ),
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

    assert (
        telegram_bot._save_basketball_prediction(
            item
        )
        is True
    )

    assert len(calls) == 1
    assert calls[0]["game_id"] == 500


def test_save_basketball_prediction_skips_insufficient_data(
    monkeypatch,
):
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

    assert (
        telegram_bot._save_basketball_prediction(
            item
        )
        is False
    )

    assert called == []


def test_accuracy_question_uses_accuracy_summary(
    monkeypatch,
):
    called = []

    monkeypatch.setattr(
        telegram_bot.storage,
        "accuracy_summary",
        lambda: (
            called.append(True)
            or {
                "total_graded": 10,
                "overall_accuracy": 0.70,
                "by_confidence": {},
            }
        ),
    )

    result = (
        telegram_bot.handle_accuracy_question(
            "what is the accuracy?"
        )
    )

    assert called == [True]
    assert result is not None


def test_count_question_does_not_claim_unrelated_text():
    result = (
        telegram_bot.handle_count_question(
            "tell me something about football"
        )
    )

    assert result is None


def test_schedule_question_returns_none_when_not_handled():
    result = (
        telegram_bot.handle_schedule_question(
            "tell me something unrelated"
        )
    )

    assert result is None
