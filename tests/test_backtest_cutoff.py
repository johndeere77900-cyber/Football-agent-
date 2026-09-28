import backtest


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


def test_same_day_fixture_before_cutoff_is_included():
    fixtures = [
        fixture(
            "2025-01-05T10:00:00+00:00",
            1,
            2,
            4,
            0,
        ),
        fixture(
            "2025-01-05T15:00:00+00:00",
            1,
            3,
            2,
            0,
        ),
    ]

    result = backtest._compute_stats_as_of(
        fixtures,
        1,
        "2025-01-05T15:00:00+00:00",
    )

    assert result == (4.0, 0.0)


def test_same_timestamp_fixture_is_excluded():
    fixtures = [
        fixture(
            "2025-01-05T15:00:00+00:00",
            1,
            2,
            4,
            0,
        ),
    ]

    result = backtest._compute_stats_as_of(
        fixtures,
        1,
        "2025-01-05T15:00:00+00:00",
    )

    assert result == (None, None)


def test_future_same_day_fixture_is_excluded():
    fixtures = [
        fixture(
            "2025-01-05T16:00:00+00:00",
            1,
            2,
            4,
            0,
        ),
    ]

    result = backtest._compute_stats_as_of(
        fixtures,
        1,
        "2025-01-05T15:00:00+00:00",
    )

    assert result == (None, None)


def test_earlier_days_remain_included():
    fixtures = [
        fixture(
            "2025-01-04T20:00:00+00:00",
            1,
            2,
            2,
            1,
        ),
        fixture(
            "2025-01-05T15:00:00+00:00",
            1,
            3,
            5,
            0,
        ),
    ]

    result = backtest._compute_stats_as_of(
        fixtures,
        1,
        "2025-01-05T15:00:00+00:00",
    )

    assert result == (2.0, 1.0)


def test_non_finished_same_day_fixture_is_excluded():
    fixtures = [
        fixture(
            "2025-01-05T10:00:00+00:00",
            1,
            2,
            4,
            0,
            status="NS",
        ),
    ]

    result = backtest._compute_stats_as_of(
        fixtures,
        1,
        "2025-01-05T15:00:00+00:00",
    )

    assert result == (None, None)


def test_missing_goals_do_not_count_as_history():
    fixtures = [
        fixture(
            "2025-01-05T10:00:00+00:00",
            1,
            2,
            None,
            None,
        ),
    ]

    result = backtest._compute_stats_as_of(
        fixtures,
        1,
        "2025-01-05T15:00:00+00:00",
    )

    assert result == (None, None)
