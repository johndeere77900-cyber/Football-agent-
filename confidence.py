"""
Turns raw match-result probabilities into a simple, honest confidence flag,
and identifies the single safest pick across ALL computed markets for a
given match/game.
"""

import config


def confidence_flag(outcome_probabilities, calibration_status="UNAVAILABLE"):
    """
    outcome_probabilities: dict of outcome -> probability, e.g.
        {"home_win": 0.55, "draw": 0.25, "away_win": 0.20}

    Returns one of "High", "Moderate", "Toss-up", plus the top pick, gap, and
    explicit calibration status metadata.
    """
    if not isinstance(outcome_probabilities, dict) or not outcome_probabilities:
        return {
            "label": "Toss-up",
            "emoji": "\U0001F534",
            "top_pick": None,
            "top_probability": 0.0,
            "gap": 0.0,
            "is_calibrated": False,
            "calibration_status": str(calibration_status).upper(),
            "calibration_limitation": "UNCALIBRATED_RAW_PROBABILITY",
            "note": "Confidence reflects raw/model outcome probability margin and does not guarantee empirical calibration.",
        }

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

    cal_status_str = str(calibration_status).upper() if calibration_status else "UNAVAILABLE"
    is_calibrated = (cal_status_str == "APPLIED")
    if is_calibrated:
        limitation = "CALIBRATED"
        note = "Confidence reflects calibrated outcome probability margin."
    elif cal_status_str == "ERROR":
        limitation = "CALIBRATION_ERROR"
        note = "Calibration encountered an error; confidence reflects raw uncalibrated probabilities."
    else:
        limitation = "UNCALIBRATED_RAW_PROBABILITY"
        note = "Confidence reflects raw/model outcome probability margin and does not guarantee empirical calibration."

    return {
        "label": label,
        "emoji": emoji,
        "top_pick": top_outcome,
        "top_probability": top_prob,
        "gap": gap,
        "is_calibrated": is_calibrated,
        "calibration_status": cal_status_str,
        "calibration_limitation": limitation,
        "note": note,
    }


def safest_pick(candidates):
    """
    candidates: list of (label, probability) tuples covering every market
    computed for this match (e.g. "Home Win", "Over 2.5 Goals", "BTTS No").
    Returns the single most one-sided outcome across all of them - explicitly
    marked non-authoritative and informational-only. It must never override
    authoritative prediction contract, Quality Gate, or market-specific decisions.
    """
    if not candidates:
        return None
    best_label, best_prob = max(candidates, key=lambda x: x[1])
    return {
        "label": best_label,
        "probability": best_prob,
        "is_authoritative": False,
        "informational_only": True,
        "warning": "Non-authoritative selection across distinct event spaces; does not represent model signal or Quality Gate decision.",
    }
