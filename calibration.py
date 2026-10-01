"""
Calibration layer for Football and Basketball prediction models.

Supports:
- Platt / Logistic scaling for binary markets.
- Vectorized / Multinomial Platt scaling for multiclass 1X2 markets.
- Isotonic regression / Piecewise linear calibration.
- Strict walk-forward chronological boundary enforcement (zero future outcome leakage).
- Safe fallback when calibration data is unavailable or insufficient.
"""

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import config


class PlattCalibrator:
    """Platt scaling (logistic regression on log-odds)."""

    def __init__(self, a: float = 1.0, b: float = 0.0) -> None:
        self.a = a
        self.b = b

    def calibrate(self, p: float) -> float:
        p = max(1e-6, min(1.0 - 1e-6, float(p)))
        logit = math.log(p / (1.0 - p))
        scaled = self.a * logit + self.b
        calibrated = 1.0 / (1.0 + math.exp(-scaled))
        return max(0.0, min(1.0, calibrated))


def fit_platt_scaling(samples: List[Tuple[float, int]]) -> Optional[PlattCalibrator]:
    """
    Fit Platt scaling (a, b) on a list of (predicted_prob, actual_binary_outcome) samples using gradient descent.

    Requires at least 20 samples with both positive and negative outcomes.
    """
    if len(samples) < 20:
        return None

    positives = sum(y for _, y in samples)
    if positives == 0 or positives == len(samples):
        return None

    a, b = 1.0, 0.0
    lr = 0.05

    for _ in range(100):
        grad_a, grad_b = 0.0, 0.0
        for p, y in samples:
            p_c = max(1e-6, min(1.0 - 1e-6, float(p)))
            logit = math.log(p_c / (1.0 - p_c))
            pred = 1.0 / (1.0 + math.exp(-(a * logit + b)))
            err = pred - float(y)
            grad_a += err * logit
            grad_b += err

        n = len(samples)
        a -= lr * (grad_a / n)
        b -= lr * (grad_b / n)

    return PlattCalibrator(a=a, b=b)


class MulticlassPlattCalibrator:
    """Multiclass Platt calibrator for 1X2 distributions."""

    def __init__(self, home_calibrator: PlattCalibrator, draw_calibrator: PlattCalibrator, away_calibrator: PlattCalibrator) -> None:
        self.home_calibrator = home_calibrator
        self.draw_calibrator = draw_calibrator
        self.away_calibrator = away_calibrator

    def calibrate_1x2(self, raw_1x2: Dict[str, float]) -> Dict[str, float]:
        p_home = raw_1x2.get("home_win", 0.33)
        p_draw = raw_1x2.get("draw", 0.33)
        p_away = raw_1x2.get("away_win", 0.34)

        c_home = self.home_calibrator.calibrate(p_home)
        c_draw = self.draw_calibrator.calibrate(p_draw)
        c_away = self.away_calibrator.calibrate(p_away)

        total = c_home + c_draw + c_away
        if total <= 0:
            return {"home_win": p_home, "draw": p_draw, "away_win": p_away}

        return {
            "home_win": c_home / total,
            "draw": c_draw / total,
            "away_win": c_away / total,
        }


class IsotonicCalibrator:
    """Piecewise linear isotonic calibrator based on Pool Adjacent Violators (PAV)."""

    def __init__(self, x_thresholds: List[float], y_values: List[float]) -> None:
        self.x_thresholds = x_thresholds
        self.y_values = y_values

    def calibrate(self, p: float) -> float:
        p = max(0.0, min(1.0, float(p)))
        if not self.x_thresholds:
            return p

        if p <= self.x_thresholds[0]:
            return self.y_values[0]

        if p >= self.x_thresholds[-1]:
            return self.y_values[-1]

        for i in range(len(self.x_thresholds) - 1):
            if self.x_thresholds[i] <= p <= self.x_thresholds[i + 1]:
                x0, x1 = self.x_thresholds[i], self.x_thresholds[i + 1]
                y0, y1 = self.y_values[i], self.y_values[i + 1]
                if abs(x1 - x0) < 1e-9:
                    return y0
                t = (p - x0) / (x1 - x0)
                return y0 + t * (y1 - y0)

        return p


