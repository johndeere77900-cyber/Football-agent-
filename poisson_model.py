"""
Poisson-distribution goal model, with a Dixon-Coles low-score correction
applied to sharpen 0-0/1-0/0-1/1-1 predictions specifically - plain
Poisson is known to slightly misjudge these particular outcomes.
"""

import math

import config


def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def expected_goals(team_attack, opponent_defense, league_avg_goals, is_home=None):
    base = team_attack * opponent_defense * league_avg_goals
    if is_home is True:
        return base * config.HOME_ADVANTAGE_MULTIPLIER
    if is_home is False:
        return base * config.AWAY_DISADVANTAGE_MULTIPLIER
    return base


def _dixon_coles_tau(x, y, home_xg, away_xg, rho):
    if x == 0 and y == 0:
        return 1 - (home_xg * away_xg * rho)
    elif x == 0 and y == 1:
        return 1 + (home_xg * rho)
    elif x == 1 and y == 0:
        return 1 + (away_xg * rho)
    elif x == 1 and y == 1:
        return 1 - rho
    return 1.0


def build_scoreline_grid(home_xg, away_xg, max_goals=None, apply_dixon_coles=True):
    max_goals = max_goals or config.MAX_GOALS_GRID
    grid = []
    for h in range(max_goals + 1):
        row = []
        for a in range(max_goals + 1):
            p = poisson_pmf(h, home_xg) * poisson_pmf(a, away_xg)
            if apply_dixon_coles and h <= 1 and a <= 1:
                p *= _dixon_coles_tau(h, a, home_xg, away_xg, config.DIXON_COLES_RHO)
            row.append(p)
        grid.append(row)

    # Renormalize so probabilities still sum to 1 after the correction
    total = sum(sum(row) for row in grid)
    if total > 0:
        grid = [[p / total for p in row] for row in grid]

    return grid


def market_probabilities(home_xg, away_xg):
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
        "match_result": {"home_win": home_win, "draw": draw, "away_win": away_win},
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
