"""
Backtesting and prediction-building helpers.
"""

import random

import api_football
import confidence
import config
import historical_features
import poisson_model


def estimate_expected_goals_from_stats(
    team_stats_for,
    team_stats_against,
    league_avg_goals,
):
    if not isinstance(team_stats_for, dict):
        team_stats_for = {}
    if not isinstance(team_stats_against, dict):
        team_stats_against = {}

    goals_for_avg = (
        team_stats_for
        .get("goals", {})
        .get("for", {})
        .get("average", {})
        .get("total")
    )
    goals_against_avg = (
        team_stats_against
        .get("goals", {})
        .get("against", {})
        .get("average", {})
        .get("total")
    )

    try:
        goals_for_avg = (
            float(goals_for_avg)
            if goals_for_avg not in (None, "")
            else league_avg_goals
        )
    except (TypeError, ValueError):
        goals_for_avg = league_avg_goals

    try:
        goals_against_avg = (
            float(goals_against_avg)
            if goals_against_avg not in (None, "")
            else league_avg_goals
        )
    except (TypeError, ValueError):
        goals_against_avg = league_avg_goals

    return (
        goals_for_avg / league_avg_goals,
        goals_against_avg / league_avg_goals,
    )


def estimate_recent_form_goals(
    team_id,
    league_avg_goals,
    last=None,
):
    last = last or config.RECENT_FORM_MATCHES
    matches = api_football.get_recent_form(team_id, last=last)

    goals_for, goals_against = [], []

    for match in matches:
        home_id = match["teams"]["home"]["id"]
        away_id = match["teams"]["away"]["id"]
        home_goals = match["goals"]["home"]
        away_goals = match["goals"]["away"]

        if home_goals is None or away_goals is None:
            continue

        if home_id == team_id:
            goals_for.append(home_goals)
            goals_against.append(away_goals)
        elif away_id == team_id:
            goals_for.append(away_goals)
            goals_against.append(home_goals)

    if not goals_for:
        return 1.0, 1.0

    avg_for = sum(goals_for) / len(goals_for)
    avg_against = sum(goals_against) / len(goals_for)

    return (
        avg_for / league_avg_goals,
        avg_against / league_avg_goals,
    )


def estimate_head_to_head_goals(
    home_id,
    away_id,
    league_avg_goals,
    last=None,
):
    """
    Estimate historical H2H goal ratios for the two teams.

    This function remains API-backed for the existing prediction path.
    Historical backtesting should use the dedicated historical feature
    modules rather than current/live API results.
    """
    last = last or config.HEAD_TO_HEAD_MATCHES
    matches = api_football.get_head_to_head(
        home_id,
        away_id,
        last=last,
    )

    home_goals_for = []
    away_goals_for = []

    for match in matches:
        match_home_id = match["teams"]["home"]["id"]
        match_away_id = match["teams"]["away"]["id"]
        home_goals = match["goals"]["home"]
        away_goals = match["goals"]["away"]

        if home_goals is None or away_goals is None:
            continue

        if match_home_id == home_id:
            home_goals_for.append(home_goals)
            away_goals_for.append(away_goals)

        elif match_away_id == home_id:
            home_goals_for.append(away_goals)
            away_goals_for.append(home_goals)

    if not home_goals_for:
        return 1.0, 1.0, 1.0, 1.0

    home_avg = sum(home_goals_for) / len(home_goals_for)
    away_avg = sum(away_goals_for) / len(away_goals_for)

    return (
        home_avg / league_avg_goals,
        1.0,
        away_avg / league_avg_goals,
        1.0,
    )


def blend_three(
    season_ratio,
    recent_ratio,
    h2h_ratio,
):
    """Weighted blend of season, recent-form, and H2H signals."""
    return (
        season_ratio * config.SEASON_WEIGHT
        + recent_ratio * config.RECENT_FORM_WEIGHT
        + h2h_ratio * config.HEAD_TO_HEAD_WEIGHT
    )


