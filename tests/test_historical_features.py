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


def test_only_completed_matches_before_cutoff_are_used():
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
            1,
            3,
            1,
            1,
            status="NS",
        ),
        fixture(
            "2025-01-03T15:00:00+00:00",
            1,
            4,
            4,
            0,
        ),
    ]

    result = historical_features.prior_completed_fixtures(
        fixtures,
        "2025-01-03T15:00:00+00:00",
    )

    assert len(result) == 1
    assert result[0]["fixture"]["date"].startswith("2025-01-01")


def test_same_cutoff_timestamp_is_excluded():
    fixtures = [
        fixture(
            "2025-01-03T15:00:00+00:00",
            1,
            2,
            2,
            0,
        ),
    ]

    result = historical_features.prior_completed_fixtures(
        fixtures,
        "2025-01-03T15:00:00+00:00",
    )

    assert result == []


def test_future_matches_are_excluded():
    fixtures = [
        fixture(
            "2025-01-04T15:00:00+00:00",
            1,
            2,
            5,
            0,
        ),
    ]

    result = historical_features.team_match_history(
        fixtures,
        1,
        "2025-01-03T15:00:00+00:00",
    )

    assert result == []


def test_team_goal_averages_are_calculated_from_prior_matches():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            2,
            1,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            3,
            1,
            0,
            4,
        ),
    ]

    result = historical_features.team_goal_averages(
        fixtures,
        1,
        "2025-01-03T15:00:00+00:00",
    )

    assert result["matches"] == 2
    assert result["goals_for"] == 3.0
    assert result["goals_against"] == 0.5


def test_team_home_and_away_matches_are_oriented_correctly():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            3,
            1,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            3,
            1,
            2,
            0,
        ),
    ]

    result = historical_features.team_goal_averages(
        fixtures,
        1,
        "2025-01-03T15:00:00+00:00",
    )

    assert result["goals_for"] == 1.5
    assert result["goals_against"] == 1.5


def test_missing_history_returns_none():
    fixtures = []

    result = historical_features.team_goal_averages(
        fixtures,
        1,
        "2025-01-03T15:00:00+00:00",
    )

    assert result is None


def test_minimum_history_is_checked_per_team():
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
            1,
            3,
            2,
            0,
        ),
        fixture(
            "2025-01-03T15:00:00+00:00",
            4,
            1,
            0,
            1,
        ),
    ]

    assert historical_features.fixture_has_minimum_history(
        fixtures,
        1,
        2,
        "2025-01-04T15:00:00+00:00",
        minimum_matches=2,
    ) is False


def test_minimum_history_passes_when_both_teams_have_enough_matches():
    fixtures = [
        fixture("2025-01-01T15:00:00+00:00", 1, 2, 1, 0),
        fixture("2025-01-02T15:00:00+00:00", 1, 3, 2, 0),
        fixture("2025-01-03T15:00:00+00:00", 2, 4, 1, 1),
        fixture("2025-01-04T15:00:00+00:00", 2, 5, 0, 2),
    ]

    assert historical_features.fixture_has_minimum_history(
        fixtures,
        1,
        2,
        "2025-01-05T15:00:00+00:00",
        minimum_matches=2,
    ) is True


def test_snapshot_contains_only_as_of_features():
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
            1,
            1,
            1,
        ),
        fixture(
            "2025-01-04T15:00:00+00:00",
            1,
            2,
            10,
            0,
        ),
    ]

    result = historical_features.historical_feature_snapshot(
        fixtures,
        1,
        2,
        "2025-01-03T15:00:00+00:00",
        minimum_matches=1,
    )

    assert result is not None
    assert result["home"]["matches"] == 2
    assert result["home"]["goals_for"] == 1.5
    assert result["home"]["goals_against"] == 0.5


def test_snapshot_returns_none_when_minimum_history_is_not_met():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            1,
            0,
        ),
    ]

    result = historical_features.historical_feature_snapshot(
        fixtures,
        1,
        2,
        "2025-01-02T15:00:00+00:00",
        minimum_matches=2,
    )

    assert result is None
