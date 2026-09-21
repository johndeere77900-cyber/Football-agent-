"""
Turns raw match-result probabilities into a simple, honest confidence flag.

The point: a bare percentage (e.g. "52%") doesn't tell you whether that's a
strong lean or a coin flip. This looks at the *gap* between the top outcome
and the next most likely one to decide how much weight the prediction
actually deserves.
"""

import config


def confidence_flag(outcome_probabilities):
    """
    outcome_probabilities: dict of outcome -> probability, e.g.
        {"home_win": 0.55, "draw": 0.25, "away_win": 0.20}

    Returns one of "High", "Moderate", "Toss-up", plus the top pick and gap.
    """
    sorted_outcomes = sorted(outcome_probabilities.items(), key=lambda x: x[1], reverse=True)
    top_outcome, top_prob = sorted_outcomes[0]
    second_prob = sorted_outcomes[1][1] if len(sorted_outcomes) > 1 else 0.0
    gap = top_prob - second_prob

    if gap >= config.CONFIDENCE_HIGH_GAP:
        label = "High"
        emoji = "\U0001F7E2"  # green circle
    elif gap <= config.CONFIDENCE_MODERATE_GAP:
        label = "Toss-up"
        emoji = "\U0001F534"  # red circle
    else:
        label = "Moderate"
        emoji = "\U0001F7E1"  # yellow circle

    return {
        "label": label,
        "emoji": emoji,
        "top_pick": top_outcome,
        "top_probability": top_prob,
        "gap": gap,
    }
