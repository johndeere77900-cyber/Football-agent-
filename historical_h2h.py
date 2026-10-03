"""
Historical, leakage-safe head-to-head (H2H) features.

All H2H data is calculated strictly from completed fixtures whose kickoff
timestamp is BEFORE the prediction cutoff.

The requested fixture's home_team_id and away_team_id define the perspective
used for all returned statistics.

This module is API-free and deterministic so it can be used safely by
chronological historical backtesting.
"""


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
    """Return True only when the fixture occurred strictly before cutoff."""
    return time_utils.is_strictly_before(_fixture_date(fixture), cutoff)


def _has_valid_goals(fixture):
    """Return True when both final goals are numeric and usable."""
    return historical_match_policy.get_football_match_goals(fixture) is not None


def historical_h2h_matches(
    fixtures,
    home_team_id,
    away_team_id,
    cutoff,
    canonical_home_id=None,
    canonical_away_id=None,
):
    """
    Return all valid historical meetings between the requested teams.

    Matches by canonical team IDs if present, falling back to numeric team IDs.
    Requirements:
    - completed fixture
    - valid final goals
    - strictly before cutoff
    - both requested teams participated

    Results are returned chronologically.
    """
    matches = []

    for fixture in fixtures:
        if not _is_finished(fixture):
            continue

        if not _is_before_cutoff(fixture, cutoff):
            continue

        if not _has_valid_goals(fixture):
            continue

        home_id = fixture.get("teams", {}).get("home", {}).get("id")
        away_id = fixture.get("teams", {}).get("away", {}).get("id")
        c_home = fixture.get("canonical_home_id")
        c_away = fixture.get("canonical_away_id")

        matched = False
        if canonical_home_id and canonical_away_id:
            if c_home or c_away:
                if {c_home, c_away} == {canonical_home_id, canonical_away_id}:
                    matched = True
            elif {home_id, away_id} == {home_team_id, away_team_id}:
                matched = True
        elif {home_id, away_id} == {home_team_id, away_team_id}:
            matched = True

        if matched:
            matches.append(fixture)

    return sorted(
        matches,
        key=lambda f: time_utils.parse_utc_datetime(_fixture_date(f)) or datetime.min.replace(tzinfo=timezone.utc),
    )


def _requested_team_result(
    fixture,
    requested_home_team_id,
    requested_away_team_id,
    canonical_home_id=None,
    canonical_away_id=None,
):
    """
    Return the result from the requested fixture's home-team perspective.

    Returns:
        "W", "D", or "L"
    """
    fixture_home_id = fixture["teams"]["home"]["id"]
    c_home = fixture.get("canonical_home_id")

    goals = historical_match_policy.get_h2h_form_goals(fixture)
    if goals is None:
        raise ValueError("Fixture has missing or invalid goals.")
    fixture_home_goals, fixture_away_goals = goals

    if canonical_home_id and canonical_away_id and (c_home or fixture.get("canonical_away_id")):
        is_home = (c_home == canonical_home_id)
        is_away = (c_home == canonical_away_id)
    else:
        is_home = (fixture_home_id == requested_home_team_id)
        is_away = (fixture_home_id == requested_away_team_id)

    if is_home:
        requested_home_goals = fixture_home_goals
        requested_away_goals = fixture_away_goals
    elif is_away:
        requested_home_goals = fixture_away_goals
        requested_away_goals = fixture_home_goals
    else:
        raise ValueError(
            "Fixture does not contain the requested teams."
        )

    if requested_home_goals > requested_away_goals:
        return "W"

    if requested_home_goals == requested_away_goals:
        return "D"

    return "L"


