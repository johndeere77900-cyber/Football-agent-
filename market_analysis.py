"""
Market analysis layer for Football and Basketball.

Calculates bookmaker implied probabilities, market normalized probabilities, edge, and EV.
Enforces strict validity, chronology (odds_timestamp < cutoff_timestamp for historical prediction),
and freshness rules. Never invents odds or substitutes arbitrary values.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import config
from probability_validation import validate_single_probability, ProbabilityValidationError
from time_utils import parse_utc_datetime


def check_odds_chronology_and_staleness(
    odds_timestamp: Optional[str],
    cutoff_timestamp: Optional[str] = None,
) -> str:
    """
    Check odds timestamp against cutoff timestamp and freshness policy.

    Returns:
    - 'AVAILABLE' if odds_timestamp < cutoff_timestamp (or cutoff is None) and within max age.
    - 'FUTURE' if cutoff_timestamp is provided and odds_timestamp >= cutoff_timestamp.
    - 'STALE' if odds_timestamp < cutoff_timestamp but older than max age.
    - 'MISSING' if odds_timestamp is missing or empty.
    """
    if not odds_timestamp:
        return "MISSING"

    dt_odds = parse_utc_datetime(odds_timestamp)
    if dt_odds is None:
        return "MISSING"

    if cutoff_timestamp:
        dt_ref = parse_utc_datetime(cutoff_timestamp)
        if dt_ref is None:
            dt_ref = datetime.now(timezone.utc)
        if dt_odds >= dt_ref:
            return "FUTURE"
    else:
        dt_ref = datetime.now(timezone.utc)

    diff_seconds = (dt_ref - dt_odds).total_seconds()
    if diff_seconds <= 0 and cutoff_timestamp:
        return "FUTURE"

    max_age = float(getattr(config, "MAX_ODDS_AGE_HOURS", 24.0))
    if diff_seconds / 3600.0 > max_age:
        return "STALE"

    return "AVAILABLE"


def is_odds_stale(odds_timestamp: Optional[str], cutoff_timestamp: Optional[str] = None) -> bool:
    """Helper returning True if odds status is not AVAILABLE."""
    return check_odds_chronology_and_staleness(odds_timestamp, cutoff_timestamp) != "AVAILABLE"


def calculate_outcome_market_analysis(
    calibrated_prob: float,
    decimal_odds: Optional[float],
    odds_timestamp: Optional[str] = None,
    raw_prob: Optional[float] = None,
    cutoff_timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Calculate bookmaker implied probability, edge, and EV for a single outcome.

    Formula:
      implied_prob = 1.0 / decimal_odds
      edge = calibrated_prob - implied_prob
      EV = (calibrated_prob * decimal_odds) - 1.0
    """
    # Validate calibrated probability
    try:
        c_prob = validate_single_probability(calibrated_prob, name="calibrated_prob")
    except ProbabilityValidationError:
        return {
            "odds": None,
            "implied_probability": None,
            "edge": None,
            "ev": None,
            "odds_timestamp": odds_timestamp,
            "odds_status": "INVALID_PROBABILITY",
        }

    # Validate decimal odds
    if decimal_odds is None or isinstance(decimal_odds, bool):
        return {
            "odds": None,
            "implied_probability": None,
            "edge": None,
            "ev": None,
            "odds_timestamp": odds_timestamp,
            "odds_status": "MISSING",
        }

    try:
        odds_val = float(decimal_odds)
    except (TypeError, ValueError):
        return {
            "odds": None,
            "implied_probability": None,
            "edge": None,
            "ev": None,
            "odds_timestamp": odds_timestamp,
            "odds_status": "INVALID_ODDS",
        }

    if odds_val <= 1.0:
        return {
            "odds": odds_val,
            "implied_probability": None,
            "edge": None,
            "ev": None,
            "odds_timestamp": odds_timestamp,
            "odds_status": "INVALID_ODDS",
        }

    status = check_odds_chronology_and_staleness(odds_timestamp, cutoff_timestamp)

    if status != "AVAILABLE":
        implied_p = 1.0 / odds_val if status == "STALE" else None
        return {
            "odds": odds_val,
            "implied_probability": implied_p,
            "edge": None,
            "ev": None,
            "odds_timestamp": odds_timestamp,
            "odds_status": status,
        }

    implied_p = 1.0 / odds_val
    edge = c_prob - implied_p
    ev = (c_prob * odds_val) - 1.0

    return {
        "odds": odds_val,
        "implied_probability": implied_p,
        "edge": edge,
        "ev": ev,
        "odds_timestamp": odds_timestamp,
        "odds_status": "AVAILABLE",
    }


def analyze_market_odds(
    calibrated_markets: Dict[str, Any],
    odds_data: Optional[Dict[str, Any]],
    raw_markets: Optional[Dict[str, Any]] = None,
    odds_timestamp: Optional[str] = None,
    cutoff_timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Analyze odds across all supported market choices.

    Returns dict mapping market_key -> outcome_key -> market analysis dict.
    """
    analysis: Dict[str, Any] = {}

    if not isinstance(calibrated_markets, dict):
        return analysis

    if not isinstance(odds_data, dict):
        odds_data = {}

    for market_key, market_val in calibrated_markets.items():
        if isinstance(market_val, dict):
            m_odds = odds_data.get(market_key, {})
            if not isinstance(m_odds, dict):
                m_odds = {}

            analysis[market_key] = {}
            for outcome_key, prob in market_val.items():
                if isinstance(prob, (int, float)) and not isinstance(prob, bool):
                    d_odds = m_odds.get(outcome_key) or m_odds.get(f"implied_{outcome_key}")
                    if d_odds is not None and 0.0 < float(d_odds) < 1.0 and "implied" in str(m_odds.keys()):
                        d_odds = 1.0 / float(d_odds)

                    raw_p = raw_markets.get(market_key, {}).get(outcome_key) if isinstance(raw_markets, dict) else None

                    analysis[market_key][outcome_key] = calculate_outcome_market_analysis(
                        calibrated_prob=float(prob),
                        decimal_odds=d_odds,
                        odds_timestamp=odds_timestamp,
                        raw_prob=raw_p,
                        cutoff_timestamp=cutoff_timestamp,
                    )

    return analysis
