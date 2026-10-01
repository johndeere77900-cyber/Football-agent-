"""Football Agent historical backtest engine.

This file is the backtest integration layer only.  It uses the repository's
existing API-Football fixture source, leakage-safe historical feature modules,
chronological Elo reconstruction, shared prediction engine, and pure market
grading functions.

No missing outcome, odds, corner, or card value is fabricated.
"""

from __future__ import annotations

import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import api_football
import basketball_model
import calibration
import config
import historical_basketball_features
import historical_elo
import historical_features
import historical_h2h
import historical_match_policy
import market_grading
import prediction_engine
import storage


BASE_DIR = Path(__file__).resolve().parent
BACKTEST_LOG_DIR = BASE_DIR / "data" / "backtests"
BACKTEST_LOG_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Legacy helper-name compatibility
# ---------------------------------------------------------------------------
#
# These names intentionally have no production role.
# Older tests verify that main.predict_fixture() does NOT use the historical
# backtest prediction implementation. The names therefore remain available
# only so those tests can monkeypatch them and fail loudly if production ever
# starts calling them again.
#
# Do not implement prediction logic here.
# ---------------------------------------------------------------------------


def estimate_expected_goals_from_stats(
    *args: Any,
    **kwargs: Any,
) -> None:
    raise RuntimeError(
        "Legacy backtest prediction helper is disabled. "
        "Use prediction_engine.predict_from_features() "
        "or predict_historical_fixture()."
    )


def estimate_recent_form_goals(
    *args: Any,
    **kwargs: Any,
) -> None:
    raise RuntimeError(
        "Legacy backtest prediction helper is disabled. "
        "Use prediction_engine.predict_from_features() "
        "or predict_historical_fixture()."
    )


def estimate_head_to_head_goals(
    *args: Any,
    **kwargs: Any,
) -> None:
    raise RuntimeError(
        "Legacy backtest prediction helper is disabled. "
        "Use prediction_engine.predict_from_features() "
        "or predict_historical_fixture()."
    )


def blend_three(
    *args: Any,
    **kwargs: Any,
) -> None:
    raise RuntimeError(
        "Legacy backtest prediction helper is disabled. "
        "Use prediction_engine.predict_from_features() "
        "or predict_historical_fixture()."
    )


# ---------------------------------------------------------------------------
# Basic validation / extraction
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(result):
        return None

    return result


def _valid_goal(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    )


def _fixture_date(
    fixture: Dict[str, Any],
) -> str:
    return str(
        fixture.get("fixture", {}).get("date", "")
    )


def _fixture_id(
    fixture: Dict[str, Any],
) -> Optional[Any]:
    """
    Return the fixture ID without coercing it to int.

    Production API-Football fixtures normally use integer IDs, while
    backtest/integration fixtures may deliberately use opaque string IDs.
    Both are valid at this integration layer.
    """
    return fixture.get("fixture", {}).get("id")


def _fixture_lookup_key(
    value: Any,
) -> str:
    """
    Produce a stable lookup key for numeric or opaque fixture IDs.

    This intentionally uses str(value) rather than int(value), because a
    backtest fixture ID may be an opaque identifier such as:

        2-4-2025-01-12T15:00:00+00:00
    """
    return str(value)


def _home_id(
    fixture: Dict[str, Any],
) -> Optional[Any]:
    return fixture.get("teams", {}).get("home", {}).get("id")


def _away_id(
    fixture: Dict[str, Any],
) -> Optional[Any]:
    return fixture.get("teams", {}).get("away", {}).get("id")


def _home_name(
    fixture: Dict[str, Any],
) -> str:
    return str(
        fixture.get("teams", {})
        .get("home", {})
        .get("name", "Unknown Home")
    )


def _away_name(
    fixture: Dict[str, Any],
) -> str:
    return str(
        fixture.get("teams", {})
        .get("away", {})
        .get("name", "Unknown Away")
    )


def _goals(
    fixture: Dict[str, Any],
) -> Tuple[Optional[int], Optional[int]]:
    goals = historical_match_policy.get_football_match_goals(fixture)
    if goals is None:
        return None, None
    return goals


def _is_finished(
    fixture: Dict[str, Any],
) -> bool:
    return historical_match_policy.is_finished_match(fixture, sport="football")


def _fixture_is_gradeable(
    fixture: Dict[str, Any],
) -> bool:
    home, away = _goals(fixture)

    return (
        home is not None
        and away is not None
    )


def _normalise_probability_distribution(
    value: Any,
) -> Dict[str, float]:
    if not isinstance(value, dict):
        return {}

    result: Dict[str, float] = {}

    for key, probability in value.items():
        number = _safe_float(probability)

        if number is not None:
            result[str(key)] = number

    return result


def _probability_pick(
    distribution: Any,
) -> Optional[Tuple[str, float]]:
    cleaned = _normalise_probability_distribution(
        distribution
    )

    if not cleaned:
        return None

    key = max(
        cleaned,
        key=cleaned.get,
    )

    return key, cleaned[key]


# ---------------------------------------------------------------------------
# Leakage-safe compatibility helpers used by tests and callers
# ---------------------------------------------------------------------------


import time_utils


def _compute_stats_as_of(
    fixtures: Sequence[Dict[str, Any]],
    team_id: Any,
    cutoff: str,
) -> Tuple[Optional[float], Optional[float]]:
    """Return average goals-for/goals-against known strictly before cutoff."""
    goals_for: List[float] = []
    goals_against: List[float] = []

    for fixture in fixtures:
        if not _is_finished(fixture):
            continue

        if not time_utils.is_strictly_before(_fixture_date(fixture), cutoff):
            continue

        home_id = _home_id(fixture)
        away_id = _away_id(fixture)

        home_goals, away_goals = _goals(fixture)

        if home_goals is None or away_goals is None:
            continue

        if team_id == home_id:
            goals_for.append(home_goals)
            goals_against.append(away_goals)

        elif team_id == away_id:
            goals_for.append(away_goals)
            goals_against.append(home_goals)

    if not goals_for:
        return None, None

    return (
        sum(goals_for) / len(goals_for),
        sum(goals_against) / len(goals_against),
    )


def _sample_backtest_candidates(
    candidates: Sequence[Any],
    sample_size: int,
    seed: Optional[int] = 42,
) -> List[Any]:
    """Sample without mutating the input or global random state."""
    if (
        isinstance(sample_size, bool)
        or not isinstance(sample_size, int)
        or sample_size <= 0
    ):
        raise ValueError(
            "sample_size must be a positive integer."
        )

    original = list(candidates)

    if sample_size >= len(original):
        return original

    rng = random.Random(seed)

    return rng.sample(
        original,
        sample_size,
    )


def _filter_candidates_by_minimum_history(
    finished_fixtures: Sequence[Dict[str, Any]],
    all_fixtures: Sequence[Dict[str, Any]],
    minimum_matches: int = 5,
) -> List[Dict[str, Any]]:
    """Keep completed fixtures whose two teams each have enough prior history."""
    if (
        isinstance(minimum_matches, bool)
        or not isinstance(minimum_matches, int)
        or minimum_matches < 0
    ):
        raise ValueError(
            "minimum_matches must be a non-negative integer."
        )

    ordered = sorted(
        [
            fixture
            for fixture in finished_fixtures
            if _fixture_is_gradeable(fixture)
        ],
        key=lambda f: time_utils.parse_utc_datetime(_fixture_date(f)) or datetime.min.replace(tzinfo=timezone.utc),
    )

    result: List[Dict[str, Any]] = []

    for fixture in ordered:
        home_id = _home_id(fixture)
        away_id = _away_id(fixture)
        cutoff = _fixture_date(fixture)

        if home_id is None or away_id is None:
            continue

        if historical_features.fixture_has_minimum_history(
            all_fixtures,
            home_id,
            away_id,
            cutoff,
            minimum_matches,
        ):
            result.append(fixture)

    return result


# ---------------------------------------------------------------------------
# Historical prediction path
# ---------------------------------------------------------------------------


