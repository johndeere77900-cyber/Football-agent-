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
import config
import historical_elo
import historical_features
import historical_h2h
import market_grading
import prediction_engine


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
    goals = fixture.get("goals", {})

    home = goals.get("home")
    away = goals.get("away")

    if not _valid_goal(home) or not _valid_goal(away):
        return None, None

    return int(home), int(away)


def _is_finished(
    fixture: Dict[str, Any],
) -> bool:
    return (
        fixture.get("fixture", {})
        .get("status", {})
        .get("short")
        == "FT"
    )


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

        if _fixture_date(fixture) >= cutoff:
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
        key=_fixture_date,
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
    home, away = _goals(fixture)

    if home is None or away is None:
        return None

    if home > away:
        return "home_win"

    if home < away:
        return "away_win"

    return "draw"


def _actual_double_chance(
    fixture: Dict[str, Any],
) -> Optional[str]:
    result = _actual_match_result(fixture)

    if result == "home_win" or result == "draw":
        return "home_or_draw"

    if result == "away_win":
        return "away_or_draw"

    return None


def _actual_btts(
    fixture: Dict[str, Any],
) -> Optional[str]:
    home, away = _goals(fixture)

    if home is None or away is None:
        return None

    return (
        "yes"
        if home >= 1 and away >= 1
        else "no"
    )


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
    """Return selected predictions plus independent actual market outcomes."""
    selected: Dict[str, Any] = {}
    outcomes: Dict[str, Any] = {}

    home, away = _goals(fixture)

    total = (
        home + away
        if home is not None and away is not None
        else None
    )

    # 1X2
    selected_1x2 = _pick_and_grade(
        prediction_markets.get("match_result"),
        _actual_match_result(fixture),
    )

    if selected_1x2 is not None:
        selected["match_result"] = selected_1x2

    outcomes["match_result"] = (
        market_grading.grade_match_result(
            home,
            away,
        )
    )

    # Double Chance
    selected_dc = _pick_and_grade(
        prediction_markets.get("double_chance"),
        _actual_double_chance(fixture),
    )

    if selected_dc is not None:
        selected["double_chance"] = selected_dc

    outcomes["double_chance"] = (
        market_grading.grade_double_chance(
            home,
            away,
        )
    )

    # BTTS
    selected_btts = _pick_and_grade(
        prediction_markets.get("btts"),
        _actual_btts(fixture),
    )

    if selected_btts is not None:
        selected["btts"] = selected_btts

    outcomes["btts"] = (
        market_grading.grade_btts(
            home,
            away,
        )
    )

    # Over / Under
    over_under_selected: Dict[str, Any] = {}

    over_under_outcomes = (
        market_grading.grade_over_under(
            home,
            away,
        )
    )

    over_under = prediction_markets.get(
        "over_under",
        {},
    )

    if isinstance(over_under, dict):
        lines: Dict[
            str,
            Tuple[float, Dict[str, float]],
        ] = {}

        for key, value in over_under.items():
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
            ):
                continue

            key_text = str(key)

            if not (
                key_text.startswith("over_")
                or key_text.startswith("under_")
            ):
                continue

            parts = key_text.split(
                "_",
                1,
            )

            if len(parts) != 2:
                continue

            try:
                line = float(
                    parts[1].replace(
                        "_",
                        ".",
                    )
                )
            except ValueError:
                continue

            lines.setdefault(
                str(line),
                (line, {}),
            )[1][key_text] = float(value)

        for line_key, (
            line,
            distribution,
        ) in lines.items():
            actual = _actual_binary_total(
                total,
                line,
            )

            picked = _pick_and_grade(
                distribution,
                actual,
            )

            if picked is not None:
                over_under_selected[
                    line_key
                ] = picked

    if over_under_selected:
        selected["over_under"] = (
            over_under_selected
        )

    outcomes["over_under"] = (
        over_under_outcomes or {}
    )

    # Team goals
    team_selected: Dict[str, Any] = {}

    team_goals = prediction_markets.get(
        "team_goals",
        {},
    )

    if isinstance(team_goals, dict):
        lines: Dict[
            str,
            Tuple[float, Dict[str, float]],
        ] = {}

        for key, value in team_goals.items():
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
            ):
                continue

            parsed = _parse_goal_market_key(
                key
            )

            if parsed is None:
                continue

            team, line, original_key = parsed

            line_key = (
                f"{team}_{str(line).replace('.', '_')}"
            )

            lines.setdefault(
                line_key,
                (line, {}),
            )[1][original_key] = float(
                value
            )

        for line_key, (
            line,
            distribution,
        ) in lines.items():
            team = line_key.split(
                "_",
                1,
            )[0]

            actual_goals = (
                home
                if team == "home"
                else away
            )

            actual = _actual_binary_total(
                actual_goals,
                line,
            )

            picked = _pick_and_grade(
                distribution,
                actual,
            )

            if picked is not None:
                team_selected[
                    line_key
                ] = picked

    if team_selected:
        selected["team_goals"] = (
            team_selected
        )

    outcomes["team_goals"] = (
        market_grading.grade_team_goals(
            home,
            away,
        )
        or {}
    )

    # Correct score / top scorelines
    scorelines = prediction_markets.get(
        "top_scorelines"
    )

    if not isinstance(scorelines, list):
        scorelines = prediction_markets.get(
            "scoreline"
        )

    if (
        isinstance(scorelines, list)
        and scorelines
    ):
        clean_scores = {
            str(item.get("score")):
                _safe_float(
                    item.get("probability")
                )
            for item in scorelines
            if (
                isinstance(item, dict)
                and item.get("score") is not None
                and _safe_float(
                    item.get("probability")
                ) is not None
            )
        }

        actual_score = (
            f"{home}-{away}"
            if home is not None
            and away is not None
            else None
        )

        picked = _pick_and_grade(
            clean_scores,
            actual_score,
        )

        if picked is not None:
            selected["scoreline"] = picked

    elif isinstance(scorelines, dict):
        actual_score = (
            f"{home}-{away}"
            if home is not None
            and away is not None
            else None
        )

        picked = _pick_and_grade(
            scorelines,
            actual_score,
        )

        if picked is not None:
            selected["scoreline"] = picked

    outcomes["scoreline"] = (
        market_grading.grade_scoreline(
            home,
            away,
        )
    )

    return {
        "selected": selected,
        "outcomes": outcomes,
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


def compute_brier_score(
    predictions: Sequence[Dict[str, float]],
    actuals: Sequence[str],
    outcomes: Optional[Sequence[str]] = None,
) -> Optional[float]:
    """Compute multi-class or binary Brier score over all prediction distributions."""
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
        ("double_chance", "Double Chance"),
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
            f"{name:20}"
            f"{brier_str:>15}"
            f"{loss_str:>15}"
            f"{ece_str:>15}"
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

    # Exactly one league/season fixture fetch.
    fixtures = api_football.get_league_fixtures(
        league_id,
        season,
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
        key=_fixture_date
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
        key=_fixture_date
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

        enriched = (
            api_football.get_enriched_fixtures(
                fixture_ids
            )
        )

        if isinstance(enriched, dict):
            for key, value in enriched.items():
                if isinstance(value, dict):
                    enriched_by_id[
                        _fixture_lookup_key(key)
                    ] = value

    log: List[Dict[str, Any]] = []
    market_summary = _new_market_summary()

    correct = 0
    graded = 0

    for candidate in selected:
        fixture_id = _fixture_id(
            candidate
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

        predicted = (
            primary.get("pick")
            if isinstance(primary, dict)
            else None
        )

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
    # Comprehensive baseline evaluation across distinct markets
    # -----------------------------------------------------------------------
    # 1. 1X2 market (multiclass)
    preds_1x2 = []
    acts_1x2 = []

    # 2. Double Chance market (multiclass)
    preds_dc = []
    acts_dc = []

    # 3. Over/Under 2.5 market (binary)
    preds_ou25 = []
    acts_ou25 = []

    # 4. BTTS market (binary)
    preds_btts = []
    acts_btts = []

    # Top-pick items across markets for picked calibration
    top_picks_1x2 = []

    for entry in log:
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

        # Double Chance - top choice or distribution
        sel_dc = selected.get("double_chance")
        m_dist_dc = p_markets.get("double_chance")
        if isinstance(sel_dc, dict) and sel_dc.get("actual") in {"home_or_draw", "away_or_draw", "home_or_away"} and isinstance(m_dist_dc, dict):
            preds_dc.append(m_dist_dc)
            acts_dc.append(sel_dc["actual"])

        # Over / Under 2.5 - check both "2.5" and "2_5" in market grading/selected
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

    evaluation = {
        "match_result": {
            "brier_score": compute_brier_score(preds_1x2, acts_1x2, outcomes=("home_win", "draw", "away_win")),
            "log_loss": compute_log_loss(preds_1x2, acts_1x2, outcomes=("home_win", "draw", "away_win")),
            "calibration": compute_market_calibration(preds_1x2, acts_1x2, outcomes=("home_win", "draw", "away_win")),
            "top_pick_calibration": compute_picked_calibration(top_picks_1x2),
        },
        "double_chance": {
            "brier_score": compute_brier_score(preds_dc, acts_dc, outcomes=("home_or_draw", "away_or_draw", "home_or_away")),
            "log_loss": compute_log_loss(preds_dc, acts_dc, outcomes=("home_or_draw", "away_or_draw", "home_or_away")),
            "calibration": compute_market_calibration(preds_dc, acts_dc, outcomes=("home_or_draw", "away_or_draw", "home_or_away")),
        },
        "over_under_2_5": {
            "brier_score": compute_brier_score(preds_ou25, acts_ou25, outcomes=("over", "under")),
            "log_loss": compute_log_loss(preds_ou25, acts_ou25, outcomes=("over", "under")),
            "calibration": compute_market_calibration(preds_ou25, acts_ou25, outcomes=("over", "under")),
        },
        "btts": {
            "brier_score": compute_brier_score(preds_btts, acts_btts, outcomes=("yes", "no")),
            "log_loss": compute_log_loss(preds_btts, acts_btts, outcomes=("yes", "no")),
            "calibration": compute_market_calibration(preds_btts, acts_btts, outcomes=("yes", "no")),
        },
    }

    # Extract top-level backward-compatible metrics for 1X2 match result
    brier_score = evaluation["match_result"]["brier_score"]
    log_loss = evaluation["match_result"]["log_loss"]
    calibration = evaluation["match_result"]["calibration"]

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
        "calibration": calibration,
        "evaluation": evaluation,
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
    season_limitations: Dict[int, str] = {}

    total_fixtures_fetched = 0
    total_finished_fixtures = 0
    total_eligible_candidates = 0
    total_selected = 0
    total_graded = 0
    total_correct = 0

    combined_market_summary = _new_market_summary()

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
            total_fixtures_fetched += res.get("fixtures_fetched", 0)
            total_finished_fixtures += res.get("finished_fixtures", 0)
            total_eligible_candidates += res.get("eligible_candidates", 0)
            total_selected += res.get("selected", 0)
            total_graded += res.get("graded", 0)
            total_correct += res.get("correct", 0)

            _update_market_summary(combined_market_summary, res.get("market_summary", {}))
            all_log.extend(res.get("log", []))

            if res.get("graded", 0) == 0:
                season_limitations[s] = (
                    f"Season {s} returned 0 graded fixtures "
                    f"(fetched {res.get('fixtures_fetched', 0)}, eligible {res.get('eligible_candidates', 0)})."
                )

        except Exception as exc:
            season_limitations[s] = f"Season {s} failed with error: {exc}"

    # Evaluate combined log across seasons
    preds_1x2, acts_1x2, top_picks_1x2 = [], [], []
    preds_dc, acts_dc = [], []
    preds_ou25, acts_ou25 = [], []
    preds_btts, acts_btts = [], []

    for entry in all_log:
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
        sel_dc = selected.get("double_chance")
        m_dist_dc = p_markets.get("double_chance")
        if isinstance(sel_dc, dict) and sel_dc.get("actual") in {"home_or_draw", "away_or_draw", "home_or_away"} and isinstance(m_dist_dc, dict):
            preds_dc.append(m_dist_dc)
            acts_dc.append(sel_dc["actual"])

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

    evaluation = {
        "match_result": {
            "accuracy": total_correct / total_graded if total_graded else 0.0,
            "brier_score": compute_brier_score(preds_1x2, acts_1x2, outcomes=("home_win", "draw", "away_win")),
            "log_loss": compute_log_loss(preds_1x2, acts_1x2, outcomes=("home_win", "draw", "away_win")),
            "calibration": compute_market_calibration(preds_1x2, acts_1x2, outcomes=("home_win", "draw", "away_win")),
            "top_pick_calibration": compute_picked_calibration(top_picks_1x2),
        },
        "double_chance": {
            "accuracy": (
                combined_market_summary.get("double_chance", {}).get("accuracy", 0.0)
            ),
            "brier_score": compute_brier_score(preds_dc, acts_dc, outcomes=("home_or_draw", "away_or_draw", "home_or_away")),
            "log_loss": compute_log_loss(preds_dc, acts_dc, outcomes=("home_or_draw", "away_or_draw", "home_or_away")),
            "calibration": compute_market_calibration(preds_dc, acts_dc, outcomes=("home_or_draw", "away_or_draw", "home_or_away")),
        },
        "over_under_2_5": {
            "accuracy": (
                combined_market_summary.get("over_under", {}).get("2_5", {}).get("accuracy")
                or combined_market_summary.get("over_under", {}).get("2.5", {}).get("accuracy", 0.0)
            ),
            "brier_score": compute_brier_score(preds_ou25, acts_ou25, outcomes=("over", "under")),
            "log_loss": compute_log_loss(preds_ou25, acts_ou25, outcomes=("over", "under")),
            "calibration": compute_market_calibration(preds_ou25, acts_ou25, outcomes=("over", "under")),
        },
        "btts": {
            "accuracy": (
                combined_market_summary.get("btts", {}).get("accuracy", 0.0)
            ),
            "brier_score": compute_brier_score(preds_btts, acts_btts, outcomes=("yes", "no")),
            "log_loss": compute_log_loss(preds_btts, acts_btts, outcomes=("yes", "no")),
            "calibration": compute_market_calibration(preds_btts, acts_btts, outcomes=("yes", "no")),
        },
    }

    return {
        "league_id": league_id,
        "seasons_evaluated": list(seasons),
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
