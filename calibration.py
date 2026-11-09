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


from time_utils import parse_utc_datetime, format_utc_iso


def filter_samples_by_cutoff(
    samples: List[Dict[str, Any]],
    cutoff_timestamp: str,
) -> List[Dict[str, Any]]:
    """
    Reject/exclude every sample where sample_timestamp >= cutoff_timestamp.
    Only samples strictly earlier than cutoff_timestamp (sample_timestamp < cutoff_timestamp) are eligible.
    Both timestamps are parsed as timezone-aware UTC datetimes.
    """
    cutoff_dt = parse_utc_datetime(cutoff_timestamp)
    if cutoff_dt is None:
        return []

    eligible = []
    for s in samples:
        if not isinstance(s, dict):
            continue

        ts_val = s.get("timestamp") or s.get("date") or s.get("prediction_timestamp")
        sample_dt = parse_utc_datetime(ts_val)
        if sample_dt is None:
            continue

        if sample_dt < cutoff_dt:
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
            return None

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

    dt_pred = parse_utc_datetime(prediction_timestamp) or parse_utc_datetime(cutoff_timestamp)
    pred_ts = format_utc_iso(dt_pred)
    dt_cutoff = parse_utc_datetime(cutoff_timestamp) or dt_pred
    cutoff_ts = format_utc_iso(dt_cutoff)
    calib_ts = pred_ts or cutoff_ts

    if calibrator is None:
        return {
            "calibrated_markets": {},
            "calibration_metadata": {
                "calibration_version": calibration_version,
                "calibration_method": "NONE",
                "calibration_status": "UNAVAILABLE",
                "calibration_dataset_identity": dataset_identity or "UNKNOWN",
                "calibration_cutoff_timestamp": cutoff_ts,
                "prediction_timestamp": pred_ts,
                "calibration_timestamp": calib_ts,
            },
        }

    calibrated_markets = {}
    method_name = calibrator.__class__.__name__

    try:
        if sport_clean == "football":
            if "match_result" in raw_markets and isinstance(raw_markets["match_result"], dict):
                if hasattr(calibrator, "calibrate_1x2"):
                    calibrated_markets["match_result"] = calibrator.calibrate_1x2(raw_markets["match_result"])
                else:
                    raise ValueError("Calibrator lacks calibrate_1x2 method for football.")

        elif sport_clean == "basketball":
            if "moneyline" in raw_markets and isinstance(raw_markets["moneyline"], dict):
                ml = raw_markets["moneyline"]
                p_home = ml.get("home_win")
                p_away = ml.get("away_win")
                if p_home is None or p_away is None or isinstance(p_home, bool) or isinstance(p_away, bool):
                    raise ValueError("Basketball moneyline missing or invalid home_win/away_win.")
                p_home = float(p_home)
                p_away = float(p_away)
                if not math.isfinite(p_home) or not math.isfinite(p_away):
                    raise ValueError("Basketball moneyline probabilities must be finite.")

                if hasattr(calibrator, "calibrate"):
                    c_home = calibrator.calibrate(p_home)
                    c_away = 1.0 - c_home
                    calibrated_markets["moneyline"] = {"home_win": c_home, "away_win": c_away}
                else:
                    raise ValueError("Calibrator lacks calibrate method for basketball.")

        status = "APPLIED"

    except Exception:
        calibrated_markets = {}
        status = "ERROR"

    return {
        "calibrated_markets": calibrated_markets,
        "calibration_metadata": {
            "calibration_version": calibration_version,
            "calibration_method": method_name if status == "APPLIED" else "NONE",
            "calibration_status": status,
            "calibration_dataset_identity": dataset_identity or "UNKNOWN",
            "calibration_cutoff_timestamp": cutoff_ts,
            "prediction_timestamp": pred_ts,
            "calibration_timestamp": calib_ts if status == "APPLIED" else None,
        },
    }
# ---------------------------------------------------------------------------
# Phase 2: active calibrator loading and controlled promotion
# ---------------------------------------------------------------------------

import datetime as _dt
import math as _math


