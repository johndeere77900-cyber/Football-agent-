"""
Turns raw match-result probabilities into a simple, honest confidence flag,
and identifies the single safest pick across ALL computed markets for a
given match/game.
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
        emoji = "\U0001F7E2"
    elif gap <= config.CONFIDENCE_MODERATE_GAP:
        label = "Toss-up"
        emoji = "\U0001F534"
    else:
        label = "Moderate"
        emoji = "\U0001F7E1"

    return {
        "label": label,
        "emoji": emoji,
        "top_pick": top_outcome,
        "top_probability": top_prob,
        "gap": gap,
    }


def safest_pick(candidates):
    """
    candidates: list of (label, probability) tuples covering every market
    computed for this match (e.g. "Home Win", "Over 2.5 Goals", "BTTS No").
    Returns the single most one-sided outcome across all of them - the
    closest thing to a "safest" pick this model can offer.
    """
    if not candidates:
        return None
    best_label, best_prob = max(candidates, key=lambda x: x[1])
    return {"label": best_label, "probability": best_prob}
