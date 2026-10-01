"""
Chronological, leakage-safe historical Elo ratings for football.

Ratings are reconstructed strictly in chronological order.

For any prediction cutoff:
- only completed fixtures before the cutoff are processed;
- the prediction fixture itself is never processed before its rating is read;
- future fixtures cannot influence historical ratings;
- team ratings start from a deterministic default rating.

This module is API-free and deterministic.
"""

from elo import DEFAULT_RATING, update_ratings


def _fixture_date(fixture):
    """Return the fixture kickoff timestamp."""
    return fixture.get("fixture", {}).get("date", "")


from datetime import datetime, timezone
import historical_match_policy
import time_utils


def _is_finished(fixture):
    """Return True only for completed fixtures."""
    return historical_match_policy.is_finished_match(fixture, sport="football")


def _is_before_cutoff(fixture, cutoff):
    """Return True only when the fixture is strictly before cutoff."""
    return time_utils.is_strictly_before(_fixture_date(fixture), cutoff)


def _valid_team_ids(fixture):
    """Return home and away team IDs when both are present."""
    home_id = fixture.get("teams", {}).get("home", {}).get("id")
    away_id = fixture.get("teams", {}).get("away", {}).get("id")

    if home_id is None or away_id is None:
        return None

    return home_id, away_id


def _valid_goals(fixture):
    """Return final goals when both are numeric."""
    return historical_match_policy.get_football_match_goals(fixture)


def prior_elo_fixtures(fixtures, cutoff):
    """
    Return valid completed fixtures strictly before cutoff.

    Fixtures are sorted chronologically. Python's stable sort preserves
    the original order when timestamps are identical.
    """
    eligible = []

    for fixture in fixtures:
        if not _is_finished(fixture):
            continue

        if not _is_before_cutoff(fixture, cutoff):
            continue

        if _valid_team_ids(fixture) is None:
            continue

        if _valid_goals(fixture) is None:
            continue

        eligible.append(fixture)

    return sorted(
        eligible,
        key=lambda f: time_utils.parse_utc_datetime(_fixture_date(f)) or datetime.min.replace(tzinfo=timezone.utc),
    )


def reconstruct_ratings(
    fixtures,
    cutoff,
    initial_rating=DEFAULT_RATING,
):
    """
    Reconstruct every team's Elo rating as of cutoff.

    The returned ratings include only matches strictly before cutoff.

    Returns:
        {
            team_id: rating
        }
    """
    if initial_rating <= 0:
        raise ValueError(
            "initial_rating must be positive."
        )

    ratings = {}

    for fixture in prior_elo_fixtures(
        fixtures,
        cutoff,
    ):
        home_id, away_id = _valid_team_ids(fixture)
        home_goals, away_goals = _valid_goals(fixture)

        home_rating = ratings.get(
            home_id,
            initial_rating,
        )
        away_rating = ratings.get(
            away_id,
            initial_rating,
        )

        new_home_rating, new_away_rating = update_ratings(
            home_rating,
            away_rating,
            home_goals,
            away_goals,
        )

        ratings[home_id] = new_home_rating
        ratings[away_id] = new_away_rating

    return ratings


def team_rating_as_of(
    fixtures,
    team_id,
    cutoff,
    initial_rating=DEFAULT_RATING,
):
    """
    Return one team's Elo rating as of cutoff.

    If the team has no qualifying prior match, its deterministic initial
    rating is returned.
    """
    if initial_rating <= 0:
        raise ValueError(
            "initial_rating must be positive."
        )

    ratings = reconstruct_ratings(
        fixtures,
        cutoff,
        initial_rating=initial_rating,
    )

    return ratings.get(
        team_id,
        initial_rating,
    )


def fixture_elo_snapshot(
    fixtures,
    home_team_id,
    away_team_id,
    cutoff,
    initial_rating=DEFAULT_RATING,
):
    """
    Return the pre-match Elo ratings for both requested teams.

    The fixture being predicted must not be included in the historical
    calculation unless it occurred strictly before the supplied cutoff.
    """
    if initial_rating <= 0:
        raise ValueError(
            "initial_rating must be positive."
        )

    ratings = reconstruct_ratings(
        fixtures,
        cutoff,
        initial_rating=initial_rating,
    )

    return {
        "cutoff": cutoff,
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        "home_rating": ratings.get(
            home_team_id,
            initial_rating,
        ),
        "away_rating": ratings.get(
            away_team_id,
            initial_rating,
        ),
  }