def _historical_prediction_for_fixture(
    fixtures: Sequence[Dict[str, Any]],
    fixture: Dict[str, Any],
    min_prior_matches: int = 5,
    calibrator: Optional[Any] = None,
    league_id: Optional[int] = None,
    season: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Build every prediction input strictly from information before cutoff."""
    if not _fixture_is_gradeable(fixture):
        return None

    if (
        isinstance(min_prior_matches, bool)
        or not isinstance(min_prior_matches, int)
        or min_prior_matches < 0
    ):
        raise ValueError(
            "min_prior_matches must be a non-negative integer."
        )

    home_id = _home_id(fixture)
    away_id = _away_id(fixture)
    cutoff = _fixture_date(fixture)

    if (
        home_id is None
        or away_id is None
        or not cutoff
    ):
        return None

    historical_snapshot = (
        historical_features.historical_feature_snapshot(
            fixtures,
            home_id,
            away_id,
            cutoff,
            minimum_matches=min_prior_matches,
        )
    )

    recent_snapshot = (
        historical_features.fixture_recent_form(
            fixtures,
            home_id,
            away_id,
            cutoff,
            window=config.RECENT_FORM_MATCHES,
            minimum_matches=min_prior_matches,
        )
    )

    if (
        historical_snapshot is None
        or recent_snapshot is None
    ):
        return None

    league_avg_goals = (
        historical_features.historical_league_avg_goals(
            fixtures,
            cutoff,
        )
    )

    if (
        league_avg_goals is None
        or league_avg_goals <= 0
    ):
        return None

    h2h_snapshot = (
        historical_h2h.historical_h2h_snapshot(
            fixtures,
            home_id,
            away_id,
            cutoff,
            window=config.HEAD_TO_HEAD_MATCHES,
            minimum_matches=0,
        )
    )

    elo_snapshot = (
        historical_elo.fixture_elo_snapshot(
            fixtures,
            home_id,
            away_id,
            cutoff,
        )
    )

    # This remains the single authoritative historical prediction path.
    prediction = (
        prediction_engine.predict_historical_fixture(
            historical_snapshot=historical_snapshot,
            recent_snapshot=recent_snapshot,
            h2h_snapshot=h2h_snapshot,
            league_avg_goals=league_avg_goals,
            home_elo=elo_snapshot["home_rating"],
            away_elo=elo_snapshot["away_rating"],
            calibrator=calibrator,
            data_cutoff_timestamp=cutoff,
            fixture_id=_fixture_id(fixture),
            league_id=league_id,
            season=season,
        )
    )

    return {
        "prediction": prediction,
        "historical_snapshot": historical_snapshot,
        "recent_snapshot": recent_snapshot,
        "h2h_snapshot": h2h_snapshot,
        "elo_snapshot": elo_snapshot,
        "league_avg_goals": league_avg_goals,
    }


# ---------------------------------------------------------------------------
# Actual outcomes and market grading
# ---------------------------------------------------------------------------


def _actual_match_result(
    fixture: Dict[str, Any],
) -> Optional[str]:
    return historical_match_policy.get_1x2_regulation_outcome(fixture)


def _actual_double_chance(
    fixture: Dict[str, Any],
) -> Optional[str]:
    dc_graded = market_grading.grade_double_chance(
        *(historical_match_policy.get_regulation_goals(fixture) or (None, None))
    )
    if not dc_graded:
        return None
    if dc_graded.get("home_or_draw", {}).get("won") and dc_graded.get("away_or_draw", {}).get("won"):
        return "home_or_draw"
    if dc_graded.get("home_or_draw", {}).get("won"):
        return "home_or_draw"
    if dc_graded.get("away_or_draw", {}).get("won"):
        return "away_or_draw"
    return None


def _actual_btts(
    fixture: Dict[str, Any],
) -> Optional[str]:
    match_goals = historical_match_policy.get_totals_and_btts_goals(fixture)
    if match_goals is None:
        return None
    btts_graded = market_grading.grade_btts(*match_goals)
    if not btts_graded:
        return None
    return "yes" if btts_graded.get("yes", {}).get("won") else "no"


def _actual_binary_total(
    total: Optional[float],
    line: Any,
) -> Optional[str]:
    threshold = _safe_float(line)

    if total is None or threshold is None:
        return None

    if total > threshold:
        return "over"

    if total < threshold:
        return "under"

    return "push"


def _pick_and_grade(
    distribution: Any,
    actual: Optional[str],
) -> Optional[Dict[str, Any]]:
    picked = _probability_pick(distribution)

    if picked is None:
        return None

    pick, probability = picked

    return {
        "pick": pick,
        "probability": probability,
        "actual": actual,
        "won": (
            pick == actual
            if actual is not None
            else None
        ),
    }


def _parse_goal_market_key(
    key: Any,
) -> Optional[Tuple[str, float, str]]:
    """
    Parse a team-goals market key.

    Supported forms:
        home_over_0_5
        home_under_0_5
        away_over_1_5
        away_under_2_5

    Returns:
        (team, threshold, original_key)
    """
    key_text = str(key)

    if "_" not in key_text:
        return None

    team, market = key_text.split(
        "_",
        1,
    )

    if team not in {"home", "away"}:
        return None

    market_parts = market.split("_")

    if len(market_parts) != 3:
        return None

    direction = market_parts[0]

    if direction not in {"over", "under"}:
        return None

    whole = market_parts[1]
    decimal = market_parts[2]

    if not (
        whole.isdigit()
        and decimal.isdigit()
    ):
        return None

    try:
        threshold = float(
            f"{whole}.{decimal}"
        )
    except ValueError:
        return None

    return (
        team,
        threshold,
        key_text,
)

def _grade_prediction_markets(
    prediction_markets: Dict[str, Any],
    fixture: Dict[str, Any],
) -> Dict[str, Any]:
    """Return selected predictions plus independent actual market outcomes from central market policy."""
    selected: Dict[str, Any] = {}

    graded_record = market_grading.grade_fixture_markets(fixture)
    goal_outcomes = graded_record.get("goal_markets", {})

    # 1X2
    actual_1x2 = goal_outcomes.get("match_result", {}).get("outcome") if isinstance(goal_outcomes.get("match_result"), dict) else None
    selected_1x2 = _pick_and_grade(
        prediction_markets.get("match_result"),
        actual_1x2,
    )
    if selected_1x2 is not None:
        selected["match_result"] = selected_1x2

    # Double Chance
    dc_outcomes = goal_outcomes.get("double_chance")
    dc_actual = None
    if isinstance(dc_outcomes, dict):
        if dc_outcomes.get("home_or_draw", {}).get("won") and dc_outcomes.get("away_or_draw", {}).get("won"):
            dc_actual = "home_or_draw"
        elif dc_outcomes.get("home_or_draw", {}).get("won"):
            dc_actual = "home_or_draw"
        elif dc_outcomes.get("away_or_draw", {}).get("won"):
            dc_actual = "away_or_draw"

    selected_dc = _pick_and_grade(
        prediction_markets.get("double_chance"),
        dc_actual,
    )
    if selected_dc is not None:
        selected["double_chance"] = selected_dc

    # BTTS
    btts_outcomes = goal_outcomes.get("btts")
    btts_actual = None
    if isinstance(btts_outcomes, dict):
        btts_actual = "yes" if btts_outcomes.get("yes", {}).get("won") else "no"

    selected_btts = _pick_and_grade(
        prediction_markets.get("btts"),
        btts_actual,
    )
    if selected_btts is not None:
        selected["btts"] = selected_btts

    # Over / Under
    over_under_selected: Dict[str, Any] = {}
    over_under = prediction_markets.get("over_under", {})
    ou_outcomes = goal_outcomes.get("over_under", {})

    if isinstance(over_under, dict) and isinstance(ou_outcomes, dict):
        lines: Dict[str, Tuple[float, Dict[str, float]]] = {}
        for key, value in over_under.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            key_text = str(key)
            if not (key_text.startswith("over_") or key_text.startswith("under_")):
                continue
            parts = key_text.split("_", 1)
            if len(parts) != 2:
                continue
            try:
                line = float(parts[1].replace("_", "."))
            except ValueError:
                continue
            lines.setdefault(str(line), (line, {}))[1][key_text] = float(value)

        for line_key, (line, distribution) in lines.items():
            key_suffix = str(line).replace(".", "_")
            over_item = ou_outcomes.get(f"over_{key_suffix}")
            if isinstance(over_item, dict):
                actual = over_item.get("outcome")
                actual_key = f"{actual}_{key_suffix}" if actual in {"over", "under"} else None
                picked = _pick_and_grade(distribution, actual_key)
                if picked is not None:
                    picked["actual"] = actual
                    over_under_selected[line_key] = picked

    if over_under_selected:
        selected["over_under"] = over_under_selected

    # Team goals
    team_selected: Dict[str, Any] = {}
    team_goals = prediction_markets.get("team_goals", {})
    tg_outcomes = goal_outcomes.get("team_goals", {})

    if isinstance(team_goals, dict) and isinstance(tg_outcomes, dict):
        lines: Dict[str, Tuple[float, Dict[str, float]]] = {}
        for key, value in team_goals.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            parsed = _parse_goal_market_key(key)
            if parsed is None:
                continue
            team, line, original_key = parsed
            line_key = f"{team}_{str(line).replace('.', '_')}"
            lines.setdefault(line_key, (line, {}))[1][original_key] = float(value)

        for line_key, (line, distribution) in lines.items():
            team = line_key.split("_", 1)[0]
            key_suffix = str(line).replace(".", "_")
            item = tg_outcomes.get(f"{team}_over_{key_suffix}")
            if isinstance(item, dict):
                actual = item.get("outcome")
                actual_key = f"{team}_{actual}_{key_suffix}" if actual in {"over", "under"} else None
                picked = _pick_and_grade(distribution, actual_key)
                if picked is not None:
                    picked["actual"] = actual
                    team_selected[line_key] = picked

    if team_selected:
        selected["team_goals"] = team_selected

    # Scoreline
    scorelines = prediction_markets.get("top_scorelines") or prediction_markets.get("scoreline")
    actual_score = goal_outcomes.get("scoreline", {}).get("outcome") if isinstance(goal_outcomes.get("scoreline"), dict) else None

    if isinstance(scorelines, list) and scorelines:
        clean_scores = {
            str(item.get("score")): _safe_float(item.get("probability"))
            for item in scorelines
            if isinstance(item, dict) and item.get("score") is not None and _safe_float(item.get("probability")) is not None
        }
        picked = _pick_and_grade(clean_scores, actual_score)
        if picked is not None:
            selected["scoreline"] = picked
    elif isinstance(scorelines, dict):
        picked = _pick_and_grade(scorelines, actual_score)
        if picked is not None:
            selected["scoreline"] = picked

    return {
        "selected": selected,
        "outcomes": goal_outcomes,
    }


# ---------------------------------------------------------------------------
# Statistical enrichment
# ---------------------------------------------------------------------------


def _statistical_actuals(
    fixture: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Return only real corners/cards present in an enriched API fixture."""
    stats = market_grading.extract_fixture_statistics(
        fixture
    )

    corners: Dict[str, Any] = {}
    cards: Dict[str, Any] = {}

    if (
        stats["home_corners"] is not None
        and stats["away_corners"] is not None
    ):
        corners = {
            "home": stats["home_corners"],
            "away": stats["away_corners"],
            "total": (
                stats["home_corners"]
                + stats["away_corners"]
            ),
        }

    if all(
        stats[key] is not None
        for key in (
            "home_yellow_cards",
            "away_yellow_cards",
            "home_red_cards",
            "away_red_cards",
        )
    ):
        cards = {
            "home_yellow": stats[
                "home_yellow_cards"
            ],
            "away_yellow": stats[
                "away_yellow_cards"
            ],
            "home_red": stats[
                "home_red_cards"
            ],
            "away_red": stats[
                "away_red_cards"
            ],
            "total": (
                stats["home_yellow_cards"]
                + stats["away_yellow_cards"]
                + stats["home_red_cards"]
                + stats["away_red_cards"]
            ),
        }

    return {
        "corners": corners,
        "cards": cards,
    }


def _merge_enriched_fixture(
    original: Dict[str, Any],
    enriched: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not isinstance(enriched, dict):
        return original

    merged = dict(original)
    merged.update(enriched)

    return merged


# ---------------------------------------------------------------------------
# Evaluation metrics (Baseline V1)
# ---------------------------------------------------------------------------


def compute_binary_accuracy(
    predictions: Sequence[Dict[str, float]],
    actuals: Sequence[str],
) -> Optional[float]:
    """Compute accuracy where top pick = argmax(probabilities) compared to actual."""
    if not predictions or len(predictions) != len(actuals):
        return None

    correct = 0
    count = 0

    for pred, actual in zip(predictions, actuals):
        if not isinstance(pred, dict) or actual is None or not pred:
            continue

        pick = max(pred, key=lambda k: pred[k])
        if pick == actual:
            correct += 1
        count += 1

    if count == 0:
        return None

    return correct / count


def compute_brier_score(
    predictions: Sequence[Dict[str, float]],
    actuals: Sequence[str],
    outcomes: Optional[Sequence[str]] = None,
    target_outcome: Optional[str] = None,
) -> Optional[float]:
    """
    Compute Brier score over all predictions.

    For binary events (2 complementary outcomes), computes standard binary
    Brier score (p_event - y)^2. For multi-class events (3+ outcomes), computes
    multiclass Brier score sum_k (p_k - y_k)^2.
    """
    if not predictions or len(predictions) != len(actuals):
        return None

    total_squared_error = 0.0
    count = 0

    for pred, actual in zip(predictions, actuals):
        if not isinstance(pred, dict) or actual is None:
            continue

        possible_outcomes = outcomes if outcomes is not None else list(pred.keys())
        if actual not in possible_outcomes:
            continue

        if target_outcome is not None:
            prob = _safe_float(pred.get(target_outcome, 0.0)) or 0.0
            target = 1.0 if actual == target_outcome else 0.0
            sample_error = (prob - target) ** 2
        elif len(possible_outcomes) == 2:
            event_outcome = possible_outcomes[0]
            prob = _safe_float(pred.get(event_outcome, 0.0)) or 0.0
            target = 1.0 if actual == event_outcome else 0.0
            sample_error = (prob - target) ** 2
        else:
            sample_error = 0.0
            for outcome in possible_outcomes:
                prob = _safe_float(pred.get(outcome, 0.0)) or 0.0
                target = 1.0 if actual == outcome else 0.0
                sample_error += (prob - target) ** 2

        total_squared_error += sample_error
        count += 1

    if count == 0:
        return None

    return total_squared_error / count


def compute_log_loss(
    predictions: Sequence[Dict[str, float]],
    actuals: Sequence[str],
    outcomes: Optional[Sequence[str]] = None,
    eps: float = 1e-15,
) -> Optional[float]:
    """Compute multi-class or binary log loss (cross-entropy) over predictions."""
    if not predictions or len(predictions) != len(actuals):
        return None

    total_loss = 0.0
    count = 0

    for pred, actual in zip(predictions, actuals):
        if not isinstance(pred, dict) or actual is None:
            continue

        possible_outcomes = outcomes if outcomes is not None else list(pred.keys())
        if actual not in possible_outcomes:
            continue

        prob = _safe_float(pred.get(actual, 0.0)) or 0.0
        clipped_prob = max(eps, min(1.0 - eps, prob))
        total_loss += -math.log(clipped_prob)
        count += 1

    if count == 0:
        return None

    return total_loss / count


CALIBRATION_BIN_RANGES = [
    ("<50%", 0.0, 0.50),
    ("50–55%", 0.50, 0.55),
    ("55–60%", 0.55, 0.60),
    ("60–65%", 0.60, 0.65),
    ("65–70%", 0.65, 0.70),
    ("70–75%", 0.70, 0.75),
    ("75–80%", 0.75, 0.80),
    ("80%+", 0.80, 1.000001),
]


def compute_calibration_bins(
    samples: Sequence[Tuple[float, bool]],
) -> Dict[str, Any]:
    """
    Compute calibration bin statistics and Expected Calibration Error (ECE)
    for a list of (predicted_probability, outcome_occurred) pairs.
    """
    cleaned_samples: List[Tuple[float, int]] = []
    for prob, occurred in samples:
        p_val = _safe_float(prob)
        if p_val is None:
            continue
        p_val = max(0.0, min(1.0, p_val))
        y_val = 1 if bool(occurred) else 0
        cleaned_samples.append((p_val, y_val))

    total_samples = len(cleaned_samples)

    if total_samples == 0:
        return {
            "ece": None,
            "bins": [
                {
                    "label": label,
                    "number_of_predictions": 0,
                    "mean_predicted_probability": None,
                    "actual_empirical_success_rate": None,
                    "calibration_gap": None,
                }
                for label, _, _ in CALIBRATION_BIN_RANGES
            ],
            "total_samples": 0,
        }

    bins_data: List[Dict[str, Any]] = []
    weighted_ece_sum = 0.0

    for label, lower, upper in CALIBRATION_BIN_RANGES:
        bin_samples = [
            (p, y)
            for p, y in cleaned_samples
            if lower <= p < upper
        ]

        count = len(bin_samples)

        if count > 0:
            mean_prob = sum(p for p, _ in bin_samples) / count
            empirical_rate = sum(y for _, y in bin_samples) / count
            gap = abs(mean_prob - empirical_rate)
            weighted_ece_sum += (count / total_samples) * gap

            bin_entry = {
                "label": label,
                "number_of_predictions": count,
                "mean_predicted_probability": round(mean_prob, 4),
                "actual_empirical_success_rate": round(empirical_rate, 4),
                "calibration_gap": round(gap, 4),
            }
        else:
            bin_entry = {
                "label": label,
                "number_of_predictions": 0,
                "mean_predicted_probability": None,
                "actual_empirical_success_rate": None,
                "calibration_gap": None,
            }

        bins_data.append(bin_entry)

    return {
        "ece": round(weighted_ece_sum, 4),
        "bins": bins_data,
        "total_samples": total_samples,
    }


def compute_market_calibration(
    predictions: Sequence[Dict[str, float]],
    actuals: Sequence[str],
    outcomes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Compute calibration metrics across all outcomes for a market."""
    samples: List[Tuple[float, bool]] = []

    for pred, actual in zip(predictions, actuals):
        if not isinstance(pred, dict) or actual is None:
            continue

        possible_outcomes = outcomes if outcomes is not None else list(pred.keys())
        if actual not in possible_outcomes:
            continue

        for outcome in possible_outcomes:
            prob = _safe_float(pred.get(outcome, 0.0)) or 0.0
            occurred = (actual == outcome)
            samples.append((prob, occurred))

    return compute_calibration_bins(samples)


def compute_multiclass_1x2_calibration(
    predictions: Sequence[Dict[str, float]],
    actuals: Sequence[str],
) -> Dict[str, Any]:
    """
    Compute calibration metrics for 1X2 market, returning overall calibration
    as well as class-specific calibration for home_win, draw, and away_win.
    """
    overall = compute_market_calibration(
        predictions, actuals, outcomes=("home_win", "draw", "away_win")
    )

    by_class = {}
    for cls in ("home_win", "draw", "away_win"):
        cls_samples = []
        for pred, actual in zip(predictions, actuals):
            if not isinstance(pred, dict) or actual is None:
                continue
            if actual not in ("home_win", "draw", "away_win"):
                continue
            prob = _safe_float(pred.get(cls, 0.0)) or 0.0
            occurred = (actual == cls)
            cls_samples.append((prob, occurred))
        by_class[cls] = compute_calibration_bins(cls_samples)

    result = dict(overall)
    result["by_class"] = by_class
    return result


def compute_picked_calibration(
    picked_items: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute calibration metrics for top-picked market choices."""
    samples: List[Tuple[float, bool]] = []

    for item in picked_items:
        if not isinstance(item, dict):
            continue

        prob = item.get("probability")
        won = item.get("won")

        if prob is not None and won is not None:
            samples.append((prob, bool(won)))

    return compute_calibration_bins(samples)


# ---------------------------------------------------------------------------
# Phase 4 Hardened Evaluation Helpers (Statistical Safety, Baselines, Diagnostics)
# ---------------------------------------------------------------------------


def compute_accuracy_confidence_interval(
    correct: int,
    graded: int,
) -> Dict[str, Optional[float]]:
    """Compute 95% Normal-approximation confidence interval for accuracy."""
    if graded <= 0:
        return {"ci_lower": None, "ci_upper": None}
    p = correct / graded
    z = 1.96
    margin = z * math.sqrt((p * (1.0 - p)) / graded)
    return {
        "ci_lower": round(max(0.0, p - margin), 4),
        "ci_upper": round(min(1.0, p + margin), 4),
    }


def compute_point_in_time_empirical_baseline(
    log_subset: Sequence[Dict[str, Any]],
    outcomes: Sequence[str],
) -> Dict[str, Any]:
    """
    Compute point-in-time empirical baseline over log entries without target leakage.
    Each entry's baseline probability distribution is constructed strictly
    from prior finished matches before that entry's cutoff timestamp.
    """
    preds = []
    acts = []

    for entry in log_subset:
        if not isinstance(entry, dict):
            continue
        act = entry.get("actual")
        prior_dist = entry.get("prior_empirical_distribution")
        if isinstance(act, str) and act in outcomes and isinstance(prior_dist, dict):
            clean_dist = {o: _safe_float(prior_dist.get(o, 0.0)) or 0.0 for o in outcomes}
            tot = sum(clean_dist.values())
            if tot > 0:
                norm_dist = {o: clean_dist[o] / tot for o in outcomes}
            else:
                norm_dist = {o: 1.0 / len(outcomes) for o in outcomes}
            preds.append(norm_dist)
            acts.append(act)

    if not preds:
        return {
            "sample_count": 0,
            "accuracy": None,
            "brier_score": None,
            "log_loss": None,
            "leakage_safe": True,
        }

    n = len(preds)
    acc = compute_binary_accuracy(preds, acts)
    brier = compute_brier_score(preds, acts, outcomes=outcomes)
    logloss = compute_log_loss(preds, acts, outcomes=outcomes)

    return {
        "sample_count": n,
        "accuracy": round(acc, 4) if acc is not None else None,
        "brier_score": round(brier, 4) if brier is not None else None,
        "log_loss": round(logloss, 4) if logloss is not None else None,
        "leakage_safe": True,
    }


def compute_empirical_baseline(
    log_or_actuals: Any,
    outcomes: Sequence[str],
) -> Dict[str, Any]:
    """
    Compute empirical baseline.
    If log_subset (sequence of dicts) is provided, uses point-in-time prior distributions.
    """
    if isinstance(log_or_actuals, (list, tuple)) and log_or_actuals and isinstance(log_or_actuals[0], dict):
        return compute_point_in_time_empirical_baseline(log_or_actuals, outcomes)

    # Fallback for plain string sequence (compatibility)
    actuals = log_or_actuals if isinstance(log_or_actuals, (list, tuple)) else []
    clean_actuals = [a for a in actuals if a in outcomes]
    n = len(clean_actuals)
    if n == 0:
        return {
            "sample_count": 0,
            "accuracy": None,
            "brier_score": None,
            "log_loss": None,
            "leakage_safe": False,
        }

    counts = {o: clean_actuals.count(o) for o in outcomes}
    probs = {o: counts[o] / n for o in outcomes}
    top_pick = max(probs, key=probs.get)
    acc = counts[top_pick] / n

    brier = compute_brier_score([probs] * n, clean_actuals, outcomes=outcomes)
    logloss = compute_log_loss([probs] * n, clean_actuals, outcomes=outcomes)

    return {
        "sample_count": n,
        "top_pick": top_pick,
        "accuracy": round(acc, 4),
        "brier_score": round(brier, 4) if brier is not None else None,
        "log_loss": round(logloss, 4) if logloss is not None else None,
        "empirical_distribution": {k: round(v, 4) for k, v in probs.items()},
        "leakage_safe": False,
    }


def compute_odds_baseline(
    log_entries: Sequence[Dict[str, Any]],
    sport: str = "football",
) -> Dict[str, Any]:
    """
    Compute odds implied probability baseline over entries with valid odds.
    Requires BOTH odds_status == 'VALID' AND explicit odds_timestamp AND cutoff_timestamp.
    Requires odds_timestamp < cutoff_timestamp strictly before cutoff.
    Missing, unknown, or non-before chronology is excluded.
    """
    preds = []
    acts = []

    for entry in log_entries:
        if not isinstance(entry, dict):
            continue

        pred_rec = entry.get("prediction", {})
        if not isinstance(pred_rec, dict):
            continue

        unc = pred_rec.get("uncertainty", {}) if isinstance(pred_rec.get("uncertainty"), dict) else {}
        odds_status = unc.get("odds_status")

        # 1. Require explicit "VALID" odds status
        if odds_status != "VALID":
            continue

        # 2. Require explicit odds timestamp AND prediction cutoff timestamp
        odds_ts = unc.get("odds_timestamp") or pred_rec.get("odds_timestamp")
        cutoff_ts = pred_rec.get("data_cutoff_timestamp") or entry.get("date") or entry.get("data_cutoff_timestamp")

        if not odds_ts or not cutoff_ts:
            continue

        # 3. Require odds_timestamp < cutoff_timestamp
        if not time_utils.is_strictly_before(str(odds_ts), str(cutoff_ts)):
            continue

        m_analysis = pred_rec.get("market_analysis", {}) if isinstance(pred_rec.get("market_analysis"), dict) else {}
        m_key = "match_result" if sport == "football" else "moneyline"
        m_odds = m_analysis.get(m_key, {})

        if isinstance(m_odds, dict) and m_odds:
            implied_dist = {}
            for k, v in m_odds.items():
                if isinstance(v, dict) and v.get("implied_probability") is not None:
                    implied_dist[k] = v["implied_probability"]

            if implied_dist:
                total_p = sum(implied_dist.values())
                if total_p > 0:
                    norm_dist = {k: v / total_p for k, v in implied_dist.items()}
                    act = entry.get("actual")
                    if act and act in norm_dist:
                        preds.append(norm_dist)
                        acts.append(act)

    if not preds:
        return {
            "sample_count": 0,
            "accuracy": None,
            "brier_score": None,
            "log_loss": None,
            "odds_available": False,
        }

    outcomes = tuple(preds[0].keys())
    acc = compute_binary_accuracy(preds, acts)
    brier = compute_brier_score(preds, acts, outcomes=outcomes)
    logloss = compute_log_loss(preds, acts, outcomes=outcomes)

    return {
        "sample_count": len(preds),
        "accuracy": round(acc, 4) if acc is not None else None,
        "brier_score": round(brier, 4) if brier is not None else None,
        "log_loss": round(logloss, 4) if logloss is not None else None,
        "odds_available": True,
    }


def compute_stability_diagnostics(
    log_entries: Sequence[Dict[str, Any]],
    sport: str = "football",
) -> Dict[str, Any]:
    """Compute factual diagnostics across log entries without ranking or best labels."""
    total = len(log_entries)
    if total == 0:
        return {
            "signal_vs_pass": {"signal_count": 0, "pass_count": 0, "signal_rate": 0.0, "pass_rate": 0.0},
            "outcome_behavior": {},
            "outcome_frequencies": {},
            "performance_by_uncertainty_state": {},
            "performance_by_confidence_bucket": {},
            "performance_by_probability_bucket": {},
            "league_season_breakdown": {},
            "calibration_by_market": {},
        }

    signal_cnt = sum(1 for e in log_entries if e.get("quality_gate") == "SIGNAL")
    pass_cnt = sum(1 for e in log_entries if e.get("quality_gate") == "PASS")

    # 1. Outcome Frequencies & Outcome Behavior (actual vs predicted mean probabilities)
    outcome_counts: Dict[str, int] = {}
    predicted_prob_sums: Dict[str, float] = {}
    m_key = "match_result" if sport == "football" else "moneyline"
    possible_outcomes = ("home_win", "draw", "away_win") if sport == "football" else ("home_win", "away_win")

    for e in log_entries:
        act = e.get("actual")
        if act:
            outcome_counts[act] = outcome_counts.get(act, 0) + 1

        p_rec = e.get("prediction", {})
        m_dist = p_rec.get("markets", {}).get(m_key) if isinstance(p_rec, dict) else None
        if isinstance(m_dist, dict):
            for o in possible_outcomes:
                val = _safe_float(m_dist.get(o, 0.0)) or 0.0
                predicted_prob_sums[o] = predicted_prob_sums.get(o, 0.0) + val

    actual_sample_cnt = sum(outcome_counts.values())
    outcome_freqs = {}
    outcome_behavior = {}

    for o in possible_outcomes:
        act_cnt = outcome_counts.get(o, 0)
        act_freq = act_cnt / actual_sample_cnt if actual_sample_cnt > 0 else 0.0
        pred_mean = predicted_prob_sums.get(o, 0.0) / total if total > 0 else 0.0

        outcome_freqs[o] = {
            "count": act_cnt,
            "frequency": round(act_freq, 4),
        }
        outcome_behavior[o] = {
            "actual_count": act_cnt,
            "actual_frequency": round(act_freq, 4),
            "predicted_mean_probability": round(pred_mean, 4),
            "calibration_gap": round(abs(act_freq - pred_mean), 4),
        }

    # 2. Performance by Uncertainty State
    by_unc: Dict[str, Dict[str, Any]] = {}
    for e in log_entries:
        pred = e.get("prediction", {})
        unc_st = pred.get("uncertainty", {}).get("state", "unknown") if isinstance(pred, dict) else "unknown"
        bucket = by_unc.setdefault(unc_st, {"sample_count": 0, "correct_count": 0, "accuracy": 0.0})
        bucket["sample_count"] += 1
        if e.get("correct"):
            bucket["correct_count"] += 1

    for unc_st, bucket in by_unc.items():
        sc = bucket["sample_count"]
        bucket["accuracy"] = round(bucket["correct_count"] / sc, 4) if sc > 0 else 0.0

    # 3. Performance by Confidence Bucket
    by_conf: Dict[str, Dict[str, Any]] = {}
    for e in log_entries:
        pred = e.get("prediction", {})
        c_label = pred.get("confidence", {}).get("label") if isinstance(pred.get("confidence"), dict) else None
        if not c_label:
            c_label = "Unspecified"
        bucket = by_conf.setdefault(c_label, {"sample_count": 0, "correct_count": 0, "accuracy": 0.0})
        bucket["sample_count"] += 1
        if e.get("correct"):
            bucket["correct_count"] += 1

    for c_label, bucket in by_conf.items():
        sc = bucket["sample_count"]
        bucket["accuracy"] = round(bucket["correct_count"] / sc, 4) if sc > 0 else 0.0

    # 4. Performance by Actual Predicted Probability Buckets (<50%, 50-60%, 60-70%, 70%+)
    prob_buckets = {"<50%": {"sample_count": 0, "correct_count": 0, "accuracy": 0.0},
                    "50-60%": {"sample_count": 0, "correct_count": 0, "accuracy": 0.0},
                    "60-70%": {"sample_count": 0, "correct_count": 0, "accuracy": 0.0},
                    "70%+": {"sample_count": 0, "correct_count": 0, "accuracy": 0.0}}

    for e in log_entries:
        pred = e.get("prediction", {})
        m_dist = pred.get("markets", {}).get(m_key) if isinstance(pred, dict) else None
        if isinstance(m_dist, dict) and m_dist:
            top_p = max(_safe_float(v) or 0.0 for v in m_dist.values())
            if top_p < 0.50:
                b_key = "<50%"
            elif top_p < 0.60:
                b_key = "50-60%"
            elif top_p < 0.70:
                b_key = "60-70%"
            else:
                b_key = "70%+"

            b_obj = prob_buckets[b_key]
            b_obj["sample_count"] += 1
            if e.get("correct"):
                b_obj["correct_count"] += 1

    for b_key, b_obj in prob_buckets.items():
        sc = b_obj["sample_count"]
        b_obj["accuracy"] = round(b_obj["correct_count"] / sc, 4) if sc > 0 else 0.0

    # 5. League / Season Breakdown
    league_season_map: Dict[str, Dict[str, Any]] = {}
    for e in log_entries:
        pred = e.get("prediction", {})
        lid = pred.get("league_id") or e.get("league_id")
        ssn = pred.get("season") or e.get("season")
        if lid is not None and ssn is not None:
            ls_key = f"{lid}_{ssn}"
            ls_bucket = league_season_map.setdefault(ls_key, {"league_id": lid, "season": ssn, "sample_count": 0, "correct_count": 0, "accuracy": 0.0})
            ls_bucket["sample_count"] += 1
            if e.get("correct"):
                ls_bucket["correct_count"] += 1

    for ls_key, ls_bucket in league_season_map.items():
        sc = ls_bucket["sample_count"]
        ls_bucket["accuracy"] = round(ls_bucket["correct_count"] / sc, 4) if sc > 0 else 0.0

    if sport == "football":
        market_calib_status = {
            "match_result": "CALIBRATED" if any(e.get("calibration_status") == "APPLIED" for e in log_entries) else "RAW_UNCALIBRATED",
            "double_chance": "RAW_UNCALIBRATED",
            "over_under_2_5": "RAW_UNCALIBRATED",
            "btts": "RAW_UNCALIBRATED",
        }
    else:
        market_calib_status = {
            "moneyline": "CALIBRATED" if any(e.get("calibration_status") == "APPLIED" for e in log_entries) else "RAW_UNCALIBRATED",
            "total_points": "RAW_UNCALIBRATED",
        }

    return {
        "signal_vs_pass": {
            "signal_count": signal_cnt,
            "pass_count": pass_cnt,
            "signal_rate": round(signal_cnt / total, 4) if total > 0 else 0.0,
            "pass_rate": round(pass_cnt / total, 4) if total > 0 else 0.0,
        },
        "outcome_behavior": outcome_behavior,
        "outcome_frequencies": outcome_freqs,
        "performance_by_uncertainty_state": by_unc,
        "performance_by_confidence_bucket": by_conf,
        "performance_by_probability_bucket": prob_buckets,
        "league_season_breakdown": league_season_map,
        "calibration_by_market": market_calib_status,
    }


def evaluate_football_log_group(
    log_subset: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Evaluate full market metrics for a specific subset of football log entries."""
    min_thresh = getattr(config, "MIN_EVALUATION_SAMPLE_THRESHOLD", 30)

    preds_1x2, acts_1x2, top_picks_1x2 = [], [], []
    preds_dc, acts_dc = [], []
    preds_dc_hd, acts_dc_hd = [], []
    preds_dc_ad, acts_dc_ad = [], []
    preds_dc_ha, acts_dc_ha = [], []
    preds_ou25, acts_ou25 = [], []
    preds_btts, acts_btts = [], []

    for entry in log_subset:
        p_markets = entry.get("prediction", {}).get("markets", {})
        m_grading = entry.get("market_grading", {})
        outcomes = m_grading.get("outcomes", {})
        selected = m_grading.get("selected", {})

        # 1X2
        act_1x2 = entry.get("actual")
        m_dist_1x2 = p_markets.get("match_result")
        if isinstance(act_1x2, str) and act_1x2 in {"home_win", "draw", "away_win"} and isinstance(m_dist_1x2, dict):
            preds_1x2.append(m_dist_1x2)
            acts_1x2.append(act_1x2)

        sel_1x2 = selected.get("match_result")
        if isinstance(sel_1x2, dict):
            top_picks_1x2.append(sel_1x2)

        # Double Chance
        m_dist_dc = p_markets.get("double_chance")
        out_dc = outcomes.get("double_chance", {})
        if isinstance(m_dist_dc, dict) and isinstance(out_dc, dict):
            p_hd = _safe_float(m_dist_dc.get("home_or_draw"))
            p_ad = _safe_float(m_dist_dc.get("away_or_draw"))
            p_ha = _safe_float(m_dist_dc.get("home_or_away"))

            won_hd = out_dc.get("home_or_draw", {}).get("won")
            won_ad = out_dc.get("away_or_draw", {}).get("won")
            won_ha = out_dc.get("home_or_away", {}).get("won")

            if p_hd is not None and won_hd is not None:
                preds_dc.append({"yes": p_hd, "no": 1.0 - p_hd})
                acts_dc.append("yes" if won_hd else "no")
                preds_dc_hd.append({"yes": p_hd, "no": 1.0 - p_hd})
                acts_dc_hd.append("yes" if won_hd else "no")

            if p_ad is not None and won_ad is not None:
                preds_dc.append({"yes": p_ad, "no": 1.0 - p_ad})
                acts_dc.append("yes" if won_ad else "no")
                preds_dc_ad.append({"yes": p_ad, "no": 1.0 - p_ad})
                acts_dc_ad.append("yes" if won_ad else "no")

            if p_ha is not None and won_ha is not None:
                preds_dc.append({"yes": p_ha, "no": 1.0 - p_ha})
                acts_dc.append("yes" if won_ha else "no")
                preds_dc_ha.append({"yes": p_ha, "no": 1.0 - p_ha})
                acts_dc_ha.append("yes" if won_ha else "no")

        # Over / Under 2.5
        sel_ou = selected.get("over_under", {})
        if isinstance(sel_ou, dict):
            sel_ou25 = sel_ou.get("2.5") or sel_ou.get("2_5")
            if isinstance(sel_ou25, dict) and sel_ou25.get("actual") in {"over", "under"}:
                act_ou25 = sel_ou25["actual"]
                m_dist_ou = p_markets.get("over_under")
                if isinstance(m_dist_ou, dict):
                    p_over = _safe_float(m_dist_ou.get("over_2_5"))
                    p_under = _safe_float(m_dist_ou.get("under_2_5"))
                    if p_over is not None and p_under is not None:
                        preds_ou25.append({"over": p_over, "under": p_under})
                        acts_ou25.append(act_ou25)

        # BTTS
        sel_btts = selected.get("btts")
        m_dist_btts = p_markets.get("btts")
        if isinstance(sel_btts, dict) and sel_btts.get("actual") in {"yes", "no"} and isinstance(m_dist_btts, dict):
            act_btts = sel_btts["actual"]
            p_yes = _safe_float(m_dist_btts.get("yes"))
            p_no = _safe_float(m_dist_btts.get("no"))
            if p_yes is not None and p_no is not None:
                preds_btts.append({"yes": p_yes, "no": p_no})
                acts_btts.append(act_btts)

    sc_1x2 = len(preds_1x2)
    acc_1x2 = compute_binary_accuracy(preds_1x2, acts_1x2)
    corr_1x2 = sum(1 for p, a in zip(preds_1x2, acts_1x2) if max(p, key=p.get) == a) if sc_1x2 > 0 else 0
    ci_1x2 = compute_accuracy_confidence_interval(corr_1x2, sc_1x2)
    cal_statuses_1x2 = [e.get("calibration_status") for e in log_subset if e.get("calibration_status")]
    if not cal_statuses_1x2:
        cal_status_1x2 = "UNAVAILABLE"
    elif all(s == "APPLIED" for s in cal_statuses_1x2):
        cal_status_1x2 = "CALIBRATED"
    elif all(s == "ERROR" for s in cal_statuses_1x2):
        cal_status_1x2 = "ERROR"
    elif all(s == "UNAVAILABLE" for s in cal_statuses_1x2):
        cal_status_1x2 = "UNAVAILABLE"
    else:
        cal_status_1x2 = "PARTIALLY_CALIBRATED"

    match_result_eval = {
        "sample_count": sc_1x2,
        "is_low_sample": sc_1x2 < min_thresh,
        "sample_reliability": "INSUFFICIENT_SAMPLE" if sc_1x2 < min_thresh else "ADEQUATE_SAMPLE",
        "accuracy": round(acc_1x2, 4) if acc_1x2 is not None else None,
        "accuracy_ci_lower": ci_1x2["ci_lower"],
        "accuracy_ci_upper": ci_1x2["ci_upper"],
        "brier_score": compute_brier_score(preds_1x2, acts_1x2, outcomes=("home_win", "draw", "away_win")),
        "log_loss": compute_log_loss(preds_1x2, acts_1x2, outcomes=("home_win", "draw", "away_win")),
        "calibration_status": cal_status_1x2,
        "calibration": compute_multiclass_1x2_calibration(preds_1x2, acts_1x2),
        "top_pick_calibration": compute_picked_calibration(top_picks_1x2),
    }

    sc_dc = len(preds_dc)
    acc_dc = compute_binary_accuracy(preds_dc, acts_dc)
    corr_dc = sum(1 for p, a in zip(preds_dc, acts_dc) if max(p, key=p.get) == a) if sc_dc > 0 else 0
    ci_dc = compute_accuracy_confidence_interval(corr_dc, sc_dc)

    double_chance_eval = {
        "sample_count": sc_dc,
        "is_low_sample": sc_dc < min_thresh,
        "sample_reliability": "INSUFFICIENT_SAMPLE" if sc_dc < min_thresh else "ADEQUATE_SAMPLE",
        "accuracy": round(acc_dc, 4) if acc_dc is not None else None,
        "accuracy_ci_lower": ci_dc["ci_lower"],
        "accuracy_ci_upper": ci_dc["ci_upper"],
        "brier_score": compute_brier_score(preds_dc, acts_dc, outcomes=("yes", "no")),
        "log_loss": compute_log_loss(preds_dc, acts_dc, outcomes=("yes", "no")),
        "calibration_status": "RAW_UNCALIBRATED",
        "calibration": compute_market_calibration(preds_dc, acts_dc, outcomes=("yes", "no")),
        "home_or_draw": {
            "sample_count": len(preds_dc_hd),
            "brier_score": compute_brier_score(preds_dc_hd, acts_dc_hd, outcomes=("yes", "no")),
            "log_loss": compute_log_loss(preds_dc_hd, acts_dc_hd, outcomes=("yes", "no")),
            "calibration": compute_market_calibration(preds_dc_hd, acts_dc_hd, outcomes=("yes", "no")),
        },
        "away_or_draw": {
            "sample_count": len(preds_dc_ad),
            "brier_score": compute_brier_score(preds_dc_ad, acts_dc_ad, outcomes=("yes", "no")),
            "log_loss": compute_log_loss(preds_dc_ad, acts_dc_ad, outcomes=("yes", "no")),
            "calibration": compute_market_calibration(preds_dc_ad, acts_dc_ad, outcomes=("yes", "no")),
        },
        "home_or_away": {
            "sample_count": len(preds_dc_ha),
            "brier_score": compute_brier_score(preds_dc_ha, acts_dc_ha, outcomes=("yes", "no")),
            "log_loss": compute_log_loss(preds_dc_ha, acts_dc_ha, outcomes=("yes", "no")),
            "calibration": compute_market_calibration(preds_dc_ha, acts_dc_ha, outcomes=("yes", "no")),
        },
    }

    sc_ou = len(preds_ou25)
    acc_ou = compute_binary_accuracy(preds_ou25, acts_ou25)
    corr_ou = sum(1 for p, a in zip(preds_ou25, acts_ou25) if max(p, key=p.get) == a) if sc_ou > 0 else 0
    ci_ou = compute_accuracy_confidence_interval(corr_ou, sc_ou)

    ou25_eval = {
        "sample_count": sc_ou,
        "is_low_sample": sc_ou < min_thresh,
        "sample_reliability": "INSUFFICIENT_SAMPLE" if sc_ou < min_thresh else "ADEQUATE_SAMPLE",
        "accuracy": round(acc_ou, 4) if acc_ou is not None else None,
        "accuracy_ci_lower": ci_ou["ci_lower"],
        "accuracy_ci_upper": ci_ou["ci_upper"],
        "brier_score": compute_brier_score(preds_ou25, acts_ou25, outcomes=("over", "under")),
        "log_loss": compute_log_loss(preds_ou25, acts_ou25, outcomes=("over", "under")),
        "calibration_status": "RAW_UNCALIBRATED",
        "calibration": compute_market_calibration(preds_ou25, acts_ou25, outcomes=("over", "under")),
    }

    sc_btts = len(preds_btts)
    acc_btts = compute_binary_accuracy(preds_btts, acts_btts)
    corr_btts = sum(1 for p, a in zip(preds_btts, acts_btts) if max(p, key=p.get) == a) if sc_btts > 0 else 0
    ci_btts = compute_accuracy_confidence_interval(corr_btts, sc_btts)

    btts_eval = {
        "sample_count": sc_btts,
        "is_low_sample": sc_btts < min_thresh,
        "sample_reliability": "INSUFFICIENT_SAMPLE" if sc_btts < min_thresh else "ADEQUATE_SAMPLE",
        "accuracy": round(acc_btts, 4) if acc_btts is not None else None,
        "accuracy_ci_lower": ci_btts["ci_lower"],
        "accuracy_ci_upper": ci_btts["ci_upper"],
        "brier_score": compute_brier_score(preds_btts, acts_btts, outcomes=("yes", "no")),
        "log_loss": compute_log_loss(preds_btts, acts_btts, outcomes=("yes", "no")),
        "calibration_status": "RAW_UNCALIBRATED",
        "calibration": compute_market_calibration(preds_btts, acts_btts, outcomes=("yes", "no")),
    }

    baselines = {
        "empirical": compute_point_in_time_empirical_baseline(log_subset, outcomes=("home_win", "draw", "away_win")),
        "odds_implied": compute_odds_baseline(log_subset, sport="football"),
    }

    return {
        "total_graded_samples": len(log_subset),
        "markets": {
            "match_result": match_result_eval,
            "double_chance": double_chance_eval,
            "over_under_2_5": ou25_eval,
            "btts": btts_eval,
        },
        "match_result": match_result_eval,
        "double_chance": double_chance_eval,
        "over_under_2_5": ou25_eval,
        "btts": btts_eval,
        "baselines": baselines,
    }


def evaluate_basketball_log_group(
    log_subset: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Evaluate market metrics for a specific subset of basketball log entries."""
    min_thresh = getattr(config, "MIN_EVALUATION_SAMPLE_THRESHOLD", 30)

    preds_ml, acts_ml = [], []
    preds_tot, acts_tot = [], []

    for entry in log_subset:
        pred_rec = entry.get("prediction", {})
        p_markets = pred_rec.get("markets", {}) if isinstance(pred_rec, dict) else {}

        act_ml = entry.get("actual")
        ml_dist = p_markets.get("moneyline")
        if isinstance(act_ml, str) and act_ml in {"home_win", "away_win"} and isinstance(ml_dist, dict):
            p_h = _safe_float(ml_dist.get("home_win"))
            p_a = _safe_float(ml_dist.get("away_win"))
            if p_h is not None and p_a is not None:
                preds_ml.append({"home_win": p_h, "away_win": p_a})
                acts_ml.append(act_ml)

        tot_dist = p_markets.get("total_points")
        act_pts = entry.get("actual_points")
        if isinstance(tot_dist, dict) and isinstance(act_pts, dict):
            h_pts = _safe_float(act_pts.get("home"))
            a_pts = _safe_float(act_pts.get("away"))
            line = _safe_float(tot_dist.get("line"))
            p_over = _safe_float(tot_dist.get("over"))
            p_under = _safe_float(tot_dist.get("under"))
            if h_pts is not None and a_pts is not None and line is not None and p_over is not None and p_under is not None:
                tot_sum = h_pts + a_pts
                act_tot = "over" if tot_sum > line else ("under" if tot_sum < line else "push")
                if act_tot in {"over", "under"}:
                    preds_tot.append({"over": p_over, "under": p_under})
                    acts_tot.append(act_tot)

    sc_ml = len(preds_ml)
    acc_ml = compute_binary_accuracy(preds_ml, acts_ml)
    corr_ml = sum(1 for p, a in zip(preds_ml, acts_ml) if ("home_win" if p.get("home_win", 0) >= p.get("away_win", 0) else "away_win") == a) if sc_ml > 0 else 0
    ci_ml = compute_accuracy_confidence_interval(corr_ml, sc_ml)
    cal_statuses_ml = [e.get("calibration_status") for e in log_subset if e.get("calibration_status")]
    if not cal_statuses_ml:
        cal_status_ml = "UNAVAILABLE"
    elif all(s == "APPLIED" for s in cal_statuses_ml):
        cal_status_ml = "CALIBRATED"
    elif all(s == "ERROR" for s in cal_statuses_ml):
        cal_status_ml = "ERROR"
    elif all(s == "UNAVAILABLE" for s in cal_statuses_ml):
        cal_status_ml = "UNAVAILABLE"
    else:
        cal_status_ml = "PARTIALLY_CALIBRATED"

    moneyline_eval = {
        "sample_count": sc_ml,
        "is_low_sample": sc_ml < min_thresh,
        "sample_reliability": "INSUFFICIENT_SAMPLE" if sc_ml < min_thresh else "ADEQUATE_SAMPLE",
        "accuracy": round(acc_ml, 4) if acc_ml is not None else None,
        "accuracy_ci_lower": ci_ml["ci_lower"],
        "accuracy_ci_upper": ci_ml["ci_upper"],
        "brier_score": compute_brier_score(preds_ml, acts_ml, outcomes=("home_win", "away_win")),
        "log_loss": compute_log_loss(preds_ml, acts_ml, outcomes=("home_win", "away_win")),
        "calibration_status": cal_status_ml,
        "calibration": compute_market_calibration(preds_ml, acts_ml, outcomes=("home_win", "away_win")),
    }

    sc_tot = len(preds_tot)
    acc_tot = compute_binary_accuracy(preds_tot, acts_tot)
    corr_tot = sum(1 for p, a in zip(preds_tot, acts_tot) if ("over" if p.get("over", 0) >= p.get("under", 0) else "under") == a) if sc_tot > 0 else 0
    ci_tot = compute_accuracy_confidence_interval(corr_tot, sc_tot)

    total_points_eval = {
        "sample_count": sc_tot,
        "is_low_sample": sc_tot < min_thresh,
        "sample_reliability": "INSUFFICIENT_SAMPLE" if sc_tot < min_thresh else "ADEQUATE_SAMPLE",
        "accuracy": round(acc_tot, 4) if acc_tot is not None else None,
        "accuracy_ci_lower": ci_tot["ci_lower"],
        "accuracy_ci_upper": ci_tot["ci_upper"],
        "brier_score": compute_brier_score(preds_tot, acts_tot, outcomes=("over", "under")),
        "log_loss": compute_log_loss(preds_tot, acts_tot, outcomes=("over", "under")),
        "calibration_status": "RAW_UNCALIBRATED",
        "calibration": compute_market_calibration(preds_tot, acts_tot, outcomes=("over", "under")),
    }

    baselines = {
        "empirical": compute_point_in_time_empirical_baseline(log_subset, outcomes=("home_win", "away_win")),
        "odds_implied": compute_odds_baseline(log_subset, sport="basketball"),
    }

    return {
        "total_graded_samples": len(log_subset),
        "markets": {
            "moneyline": moneyline_eval,
            "total_points": total_points_eval,
        },
        "moneyline": moneyline_eval,
        "total_points": total_points_eval,
        "baselines": baselines,
    }


# ---------------------------------------------------------------------------
# Summary/reporting
# ---------------------------------------------------------------------------


def _new_market_summary() -> Dict[str, Any]:
    """Create a summary that supports both aggregate and line-specific markets."""
    return {}


def _summary_entry() -> Dict[str, Any]:
    return {
        "graded": 0,
        "correct": 0,
        "accuracy": 0.0,
    }


def _update_summary_bucket(
    bucket: Dict[str, Any],
    won: Optional[bool],
) -> None:
    if won is None:
        return

    bucket["graded"] += 1

    if won:
        bucket["correct"] += 1

    bucket["accuracy"] = (
        bucket["correct"]
        / bucket["graded"]
    )


def _update_market_summary(
    summary: Dict[str, Any],
    selected: Dict[str, Any],
) -> None:
    if not isinstance(selected, dict):
        return

    for market in (
        "match_result",
        "double_chance",
        "btts",
        "scoreline",
    ):
        item = selected.get(market)

        if isinstance(item, dict):
            bucket = summary.setdefault(
                market,
                _summary_entry(),
            )

            _update_summary_bucket(
                bucket,
                item.get("won"),
            )

    over_under = selected.get(
        "over_under"
    )

    if isinstance(over_under, dict):
        market_bucket = summary.setdefault(
            "over_under",
            {},
        )

        for line, item in over_under.items():
            if not isinstance(item, dict):
                continue

            line_bucket = market_bucket.setdefault(
                str(line),
                _summary_entry(),
            )

            _update_summary_bucket(
                line_bucket,
                item.get("won"),
            )

    team_goals = selected.get(
        "team_goals"
    )

    if isinstance(team_goals, dict):
        market_bucket = summary.setdefault(
            "team_goals",
            {},
        )

        for line, item in team_goals.items():
            if not isinstance(item, dict):
                continue

            line_bucket = market_bucket.setdefault(
                str(line),
                _summary_entry(),
            )

            _update_summary_bucket(
                line_bucket,
                item.get("won"),
            )


def _format_probability(
    value: Any,
) -> str:
    number = _safe_float(value)

    if number is None:
        return "N/A"

    return (
        f"{number:.1%}"
        if abs(number) <= 1
        else f"{number:.2f}"
    )


def _print_market_summary(
    summary: Dict[str, Any],
) -> None:
    print("\nMARKET SUMMARY")
    print("-" * 72)
    print(
        f"{'Market':30}"
        f"{'Graded':>10}"
        f"{'Correct':>10}"
        f"{'Accuracy':>12}"
    )
    print("-" * 72)

    for market in (
        "match_result",
        "double_chance",
        "btts",
        "scoreline",
    ):
        item = summary.get(market)

        if (
            not isinstance(item, dict)
            or "graded" not in item
        ):
            continue

        print(
            f"{market:30}"
            f"{item.get('graded', 0):>10}"
            f"{item.get('correct', 0):>10}"
            f"{item.get('accuracy', 0.0):>11.1%}"
        )

    for market in (
        "over_under",
        "team_goals",
    ):
        lines = summary.get(
            market,
            {},
        )

        if not isinstance(lines, dict):
            continue

        for line, item in lines.items():
            if (
                not isinstance(item, dict)
                or "graded" not in item
            ):
                continue

            label = f"{market} {line}"

            print(
                f"{label:30}"
                f"{item.get('graded', 0):>10}"
                f"{item.get('correct', 0):>10}"
                f"{item.get('accuracy', 0.0):>11.1%}"
            )

    print("-" * 72)


def _print_market_metrics_summary(
    evaluation: Dict[str, Any],
) -> None:
    print("\nMARKET EVALUATION METRICS")
    print("-" * 72)
    print(
        f"{'Market':20}"
        f"{'Brier Score':>15}"
        f"{'Log Loss':>15}"
        f"{'ECE':>15}"
    )
    print("-" * 72)

    for market_key, name in (
        ("match_result", "1X2"),
        ("double_chance", "Double Chance (Pooled)"),
        ("over_under_2_5", "Over/Under 2.5"),
        ("btts", "BTTS"),
    ):
        m_eval = evaluation.get(market_key, {})
        if not isinstance(m_eval, dict):
            continue

        brier = m_eval.get("brier_score")
        loss = m_eval.get("log_loss")
        ece = m_eval.get("calibration", {}).get("ece")

        brier_str = f"{brier:.4f}" if brier is not None else "N/A"
        loss_str = f"{loss:.4f}" if loss is not None else "N/A"
        ece_str = f"{ece:.4f}" if ece is not None else "N/A"

        print(
            f"{name:25}"
            f"{brier_str:>15}"
            f"{loss_str:>15}"
            f"{ece_str:>15}"
        )

        if market_key == "double_chance":
            for sub_key, sub_name in (
                ("home_or_draw", "  DC: Home/Draw"),
                ("away_or_draw", "  DC: Away/Draw"),
                ("home_or_away", "  DC: Home/Away"),
            ):
                sub_eval = m_eval.get(sub_key, {})
                if isinstance(sub_eval, dict):
                    sb = sub_eval.get("brier_score")
                    sl = sub_eval.get("log_loss")
                    se = sub_eval.get("calibration", {}).get("ece")

                    sb_str = f"{sb:.4f}" if sb is not None else "N/A"
                    sl_str = f"{sl:.4f}" if sl is not None else "N/A"
                    se_str = f"{se:.4f}" if se is not None else "N/A"

                    print(
                        f"{sub_name:25}"
                        f"{sb_str:>15}"
                        f"{sl_str:>15}"
                        f"{se_str:>15}"
                    )

    print("-" * 72)

    # Print 1X2 Calibration Bin Details
    match_cal = evaluation.get("match_result", {}).get("calibration", {})
    bins = match_cal.get("bins", [])
    if bins:
        print("\nPROBABILITY CALIBRATION BINS (1X2 Market)")
        print("-" * 78)
        print(
            f"{'Bin Range':12}"
            f"{'Count':>10}"
            f"{'Mean Prob':>16}"
            f"{'Actual Rate':>18}"
            f"{'Calib Gap':>16}"
        )
        print("-" * 78)

        for b in bins:
            label = b.get("label", "")
            count = b.get("number_of_predictions", 0)
            p_mean = b.get("mean_predicted_probability")
            p_str = f"{p_mean:.1%}" if p_mean is not None else "N/A"
            r_act = b.get("actual_empirical_success_rate")
            r_str = f"{r_act:.1%}" if r_act is not None else "N/A"
            gap = b.get("calibration_gap")
            g_str = f"{gap:.4f}" if gap is not None else "N/A"

            print(
                f"{label:12}"
                f"{count:>10}"
                f"{p_str:>16}"
                f"{r_str:>18}"
                f"{g_str:>16}"
            )

        print("-" * 78)


def _print_backtest_report(
    result: Dict[str, Any],
) -> None:
    print(
        "\n=== FULL MARKET BACKTEST REPORT ==="
    )

    print(
        "Fixture Source: Neon historical dataset"
    )

    print(
        f"League ID: {result.get('league_id')}, "
        f"Season: {result.get('season')}"
    )

    print(
        f"Minimum prior matches requirement: "
        f"{result.get('min_prior_matches')}"
    )

    print(
        f"Fixtures fetched: "
        f"{result['fixtures_fetched']}"
    )

    print(
        f"Finished fixtures: "
        f"{result['finished_fixtures']}"
    )

    print(
        f"Eligible candidates: "
        f"{result['eligible_candidates']}"
    )

    print(
        f"Sample requested: "
        f"{result['requested_sample']}"
    )

    print(
        f"Fixtures actually graded: "
        f"{result['graded']}"
    )

    print(
        "1X2: "
        f"{result['market_summary'].get('match_result', _summary_entry())}"
    )

    print(
        "Double Chance: "
        f"{result['market_summary'].get('double_chance', _summary_entry())}"
    )

    print(
        "BTTS: "
        f"{result['market_summary'].get('btts', _summary_entry())}"
    )

    print(
        "Over/Under: "
        f"{result['market_summary'].get('over_under', {})}"
    )

    print(
        "Team Goals: "
        f"{result['market_summary'].get('team_goals', {})}"
    )

    print(
        "Scoreline: "
        f"{result['market_summary'].get('scoreline', _summary_entry())}"
    )

    print(
        "Real statistical availability: "
        f"corners={result['statistical_data_available']['corners']}, "
        f"cards={result['statistical_data_available']['cards']}"
    )

    print(
        f"Statistics enriched: "
        f"{result['statistics_enriched']}"
    )

    print(
        "No zero-fixture regression: "
        f"{'PASS' if result['graded'] > 0 else 'FAIL'}"
    )

    _print_market_summary(
        result["market_summary"]
    )

    if result.get("evaluation"):
        _print_market_metrics_summary(result["evaluation"])

    print(
        "=== END FULL MARKET BACKTEST REPORT ===\n"
        )

# ---------------------------------------------------------------------------
# Main backtest
# ---------------------------------------------------------------------------


def run_real_backtest(
    league_id: Any,
    season: Any,
    sample_size: int = 50,
    min_prior_matches: int = 5,
    sample_seed: Optional[int] = 42,
    enrich_statistics: bool = False,
) -> Dict[str, Any]:
    """Run a chronological, leakage-safe historical backtest."""

    if (
        isinstance(league_id, bool)
        or not isinstance(league_id, int)
    ):
        raise ValueError(
            "league_id must be an integer."
        )

    if (
        isinstance(season, bool)
        or not isinstance(season, int)
        or season < 1900
    ):
        raise ValueError(
            "season must be a valid integer year."
        )

    if (
        isinstance(sample_size, bool)
        or not isinstance(sample_size, int)
        or sample_size <= 0
    ):
        raise ValueError(
            "sample_size must be a positive integer."
        )

    if (
        isinstance(min_prior_matches, bool)
        or not isinstance(min_prior_matches, int)
        or min_prior_matches < 0
    ):
        raise ValueError(
            "min_prior_matches must be a non-negative integer."
        )

    if enrich_statistics not in (
        True,
        False,
    ):
        raise ValueError(
            "enrich_statistics must be a boolean."
        )

    if (
        sample_seed is not None
        and (
            isinstance(sample_seed, bool)
            or not isinstance(sample_seed, int)
        )
    ):
        raise ValueError(
            "sample_seed must be an integer or None."
        )

    # Verify dataset completion status database-first
    dataset_status = storage.get_historical_dataset_status(league_id, season)
    if dataset_status.get("status") != "COMPLETE":
        raise RuntimeError(
            f"Historical dataset missing or incomplete for league {league_id} season {season} (status: {dataset_status.get('status')}). "
            f"Run the historical sync job first."
        )

    actual_stored_count = storage.get_historical_fixture_count(league_id, season)
    manifest_count = dataset_status.get("fixture_count", 0)

    if manifest_count != actual_stored_count:
        raise RuntimeError(
            f"Historical dataset integrity mismatch for league {league_id} season {season}: "
            f"manifest count ({manifest_count}) != actual stored count ({actual_stored_count}). "
            f"Re-run the historical sync job."
        )

    # Fetch fixtures database-first from persistent historical dataset
    fixtures = storage.get_historical_fixtures(
        league_id,
        season,
    )

    if not fixtures:
        raise RuntimeError(
            f"Historical dataset missing or incomplete for league {league_id} season {season}. "
            f"Run the historical sync job first."
        )

    if not isinstance(fixtures, list):
        fixtures = []

    fixtures = [
        fixture
        for fixture in fixtures
        if isinstance(fixture, dict)
    ]

    finished = [
        fixture
        for fixture in fixtures
        if _is_finished(fixture)
    ]

    finished.sort(
        key=lambda f: time_utils.parse_utc_datetime(_fixture_date(f)) or datetime.min.replace(tzinfo=timezone.utc)
    )

    eligible = (
        _filter_candidates_by_minimum_history(
            finished,
            fixtures,
            minimum_matches=min_prior_matches,
        )
    )

    selected = _sample_backtest_candidates(
        eligible,
        sample_size,
        seed=sample_seed,
    )

    selected.sort(
        key=lambda f: time_utils.parse_utc_datetime(_fixture_date(f)) or datetime.min.replace(tzinfo=timezone.utc)
    )

    enriched_by_id: Dict[
        str,
        Dict[str, Any],
    ] = {}

    statistics_enriched = False

    statistical_data_available = {
        "corners": 0,
        "cards": 0,
    }

    if (
        enrich_statistics
        and selected
    ):
        statistics_enriched = True

        fixture_ids = [
            _fixture_id(fixture)
            for fixture in selected
            if _fixture_id(fixture) is not None
        ]

        enriched = storage.get_historical_enrichment(
            fixture_ids
        )

        if isinstance(enriched, dict):
            for key, value in enriched.items():
                if isinstance(value, dict):
                    enriched_by_id[
                        _fixture_lookup_key(key)
                    ] = value

    log: List[Dict[str, Any]] = []
    market_summary = _new_market_summary()
    prior_raw_predictions_by_id: Dict[Any, Dict[str, Any]] = {}

    correct = 0
    graded = 0

    for candidate in selected:
        fixture_id = _fixture_id(
            candidate
        )
        cutoff = _fixture_date(candidate)

        for prev_f in finished:
            prev_date = _fixture_date(prev_f)
            if not time_utils.is_strictly_before(prev_date, cutoff):
                break
            prev_id = _fixture_id(prev_f)
            if prev_id not in prior_raw_predictions_by_id:
                prev_hist = _historical_prediction_for_fixture(
                    fixtures,
                    prev_f,
                    min_prior_matches=min_prior_matches,
                    calibrator=None,
                    league_id=league_id,
                    season=season,
                )
                if prev_hist is not None:
                    prev_pred = prev_hist["prediction"]
                    prev_act = _actual_match_result(prev_f)
                    if prev_act in ("home_win", "draw", "away_win") and isinstance(prev_pred, dict):
                        prior_raw_predictions_by_id[prev_id] = {
                            "fixture_id": prev_id,
                            "game_id": prev_id,
                            "raw_probabilities": prev_pred.get("raw_probabilities", {}),
                            "actual": prev_act,
                            "timestamp": prev_date,
                        }

        calibrator = calibration.train_walk_forward_calibrator(
            list(prior_raw_predictions_by_id.values()),
            cutoff,
            sport="football",
        )

        enriched_fixture = (
            enriched_by_id.get(
                _fixture_lookup_key(
                    fixture_id
                )
            )
            if fixture_id is not None
            else None
        )

        fixture_for_stats = (
            _merge_enriched_fixture(
                candidate,
                enriched_fixture,
            )
        )

        historical = (
            _historical_prediction_for_fixture(
                fixtures,
                candidate,
                min_prior_matches=min_prior_matches,
                calibrator=calibrator,
                league_id=league_id,
                season=season,
            )
        )

        if historical is None:
            continue

        prediction = historical[
            "prediction"
        ]

        prediction_markets = prediction.get(
            "markets",
            {},
        )

        market_result = (
            _grade_prediction_markets(
                prediction_markets,
                candidate,
            )
        )

        selected_markets = (
            market_result["selected"]
        )

        primary = selected_markets.get(
            "match_result"
        )

        primary_won = (
            primary.get("won")
            if isinstance(primary, dict)
            else None
        )

        if primary_won is not None:
            graded += 1

            if primary_won:
                correct += 1

        _update_market_summary(
            market_summary,
            selected_markets,
        )

        statistical_actuals = (
            _statistical_actuals(
                fixture_for_stats
            )
        )

        if statistical_actuals[
            "corners"
        ]:
            statistical_data_available[
                "corners"
            ] += 1

        if statistical_actuals[
            "cards"
        ]:
            statistical_data_available[
                "cards"
            ] += 1

        match_result = _actual_match_result(
            candidate
        )

        if match_result in ("home_win", "draw", "away_win") and fixture_id is not None:
            prior_raw_predictions_by_id[fixture_id] = {
                "fixture_id": fixture_id,
                "game_id": fixture_id,
                "raw_probabilities": prediction.get("raw_probabilities", {}),
                "actual": match_result,
                "timestamp": cutoff,
            }

        predicted = (
            primary.get("pick")
            if isinstance(primary, dict)
            else None
        )

        qg_decision = prediction.get("quality_gate") or prediction.get("quality_gate_result", {}).get("decision", "PASS")
        calib_status = prediction.get("calibration_metadata", {}).get("calibration_status", "UNAVAILABLE")

        prior_outcomes = []
        for prev_f in finished:
            prev_date = _fixture_date(prev_f)
            if not time_utils.is_strictly_before(prev_date, cutoff):
                break
            prev_act = _actual_match_result(prev_f)
            if prev_act in ("home_win", "draw", "away_win"):
                prior_outcomes.append(prev_act)

        if prior_outcomes:
            n_p = len(prior_outcomes)
            prior_dist_1x2 = {
                "home_win": prior_outcomes.count("home_win") / n_p,
                "draw": prior_outcomes.count("draw") / n_p,
                "away_win": prior_outcomes.count("away_win") / n_p,
            }
        else:
            prior_dist_1x2 = {"home_win": 1.0 / 3.0, "draw": 1.0 / 3.0, "away_win": 1.0 / 3.0}

        entry = {
            "fixture_id": fixture_id,
            "match": (
                f"{_home_name(candidate)} "
                f"vs {_away_name(candidate)}"
            ),
            "date": _fixture_date(
                candidate
            ),
            "home_team": _home_name(
                candidate
            ),
            "away_team": _away_name(
                candidate
            ),
            "correct": (
                bool(primary_won)
                if primary_won is not None
                else False
            ),
            "predicted": predicted,
            "actual": match_result,
            "quality_gate": qg_decision,
            "calibration_status": calib_status,
            "prior_empirical_distribution": prior_dist_1x2,
            "probabilities": (
                prediction_markets.get(
                    "match_result",
                    {},
                )
            ),
            "market_grading": {
                "selected": selected_markets,
                "outcomes": (
                    market_result[
                        "outcomes"
                    ]
                ),
                "statistical_actuals": (
                    statistical_actuals
                ),
            },
            "prediction": prediction,
            "historical_features": {
                "historical_snapshot": (
                    historical[
                        "historical_snapshot"
                    ]
                ),
                "recent_snapshot": (
                    historical[
                        "recent_snapshot"
                    ]
                ),
                "h2h_snapshot": (
                    historical[
                        "h2h_snapshot"
                    ]
                ),
                "elo_snapshot": (
                    historical[
                        "elo_snapshot"
                    ]
                ),
                "league_avg_goals": (
                    historical[
                        "league_avg_goals"
                    ]
                ),
            },
        }

        log.append(entry)

    # -----------------------------------------------------------------------
    # Comprehensive Phase 4 evaluation across groups, baselines, and diagnostics
    # -----------------------------------------------------------------------
    sampling_info = {
        "total_eligible_population": len(eligible),
        "selected_sample": len(selected),
        "sample_seed": sample_seed,
        "sampling_mode": "FULL" if len(selected) >= len(eligible) else "RANDOM_SAMPLED",
        "evaluation_coverage": round(len(selected) / len(eligible), 4) if len(eligible) > 0 else 0.0,
        "is_sampled": len(selected) < len(eligible),
    }

    all_eval = evaluate_football_log_group(log)
    signal_log = [e for e in log if e.get("quality_gate") == "SIGNAL"]
    pass_log = [e for e in log if e.get("quality_gate") == "PASS"]

    signal_eval = evaluate_football_log_group(signal_log)
    pass_eval = evaluate_football_log_group(pass_log)

    diagnostics = compute_stability_diagnostics(log, sport="football")

    evaluation = dict(all_eval["markets"])
    evaluation["groups"] = {
        "all": all_eval,
        "signal": signal_eval,
        "pass": pass_eval,
    }
    evaluation["all"] = all_eval
    evaluation["signal"] = signal_eval
    evaluation["pass"] = pass_eval
    evaluation["sampling"] = sampling_info
    evaluation["diagnostics"] = diagnostics
    evaluation["baselines"] = all_eval.get("baselines", {})

    # Extract top-level backward-compatible metrics for 1X2 match result
    brier_score = evaluation["match_result"]["brier_score"]
    log_loss = evaluation["match_result"]["log_loss"]
    match_result_calibration = evaluation["match_result"]["calibration"]

    result = {
        "fixtures_fetched": len(
            fixtures
        ),
        "finished_fixtures": len(
            finished
        ),
        "eligible_candidates": len(
            eligible
        ),
        "requested_sample": sample_size,
        "selected": len(selected),
        "sample_size": min(sample_size, len(selected)),
        "graded": graded,
        "correct": correct,
        "accuracy": (
            correct / graded
            if graded
            else 0.0
        ),
        "brier_score": brier_score,
        "log_loss": log_loss,
        "calibration": match_result_calibration,
        "evaluation": evaluation,
        "sampling": sampling_info,
        "diagnostics": diagnostics,
        "league_id": league_id,
        "season": season,
        "min_prior_matches": (
            min_prior_matches
        ),
        "sample_seed": sample_seed,
        "statistics_enriched": (
            statistics_enriched
        ),
        "statistical_data_available": (
            statistical_data_available
        ),
        "market_summary": (
            market_summary
        ),
        "log": log,
    }

    timestamp = (
        datetime.now(
            timezone.utc
        ).strftime(
            "%Y%m%d_%H%M%S"
        )
    )

    log_path = (
        BACKTEST_LOG_DIR
        / (
            f"backtest_{league_id}_"
            f"{season}_{timestamp}.json"
        )
    )

    try:
        log_path.write_text(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )

        result["log_path"] = str(
            log_path
        )

    except OSError:
        result["log_path"] = None

    _print_backtest_report(
        result
    )

    try:
        run_id = f"football_{league_id}_{season}_{timestamp}"
        run_data = {
            "run_id": run_id,
            "sport": "football",
            "league_id": league_id,
            "season": season,
            "dataset_identity": f"football_{league_id}_{season}",
            "model_version": getattr(config, "MODEL_VERSION", "v3.0.0"),
            "feature_version": getattr(config, "FEATURE_VERSION", "v3.0.0"),
            "calibration_version": getattr(config, "CALIBRATION_VERSION", "v3.0.0"),
            "dataset_fixture_count": actual_stored_count,
            "sample_size": sample_size,
            "min_prior_matches": min_prior_matches,
            "sample_seed": sample_seed,
            "selected_count": len(selected),
            "graded_count": graded,
            "accuracy": result["accuracy"],
            "brier_score": brier_score,
            "log_loss": log_loss,
            "ece": match_result_calibration.get("ece") if isinstance(match_result_calibration, dict) else None,
            "enrichment_status": dataset_status.get("enrichment_status", "NONE"),
            "started_at": timestamp,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "evaluation_json": evaluation,
            "code_version": "authoritative",
        }
        market_metrics = []
        for m_key in ("match_result", "double_chance", "over_under_2_5", "btts"):
            m_eval = evaluation.get(m_key, {})
            if isinstance(m_eval, dict):
                s_cnt = m_eval.get("sample_count", graded)
                acc_val = m_eval.get("accuracy")
                br_val = m_eval.get("brier_score")
                ll_val = m_eval.get("log_loss")
                ece_val = m_eval.get("calibration", {}).get("ece") if isinstance(m_eval.get("calibration"), dict) else None
                market_metrics.append({
                    "market_key": m_key,
                    "sample_count": s_cnt,
                    "accuracy": acc_val,
                    "brier_score": br_val,
                    "log_loss": ll_val,
                    "ece": ece_val,
                    "metrics_json": m_eval,
                })
        storage.save_backtest_run(run_data, market_metrics)
        result["status"] = "COMPLETED"
        result["persisted"] = True
        result["persistence_error"] = None
    except Exception as exc:
        result["status"] = "PERSISTENCE_FAILED"
        result["persisted"] = False
        result["persistence_error"] = str(exc)
        print(f"ERROR: Could not persist backtest experiment record: {exc}", flush=True)

    return result


def run_basketball_backtest(
    league_id: Any,
    season: Any,
    sample_size: int = 50,
    min_prior_matches: int = 5,
    sample_seed: Optional[int] = 42,
) -> Dict[str, Any]:
    """
    Run a chronological, leakage-safe historical basketball backtest database-first.
    """
    if isinstance(league_id, bool) or not isinstance(league_id, int):
        raise ValueError("league_id must be an integer.")

    if isinstance(season, bool) or not isinstance(season, int) or season < 1900:
        raise ValueError("season must be a valid integer year.")

    if isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size <= 0:
        raise ValueError("sample_size must be a positive integer.")

    if isinstance(min_prior_matches, bool) or not isinstance(min_prior_matches, int) or min_prior_matches < 0:
        raise ValueError("min_prior_matches must be a non-negative integer.")

    if sample_seed is not None and (isinstance(sample_seed, bool) or not isinstance(sample_seed, int)):
        raise ValueError("sample_seed must be an integer or None.")

    dataset_status = storage.get_historical_dataset_status(league_id, season, sport="basketball")
    if dataset_status.get("status") != "COMPLETE":
        raise RuntimeError(
            f"Historical basketball dataset missing or incomplete for league {league_id} season {season} (status: {dataset_status.get('status')}). "
            f"Run the historical sync job first."
        )

    actual_stored_count = storage.get_historical_basketball_game_count(league_id, season)
    manifest_count = dataset_status.get("fixture_count", 0)

    if manifest_count != actual_stored_count:
        raise RuntimeError(
            f"Historical basketball dataset integrity mismatch for league {league_id} season {season}: "
            f"manifest count ({manifest_count}) != actual stored count ({actual_stored_count}). "
            f"Re-run the historical sync job."
        )

    games = storage.get_historical_basketball_games(league_id, season)

    if not games:
        raise RuntimeError(
            f"Historical basketball dataset missing or incomplete for league {league_id} season {season}. "
            f"Run the historical sync job first."
        )

    games = [g for g in games if isinstance(g, dict)]

    finished = [g for g in games if historical_match_policy.is_finished_match(g, sport="basketball")]
    finished.sort(key=lambda g: time_utils.parse_utc_datetime(str(g.get("date", ""))) or datetime.min.replace(tzinfo=timezone.utc))

    eligible = [
        g for g in finished
        if historical_basketball_features.game_has_minimum_history(
            games,
            g.get("teams", {}).get("home", {}).get("id"),
            g.get("teams", {}).get("away", {}).get("id"),
            str(g.get("date", "")),
            minimum_matches=min_prior_matches,
        )
    ]

    selected = _sample_backtest_candidates(eligible, sample_size, seed=sample_seed)
    selected.sort(key=lambda g: time_utils.parse_utc_datetime(str(g.get("date", ""))) or datetime.min.replace(tzinfo=timezone.utc))

    log: List[Dict[str, Any]] = []
    preds_ml = []
    acts_ml = []
    preds_tot = []
    acts_tot = []
    prior_raw_predictions_by_id: Dict[Any, Dict[str, Any]] = {}

    correct = 0
    graded = 0

    for candidate in selected:
        cutoff = str(candidate.get("date", ""))
        home_id = candidate.get("teams", {}).get("home", {}).get("id")
        away_id = candidate.get("teams", {}).get("away", {}).get("id")

        if not home_id or not away_id or not cutoff:
            continue

        for prev_g in finished:
            prev_date = str(prev_g.get("date", ""))
            if not time_utils.is_strictly_before(prev_date, cutoff):
                break
            prev_id = prev_g.get("id")
            if prev_id not in prior_raw_predictions_by_id:
                prev_h_id = prev_g.get("teams", {}).get("home", {}).get("id")
                prev_a_id = prev_g.get("teams", {}).get("away", {}).get("id")
                if prev_h_id and prev_a_id:
                    p_h_stats = historical_basketball_features.reconstruct_basketball_team_stats(games, prev_h_id, prev_date)
                    p_a_stats = historical_basketball_features.reconstruct_basketball_team_stats(games, prev_a_id, prev_date)
                    if p_h_stats and p_a_stats:
                        prev_pred = basketball_model.predict_game(prev_g, home_stats_override=p_h_stats, away_stats_override=p_a_stats, calibrator=None)
                        if not prev_pred.get("insufficient_data"):
                            p_h_pts, p_a_pts = historical_match_policy.get_basketball_match_points(prev_g)
                            if p_h_pts is not None and p_a_pts is not None:
                                p_act = "home_win" if p_h_pts > p_a_pts else ("away_win" if p_a_pts > p_h_pts else "draw")
                                if p_act in ("home_win", "away_win"):
                                    prior_raw_predictions_by_id[prev_id] = {
                                        "fixture_id": prev_id,
                                        "game_id": prev_id,
                                        "raw_probabilities": prev_pred.get("raw_probabilities", {}),
                                        "actual": p_act,
                                        "timestamp": prev_date,
                                    }

        calibrator = calibration.train_walk_forward_calibrator(
            list(prior_raw_predictions_by_id.values()),
            cutoff,
            sport="basketball",
        )

        home_stats = historical_basketball_features.reconstruct_basketball_team_stats(games, home_id, cutoff)
        away_stats = historical_basketball_features.reconstruct_basketball_team_stats(games, away_id, cutoff)

        if home_stats is None or away_stats is None:
            continue

        pred = basketball_model.predict_game(
            candidate,
            home_stats_override=home_stats,
            away_stats_override=away_stats,
            calibrator=calibrator,
            data_cutoff_timestamp=cutoff,
        )
        if pred.get("insufficient_data"):
            continue

        h_pts, a_pts = historical_match_policy.get_basketball_match_points(candidate)
        if h_pts is None or a_pts is None:
            continue

        actual_outcome = "home_win" if h_pts > a_pts else ("away_win" if a_pts > h_pts else "draw")

        if actual_outcome in ("home_win", "away_win") and candidate.get("id") is not None:
            prior_raw_predictions_by_id[candidate.get("id")] = {
                "raw_probabilities": pred.get("raw_probabilities", {}),
                "actual": actual_outcome,
                "timestamp": cutoff,
            }
        ml_markets = pred["markets"]["moneyline"]
        p_home = ml_markets["home_win"]
        p_away = ml_markets["away_win"]

        picked_ml = "home_win" if p_home >= p_away else "away_win"
        won_ml = (picked_ml == actual_outcome)

        graded += 1
        if won_ml:
            correct += 1

        preds_ml.append({"home_win": p_home, "away_win": p_away})
        acts_ml.append(actual_outcome)

        tot_m = pred["markets"]["total_points"]
        p_over = tot_m["over"]
        p_under = tot_m["under"]
        total_line = tot_m["line"]
        actual_total_pts = h_pts + a_pts
        actual_tot_outcome = "over" if actual_total_pts > total_line else ("under" if actual_total_pts < total_line else "push")

        if actual_tot_outcome in ("over", "under"):
            preds_tot.append({"over": p_over, "under": p_under})
            acts_tot.append(actual_tot_outcome)

        qg_decision = pred.get("quality_gate") or pred.get("quality_gate_result", {}).get("decision", "PASS")
        calib_status = pred.get("calibration_metadata", {}).get("calibration_status", "UNAVAILABLE")

        prior_outcomes = []
        for prev_g in finished:
            prev_date = str(prev_g.get("date", ""))
            if not time_utils.is_strictly_before(prev_date, cutoff):
                break
            p_h_pts, p_a_pts = historical_match_policy.get_basketball_match_points(prev_g)
            if p_h_pts is not None and p_a_pts is not None:
                p_act = "home_win" if p_h_pts > p_a_pts else ("away_win" if p_a_pts > p_h_pts else "draw")
                if p_act in ("home_win", "away_win"):
                    prior_outcomes.append(p_act)

        if prior_outcomes:
            n_p = len(prior_outcomes)
            prior_dist_ml = {
                "home_win": prior_outcomes.count("home_win") / n_p,
                "away_win": prior_outcomes.count("away_win") / n_p,
            }
        else:
            prior_dist_ml = {"home_win": 0.5, "away_win": 0.5}

        entry = {
            "game_id": candidate.get("id"),
            "match": f"{candidate.get('teams', {}).get('home', {}).get('name')} vs {candidate.get('teams', {}).get('away', {}).get('name')}",
            "date": cutoff,
            "correct": won_ml,
            "predicted": picked_ml,
            "actual": actual_outcome,
            "quality_gate": qg_decision,
            "calibration_status": calib_status,
            "prior_empirical_distribution": prior_dist_ml,
            "actual_points": {"home": h_pts, "away": a_pts},
            "prediction": pred,
        }
        log.append(entry)

    sampling_info = {
        "total_eligible_population": len(eligible),
        "selected_sample": len(selected),
        "sample_seed": sample_seed,
        "sampling_mode": "FULL" if len(selected) >= len(eligible) else "RANDOM_SAMPLED",
        "evaluation_coverage": round(len(selected) / len(eligible), 4) if len(eligible) > 0 else 0.0,
        "is_sampled": len(selected) < len(eligible),
    }

    all_eval = evaluate_basketball_log_group(log)
    signal_log = [e for e in log if e.get("quality_gate") == "SIGNAL"]
    pass_log = [e for e in log if e.get("quality_gate") == "PASS"]

    signal_eval = evaluate_basketball_log_group(signal_log)
    pass_eval = evaluate_basketball_log_group(pass_log)

    diagnostics = compute_stability_diagnostics(log, sport="basketball")

    evaluation = dict(all_eval["markets"])
    evaluation["groups"] = {
        "all": all_eval,
        "signal": signal_eval,
        "pass": pass_eval,
    }
    evaluation["all"] = all_eval
    evaluation["signal"] = signal_eval
    evaluation["pass"] = pass_eval
    evaluation["sampling"] = sampling_info
    evaluation["diagnostics"] = diagnostics
    evaluation["baselines"] = all_eval.get("baselines", {})

    brier_ml = evaluation["moneyline"]["brier_score"]
    loss_ml = evaluation["moneyline"]["log_loss"]
    cal_ml = evaluation["moneyline"]["calibration"]

    start_ts = datetime.now(timezone.utc).isoformat()
    result = {
        "sport": "basketball",
        "league_id": league_id,
        "season": season,
        "fixtures_fetched": len(games),
        "finished_fixtures": len(finished),
        "eligible_candidates": len(eligible),
        "requested_sample": sample_size,
        "selected": len(selected),
        "graded": graded,
        "correct": correct,
        "accuracy": correct / graded if graded else 0.0,
        "brier_score": brier_ml,
        "log_loss": loss_ml,
        "calibration": cal_ml,
        "evaluation": evaluation,
        "sampling": sampling_info,
        "diagnostics": diagnostics,
        "min_prior_matches": min_prior_matches,
        "sample_seed": sample_seed,
        "log": log,
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_id = f"basketball_{league_id}_{season}_{timestamp}"

    try:
        run_data = {
            "run_id": run_id,
            "sport": "basketball",
            "league_id": league_id,
            "season": season,
            "dataset_identity": f"basketball_{league_id}_{season}",
            "model_version": getattr(config, "MODEL_VERSION", "v3.0.0"),
            "feature_version": getattr(config, "FEATURE_VERSION", "v3.0.0"),
            "calibration_version": getattr(config, "CALIBRATION_VERSION", "v3.0.0"),
            "dataset_fixture_count": actual_stored_count,
            "sample_size": sample_size,
            "min_prior_matches": min_prior_matches,
            "sample_seed": sample_seed,
            "selected_count": len(selected),
            "graded_count": graded,
            "accuracy": result["accuracy"],
            "brier_score": brier_ml,
            "log_loss": loss_ml,
            "ece": cal_ml.get("ece") if isinstance(cal_ml, dict) else None,
            "enrichment_status": "NONE",
            "started_at": start_ts,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "evaluation_json": evaluation,
            "code_version": "authoritative",
        }
        acc_tot = evaluation.get("total_points", {}).get("accuracy")
        brier_tot = evaluation.get("total_points", {}).get("brier_score")
        loss_tot = evaluation.get("total_points", {}).get("log_loss")
        cal_tot_dict = evaluation.get("total_points", {}).get("calibration", {})
        ece_tot = cal_tot_dict.get("ece") if isinstance(cal_tot_dict, dict) else None

        market_metrics = [
            {
                "market_key": "moneyline",
                "sample_count": evaluation.get("moneyline", {}).get("sample_count", graded),
                "accuracy": result["accuracy"],
                "brier_score": brier_ml,
                "log_loss": loss_ml,
                "ece": cal_ml.get("ece") if isinstance(cal_ml, dict) else None,
                "metrics_json": evaluation["moneyline"],
            },
            {
                "market_key": "total_points",
                "sample_count": evaluation.get("total_points", {}).get("sample_count", 0),
                "accuracy": acc_tot,
                "brier_score": brier_tot,
                "log_loss": loss_tot,
                "ece": ece_tot,
                "metrics_json": evaluation["total_points"],
            },
        ]
        storage.save_backtest_run(run_data, market_metrics)
        result["status"] = "COMPLETED"
        result["persisted"] = True
        result["persistence_error"] = None
    except Exception as exc:
        result["status"] = "PERSISTENCE_FAILED"
        result["persisted"] = False
        result["persistence_error"] = str(exc)
        print(f"ERROR: Could not persist basketball backtest experiment record: {exc}", flush=True)

    return result


# ---------------------------------------------------------------------------
# Multi-season backtest runner
# ---------------------------------------------------------------------------


def run_multi_season_backtest(
    league_id: Any,
    seasons: Sequence[int],
    sample_size_per_season: int = 380,
    min_prior_matches: int = 5,
    sample_seed: Optional[int] = 42,
    enrich_statistics: bool = False,
) -> Dict[str, Any]:
    """
    Run point-in-time historical backtests across multiple seasons and aggregate results.
    """
    all_log: List[Dict[str, Any]] = []
    season_reports: Dict[int, Dict[str, Any]] = {}
    season_statuses: Dict[int, Dict[str, Any]] = {}
    season_limitations: Dict[int, str] = {}
    contributing_seasons: List[int] = []

    total_fixtures_fetched = 0
    total_finished_fixtures = 0
    total_eligible_candidates = 0
    total_selected = 0
    total_graded = 0
    total_correct = 0

    combined_market_summary = _new_market_summary()
    is_aggregate_complete = True

    for s in seasons:
        try:
            res = run_real_backtest(
                league_id=league_id,
                season=s,
                sample_size=sample_size_per_season,
                min_prior_matches=min_prior_matches,
                sample_seed=sample_seed,
                enrich_statistics=enrich_statistics,
            )

            season_reports[s] = res
            graded_s = res.get("graded", 0)

            if graded_s > 0 and res.get("status") == "COMPLETED":
                season_st = "COMPLETE"
                err_reason = None
                contributing_seasons.append(s)
            else:
                season_st = "PARTIAL"
                err_reason = f"Season {s} returned {graded_s} graded fixtures."
                is_aggregate_complete = False
                season_limitations[s] = err_reason

            season_statuses[s] = {
                "status": season_st,
                "graded_count": graded_s,
                "error_reason": err_reason,
            }

            total_fixtures_fetched += res.get("fixtures_fetched", 0)
            total_finished_fixtures += res.get("finished_fixtures", 0)
            total_eligible_candidates += res.get("eligible_candidates", 0)
            total_selected += res.get("selected", 0)
            total_graded += graded_s
            total_correct += res.get("correct", 0)

            all_log.extend(res.get("log", []))

            # Update market summary
            for entry in res.get("log", []):
                selected = entry.get("market_grading", {}).get("selected", {})
                _update_market_summary(combined_market_summary, selected)

        except Exception as exc:
            is_aggregate_complete = False
            err_msg = str(exc)
            season_limitations[s] = f"Season {s} failed with error: {err_msg}"
            season_statuses[s] = {
                "status": "FAILED",
                "graded_count": 0,
                "error_reason": err_msg,
            }

    all_failed = all(st.get("status") == "FAILED" for st in season_statuses.values()) if season_statuses else False
    if all_failed:
        overall_status = "FAILED"
    elif not is_aggregate_complete:
        overall_status = "PARTIAL"
    else:
        overall_status = "COMPLETE"

    all_eval = evaluate_football_log_group(all_log)
    signal_log = [e for e in all_log if e.get("quality_gate") == "SIGNAL"]
    pass_log = [e for e in all_log if e.get("quality_gate") == "PASS"]

    signal_eval = evaluate_football_log_group(signal_log)
    pass_eval = evaluate_football_log_group(pass_log)

    diagnostics = compute_stability_diagnostics(all_log, sport="football")

    evaluation = dict(all_eval["markets"])
    evaluation["groups"] = {
        "all": all_eval,
        "signal": signal_eval,
        "pass": pass_eval,
    }
    evaluation["all"] = all_eval
    evaluation["signal"] = signal_eval
    evaluation["pass"] = pass_eval
    evaluation["diagnostics"] = diagnostics
    evaluation["baselines"] = all_eval.get("baselines", {})

    return {
        "league_id": league_id,
        "seasons_evaluated": list(seasons),
        "contributing_seasons": contributing_seasons,
        "season_statuses": season_statuses,
        "overall_status": overall_status,
        "is_aggregate_complete": is_aggregate_complete,
        "fixtures_fetched": total_fixtures_fetched,
        "finished_fixtures": total_finished_fixtures,
        "eligible_candidates": total_eligible_candidates,
        "selected": total_selected,
        "graded": total_graded,
        "correct": total_correct,
        "accuracy": total_correct / total_graded if total_graded else 0.0,
        "evaluation": evaluation,
        "market_summary": combined_market_summary,
        "season_reports": season_reports,
        "season_limitations": season_limitations,
        "log": all_log,
    }


# ---------------------------------------------------------------------------
# Compatibility aliases
# ---------------------------------------------------------------------------


def run_backtest(
    league_id: Any,
    season: Any,
    sample_size: int = 50,
    **kwargs: Any,
) -> Dict[str, Any]:
    return run_real_backtest(
        league_id,
        season,
        sample_size,
        **kwargs,
    )


def backtest(
    league_id: Any,
    season: Any,
    sample_size: int = 50,
    **kwargs: Any,
) -> Dict[str, Any]:
    return run_real_backtest(
        league_id,
        season,
        sample_size,
        **kwargs,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Run the Football Agent "
            "historical backtest."
        )
    )

    parser.add_argument(
        "--league-id",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--league-name",
        default=None,
    )

    parser.add_argument(
        "--season",
        type=int,
        required=True,
    )

    parser.add_argument(
        "--sample",
        type=int,
        default=50,
    )

    args = parser.parse_args()

    league_id = args.league_id

    if (
        league_id is None
        and args.league_name
    ):
        league_names = {
            "premier league": 39,
            "la liga": 140,
            "serie a": 135,
            "bundesliga": 78,
            "ligue 1": 61,
        }

        league_id = league_names.get(
            args.league_name.strip().lower()
        )

    if league_id is None:
        parser.error(
            "A supported --league-id or "
            "--league-name is required."
        )

    result = run_real_backtest(
        league_id,
        args.season,
        args.sample,
    )

    print(
        "Backtest accuracy: "
        f"{result['accuracy']:.1%} "
        f"({result['correct']}/{result['graded']})"
    )
