"""
Poisson-distribution goal model with Dixon-Coles correction.

This module is the core football market-probability engine.

Important:
- All probability distributions are normalized.
- Match-result probabilities are derived from the same scoreline grid.
- Double Chance and goals markets are derived from that grid.
- Additional markets can be added without changing the core score model.
- Missing historical data must be handled by the caller; this module does
  not fabricate inputs.
"""

import math

import config


def poisson_pmf(k, lam):
    """Return the Poisson probability for k events at rate lam."""
    if k < 0 or lam < 0:
        return 0.0

    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def expected_goals(
    team_attack,
    opponent_defense,
    league_avg_goals,
    is_home=None,
):
    """
    Calculate expected goals from attack strength, opponent defense,
    and league scoring average.
    """
    base = team_attack * opponent_defense * league_avg_goals

    if is_home is True:
        return base * config.HOME_ADVANTAGE_MULTIPLIER

    if is_home is False:
        return base * config.AWAY_DISADVANTAGE_MULTIPLIER

    return base


def _dixon_coles_tau(x, y, home_xg, away_xg, rho):
    """Dixon-Coles correction for low-scoring outcomes."""
    if x == 0 and y == 0:
        return 1 - (home_xg * away_xg * rho)

    if x == 0 and y == 1:
        return 1 + (home_xg * rho)

    if x == 1 and y == 0:
        return 1 + (away_xg * rho)

    if x == 1 and y == 1:
        return 1 - rho

    return 1.0


def build_scoreline_grid(
    home_xg,
    away_xg,
    max_goals=None,
    apply_dixon_coles=True,
):
    """
    Build and normalize the complete scoreline probability grid.
    """
    if home_xg < 0 or away_xg < 0:
        raise ValueError("Expected goals cannot be negative.")

    if max_goals is None:
        max_goals = config.MAX_GOALS_GRID

    if max_goals < 1:
        raise ValueError("max_goals must be at least 1.")

    grid = []

    for home_goals in range(max_goals + 1):
        row = []

        for away_goals in range(max_goals + 1):
            probability = (
                poisson_pmf(home_goals, home_xg)
                * poisson_pmf(away_goals, away_xg)
            )

            if (
                apply_dixon_coles
                and home_goals <= 1
                and away_goals <= 1
            ):
                probability *= _dixon_coles_tau(
                    home_goals,
                    away_goals,
                    home_xg,
                    away_xg,
                    config.DIXON_COLES_RHO,
                )

            row.append(probability)

        grid.append(row)

    total = sum(sum(row) for row in grid)

    if total <= 0:
        raise ValueError("Scoreline probability grid has zero probability.")

    return [
        [probability / total for probability in row]
        for row in grid
    ]


def _probability_for_total_goals(grid, condition):
    """Sum scoreline probabilities satisfying a total-goals condition."""
    probability = 0.0

    for home_goals, row in enumerate(grid):
        for away_goals, value in enumerate(row):
            if condition(home_goals + away_goals):
                probability += value

    return probability


def _probability_for_home_goals(grid, condition):
    """Sum probabilities satisfying a home-team-goals condition."""
    probability = 0.0

    for home_goals, row in enumerate(grid):
        if condition(home_goals):
            probability += sum(row)

    return probability


def _probability_for_away_goals(grid, condition):
    """Sum probabilities satisfying an away-team-goals condition."""
    probability = 0.0

    for home_goals, row in enumerate(grid):
        for away_goals, value in enumerate(row):
            if condition(away_goals):
                probability += value

    return probability


def _probability_sum(probabilities):
    """Return the sum of a probability dictionary."""
    return sum(probabilities.values())