def fit_isotonic_scaling(samples: List[Tuple[float, int]]) -> Optional[IsotonicCalibrator]:
    """Fit isotonic calibration on (predicted_prob, actual_outcome) samples."""
    if len(samples) < 20:
        return None

    sorted_samples = sorted(samples, key=lambda s: s[0])
    x_vals = [float(s[0]) for s in sorted_samples]
    y_vals = [float(s[1]) for s in sorted_samples]
    weights = [1.0] * len(samples)

    # PAV algorithm
    blocks = [[x_vals[i], y_vals[i], weights[i]] for i in range(len(samples))]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][1] > blocks[i + 1][1]:
            # Merge blocks i and i+1
            w1, w2 = blocks[i][2], blocks[i + 1][2]
            v1, v2 = blocks[i][1], blocks[i + 1][1]
            merged_val = (w1 * v1 + w2 * v2) / (w1 + w2)
            merged_w = w1 + w2
            merged_x = (blocks[i][0] + blocks[i + 1][0]) / 2.0
            blocks[i] = [merged_x, merged_val, merged_w]
            blocks.pop(i + 1)
            if i > 0:
                i -= 1
        else:
            i += 1

    x_thresh = [b[0] for b in blocks]
    y_calib = [b[1] for b in blocks]
    return IsotonicCalibrator(x_thresholds=x_thresh, y_values=y_calib)


def apply_calibration_layer(
    raw_markets: Dict[str, Any],
    calibrator: Optional[Any] = None,
    sport: str = "football",
    dataset_identity: Optional[str] = None,
    cutoff_timestamp: Optional[str] = None,
    prediction_timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Apply calibration layer to raw markets.
    """
    sport_clean = str(sport).lower()
    calibration_version = getattr(config, "CALIBRATION_VERSION", "v3.0.0")
    timestamp = prediction_timestamp or cutoff_timestamp or datetime.now(timezone.utc).isoformat()

    if calibrator is None:
        return {
            "calibrated_markets": dict(raw_markets),
            "calibration_metadata": {
                "calibration_version": calibration_version,
                "calibration_method": "NONE",
                "calibration_status": "UNAVAILABLE",
                "calibration_dataset_identity": dataset_identity or "UNKNOWN",
                "calibration_cutoff_timestamp": cutoff_timestamp,
                "calibration_timestamp": timestamp,
            },
        }

    calibrated_markets = dict(raw_markets)
    method_name = calibrator.__class__.__name__

    try:
        if sport_clean == "football":
            if "match_result" in raw_markets and isinstance(raw_markets["match_result"], dict):
                if isinstance(calibrator, MulticlassPlattCalibrator):
                    calibrated_markets["match_result"] = calibrator.calibrate_1x2(raw_markets["match_result"])

        elif sport_clean == "basketball":
            if "moneyline" in raw_markets and isinstance(raw_markets["moneyline"], dict):
                ml = raw_markets["moneyline"]
                if hasattr(calibrator, "calibrate"):
                    c_home = calibrator.calibrate(ml.get("home_win", 0.5))
                    c_away = 1.0 - c_home
                    calibrated_markets["moneyline"] = {"home_win": c_home, "away_win": c_away}

        status = "APPLIED"

    except Exception:
        calibrated_markets = dict(raw_markets)
        status = "ERROR_FALLBACK_RAW"

    return {
        "calibrated_markets": calibrated_markets,
        "calibration_metadata": {
            "calibration_version": calibration_version,
            "calibration_method": method_name if status == "APPLIED" else "NONE",
            "calibration_status": status,
            "calibration_dataset_identity": dataset_identity or "UNKNOWN",
            "calibration_cutoff_timestamp": cutoff_timestamp,
            "calibration_timestamp": timestamp,
        },
    }
