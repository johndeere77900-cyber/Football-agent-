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
        if not isinstance(raw_1x2, dict):
            raise ValueError("raw_1x2 must be a dictionary.")

        if "home_win" not in raw_1x2 or "draw" not in raw_1x2 or "away_win" not in raw_1x2:
            raise ValueError("Missing required 1X2 keys (home_win, draw, away_win) in raw_1x2.")

        p_home = raw_1x2["home_win"]
        p_draw = raw_1x2["draw"]
        p_away = raw_1x2["away_win"]

        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) for v in (p_home, p_draw, p_away)):
            raise ValueError("Invalid non-numeric or boolean value in raw_1x2.")

        c_home = self.home_calibrator.calibrate(float(p_home))
        c_draw = self.draw_calibrator.calibrate(float(p_draw))
        c_away = self.away_calibrator.calibrate(float(p_away))

        total = c_home + c_draw + c_away
        if total <= 0:
            raise ValueError("Calibrated 1X2 probabilities summed to non-positive total.")

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


def filter_samples_by_cutoff(
    samples: List[Dict[str, Any]],
    cutoff_timestamp: str,
) -> List[Dict[str, Any]]:
    """
    Reject/exclude every sample where sample_timestamp >= cutoff_timestamp.
    Only samples strictly earlier than cutoff_timestamp (sample_timestamp < cutoff_timestamp) are eligible.
    """
    if not cutoff_timestamp:
        return []

    eligible = []
    for s in samples:
        if not isinstance(s, dict):
            continue

        ts = str(s.get("timestamp") or s.get("date") or "")
        if not ts:
            continue

        if ts < str(cutoff_timestamp):
            eligible.append(s)

    return eligible


def train_walk_forward_calibrator(
    prior_prediction_samples: List[Dict[str, Any]],
    cutoff_timestamp: str,
    sport: str = "football",
) -> Optional[Any]:
    """
    Train a walk-forward calibrator using only eligible prior samples strictly before cutoff_timestamp.
    """
    eligible = filter_samples_by_cutoff(prior_prediction_samples, cutoff_timestamp)
    if len(eligible) < 20:
        return None

    sport_clean = str(sport).lower()

    if sport_clean == "football":
        home_samples = []
        draw_samples = []
        away_samples = []

        for s in eligible:
            raw_m = s.get("raw_probabilities") or s.get("probabilities") or {}
            mr = raw_m.get("match_result", {}) if isinstance(raw_m, dict) else {}
            act = s.get("actual") or s.get("actual_outcome")

            if act in ("home_win", "draw", "away_win") and isinstance(mr, dict):
                p_h = mr.get("home_win")
                p_d = mr.get("draw")
                p_a = mr.get("away_win")

                if all(v is not None for v in (p_h, p_d, p_a)):
                    home_samples.append((p_h, 1 if act == "home_win" else 0))
                    draw_samples.append((p_d, 1 if act == "draw" else 0))
                    away_samples.append((p_a, 1 if act == "away_win" else 0))

        if len(home_samples) < 20:
            return None

        cal_h = fit_platt_scaling(home_samples)
        cal_d = fit_platt_scaling(draw_samples)
        cal_a = fit_platt_scaling(away_samples)

        if cal_h is None or cal_d is None or cal_a is None:
            # Fall back to default PlattCalibrators if fitting doesn't converge or has 0 variance
            cal_h = cal_h or PlattCalibrator(1.0, 0.0)
            cal_d = cal_d or PlattCalibrator(1.0, 0.0)
            cal_a = cal_a or PlattCalibrator(1.0, 0.0)

        return MulticlassPlattCalibrator(cal_h, cal_d, cal_a)

    elif sport_clean == "basketball":
        ml_samples = []
        for s in eligible:
            raw_m = s.get("raw_probabilities") or s.get("probabilities") or {}
            ml = raw_m.get("moneyline", {}) if isinstance(raw_m, dict) else {}
            act = s.get("actual") or s.get("actual_outcome")

            if act in ("home_win", "away_win") and isinstance(ml, dict):
                p_h = ml.get("home_win")
                if p_h is not None:
                    ml_samples.append((p_h, 1 if act == "home_win" else 0))

        if len(ml_samples) < 20:
            return None

        return fit_platt_scaling(ml_samples)

    return None


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