def market_probabilities(home_xg, away_xg):
    """
    Generate the supported football market probability distributions.

    Every mutually exclusive market distribution is normalized or derived
    from the normalized scoreline grid.
    """
    grid = build_scoreline_grid(home_xg, away_xg)
    max_goals = len(grid) - 1

    home_win = 0.0
    draw = 0.0
    away_win = 0.0
    btts_yes = 0.0

    scorelines = []

    for home_goals in range(max_goals + 1):
        for away_goals in range(max_goals + 1):
            probability = grid[home_goals][away_goals]

            scorelines.append(
                ((home_goals, away_goals), probability)
            )

            if home_goals > away_goals:
                home_win += probability
            elif home_goals < away_goals:
                away_win += probability
            else:
                draw += probability

            if home_goals >= 1 and away_goals >= 1:
                btts_yes += probability

    match_result = {
        "home_win": home_win,
        "draw": draw,
        "away_win": away_win,
    }

    # Explicit normalization guard.
    match_total = _probability_sum(match_result)

    if match_total <= 0:
        raise ValueError("Match-result probabilities are invalid.")

    match_result = {
        key: value / match_total
        for key, value in match_result.items()
    }

    double_chance = {
        "home_or_draw": (
            match_result["home_win"]
            + match_result["draw"]
        ),
        "away_or_draw": (
            match_result["away_win"]
            + match_result["draw"]
        ),
        "home_or_away": (
            match_result["home_win"]
            + match_result["away_win"]
        ),
    }

    over_under = {}

    for line in (1.5, 2.5, 3.5, 4.5, 5.5):
        over_key = f"over_{str(line).replace('.', '_')}"
        under_key = f"under_{str(line).replace('.', '_')}"

        over_probability = _probability_for_total_goals(
            grid,
            lambda total_goals, line=line: total_goals > line,
        )

        over_probability = min(
            max(over_probability, 0.0),
            1.0,
        )

        over_under[over_key] = over_probability
        over_under[under_key] = 1.0 - over_probability

    btts = {
        "yes": min(max(btts_yes, 0.0), 1.0),
        "no": 1.0 - min(max(btts_yes, 0.0), 1.0),
    }

    team_goals = {
        "home_over_0_5": _probability_for_home_goals(
            grid,
            lambda goals: goals > 0.5,
        ),
        "home_under_0_5": _probability_for_home_goals(
            grid,
            lambda goals: goals < 0.5,
        ),
        "home_over_1_5": _probability_for_home_goals(
            grid,
            lambda goals: goals > 1.5,
        ),
        "home_under_1_5": _probability_for_home_goals(
            grid,
            lambda goals: goals < 1.5,
        ),
        "home_over_2_5": _probability_for_home_goals(
            grid,
            lambda goals: goals > 2.5,
        ),
        "home_under_2_5": _probability_for_home_goals(
            grid,
            lambda goals: goals < 2.5,
        ),
        "away_over_0_5": _probability_for_away_goals(
            grid,
            lambda goals: goals > 0.5,
        ),
        "away_under_0_5": _probability_for_away_goals(
            grid,
            lambda goals: goals < 0.5,
        ),
        "away_over_1_5": _probability_for_away_goals(
            grid,
            lambda goals: goals > 1.5,
        ),
        "away_under_1_5": _probability_for_away_goals(
            grid,
            lambda goals: goals < 1.5,
        ),
        "away_over_2_5": _probability_for_away_goals(
            grid,
            lambda goals: goals > 2.5,
        ),
        "away_under_2_5": _probability_for_away_goals(
            grid,
            lambda goals: goals < 2.5,
        ),
    }

    scorelines.sort(
        key=lambda item: item[1],
        reverse=True,
    )

    top_scorelines = [
        {
            "score": f"{home_goals}-{away_goals}",
            "probability": probability,
        }
        for (home_goals, away_goals), probability
        in scorelines[:5]
    ]

    return {
        "expected_goals": {
            "home": round(home_xg, 2),
            "away": round(away_xg, 2),
        },
        "match_result": match_result,
        "double_chance": double_chance,
        "over_under": over_under,
        "btts": btts,
        "team_goals": team_goals,
        "top_scorelines": top_scorelines,
    }


def cards_market(
    home_avg_cards,
    away_avg_cards,
    line=3.5,
):
    """
    Calculate over/under card probabilities.

    The Poisson distribution is truncated at MAX_CARDS, then normalized so
    over + under remains exactly 1 within the modeled range.
    """
    combined_lambda = max(
        0.0,
        home_avg_cards + away_avg_cards,
    )

    max_cards = 20

    probabilities = [
        poisson_pmf(total_cards, combined_lambda)
        for total_cards in range(max_cards + 1)
    ]

    total_probability = sum(probabilities)

    if total_probability <= 0:
        raise ValueError("Card probability distribution is invalid.")

    probabilities = [
        probability / total_probability
        for probability in probabilities
    ]

    over_probability = sum(
        probability
        for total_cards, probability
        in enumerate(probabilities)
        if total_cards > line
    )

    over_probability = min(
        max(over_probability, 0.0),
        1.0,
    )

    return {
        "expected_total_cards": round(
            combined_lambda,
            2,
        ),
        "over_line": line,
        "over": over_probability,
        "under": 1.0 - over_probability,
        }
