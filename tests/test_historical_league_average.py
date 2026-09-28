import historical_features


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


def test_historical_league_average_uses_per_team_match_average():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            2,
            0,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            3,
            4,
            1,
            3,
        ),
    ]

    result = historical_features.historical_league_avg_goals(
        fixtures,
        "2025-01-03T15:00:00+00:00",
    )

    # Total goals = 6.
    # Two matches = four team-match observations.
    # 6 / 4 = 1.5.
    assert result == 1.5


def test_same_cutoff_fixture_is_excluded():
    fixtures = [
        fixture(
            "2025-01-03T15:00:00+00:00",
            1,
            2,
            10,
            0,
        ),
    ]

    result = historical_features.historical_league_avg_goals(
        fixtures,
        "2025-01-03T15:00:00+00:00",
    )

    assert result is None


def test_future_fixtures_are_excluded():
    fixtures = [
        fixture(
            "2025-01-04T15:00:00+00:00",
            1,
            2,
            10,
            0,
        ),
    ]

    result = historical_features.historical_league_avg_goals(
        fixtures,
        "2025-01-03T15:00:00+00:00",
    )

    assert result is None


def test_same_day_fixture_before_cutoff_is_included():
    fixtures = [
        fixture(
            "2025-01-03T10:00:00+00:00",
            1,
            2,
            4,
            0,
        ),
        fixture(
            "2025-01-03T15:00:00+00:00",
            3,
            4,
            2,
            0,
        ),
    ]

    result = historical_features.historical_league_avg_goals(
        fixtures,
        "2025-01-03T15:00:00+00:00",
    )

    assert result == 2.0


def test_unfinished_fixture_is_excluded():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            4,
            0,
            status="NS",
        ),
    ]

    result = historical_features.historical_league_avg_goals(
        fixtures,
        "2025-01-03T15:00:00+00:00",
    )

    assert result is None


def test_missing_goals_are_excluded():
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
            3,
            4,
            2,
            2,
        ),
    ]

    result = historical_features.historical_league_avg_goals(
        fixtures,
        "2025-01-03T15:00:00+00:00",
    )

    assert result == 2.0


def test_invalid_team_ids_are_excluded():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            None,
            2,
            4,
            0,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            3,
            4,
            2,
            2,
        ),
    ]

    result = historical_features.historical_league_avg_goals(
        fixtures,
        "2025-01-03T15:00:00+00:00",
    )

    assert result == 2.0


def test_later_results_do_not_change_earlier_historical_average():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            2,
            0,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            3,
            4,
            2,
            2,
        ),
        fixture(
            "2025-01-04T15:00:00+00:00",
            1,
            2,
            20,
            0,
        ),
    ]

    result = historical_features.historical_league_avg_goals(
        fixtures,
        "2025-01-03T15:00:00+00:00",
    )

    assert result == 1.5