def estimate_avg_cards(
    team_stats,
    league_avg_cards=3.8,
):
    if not isinstance(team_stats, dict):
        return league_avg_cards

    try:
        yellow = (
            team_stats
            .get("cards", {})
            .get("yellow", {})
        )

        total_yellow = sum(
            value.get("total") or 0
            for value in yellow.values()
            if isinstance(value, dict)
        )

        fixtures_played = (
            team_stats
            .get("fixtures", {})
            .get("played", {})
            .get("total")
        )

        if fixtures_played and fixtures_played > 0:
            return total_yellow / fixtures_played

    except (TypeError, AttributeError):
        pass

    return league_avg_cards


def predict_match(
    home_attack,
    home_defense,
    away_attack,
    away_defense,
    league_avg_goals,
):
    home_xg = poisson_model.expected_goals(
        home_attack,
        away_defense,
        league_avg_goals,
    )

    away_xg = poisson_model.expected_goals(
        away_attack,
        home_defense,
        league_avg_goals,
    )

    markets = poisson_model.market_probabilities(
        home_xg,
        away_xg,
    )

    conf = confidence.confidence_flag(
        markets["match_result"]
    )

    return markets, conf


def run_synthetic_selftest():
    league_avg_goals = 1.4
    home_attack = 1.6
    home_defense = 0.7
    away_attack = 0.7
    away_defense = 1.4

    markets, conf = predict_match(
        home_attack,
        home_defense,
        away_attack,
        away_defense,
        league_avg_goals,
    )

    print(
        "=== Synthetic self-test: "
        "strong home side vs weak away side ==="
    )

    print(
        f"Expected goals -> "
        f"Home: {markets['expected_goals']['home']}, "
        f"Away: {markets['expected_goals']['away']}"
    )

    total_prob = sum(
        markets["match_result"].values()
    )

    assert 0.99 <= total_prob <= 1.01, (
        f"Probabilities don't sum to 1: {total_prob}"
    )

    assert (
        markets["match_result"]["home_win"]
        > markets["match_result"]["away_win"]
    )

    print("Self-test passed: math checks out.")

    return markets, conf


def _compute_stats_as_of(
    all_fixtures,
    team_id,
    cutoff_date_str,
):
    goals_for = []
    goals_against = []

    for fixture in all_fixtures:
        if fixture["fixture"]["date"][:10] >= cutoff_date_str:
            continue

        if fixture["fixture"]["status"]["short"] != "FT":
            continue

        home_id = fixture["teams"]["home"]["id"]
        away_id = fixture["teams"]["away"]["id"]

        home_goals = fixture["goals"]["home"]
        away_goals = fixture["goals"]["away"]

        if home_goals is None or away_goals is None:
            continue

        if home_id == team_id:
            goals_for.append(home_goals)
            goals_against.append(away_goals)

        elif away_id == team_id:
            goals_for.append(away_goals)
            goals_against.append(home_goals)

    if not goals_for:
        return None, None

    return (
        sum(goals_for) / len(goals_for),
        sum(goals_against) / len(goals_for),
    )


def _filter_candidates_by_minimum_history(
    finished_fixtures,
    all_fixtures,
    minimum_matches,
):
    """
    Keep only fixtures for which BOTH participating teams have the required
    number of valid completed matches before that fixture's kickoff.

    The prediction fixture itself is excluded because its kickoff timestamp
    is used as the strict historical cutoff.
    """
    if (
        not isinstance(minimum_matches, int)
        or isinstance(minimum_matches, bool)
    ):
        raise ValueError(
            "minimum_matches must be a non-negative integer."
        )

    if minimum_matches < 0:
        raise ValueError(
            "minimum_matches must be a non-negative integer."
        )

    eligible = []

    for fixture in finished_fixtures:
        fixture_date = (
            fixture.get("fixture", {}).get("date", "")
        )

        if not fixture_date:
            continue

        home_id = (
            fixture
            .get("teams", {})
            .get("home", {})
            .get("id")
        )

        away_id = (
            fixture
            .get("teams", {})
            .get("away", {})
            .get("id")
        )

        if home_id is None or away_id is None:
            continue

        if historical_features.fixture_has_minimum_history(
            all_fixtures,
            home_id,
            away_id,
            fixture_date,
            minimum_matches=minimum_matches,
        ):
            eligible.append(fixture)

    return eligible


