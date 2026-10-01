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


def _categorize_market(label: str) -> str:
    lbl = str(label).lower()
    if "btts" in lbl:
        return "btts"
    elif "cards" in lbl:
        return "cards"
    elif "over" in lbl or "under" in lbl:
        if "home over" in lbl or "home under" in lbl or "away over" in lbl or "away under" in lbl:
            return "team_goals"
        return "over_under"
    elif "draw" in lbl or "or" in lbl:
        if "home or" in lbl or "away or" in lbl or "home_or" in lbl or "away_or" in lbl:
            return "double_chance"
        return "match_result"
    elif "win" in lbl or "home" in lbl or "away" in lbl:
        return "match_result" if ("home win" in lbl or "away win" in lbl or "draw" in lbl or lbl in ("home_win", "draw", "away_win")) else "moneyline"
    elif "points" in lbl:
        return "total_points"
    return "other"


def safest_pick(candidates):
    """
    candidates: dict of market_key -> list of (label, probability), OR list of (label, probability) tuples.

    Returns market-scoped safest picks - explicitly marked non-authoritative and
    informational-only. It must never override authoritative prediction contract,
    Quality Gate, or market-specific decisions.
    """
    if not candidates:
        return None

    market_groups: dict = {}

    if isinstance(candidates, dict):
        for m_key, items in candidates.items():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, (tuple, list)) and len(item) >= 2:
                        market_groups.setdefault(str(m_key), []).append((str(item[0]), float(item[1])))
            elif isinstance(items, (tuple, list)) and len(items) >= 2:
                market_groups.setdefault(str(m_key), []).append((str(items[0]), float(items[1])))
    elif isinstance(candidates, (list, tuple)):
        for item in candidates:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                label, prob = str(item[0]), float(item[1])
                m_key = _categorize_market(label)
                market_groups.setdefault(m_key, []).append((label, prob))

    if not market_groups:
        return None

    by_market = {}
    for m_key, items in market_groups.items():
        if items:
            best_label, best_prob = max(items, key=lambda x: x[1])
            by_market[m_key] = {"label": best_label, "probability": best_prob}

    if not by_market:
        return None

    return {
        "by_market": by_market,
        "is_authoritative": False,
        "informational_only": True,
        "warning": "Informational-only market-scoped selection; does not represent model signal or Quality Gate decision.",
    }
