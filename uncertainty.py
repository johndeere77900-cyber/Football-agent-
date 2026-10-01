"""
Uncertainty estimation layer for Football and Basketball predictions.

Calculates deterministic uncertainty metrics and classifies prediction state into:
- sufficient_data
- limited_data
- high_uncertainty
- insufficient_data
"""

from typing import Any, Dict, Optional
import config


def calculate_uncertainty(
    feature_coverage: Optional[float],
    sample_count: int,
    top_probability: float,
    calibration_status: str = "UNAVAILABLE",
    odds_status: str = "MISSING",
    is_live: bool = False,
) -> Dict[str, Any]:
    """
    Deterministically assess prediction uncertainty based on feature coverage,
    sample size, probability concentration, and data freshness.
    """
    cov = max(0.0, min(1.0, float(feature_coverage))) if feature_coverage is not None else None
    samples = int(sample_count) if sample_count is not None else 0
    top_p = max(0.0, min(1.0, float(top_probability))) if top_probability is not None else 0.0

    min_sample = getattr(config, "MIN_HISTORICAL_SAMPLE", 5)
    min_cov = getattr(config, "MIN_FEATURE_COVERAGE", 0.50)

    if feature_coverage is None or samples < min_sample or (cov is not None and cov < 0.20):
        state = "insufficient_data"
    elif (cov is not None and cov < min_cov) or samples < 8:
        state = "limited_data"
    elif top_p < 0.40 or (is_live and samples < 10):
        state = "high_uncertainty"
    else:
        state = "sufficient_data"

    concentration = top_p

    return {
        "state": state,
        "feature_coverage": cov if cov is not None else 0.0,
        "historical_sample_count": samples,
        "probability_concentration": concentration,
        "calibration_available": (calibration_status == "APPLIED"),
        "odds_available": (odds_status == "AVAILABLE"),
        "is_live": is_live,
    }
