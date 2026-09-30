"""
B2 Unit tests for storage engine, bot memory, prediction_context, and past-date integrity.
"""

import pytest
import storage
import main


def test_bot_memory_capping():
    storage.init_db()

    # Append 55 messages for chat_id 777
    for i in range(55):
        storage.append_bot_message(chat_id=777, role="user", text=f"Message {i}")

    messages = storage.get_bot_messages(chat_id=777, limit=100)

    # Must be capped at max 50 recent
    assert len(messages) == 50
    # Must be chronologically sorted (last message is "Message 54")
    assert messages[-1]["text"] == "Message 54"
    assert messages[0]["text"] == "Message 5"


def test_prediction_context_recording():
    storage.init_db()

    import random
    fid1 = random.randint(100000, 999999)
    fid2 = random.randint(100000, 999999)

    # Save pre-match prediction
    inserted_pre = storage.save_prediction(
        fixture_id=fid1,
        match_date="2026-10-10",
        home_team="Home Pre",
        away_team="Away Pre",
        league="Premier League",
        markets={"match_result": {"home_win": 0.5}},
        confidence={"label": "High", "top_pick": "home_win", "top_probability": 0.5},
        prediction_context="PRE_MATCH",
    )
    assert inserted_pre is True

    # Save live prediction
    inserted_live = storage.save_prediction(
        fixture_id=fid2,
        match_date="2026-10-10",
        home_team="Home Live",
        away_team="Away Live",
        league="Premier League",
        markets={"match_result": {"home_win": 0.6}},
        confidence={"label": "High", "top_pick": "home_win", "top_probability": 0.6},
        prediction_context="LIVE",
    )
    assert inserted_live is True

    # Check database rows
    conn, is_pg = storage._connect()
    try:
        sql = storage._q("SELECT prediction_context FROM predictions WHERE fixture_id = ?", is_pg)
        if is_pg:
            with conn.cursor() as cur:
                cur.execute(sql, (fid1,))
                ctx_pre = cur.fetchone()[0]
                cur.execute(sql, (fid2,))
                ctx_live = cur.fetchone()[0]
        else:
            ctx_pre = conn.execute(sql, (fid1,)).fetchone()[0]
            ctx_live = conn.execute(sql, (fid2,)).fetchone()[0]

        assert ctx_pre == "PRE_MATCH"
        assert ctx_live == "LIVE"
    finally:
        conn.close()


def test_past_date_integrity_rejection(capsys):
    # Historical date in the past
    past_date = "2020-01-01"

    main.run_daily(date_str=past_date)

    captured = capsys.readouterr()
    assert "Unsupported historical prediction date '2020-01-01'" in captured.out
