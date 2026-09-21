"""
A simple Elo rating system for football teams.

Elo gives us a single number representing team strength that updates after
every match - it's the same underlying idea used by chess ratings, FIFA's
own rankings, and most serious football prediction models (e.g. ClubElo).

This module doesn't call any API - it just does the math. main.py feeds it
match results to build up ratings over time, storing them via storage.py.
"""

K_FACTOR = 20          # how much one result moves a team's rating
HOME_ADVANTAGE = 60    # Elo points added to the home team's rating pre-match
DEFAULT_RATING = 1500  # starting point for a team with no history


def expected_score(rating_a, rating_b):
    """Probability that team A beats team B (draws are split 50/50 into this)."""
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))


def update_ratings(rating_home, rating_away, home_goals, away_goals):
    """
    Returns (new_rating_home, new_rating_away) after a match result.
    Score for Elo purposes: 1 = win, 0.5 = draw, 0 = loss.
    """
    adjusted_home = rating_home + HOME_ADVANTAGE

    if home_goals > away_goals:
        actual_home = 1.0
    elif home_goals < away_goals:
        actual_home = 0.0
    else:
        actual_home = 0.5

    expected_home = expected_score(adjusted_home, rating_away)

    # Scale K by goal difference so a 4-0 moves ratings more than a 1-0
    margin_multiplier = 1 + min(abs(home_goals - away_goals), 4) * 0.15

    new_rating_home = rating_home + K_FACTOR * margin_multiplier * (actual_home - expected_home)
    new_rating_away = rating_away + K_FACTOR * margin_multiplier * ((1 - actual_home) - (1 - expected_home))

    return new_rating_home, new_rating_away


def win_draw_loss_probabilities(rating_home, rating_away):
    """
    Elo alone can't cleanly produce a draw probability (it's a two-outcome
    model), so this is a rough approximation used only as a cross-check
    against the Poisson model's win/draw/loss numbers - the Poisson model
    is the primary source for match markets.
    """
    adjusted_home = rating_home + HOME_ADVANTAGE
    p_home_raw = expected_score(adjusted_home, rating_away)

    #
