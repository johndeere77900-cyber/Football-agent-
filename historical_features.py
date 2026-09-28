"""
Historical, leakage-safe football feature calculations.

All features in this module are calculated strictly from completed matches
whose kickoff timestamp is BEFORE the prediction cutoff.

This module is independent of live API calls so it can be used by
chronological backtests and tested deterministically.
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


def _team_history_with_valid_goals(fixtures, team_id, cutoff):
    """
    Return prior team matches that have usable numeric final goals.

    Fixtures with missing final goals are not silently treated as 0-0.
    """
    history = team_match_history(
        fixtures,
        team_id,
        cutoff,
    )

    valid = []

    for fixture in history:
        home_goals = fixture.get("goals", {}).get("home")
        away_goals = fixture.get("goals", {}).get("away")

        if isinstance(home_goals, (int, float)) and not isinstance(
            home_goals, bool
        ) and isinstance(away_goals, (int, float)) and not isinstance(
            away_goals, bool
        ):
            valid.append(fixture)

    return valid


def team_goal_averages(fixtures, team_id, cutoff):
    """
    Calculate goals-for and goals-against averages for one team using only
    its prior completed matches.

    Returns None when the team has no qualifying history.
    """
    history = _team_history_with_valid_goals(
        fixtures,
        team_id,
        cutoff,
    )

    goals_for = []
    goals_against = []

    for fixture in history:
        home_id = fixture["teams"]["home"]["id"]
        home_goals = fixture["goals"]["home"]
        away_goals = fixture["goals"]["away"]

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
    """Check whether a team individually has enough prior valid matches."""
    if minimum_matches < 0:
        raise ValueError("minimum_matches cannot be negative.")

    history = _team_history_with_valid_goals(
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


def team_recent_form(
    fixtures,
    team_id,
    cutoff,
    window=8,
    minimum_matches=0,
):
    """
    Calculate a team's recent form strictly as of the historical cutoff.

    The window is applied AFTER filtering to completed, pre-cutoff matches,
    so the result is the team's most recent N matches known at that time.

    Returns None when fewer than minimum_matches valid matches are available.
    If minimum_matches is zero, the function still returns None when there is
    no valid historical match at all.
    """
    if window < 1:
        raise ValueError("window must be at least 1.")

    if minimum_matches < 0:
        raise ValueError("minimum_matches cannot be negative.")

    history = _team_history_with_valid_goals(
        fixtures,
        team_id,
        cutoff,
    )

    recent = history[-window:]

    if len(recent) < minimum_matches or not recent:
        return None

    wins = 0
    draws = 0
    losses = 0
    goals_for = 0
    goals_against = 0
    points = 0

    for fixture in recent:
        home_id = fixture["teams"]["home"]["id"]
        home_goals = fixture["goals"]["home"]
        away_goals = fixture["goals"]["away"]

        if home_id == team_id:
            team_goals = home_goals
            opponent_goals = away_goals
        else:
            team_goals = away_goals
            opponent_goals = home_goals

        goals_for += team_goals
        goals_against += opponent_goals

        if team_goals > opponent_goals:
            wins += 1
            points += 3
        elif team_goals == opponent_goals:
            draws += 1
            points += 1
        else:
            losses += 1

    matches = len(recent)

    return {
        "matches": matches,
        "window": window,
        "goals_for": goals_for / matches,
        "goals_against": goals_against / matches,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "points": points,
        "points_per_match": points / matches,
        "form_sequence": [
            (
                "W"
                if (
                    (
                        fixture["goals"]["home"]
                        > fixture["goals"]["away"]
                        and fixture["teams"]["home"]["id"] == team_id
                    )
                    or (
                        fixture["goals"]["away"]
                        > fixture["goals"]["home"]
                        and fixture["teams"]["away"]["id"] == team_id
                    )
                )
                else "D"
                if fixture["goals"]["home"] == fixture["goals"]["away"]
                else "L"
            )
            for fixture in recent
        ],
        "source_dates": [
            _fixture_date(fixture)
            for fixture in recent
        ],
    }


def fixture_recent_form(
    fixtures,
    home_team_id,
    away_team_id,
    cutoff,
    window=8,
    minimum_matches=0,
):
    """
    Return independent historical recent-form snapshots for both teams.

    No current/live API data is accessed and no future fixture is included.
    Returns None if either team lacks the requested minimum history.
    """
    home = team_recent_form(
        fixtures,
        home_team_id,
        cutoff,
        window=window,
        minimum_matches=minimum_matches,
    )

    away = team_recent_form(
        fixtures,
        away_team_id,
        cutoff,
        window=window,
        minimum_matches=minimum_matches,
    )

    if home is None or away is None:
        return None

    return {
        "cutoff": cutoff,
        "window": window,
        "home": home,
        "away": away,
    }
