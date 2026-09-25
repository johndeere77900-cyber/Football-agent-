"""
Live in-play prediction adjustments.

Takes a match's pre-match expected-goals rates (from the normal model)
plus the CURRENT score and minutes elapsed, and recalculates every market
based on what's actually left to happen - rather than just repeating the
pre-match view once a game has kicked off.

Approach: scale each team's expected-goals rate down to reflect only the
time remaining, build a probability grid for ADDITIONAL goals only, then
add that to the current score to get final-outcome probabilities.

Honest limitation: this only accounts for time remaining and the current
score. It does NOT account for red cards, injuries, momentum shifts, or
tactical changes mid-game - a genuinely complete live model would need
much more real-time data than the free API tier provides.
"""

import poisson_model

FULL_MATCH_MINUTES = 90


def remaining_minutes(elapsed, status_short):
    """Estimates minutes left. Treats halftime as 45 minutes elapsed."""
    if status_short == "HT":
        return FULL_MATCH_MINUTES - 45
    if elapsed is None:
        return FULL_MATCH_MINUTES
    return max(FULL_MATCH_MINUTES - elapsed, 1)


def live_market_probabilities(home_xg_full, away_xg_full, elapsed, status_short,
                               current_home_goals, current_away_goals):
    remaining = remaining_minutes(elapsed, status_short)
    time_fraction = remaining / FULL_MATCH_MINUTES

    remaining_home_xg = home_xg_full * time_fraction
    remaining_away_xg = away_xg_full * time_fraction

    grid = poisson_model.build_scoreline_grid(remaining_home_xg, remaining_away_xg)
    max_extra = len(grid) - 1

    home_win = draw = away_win = 0.0
    over_1_5 = over_2_5 = over_3_5 = 0.0
    scorelines = []
    current_total = current_home_goals + current_away_goals

    for h_extra in range(max_extra + 1):
        for a_extra in range(max_extra + 1):
            p = grid[h_extra][a_extra]
            final_home = current_home_goals + h_extra
            final_away = current_away_goals + a_extra
            scorelines.append(((final_home, final_away), p))

            if final_home > final_away:
                home_win += p
            elif final_home < final_away:
                away_win += p
            else:
                draw += p

            final_total = current_total + h_extra + a_extra
            if final_total > 1.5:
                over_1_5 += p
            if final_total > 2.5:
                over_2_5 += p
            if final_total > 3.5:
                over_3_5 += p

    if current_home_goals >= 1 and current_away_goals >= 1:
        btts_yes = 1.0
    elif current_home_goals >= 1:
        btts_yes = 1 - poisson_model.poisson_pmf(0, remaining_away_xg)
    elif current_away_goals >= 1:
        btts_yes = 1 - poisson_model.poisson_pmf(0, remaining_home_xg)
    else:
        btts_yes = (1 - poisson_model.poisson_pmf(0, remaining_home_xg)) * \
                    (1 - poisson_model.poisson_pmf(0, remaining_away_xg))

    scorelines.sort(key=lambda x: x[1], reverse=True)
    top_scorelines = scorelines[:5]

    return {
        "is_live": True,
        "minutes_elapsed": elapsed,
        "minutes_remaining_estimate": remaining,
        "current_score": {"home": current_home_goals, "away": current_away_goals},
        "expected_additional_goals": {
            "home": round(remaining_home_xg, 2),
            "away": round(remaining_away_xg, 2),
        },
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