def _calibrated_probabilities(sample, sport, calibrator):
    """Probabilities actually used for a decision, after calibration if any."""
    raw = sample.get("raw_probabilities") or {}
    if str(sport).lower() == "football":
        market = raw.get("match_result") if isinstance(raw, dict) else None
        if not isinstance(market, dict):
            return None
        probs = {k: market.get(k) for k in ("home_win", "draw", "away_win")}
        if any(not isinstance(v, (int, float)) for v in probs.values()):
            return None
        probs = {k: float(v) for k, v in probs.items()}
        if calibrator is not None:
            try:
                calibrated = calibrator.calibrate_1x2(probs)
                total = sum(float(v) for v in calibrated.values())
                if total > 0:
                    probs = {k: float(v) / total for k, v in calibrated.items()}
            except Exception:
                pass
        return probs

    market = raw.get("moneyline") if isinstance(raw, dict) else None
    if not isinstance(market, dict):
        return None
    p_home = market.get("home_win")
    if not isinstance(p_home, (int, float)) or isinstance(p_home, bool):
        return None
    if calibrator is not None:
        try:
            p_home = float(calibrator.calibrate(float(p_home)))
        except Exception:
            p_home = float(p_home)
    return {"home_win": float(p_home), "away_win": 1.0 - float(p_home)}


def evaluate_predictions(samples, sport, calibrator=None):
    """Brier score, log loss and expected calibration error on graded samples."""
    rows = []
    for sample in samples:
        probs = _calibrated_probabilities(sample, sport, calibrator)
        actual = sample.get("actual")
        if not probs or actual not in probs:
            continue
        rows.append((probs, actual))
    if not rows:
        return {"count": 0, "brier": None, "log_loss": None, "ece": None}

    brier_total = 0.0
    logloss_total = 0.0
    bin_conf = [[] for _ in range(10)]
    bin_hit = [[] for _ in range(10)]

    for probs, actual in rows:
        brier_total += sum((p - (1.0 if key == actual else 0.0)) ** 2 for key, p in probs.items())
        p_actual = max(min(float(probs[actual]), 1.0 - 1e-12), 1e-12)
        logloss_total += -_math.log(p_actual)
        top_key = max(probs, key=probs.get)
        confidence = float(probs[top_key])
        idx = min(9, max(0, int(confidence * 10)))
        bin_conf[idx].append(confidence)
        bin_hit[idx].append(1.0 if top_key == actual else 0.0)

    count = len(rows)
    ece = 0.0
    for idx in range(10):
        if bin_conf[idx]:
            ece += abs(sum(bin_conf[idx]) / len(bin_conf[idx]) - sum(bin_hit[idx]) / len(bin_hit[idx])) * (
                len(bin_conf[idx]) / count
            )

    return {
        "count": count,
        "brier": brier_total / count,
        "log_loss": logloss_total / count,
        "ece": ece,
    }


def load_active_calibrator(sport, league_id=None, season=None, cutoff_timestamp=None):
    """
    Load the promoted calibrator for a sport/competition scope.

    Returns None when nothing has been promoted, so a prediction never claims
    calibration that was not actually applied. The calibrator is rebuilt
    deterministically from verified, graded samples strictly before the cutoff.
    """
    try:
        import lifecycle_storage
    except Exception:
        return None

    scopes = ["all"]
    if league_id is not None:
        scopes.insert(0, f"league_{league_id}")

    active = None
    for scope in scopes:
        active = lifecycle_storage.get_active_calibration(str(sport).lower(), scope)
        if active:
            break
    if not active or not cutoff_timestamp:
        return None

    # Rebuild from the version's own training cutoff, not from all outcomes
    # available at the current prediction time.
    training_cutoff = active.get("training_cutoff") or cutoff_timestamp
    samples = lifecycle_storage.get_graded_samples(str(sport).lower(), before=training_cutoff)
    calibrator = train_walk_forward_calibrator(samples, training_cutoff, sport=str(sport).lower())
    if calibrator is not None:
        try:
            setattr(calibrator, "calibration_version", active.get("calibration_version"))
        except Exception:
            pass
    return calibrator


def rollback_calibration(sport, scope, reason, cutoff_timestamp=None):
    """Restore the previous active calibration version after a regression."""
    import lifecycle_storage

    current = lifecycle_storage.get_active_calibration(sport, scope)
    if not current:
        return {"status": "no_active_version"}
    previous_version = current.get("rollback_reference")
    if not previous_version:
        return {"status": "no_rollback_target"}
    previous = lifecycle_storage.get_calibration_version(sport, scope, previous_version)
    if not previous:
        return {"status": "rollback_target_missing"}
    lifecycle_storage.save_calibration_version({**current, "status": "retired"})
    lifecycle_storage.save_calibration_version({**previous, "status": "active"})
    return {"status": "rolled_back", "restored": previous_version, "reason": reason}