def _requested_team_goals(
    fixture,
    requested_home_team_id,
    requested_away_team_id,
    canonical_home_id=None,
    canonical_away_id=None,
):
    """
    Return goals from the requested fixture's home-team perspective.

    Returns:
        (goals_for, goals_against)
    """
    fixture_home_id = fixture["teams"]["home"]["id"]
    c_home = fixture.get("canonical_home_id")

    goals = historical_match_policy.get_h2h_form_goals(fixture)
    if goals is None:
        raise ValueError("Fixture has missing or invalid goals.")
    fixture_home_goals, fixture_away_goals = goals

    is_home = (
        (canonical_home_id and c_home == canonical_home_id)
        or (fixture_home_id == requested_home_team_id)
    )
    is_away = (
        (canonical_away_id and c_home == canonical_away_id)
        or (fixture_home_id == requested_away_team_id)
    )

    if is_home:
        return fixture_home_goals, fixture_away_goals

    if is_away:
        return fixture_away_goals, fixture_home_goals

    raise ValueError(
        "Fixture does not contain the requested teams."
    )


def h2h_minimum_history(
    fixtures,
    home_team_id,
    away_team_id,
    cutoff,
    minimum_matches,
    canonical_home_id=None,
    canonical_away_id=None,
):
    """
    Check whether the requested teams have enough historical H2H meetings.
    """
    if minimum_matches < 0:
        raise ValueError(
            "minimum_matches cannot be negative."
        )

    matches = historical_h2h_matches(
        fixtures,
        home_team_id,
        away_team_id,
        cutoff,
        canonical_home_id=canonical_home_id,
        canonical_away_id=canonical_away_id,
    )

    return len(matches) >= minimum_matches


def historical_h2h_snapshot(
    fixtures,
    home_team_id,
    away_team_id,
    cutoff,
    window=6,
    minimum_matches=0,
    canonical_home_id=None,
    canonical_away_id=None,
):
    """
    Build a historical H2H feature snapshot as of cutoff.

    The most recent `window` valid H2H meetings are used.

    Returns None when:
    - window is invalid
    - insufficient H2H history exists
    - no valid H2H meeting exists
    """
    if window < 1:
        raise ValueError(
            "window must be at least 1."
        )

    if minimum_matches < 0:
        raise ValueError(
            "minimum_matches cannot be negative."
        )

    matches = historical_h2h_matches(
        fixtures,
        home_team_id,
        away_team_id,
        cutoff,
        canonical_home_id=canonical_home_id,
        canonical_away_id=canonical_away_id,
    )

    if len(matches) < minimum_matches:
        return None

    if not matches:
        return None

    recent = matches[-window:]

    wins = 0
    draws = 0
    losses = 0

    goals_for = 0
    goals_against = 0

    btts_count = 0
    over_1_5_count = 0
    over_2_5_count = 0
    over_3_5_count = 0

    form_sequence = []

    for fixture in recent:
        result = _requested_team_result(
            fixture,
            home_team_id,
            away_team_id,
            canonical_home_id=canonical_home_id,
            canonical_away_id=canonical_away_id,
        )

        gf, ga = _requested_team_goals(
            fixture,
            home_team_id,
            away_team_id,
            canonical_home_id=canonical_home_id,
            canonical_away_id=canonical_away_id,
        )

        goals_for += gf
        goals_against += ga

        form_sequence.append(result)

        if result == "W":
            wins += 1
        elif result == "D":
            draws += 1
        else:
            losses += 1

        if gf > 0 and ga > 0:
            btts_count += 1

        total_goals = gf + ga

        if total_goals > 1.5:
            over_1_5_count += 1

        if total_goals > 2.5:
            over_2_5_count += 1

        if total_goals > 3.5:
            over_3_5_count += 1

    meetings = len(recent)

    return {
        "cutoff": cutoff,
        "window": window,
        "meetings": meetings,
        "available_meetings": len(matches),
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "goals_for": goals_for / meetings,
        "goals_against": goals_against / meetings,
        "btts_rate": btts_count / meetings,
        "over_1_5_rate": over_1_5_count / meetings,
        "over_2_5_rate": over_2_5_count / meetings,
        "over_3_5_rate": over_3_5_count / meetings,
        "form_sequence": form_sequence,
        "source_dates": [
            _fixture_date(fixture)
            for fixture in recent
        ],
  }
