"""
Historical, leakage-safe football feature calculations.

All features in this module are calculated strictly from completed matches
whose kickoff date is BEFORE the prediction cutoff.

This module is intentionally independent of live API calls so it can be
used by chronological backtests and tested deterministically.
"""


def _is_finished(fixture):
    """Return True only for completed fixtures."""
    return fixture.get("fixture", {}).get("status", {}).get("short") == "FT"


def _fixture_date(fixture):
    """Return the fixture date/time string."""
    return fixture.get("fixture", {}).get("date", "")


def _is_before_cutoff(fixture, cutoff):
    """
    A historical fixture is usable only when its timestamp is strictly
    earlier than the prediction cutoff.

    Strictly-before prevents same-day/same-time leakage.
    """
    return _fixture_date(fixture) < cutoff


def prior_completed_fixtures(fixtures, cutoff):
    """
    Return all completed fixtures strictly before cutoff.

    The result is chronological and deterministic.
    """
    eligible = [
        fixture
        for fixture in fixtures
        if _is_finished(fixture)
        and _is_before_cutoff(fixture, cutoff)
    ]

    return sorted(
        eligible,
        key=_fixture_date,
    )


def team_match_history(fixtures, team_id, cutoff):
    """
    Return a team's completed historical matches available at cutoff.

    No match at or after cutoff is included.
    """
    history = []

    for fixture in prior_completed_fixtures(fixtures, cutoff):
        home_id = fixture.get("teams", {}).get("home", {}).get("id")
        away_id = fixture.get("teams", {}).get("away", {}).get("id")

        if team_id in (home_id, away_id):
            history.append(fixture)

    return history


def team_goal_averages(fixtures, team_id, cutoff):
    """
    Calculate goals-for and goals-against averages for one team using only
    its prior completed matches.

    Returns:
        None when the team has no qualifying history.
        Otherwise:
        {
            "matches": int,
            "goals_for": float,
            "goals_against": float,
        }
    """
    history = team_match_history(
        fixtures,
        team_id,
        cutoff,
    )

    goals_for = []
    goals_against = []

    for fixture in history:
        home_id = fixture["teams"]["home"]["id"]
        away_id = fixture["teams"]["away"]["id"]

        home_goals = fixture["goals"]["home"]
        away_goals = fixture["goals"]["away"]

        if home_goals is None or away_goals is None:
            continue

        if home_id == team_id:
            goals_for.append(home_goals)
            goals_against.append(away_goals)
        else:
            goals_for.append(away_goals)
            goals_against.append(home_goals)

    if not goals_for:
        return None

    return {
        "matches": len(goals_for),
        "goals_for": sum(goals_for) / len(goals_for),
        "goals_against": sum(goals_against) / len(goals_against),
    }


def has_minimum_history(fixtures, team_id, cutoff, minimum_matches):
    """Check whether a team individually has enough prior matches."""
    if minimum_matches < 0:
        raise ValueError("minimum_matches cannot be negative.")

    history = team_match_history(
        fixtures,
        team_id,
        cutoff,
    )

    return len(history) >= minimum_matches


def fixture_has_minimum_history(
    fixtures,
    home_team_id,
    away_team_id,
    cutoff,
    minimum_matches=5,
):
    """
    Validate minimum historical coverage for BOTH teams independently.

    This fixes the previous global shortcut where the first N league
    fixtures were discarded instead of checking each team's actual history.
    """
    return (
        has_minimum_history(
            fixtures,
            home_team_id,
            cutoff,
            minimum_matches,
        )
        and
        has_minimum_history(
            fixtures,
            away_team_id,
            cutoff,
            minimum_matches,
        )
    )


def historical_feature_snapshot(
    fixtures,
    home_team_id,
    away_team_id,
    cutoff,
    minimum_matches=0,
):
    """
    Build a leakage-safe historical feature snapshot for a fixture.

    Returns None when either team does not satisfy the requested minimum
    history requirement.
    """
    if not fixture_has_minimum_history(
        fixtures,
        home_team_id,
        away_team_id,
        cutoff,
        minimum_matches,
    ):
        return None

    home = team_goal_averages(
        fixtures,
        home_team_id,
        cutoff,
    )

    away = team_goal_averages(
        fixtures,
        away_team_id,
        cutoff,
    )

    if home is None or away is None:
        return None

    return {
        "cutoff": cutoff,
        "home": home,
        "away": away,
        "source_match_count": (
            home["matches"] + away["matches"]
        ),
  }
