"""
Authoritative Quality Gate for Football and Basketball predictions.

Evaluates standardized criteria against centralized config thresholds to output:
- SIGNAL
or
- PASS (with machine-readable reason codes)
"""

from typing import Any, Dict, List, Optional
import config


def evaluate_quality_gate(
    uncertainty_info: Dict[str, Any],
    probability_valid: bool = True,
    edge: Optional[float] = None,
    ev: Optional[float] = None,
    odds_status: str = "MISSING",
    calibration_status: str = "UNAVAILABLE",
    model_error: bool = False,
    require_odds: bool = False,
    require_calibration: bool = False,
) -> Dict[str, Any]:
    """
    Single authoritative Quality Gate for prediction signals.

    Outputs SIGNAL or PASS with machine-readable reason codes.
    """
    reasons: List[str] = []

    if model_error:
        reasons.append("model_error")

    if not probability_valid:
        reasons.append("invalid_probability")

    min_sample = getattr(config, "MIN_HISTORICAL_SAMPLE", 5)
    samples = uncertainty_info.get("historical_sample_count", 0)
    if samples < min_sample:
        reasons.append("insufficient_history")

    min_cov = getattr(config, "MIN_FEATURE_COVERAGE", 0.50)
    cov = uncertainty_info.get("feature_coverage", 0.0)
    if cov < min_cov:
        reasons.append("insufficient_data")

    state = uncertainty_info.get("state", "insufficient_data")
    if state in ("high_uncertainty", "insufficient_data"):
        if "insufficient_data" not in reasons and "insufficient_history" not in reasons:
            reasons.append("high_uncertainty")

    if calibration_status == "ERROR":
        reasons.append("calibration_error")
    elif calibration_status != "APPLIED":
        reasons.append("calibration_unavailable")

    if odds_status == "FUTURE":
        reasons.append("future_odds")
    elif odds_status == "STALE":
        reasons.append("stale_data")
    elif odds_status == "MISSING" and require_odds:
        reasons.append("missing_odds")

    min_edge = getattr(config, "MIN_EDGE_THRESHOLD", 0.02)
    if edge is not None:
        if edge < min_edge:
            reasons.append("insufficient_edge")

    min_ev = getattr(config, "MIN_EV_THRESHOLD", 0.00)
    if ev is not None:
        if ev < min_ev:
            reasons.append("insufficient_ev")

    # Deduplicate reason codes
    seen = set()
    dedup_reasons = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            dedup_reasons.append(r)

    # Determine gate triggers that force a PASS decision
    pass_triggers = {
        "model_error",
        "invalid_probability",
        "insufficient_history",
        "insufficient_data",
        "high_uncertainty",
        "stale_data",
        "insufficient_edge",
        "insufficient_ev",
        "calibration_error",
        "calibration_unavailable",
        "future_odds",
    }
    if require_odds:
        pass_triggers.update({"missing_odds"})

    is_pass = any(r in pass_triggers for r in dedup_reasons)
    decision = "PASS" if is_pass else "SIGNAL"
    gate_status = "BLOCKED" if is_pass else "APPROVED"

    return {
        "decision": decision,
        "gate_status": gate_status,
        "reason_codes": dedup_reasons if decision == "PASS" else [],
    }
