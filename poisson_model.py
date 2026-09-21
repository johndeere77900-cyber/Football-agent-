"""
Poisson-distribution goal model.

Core idea: if we know how many goals a team is expected to score against an
average opponent, and how many an average team is expected to score against
this opponent's defense, we can model the number of goals in a match as a
Poisson-distributed random variable. From there, nearly every football
betting market falls out of the same probability grid.
"""

import math

import config


def poisson_pmf(k, lam):
    """Probability of exactly k goals, given an expected-goals rate of lam."""
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def expected_goals(team_attack, opponent_defense, league_avg_goals):
    """
    team_attack: this team's average goals scored, relative to league average
                 (e.g. 1.3 means they score 30% more than a typical team)
    opponent_defense: opponent's average goals conceded, relative to league
                       average (e.g. 1.2 means they concede 20% more than typical)
    league_avg_goals: league-wide average goals scored per team per game
    """
    return team_attack * opponent_defense * league_avg_goals


def build_scoreline_grid(home_xg, away_xg, max_goals=None):
    """
    Returns a 2D grid where grid[h][a] = probability of a final score of
    h goals (home) to a goals (away).
    """
    max_goals = max_goals or config.MAX_GOALS_GRID
    grid = []
    for h in range(max_goals + 1):
        row = []
        for a in range(max_goals + 1):
            row.append(poisson_pmf(h, home_xg) * poisson_pmf(a, away_xg))
        grid.append(row)
    return grid


def market_probabilities(home_xg, away_xg):
    """
    Takes expected goals for each team and returns probabilities for every
    market the agent supports.
    """
    grid = build_scoreline_grid(home_xg, away_xg)
    max_goals = len(grid) - 1

    home_win = draw = away_win = 0.0
    over_1_5 = over_2_5 = over_3_5 = 0.0
    btts_yes = 0.0
    scorelines = []

    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            p = grid[h][a]
            scorelines.append(((h, a), p))

            if h > a:
                home_win += p
            elif h < a:
                away_win += p
            else:
                draw += p

            total_goals = h + a
            if total_goals > 1.5:
                over_1_5 += p
            if total_goals > 2.5:
                over_2_5 += p
            if total_goals > 3.5:
                over_3_5 += p

            if h >= 1 and a >= 1:
                btts_yes += p

    scorelines.sort(key=lambda x: x[1], reverse=True)
    top_scorelines = scorelines[:5]

    return {
        "expected_goals": {"home": round(home_xg, 2), "away": round(away_xg, 2)},
        "match_result": {
            "home_win": home_win,
            "draw": draw,
            "away_win": away_win,
        },
        "double_chance": {
            "home_or_draw": home_win + draw,
            "away_or_draw": away_win + draw,
            "home_or_away": home_win + away_win,
        },
        "over_under": {
            "over_1_5": over_1_5, "under_1_5": 1 - over_1_5,
            "over_2_5": over_2_5, "under_2_5": 1 - over_2_5,
            "over_3_5": over_3_5, "under_3_5": 1 - over_3_5,
        },
        "btts": {"yes": btts_yes, "no": 1 - btts_yes},
        "top_scorelines": [
            {"score": f"{h}-{a}", "probability": p} for (h, a), p in top_scorelines
        ],
    }


def cards_market(home_avg_cards, away_avg_cards, line=3.5):
    """
    Simple Poisson model for total match cards (yellow + red), using each
    team's average cards-per-game.
    """
    combined_lambda = home_avg_cards + away_avg_cards
    max_cards = 12
    over_prob = 0.0
    for total in range(max_cards + 1):
        p = poisson_pmf(total, combined_lambda)
        if total > line:
            over_prob += p
    return {
        "expected_total_cards": round(combined_lambda, 2),
        "over_line": line,
        "over": over_prob,
        "under": 1 - over_prob,
    }
