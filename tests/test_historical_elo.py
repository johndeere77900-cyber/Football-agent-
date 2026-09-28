import historical_elo
from elo import DEFAULT_RATING, update_ratings


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


def test_prior_elo_fixtures_are_strictly_before_cutoff():
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
            0,
        ),
        fixture(
            "2025-01-03T15:00:00+00:00",
            1,
            4,
            5,
            0,
        ),
    ]

    result = historical_elo.prior_elo_fixtures(
        fixtures,
        "2025-01-03T15:00:00+00:00",
    )

    assert len(result) == 2
    assert result[0]["fixture"]["date"].startswith(
        "2025-01-01"
    )
    assert result[1]["fixture"]["date"].startswith(
        "2025-01-02"
    )


def test_future_and_same_cutoff_matches_do_not_affect_rating():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            3,
            0,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            1,
            2,
            0,
            5,
        ),
        fixture(
            "2025-01-03T15:00:00+00:00",
            1,
            2,
            10,
            0,
        ),
    ]

    cutoff = "2025-01-03T15:00:00+00:00"

    expected_home, expected_away = update_ratings(
        DEFAULT_RATING,
        DEFAULT_RATING,
        3,
        0,
    )

    result = historical_elo.fixture_elo_snapshot(
        fixtures,
        1,
        2,
        cutoff,
    )

    assert result["home_rating"] == expected_home
    assert result["away_rating"] == expected_away


def test_ratings_are_reconstructed_in_chronological_order():
    fixtures = [
        fixture(
            "2025-01-02T15:00:00+00:00",
            1,
            2,
            0,
            2,
        ),
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            3,
            0,
        ),
    ]

    expected_home, expected_away = update_ratings(
        DEFAULT_RATING,
        DEFAULT_RATING,
        3,
        0,
    )

    expected_home, expected_away = update_ratings(
        expected_home,
        expected_away,
        0,
        2,
    )

    result = historical_elo.reconstruct_ratings(
        fixtures,
        "2025-01-03T15:00:00+00:00",
    )

    assert result[1] == expected_home
    assert result[2] == expected_away


def test_new_team_starts_from_default_rating():
    result = historical_elo.team_rating_as_of(
        [],
        99,
        "2025-01-03T15:00:00+00:00",
    )

    assert result == DEFAULT_RATING


def test_home_and_away_ratings_are_updated_after_match():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            2,
            0,
        ),
    ]

    result = historical_elo.fixture_elo_snapshot(
        fixtures,
        1,
        2,
        "2025-01-02T15:00:00+00:00",
    )

    assert result["home_rating"] > DEFAULT_RATING
    assert result["away_rating"] < DEFAULT_RATING


def test_missing_goals_are_not_treated_as_zero_zero():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            None,
            None,
        ),
    ]

    result = historical_elo.reconstruct_ratings(
        fixtures,
        "2025-01-02T15:00:00+00:00",
    )

    assert result == {}


def test_non_finished_matches_are_not_used():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            2,
            5,
            0,
            status="NS",
        ),
    ]

    result = historical_elo.reconstruct_ratings(
        fixtures,
        "2025-01-02T15:00:00+00:00",
    )

    assert result == {}


def test_unrelated_matches_do_not_change_requested_team_rating():
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
            5,
            0,
        ),
    ]

    result = historical_elo.team_rating_as_of(
        fixtures,
        1,
        "2025-01-03T15:00:00+00:00",
    )

    expected_home, _ = update_ratings(
        DEFAULT_RATING,
        DEFAULT_RATING,
        2,
        0,
    )

    assert result == expected_home


def test_fixture_snapshot_returns_both_requested_team_ratings():
    fixtures = [
        fixture(
            "2025-01-01T15:00:00+00:00",
            1,
            3,
            2,
            0,
        ),
        fixture(
            "2025-01-02T15:00:00+00:00",
            4,
            2,
            0,
            1,
        ),
    ]

    result = historical_elo.fixture_elo_snapshot(
        fixtures,
        1,
        2,
        "2025-01-03T15:00:00+00:00",
    )

    assert result["home_team_id"] == 1
    assert result["away_team_id"] == 2
    assert result["home_rating"] > DEFAULT_RATING
    assert result["away_rating"] > DEFAULT_RATING


def test_invalid_initial_rating_is_rejected():
    try:
        historical_elo.reconstruct_ratings(
            [],
            "2025-01-03T15:00:00+00:00",
            initial_rating=0,
        )
    except ValueError as exc:
        assert "initial_rating" in str(exc)
    else:
        raise AssertionError(
            "Expected ValueError"
)