def _sample_backtest_candidates(
    candidates,
    sample_size,
    seed=42,
):
    """
    Select a reproducible random sample from eligible backtest candidates.

    A local Random instance is used so sampling does not modify Python's
    global random state.
    """
    if (
        not isinstance(sample_size, int)
        or isinstance(sample_size, bool)
    ):
        raise ValueError(
            "sample_size must be a positive integer."
        )

    if sample_size <= 0:
        raise ValueError(
            "sample_size must be a positive integer."
        )

    candidates = list(candidates)

    if len(candidates) <= sample_size:
        return candidates.copy()

    rng = random.Random(seed)

    return rng.sample(
        candidates,
        sample_size,
    )


def run_real_backtest(
    league_id,
    season,
    sample_size=20,
    min_prior_matches=5,
    sample_seed=42,
):
    all_fixtures = api_football.get_league_fixtures(
        league_id,
        season,
    )

    print(
        f"DEBUG: fetched {len(all_fixtures)} total fixtures "
        f"for league {league_id}, season {season}"
    )

    finished = sorted(
        [
            fixture
            for fixture in all_fixtures
            if fixture["fixture"]["status"]["short"] == "FT"
        ],
        key=lambda fixture: fixture["fixture"]["date"],
    )

    print(
        f"DEBUG: {len(finished)} of those are finished (FT)"
    )

    candidates = _filter_candidates_by_minimum_history(
        finished,
        all_fixtures,
        min_prior_matches,
    )

    print(
        f"DEBUG: {len(candidates)} candidates "
        f"with {min_prior_matches} prior matches "
        f"per team"
    )

    candidates = _sample_backtest_candidates(
        candidates,
        sample_size,
        seed=sample_seed,
    )

    print(
        f"DEBUG: {len(candidates)} candidates selected "
        f"for backtest (seed={sample_seed})"
    )

    league_avg_goals = config.LEAGUE_AVG_GOALS.get(
        league_id,
        config.LEAGUE_AVG_GOALS_FALLBACK,
    )

    correct = 0
    graded = 0
    log = []

    for match in candidates:
        cutoff = match["fixture"]["date"][:10]

        home_id = match["teams"]["home"]["id"]
        away_id = match["teams"]["away"]["id"]

        home_for, home_against = _compute_stats_as_of(
            all_fixtures,
            home_id,
            cutoff,
        )

        away_for, away_against = _compute_stats_as_of(
            all_fixtures,
            away_id,
            cutoff,
        )

        if home_for is None or away_for is None:
            continue

        home_attack = home_for / league_avg_goals
        home_defense = home_against / league_avg_goals

        away_attack = away_for / league_avg_goals
        away_defense = away_against / league_avg_goals

        home_xg = poisson_model.expected_goals(
            home_attack,
            away_defense,
            league_avg_goals,
            is_home=True,
        )

        away_xg = poisson_model.expected_goals(
            away_attack,
            home_defense,
            league_avg_goals,
            is_home=False,
        )

        markets = poisson_model.market_probabilities(
            home_xg,
            away_xg,
        )

        predicted = max(
            markets["match_result"],
            key=markets["match_result"].get,
        )

        actual_home = match["goals"]["home"]
        actual_away = match["goals"]["away"]

        actual = (
            "home_win"
            if actual_home > actual_away
            else "away_win"
            if actual_home < actual_away
            else "draw"
        )

        graded += 1

        is_correct = predicted == actual

        correct += int(is_correct)

        log.append(
            {
                "match": (
                    f"{match['teams']['home']['name']} "
                    f"{actual_home}-{actual_away} "
                    f"{match['teams']['away']['name']}"
                ),
                "predicted": predicted,
                "actual": actual,
                "correct": is_correct,
            }
        )

    return {
        "graded": graded,
        "correct": correct,
        "accuracy": (
            correct / graded
            if graded
            else 0
        ),
        "log": log,
    }


if __name__ == "__main__":
    run_synthetic_selftest()
