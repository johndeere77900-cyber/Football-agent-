import historical_h2h


def fixture(
    date,
    home_id,
    away_id,
    home_goals,
    away_goals,
    status="FT",
):
    return {
        "fixture": {
            "date": date,
            "status": {
                "short": status,
            },
        },
        "teams": {
            "home": {
                "id": home_id,
            },
            "away": {
                "id": away_id,
            },
        },
        "goals": {
            "home": home_goals,
            "away": away_goals,
        },
    }


def test_h2h_finds_meetings_regardless_of_home_away_order():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            2,
            0,
        ),
        fixture(
            "2025-02-01T15:00:00+00:00",
            2,
            1,
            1,
            1,
        ),
        fixture(
            "2025-03-01T15:00:00+00:00",
            1,
            3,
            4,
            0,
        ),
    ]

    result = historical_h2h.historical_h2h_matches(
        fixtures,
        1,
        2,
        "2025-04-01T15:00:00+00:00",
    )

    assert len(result) == 2


def test_h2h_excludes_future_and_same_cutoff_matches():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            2,
            0,
        ),
        fixture(
            "2025-04-01T15:00:00+00:00",
            1,
            2,
            5,
            0,
        ),
        fixture(
            "2025-05-01T15:00:00+00:00",
            1,
            2,
            9,
            0,
        ),
    ]

    result = historical_h2h.historical_h2h_matches(
        fixtures,
        1,
        2,
        "2025-04-01T15:00:00+00:00",
    )

    assert len(result) == 1
    assert result[0]["fixture"]["date"].startswith(
        "2025-01-01"
    )


def test_h2h_excludes_non_finished_matches():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            2,
            0,
            status="NS",
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            1,
            2,
            1,
            0,
        ),
    ]

    result = historical_h2h.historical_h2h_matches(
        fixtures,
        1,
        2,
        "2025-01-03T15:00:00+00:00",
    )

    assert len(result) == 1


def test_h2h_excludes_missing_goal_results():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            None,
            None,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            1,
            2,
            2,
            1,
        ),
    ]

    result = historical_h2h.historical_h2h_matches(
        fixtures,
        1,
        2,
        "2025-01-03T15:00:00+00:00",
    )

    assert len(result) == 1


def test_h2h_snapshot_uses_most_recent_window():
    fixtures = [
        fixture("2025-01-01T15:00:00+00:00", 1, 2, 1, 0),
        fixture("2025-02-01T15:00:00+00:00", 2, 1, 2, 0),
        fixture("2025-03-01T15:00:00+00:00", 1, 2, 3, 1),
        fixture("2025-04-01T15:00:00+00:00", 2, 1, 0, 2),
    ]

    result = historical_h2h.historical_h2h_snapshot(
        fixtures,
        1,
        2,
        "2025-05-01T15:00:00+00:00",
        window=2,
    )

    assert result["meetings"] == 2
    assert result["source_dates"] == [
        "2025-03-01T15:00:00+00:00",
        "2025-04-01T15:00:00+00:00",
    ]


def test_h2h_snapshot_preserves_requested_home_perspective():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            2,
            1,
            2,
            1,
        ),
        fixture(
            "2025-02-01T15:00:00+00:00",
            1,
            2,
            3,
            0,
        ),
    ]

    result = historical_h2h.historical_h2h_snapshot(
        fixtures,
        1,
        2,
        "2025-03-01T15:00:00+00:00",
        window=2,
    )

    assert result["wins"] == 1
    assert result["draws"] == 0
    assert result["losses"] == 1
    assert result["goals_for"] == 2.0
    assert result["goals_against"] == 1.0


def test_h2h_snapshot_calculates_form_and_goal_rates():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            2,
            1,
        ),
        fixture(
            "2025-02-01T15:00:00+00:00",
            2,
            1,
            0,
            0,
        ),
        fixture(
            "2025-03-01T15:00:00+00:00",
            1,
            2,
            0,
            3,
        ),
        fixture(
            "2025-04-01T15:00:00+00:00",
            2,
            1,
            1,
            2,
        ),
    ]

    result = historical_h2h.historical_h2h_snapshot(
        fixtures,
        1,
        2,
        "2025-05-01T15:00:00+00:00",
        window=4,
    )

    assert result["wins"] == 2
    assert result["draws"] == 1
    assert result["losses"] == 1
    assert result["form_sequence"] == [
        "W",
        "D",
        "L",
        "W",
    ]

    assert result["goals_for"] == 1.75
    assert result["goals_against"] == 1.25

    assert result["btts_rate"] == 0.5
    assert result["over_1_5_rate"] == 0.75
    assert result["over_2_5_rate"] == 0.5
    assert result["over_3_5_rate"] == 0.0


def test_h2h_minimum_history_is_enforced():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            1,
            0,
        ),
    ]

    assert historical_h2h.h2h_minimum_history(
        fixtures,
        1,
        2,
        "2025-02-01T15:00:00+00:00",
        minimum_matches=1,
    ) is True

    assert historical_h2h.h2h_minimum_history(
        fixtures,
        1,
        2,
        "2025-02-01T15:00:00+00:00",
        minimum_matches=2,
    ) is False


def test_h2h_snapshot_returns_none_without_history():
    result = historical_h2h.historical_h2h_snapshot(
        [],
        1,
        2,
        "2025-02-01T15:00:00+00:00",
        window=6,
    )

    assert result is None


def test_h2h_snapshot_returns_none_when_minimum_is_not_met():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            1,
            0,
        ),
    ]

    result = historical_h2h.historical_h2h_snapshot(
        fixtures,
        1,
        2,
        "2025-02-01T15:00:00+00:00",
        window=6,
        minimum_matches=2,
    )

    assert result is None


def test_h2h_snapshot_rejects_invalid_window():
    try:
        historical_h2h.historical_h2h_snapshot(
            [],
            1,
            2,
            "2025-02-01T15:00:00+00:00",
            window=0,
        )
    except ValueError as exc:
        assert "window" in str(exc)
    else:
        raise AssertionError(
            "Expected ValueError"
        )


def test_h2h_snapshot_rejects_negative_minimum_history():
    try:
        historical_h2h.historical_h2h_snapshot(
            [],
            1,
            2,
            "2025-02-01T15:00:00+00:00",
            minimum_matches=-1,
        )
    except ValueError as exc:
        assert "minimum_matches" in str(exc)
    else:
        raise AssertionError(
            "Expected ValueError"
      )
