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
                "name": f"Team {home_id}",
            },
            "away": {
                "id": away_id,
                "name": f"Team {away_id}",
            },
        },
        "goals": {
            "home": home_goals,
            "away": away_goals,
        },
    }


def test_candidate_requires_minimum_history_for_both_teams():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            3,
            1,
            0,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            1,
            4,
            2,
            0,
        ),
        fixture(
            "2025-01-03T15:00:00+00:00",
            2,
            3,
            1,
            1,
        ),
        fixture(
            "2025-01-04T15:00:00+00:00",
            2,
            4,
            0,
            1,
        ),
        fixture(
            "2025-01-05T15:00:00+00:00",
            1,
            2,
            2,
            1,
        ),
    ]

    finished = fixtures.copy()

    result = backtest._filter_candidates_by_minimum_history(
        finished,
        fixtures,
        minimum_matches=2,
    )

    assert len(result) == 1

    assert result[0]["fixture"]["date"] == (
        "2025-01-05T15:00:00+00:00"
    )


def test_candidate_fixture_is_not_counted_as_prior_history():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            1,
            0,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            3,
            1,
            0,
            1,
        ),
        fixture(
            "2025-01-03T15:00:00+00:00",
            2,
            4,
            1,
            1,
        ),
        fixture(
            "2025-01-04T15:00:00+00:00",
            2,
            1,
            2,
            0,
        ),
    ]

    result = backtest._filter_candidates_by_minimum_history(
        fixtures,
        fixtures,
        minimum_matches=2,
    )

    assert result == []


def test_future_matches_do_not_count_toward_minimum_history():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            3,
            1,
            0,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            1,
            4,
            2,
            0,
        ),
        fixture(
            "2025-01-03T15:00:00+00:00",
            2,
            3,
            1,
            1,
        ),
        fixture(
            "2025-01-05T15:00:00+00:00",
            1,
            2,
            5,
            0,
        ),
    ]

    result = backtest._filter_candidates_by_minimum_history(
        fixtures,
        fixtures,
        minimum_matches=3,
    )

    assert result == []


def test_invalid_goal_match_does_not_count_as_history():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            3,
            1,
            0,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            1,
            4,
            None,
            None,
        ),
        fixture(
            "2025-01-03T15:00:00+00:00",
            2,
            3,
            1,
            1,
        ),
        fixture(
            "2025-01-04T15:00:00+00:00",
            2,
            4,
            0,
            1,
        ),
        fixture(
            "2025-01-05T15:00:00+00:00",
            1,
            2,
            2,
            1,
        ),
    ]

    result = backtest._filter_candidates_by_minimum_history(
        fixtures,
        fixtures,
        minimum_matches=2,
    )

    assert result == []


def test_non_finished_matches_do_not_count_as_history():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            3,
            1,
            0,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            1,
            4,
            2,
            0,
            status="NS",
        ),
        fixture(
            "2025-01-03T15:00:00+00:00",
            2,
            3,
            1,
            1,
        ),
        fixture(
            "2025-01-04T15:00:00+00:00",
            2,
            4,
            0,
            1,
        ),
        fixture(
            "2025-01-05T15:00:00+00:00",
            1,
            2,
            2,
            1,
        ),
    ]

    result = backtest._filter_candidates_by_minimum_history(
        fixtures,
        fixtures,
        minimum_matches=2,
    )

    assert result == []


def test_minimum_history_zero_allows_valid_finished_candidates():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            1,
            0,
        ),
    ]

    result = backtest._filter_candidates_by_minimum_history(
        fixtures,
        fixtures,
        minimum_matches=0,
    )

    assert len(result) == 1


def test_minimum_history_rejects_negative_values():
    try:
        backtest._filter_candidates_by_minimum_history(
            [],
            [],
            minimum_matches=-1,
        )
    except ValueError as exc:
        assert "non-negative integer" in str(exc)
    else:
        raise AssertionError(
            "Expected ValueError"
        )


def test_minimum_history_rejects_boolean_values():
    try:
        backtest._filter_candidates_by_minimum_history(
            [],
            [],
            minimum_matches=True,
        )
    except ValueError as exc:
        assert "non-negative integer" in str(exc)
    else:
        raise AssertionError(
            "Expected ValueError"
        )


def test_minimum_history_preserves_chronological_candidate_order():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            3,
            1,
            0,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            2,
            4,
            1,
            0,
        ),
        fixture(
            "2025-01-03T15:00:00+00:00",
            1,
            4,
            2,
            0,
        ),
        fixture(
            "2025-01-04T15:00:00+00:00",
            2,
            3,
            1,
            1,
        ),
        fixture(
            "2025-01-05T15:00:00+00:00",
            1,
            2,
            2,
            1,
        ),
        fixture(
            "2025-01-06T15:00:00+00:00",
            3,
            4,
            0,
            1,
        ),
        fixture(
            "2025-01-07T15:00:00+00:00",
            1,
            2,
            1,
            1,
        ),
    ]

    unordered_finished = [
        fixtures[6],
        fixtures[4],
        fixtures[5],
        fixtures[2],
        fixtures[3],
        fixtures[0],
        fixtures[1],
    ]

    result = backtest._filter_candidates_by_minimum_history(
        unordered_finished,
        fixtures,
        minimum_matches=2,
    )

    dates = [
        item["fixture"]["date"]
        for item in result
    ]

    assert dates == [
        "2025-01-05T15:00:00+00:00",
        "2025-01-06T15:00:00+00:00",
        "2025-01-07T15:00:00+00:00",
  ]