def evaluate_and_promote(sport="football", cutoff_timestamp=None, scope="all", min_train=20, min_eval=10):
    """
    Walk-forward candidate evaluation with controlled promotion and rollback.

    A candidate is promoted only when it improves Brier score against the
    active baseline on strictly later, verified outcomes and clears the minimum
    evaluation sample threshold. Every decision is persisted with its metrics
    and the previous version is retained as the rollback reference.
    """
    import lifecycle_storage

    cutoff = cutoff_timestamp or format_utc_iso(_dt.datetime.now(_dt.timezone.utc))
    samples = lifecycle_storage.get_graded_samples(str(sport).lower(), before=cutoff)

    if len(samples) < min_train + min_eval:
        record = lifecycle_storage.save_calibration_version(
            {
                "sport": sport,
                "competition_scope": scope,
                "model_version": getattr(config, "MODEL_VERSION", None),
                "calibration_version": f"{sport}-cal-{cutoff[:10]}-insufficient",
                "training_cutoff": cutoff,
                "training_sample_count": len(samples),
                "evaluation_metrics": {"reason": "insufficient_verified_samples", "count": len(samples)},
                "status": "rejected",
                "dataset_identity": f"{sport}_graded_snapshots",
            }
        )
        return {
            "sport": sport,
            "status": "rejected",
            "reason": "insufficient_verified_samples",
            "count": len(samples),
            "version": record.get("calibration_version"),
        }

    split = max(min_train, int(len(samples) * 0.8))
    train, test = samples[:split], samples[split:]
    train_cutoff = train[-1]["timestamp"]
    candidate = train_walk_forward_calibrator(train, train_cutoff, sport=sport)

    if candidate is None:
        record = lifecycle_storage.save_calibration_version(
            {
                "sport": sport,
                "competition_scope": scope,
                "model_version": getattr(config, "MODEL_VERSION", None),
                "calibration_version": f"{sport}-cal-{cutoff[:10]}-untrainable",
                "training_cutoff": train_cutoff,
                "training_sample_count": len(train),
                "evaluation_metrics": {"reason": "calibrator_not_trainable"},
                "status": "rejected",
                "dataset_identity": f"{sport}_graded_snapshots",
            }
        )
        return {
            "sport": sport,
            "status": "rejected",
            "reason": "calibrator_not_trainable",
            "version": record.get("calibration_version"),
        }

    baseline_metrics = evaluate_predictions(test, sport, None)
    candidate_metrics = evaluate_predictions(test, sport, candidate)

    # Candidate and active must be compared on the same evaluation samples.
    active = lifecycle_storage.get_active_calibration(sport, scope)
    active_calibrator = None
    if active and active.get("training_cutoff"):
        prior = lifecycle_storage.get_graded_samples(str(sport).lower(), before=active["training_cutoff"])
        active_calibrator = train_walk_forward_calibrator(prior, active["training_cutoff"], sport=str(sport).lower())

    active_metrics = (
        evaluate_predictions(test, sport, active_calibrator)
        if active_calibrator is not None
        else (baseline_metrics if active else None)
    )
    active_brier = active_metrics.get("brier") if isinstance(active_metrics, dict) else None
    baseline_brier = active_brier if isinstance(active_brier, (int, float)) else baseline_metrics.get("brier")

    promote = (
        isinstance(candidate_metrics.get("brier"), float)
        and candidate_metrics.get("count", 0) >= min_eval
        and isinstance(baseline_brier, float)
        and candidate_metrics["brier"] <= baseline_brier - 1e-4
    )

    if promote and active and active.get("calibration_version") != version:
        # One active version per scope.
        lifecycle_storage.save_calibration_version({**active, "status": "retired"})

    version = f"{sport}-cal-{train_cutoff[:10]}-n{len(train)}"
    lifecycle_storage.save_calibration_version(
        {
            "sport": sport,
            "competition_scope": scope,
            "model_version": getattr(config, "MODEL_VERSION", None),
            "calibration_version": version,
            "training_cutoff": train_cutoff,
            "training_sample_count": len(train),
            "evaluation_metrics": {
                **candidate_metrics,
                "baseline_brier": baseline_brier,
                "raw_brier": baseline_metrics.get("brier"),
                "evaluated_at": cutoff,
            },
            "status": "active" if promote else "candidate",
            "promoted_at": cutoff if promote else None,
            "rollback_reference": (active or {}).get("calibration_version") if promote else None,
            "dataset_identity": f"{sport}_graded_snapshots",
        }
    )

    if not promote and active and isinstance(active_brier, float) and isinstance(active_metrics, dict):
        if isinstance(active_metrics.get("brier"), float) and active_metrics["brier"] > active_brier * 1.10 + 0.01:
            rollback = rollback_calibration(sport, scope, "active_version_degraded", cutoff)
            return {"sport": sport, "status": "rolled_back", "version": version, "rollback": rollback}

    return {
        "sport": sport,
        "status": "promoted" if promote else "candidate_kept",
        "version": version,
        "metrics": candidate_metrics,
        "baseline_brier": baseline_brier,
    }
