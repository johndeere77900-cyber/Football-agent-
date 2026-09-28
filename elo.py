"""
Simple Elo rating system for football teams.

This module is deterministic and API-free so it can be used by both
production prediction and chronological historical backtesting.

Important:
- Elo ratings must be updated chronologically in historical backtests.
- The 1X2 probabilities are an approximation, not a calibrated standalone
  football model.
- Calibration and model weighting must be validated separately.
"""

K_FACTOR = 20
HOME_ADVANTAGE = 60
DEFAULT_RATING = 1500


def expected_score(rating_a, rating_b):
    """
    Expected share for team A in a two-outcome Elo comparison.
    """
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))


def update_ratings(rating_home, rating_away, home_goals, away_goals):
    """
    Update both ratings after a completed match.

    Returns:
        (new_home_rating, new_away_rating)
    """
    adjusted_home = rating_home + HOME_ADVANTAGE

    if home_goals > away_goals:
        actual_home = 1.0
    elif home_goals < away_goals:
        actual_home = 0.0
    else:
        actual_home = 0.5

    expected_home = expected_score(adjusted_home, rating_away)

    margin_multiplier = (
        1 + min(abs(home_goals - away_goals), 4) * 0.15
    )

    delta = (
        K_FACTOR
        * margin_multiplier
        * (actual_home - expected_home)
    )

    return rating_home + delta, rating_away - delta


def win_draw_loss_probabilities(rating_home, rating_away):
    """
    Convert Elo strength into a normalized 1X2 probability vector.

    Elo is fundamentally a two-outcome rating system, so draw probability
    requires an explicit approximation here.

    This function is therefore intended as an Elo cross-check/blend signal,
    NOT as a standalone calibrated football probability model.

    Returns:
        {
            "home": float,
            "draw": float,
            "away": float
        }
    """
    adjusted_home = rating_home + HOME_ADVANTAGE

    p_home_raw = expected_score(
        adjusted_home,
        rating_away,
    )

    # Conservative draw approximation.
    # Draw likelihood is highest when adjusted ratings are close.
    rating_gap = abs(adjusted_home - rating_away)

    draw = 0.28 * max(
        0.0,
        1.0 - rating_gap / 400.0,
    )

    # Keep the approximation within reasonable bounds.
    draw = min(max(draw, 0.05), 0.28)

    remaining = 1.0 - draw

    home = remaining * p_home_raw
    away = remaining * (1.0 - p_home_raw)

    total = home + draw + away

    return {
        "home": home / total,
        "draw": draw / total,
        "away": away / total,
    }
