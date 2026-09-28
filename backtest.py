"""
Football Agent — Real Historical Backtest Engine

Purpose
-------
Run the prediction engine against real historical fixtures and grade the
markets that the agent actually predicts.

Important principles
--------------------
- Historical fixtures are the source of truth.
- No fabricated odds, outcomes, corners, or cards.
- Match-result accuracy remains the primary returned `accuracy` metric for
  backward compatibility with the existing application.
- Additional markets are graded independently and reported separately.
- Every probability distribution produced by the prediction engine is
  retained in the backtest log.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import market_grading
import prediction_engine


# ---------------------------------------------------------------------------
# Paths / configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

BACKTEST_LOG_DIR = BASE_DIR / "data" / "backtests"
BACKTEST_LOG_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> Optional[float]:
    """Return a finite float or None."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(result):
        return None

    return result


def _safe_int(value: Any) -> Optional[int]:
    """Return an integer or None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalise_text(value: Any) -> Optional[str]:
    """Return stripped text or None."""
    if value is None:
        return None

    text = str(value).strip()

    return text or None


def _first_present(
    mapping: Dict[str, Any],
    keys: Iterable[str],
    default: Any = None,
) -> Any:
    """Return the first non-None value found for the supplied keys."""
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]

    return default


def _extract_team_name(team: Any) -> Optional[str]:
    """Extract a team name from common historical-data representations."""
    if isinstance(team, str):
        return _normalise_text(team)

    if not isinstance(team, dict):
        return None

    return _normalise_text(
        _first_present(
            team,
            (
                "name",
                "team_name",
                "short_name",
                "display_name",
            ),
        )
    )


def _extract_score_value(
    score: Any,
    keys: Iterable[str],
) -> Optional[int]:
    """Extract an integer score from a mapping."""
    if not isinstance(score, dict):
        return None

    value = _first_present(score, keys)

    return _safe_int(value)


def _extract_goals(
    fixture: Dict[str, Any],
) -> Tuple[Optional[int], Optional[int]]:
    """
    Extract full-time home and away goals.

    The historical data used by the project has appeared in several
    representations over time, so this function intentionally accepts the
    common variants without inventing values.
    """

    home_score = fixture.get("home_score")
    away_score = fixture.get("away_score")

    home = _safe_int(home_score)
    away = _safe_int(away_score)

    if home is not None and away is not None:
        return home, away

    score = fixture.get("score")

    if isinstance(score, dict):
        home = _extract_score_value(
            score,
            (
                "home",
                "home_score",
                "home_goals",
                "fulltime_home",
                "full_time_home",
            ),
        )

        away = _extract_score_value(
            score,
            (
                "away",
                "away_score",
                "away_goals",
                "fulltime_away",
                "full_time_away",
            ),
        )

        if home is not None and away is not None:
            return home, away

        full_time = score.get("fullTime")

        if isinstance(full_time, dict):
            home = _extract_score_value(
                full_time,
                (
                    "home",
                    "home_score",
                    "home_goals",
                ),
            )

            away = _extract_score_value(
                full_time,
                (
                    "away",
                    "away_score",
                    "away_goals",
                ),
            )

            if home is not None and away is not None:
                return home, away

    return None, None


def _extract_fixture_id(
    fixture: Dict[str, Any],
) -> Optional[str]:
    """Extract the most stable fixture identifier available."""
    value = _first_present(
        fixture,
        (
            "fixture_id",
            "id",
            "match_id",
            "event_id",
        ),
    )

    if value is None:
        return None

    return str(value)


def _extract_fixture_date(
    fixture: Dict[str, Any],
) -> Optional[str]:
    """Extract the fixture date without changing it."""
    value = _first_present(
        fixture,
        (
            "date",
            "fixture_date",
            "match_date",
            "kickoff",
            "kickoff_time",
        ),
    )

    return _normalise_text(value)


def _extract_home_team(
    fixture: Dict[str, Any],
) -> Optional[str]:
    """Extract the historical home-team name."""
    direct = _first_present(
        fixture,
        (
            "home_team",
            "home_name",
            "home",
        ),
    )

    name = _extract_team_name(direct)

    if name:
        return name

    teams = fixture.get("teams")

    if isinstance(teams, dict):
        return _extract_team_name(teams.get("home"))

    return None


def _extract_away_team(
    fixture: Dict[str, Any],
) -> Optional[str]:
    """Extract the historical away-team name."""
    direct = _first_present(
        fixture,
        (
            "away_team",
            "away_name",
            "away",
        ),
    )

    name = _extract_team_name(direct)

    if name:
        return name

    teams = fixture.get("teams")

    if isinstance(teams, dict):
        return _extract_team_name(teams.get("away"))

    return None


def _fixture_is_graded(
    fixture: Dict[str, Any],
) -> bool:
    """A fixture is gradeable only when both full-time scores exist."""
    home_goals, away_goals = _extract_goals(fixture)

    return home_goals is not None and away_goals is not None


def _actual_match_result(
    home_goals: Optional[int],
    away_goals: Optional[int],
) -> Optional[str]:
    """Return Home Win, Draw, or Away Win."""
    if home_goals is None or away_goals is None:
        return None

    if home_goals > away_goals:
        return "Home Win"

    if home_goals < away_goals:
        return "Away Win"

    return "Draw"


def _actual_double_chance(
    home_goals: Optional[int],
    away_goals: Optional[int],
) -> Optional[str]:
    """Return the correct double-chance result."""
    if home_goals is None or away_goals is None:
        return None

    if home_goals > away_goals:
        return "1X"

    if home_goals < away_goals:
        return "X2"

    return "1X"


def _actual_btts(
    home_goals: Optional[int],
    away_goals: Optional[int],
) -> Optional[str]:
    """Return BTTS Yes/No."""
    if home_goals is None or away_goals is None:
        return None

    return "Yes" if home_goals > 0 and away_goals > 0 else "No"


def _actual_total_goals(
    home_goals: Optional[int],
    away_goals: Optional[int],
) -> Optional[int]:
    """Return total goals."""
    if home_goals is None or away_goals is None:
        return None

    return home_goals + away_goals


def _actual_over_under(
    home_goals: Optional[int],
    away_goals: Optional[int],
    line: Any,
) -> Optional[str]:
    """
    Grade an Over/Under line.

    Pushes are represented as `Push` when the total equals the line.
    """
    total = _actual_total_goals(home_goals, away_goals)

    threshold = _safe_float(line)

    if total is None or threshold is None:
        return None

    if total > threshold:
        return "Over"

    if total < threshold:
        return "Under"

    return "Push"


def _actual_team_goals(
    goals: Optional[int],
    line: Any,
) -> Optional[str]:
    """Grade a team-goal Over/Under line."""
    threshold = _safe_float(line)

    if goals is None or threshold is None:
        return None

    if goals > threshold:
        return "Over"

    if goals < threshold:
        return "Under"

    return "Push"


# ---------------------------------------------------------------------------
# Probability helpers
# ---------------------------------------------------------------------------


def _normalise_probability_distribution(
    distribution: Any,
) -> Dict[str, float]:
    """
    Return a clean probability mapping.

    Values are not re-scaled because the prediction engine's original
    distribution is useful for audit purposes exactly as produced.
    """
    if not isinstance(distribution, dict):
        return {}

    cleaned: Dict[str, float] = {}

    for key, value in distribution.items():
        probability = _safe_float(value)

        if probability is None:
            continue

        cleaned[str(key)] = probability

    return cleaned


def _pick_probability(
    probabilities: Any,
) -> Optional[Tuple[str, float]]:
    """Return the highest-probability outcome."""
    distribution = _normalise_probability_distribution(probabilities)

    if not distribution:
        return None

    selected = max(
        distribution,
        key=distribution.get,
    )

    return selected, distribution[selected]


def _pick_binary_line(
    probabilities,
    actual_distribution,
    over_key,
    under_key,
):
    if not isinstance(probabilities, dict):
        return None

    candidates = {}

    for key in (over_key, under_key):
        value = probabilities.get(key)

        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            candidates[key] = value

    if not candidates:
        return None

    selected = max(
        candidates,
        key=candidates.get,
    )

    actual = (
        actual_distribution.get(selected)
        if isinstance(actual_distribution, dict)
        else None
    )

    if not isinstance(actual, dict):
        return {
            "pick": selected,
            "probability": candidates[selected],
            "won": None,
            "outcome": None,
        }

    return {
        "pick": selected,
        "probability": candidates[selected],
        "won": (
            selected
            == actual.get("outcome")
            if actual.get("outcome") is not None
            else None
        ),
        "outcome": actual.get("outcome"),
    }


def _grade_prediction_markets(
    prediction_markets,
    fixture,
):
    """
    Grade every probability distribution produced by prediction_engine.

    This grades selected outcomes while retaining the complete original
    probability distributions in the prediction record.
    """
    outcomes = market_grading.grade_goal_markets(
        fixture
        .get("goals", {})
        .get("home"),
        fixture
        .get("goals", {})
        .get("away"),
        )
            if not isinstance(prediction_markets, dict):
        return {}

    graded: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Match result
    # ------------------------------------------------------------------

    match_result = _normalise_probability_distribution(
        prediction_markets.get("match_result")
    )

    if match_result:
        graded["match_result"] = {
            "probabilities": match_result,
            "pick": max(
                match_result,
                key=match_result.get,
            ),
            "probability": match_result[
                max(match_result, key=match_result.get)
            ],
            "actual": _actual_match_result(
                *_extract_goals(fixture)
            ),
        }

        graded["match_result"]["won"] = (
            graded["match_result"]["pick"]
            == graded["match_result"]["actual"]
        )

    # ------------------------------------------------------------------
    # Double chance
    # ------------------------------------------------------------------

    double_chance = _normalise_probability_distribution(
        prediction_markets.get("double_chance")
    )

    if double_chance:
        pick = max(
            double_chance,
            key=double_chance.get,
        )

        graded["double_chance"] = {
            "probabilities": double_chance,
            "pick": pick,
            "probability": double_chance[pick],
            "actual": _actual_double_chance(
                *_extract_goals(fixture)
            ),
        }

        graded["double_chance"]["won"] = (
            pick == graded["double_chance"]["actual"]
        )

    # ------------------------------------------------------------------
    # BTTS
    # ------------------------------------------------------------------

    btts = _normalise_probability_distribution(
        prediction_markets.get("btts")
    )

    if btts:
        pick = max(
            btts,
            key=btts.get,
        )

        graded["btts"] = {
            "probabilities": btts,
            "pick": pick,
            "probability": btts[pick],
            "actual": _actual_btts(
                *_extract_goals(fixture)
            ),
        }

        graded["btts"]["won"] = (
            pick == graded["btts"]["actual"]
        )

    # ------------------------------------------------------------------
    # Over / Under
    # ------------------------------------------------------------------

    over_under = prediction_markets.get("over_under")

    if isinstance(over_under, dict):
        graded_over_under: Dict[str, Any] = {}

        home_goals, away_goals = _extract_goals(fixture)

        for line, probabilities in over_under.items():
            distribution = _normalise_probability_distribution(
                probabilities
            )

            if not distribution:
                continue

            pick = max(
                distribution,
                key=distribution.get,
            )

            graded_over_under[str(line)] = {
                "probabilities": distribution,
                "pick": pick,
                "probability": distribution[pick],
                "actual": _actual_over_under(
                    home_goals,
                    away_goals,
                    line,
                ),
            }

            graded_over_under[str(line)]["won"] = (
                graded_over_under[str(line)]["actual"] == pick
            )

        if graded_over_under:
            graded["over_under"] = graded_over_under

    # ------------------------------------------------------------------
    # Team goals
    # ------------------------------------------------------------------

    team_goals = prediction_markets.get("team_goals")

    if isinstance(team_goals, dict):
        graded_team_goals: Dict[str, Any] = {}

        home_goals, away_goals = _extract_goals(fixture)

        for team, team_lines in team_goals.items():
            if not isinstance(team_lines, dict):
                continue

            team_result: Dict[str, Any] = {}

            if str(team).lower() in {
                "home",
                "home_team",
            }:
                actual_goals = home_goals
            elif str(team).lower() in {
                "away",
                "away_team",
            }:
                actual_goals = away_goals
            else:
                actual_goals = None

            for line, probabilities in team_lines.items():
                distribution = _normalise_probability_distribution(
                    probabilities
                )

                if not distribution:
                    continue

                pick = max(
                    distribution,
                    key=distribution.get,
                )

                team_result[str(line)] = {
                    "probabilities": distribution,
                    "pick": pick,
                    "probability": distribution[pick],
                    "actual": _actual_team_goals(
                        actual_goals,
                        line,
                    ),
                }

                team_result[str(line)]["won"] = (
                    team_result[str(line)]["actual"] == pick
                )

            if team_result:
                graded_team_goals[str(team)] = team_result

        if graded_team_goals:
            graded["team_goals"] = graded_team_goals

    # ------------------------------------------------------------------
    # Scorelines
    # ------------------------------------------------------------------

    scorelines = _normalise_probability_distribution(
        prediction_markets.get("scoreline")
        or prediction_markets.get("scorelines")
        or prediction_markets.get("correct_score")
    )

    if scorelines:
        home_goals, away_goals = _extract_goals(fixture)

        actual_scoreline = None

        if home_goals is not None and away_goals is not None:
            actual_scoreline = f"{home_goals}-{away_goals}"

        pick = max(
            scorelines,
            key=scorelines.get,
        )

        graded["scoreline"] = {
            "probabilities": scorelines,
            "pick": pick,
            "probability": scorelines[pick],
            "actual": actual_scoreline,
            "won": (
                pick == actual_scoreline
                if actual_scoreline is not None
                else None
            ),
        }

    return graded


# ---------------------------------------------------------------------------
# Market summary helpers
# ---------------------------------------------------------------------------


def _new_market_summary() -> Dict[str, Dict[str, Any]]:
    """Create the market-by-market summary container."""
    return {}


def _update_summary_entry(
    summary: Dict[str, Dict[str, Any]],
    market: str,
    won: Optional[bool],
) -> None:
    """Update one market summary entry."""
    if market not in summary:
        summary[market] = {
            "graded": 0,
            "correct": 0,
            "accuracy": 0.0,
        }

    if won is None:
        return

    summary[market]["graded"] += 1

    if won:
        summary[market]["correct"] += 1

    graded = summary[market]["graded"]

    summary[market]["accuracy"] = (
        summary[market]["correct"] / graded
        if graded
        else 0.0
    )


def _update_market_summary(
    summary: Dict[str, Dict[str, Any]],
    graded_markets: Dict[str, Any],
) -> None:
    """Update the global market summary from one fixture."""
    if not isinstance(graded_markets, dict):
        return

    for market, result in graded_markets.items():
        if market in {
            "match_result",
            "double_chance",
            "btts",
            "scoreline",
        }:
            if isinstance(result, dict):
                _update_summary_entry(
                    summary,
                    market,
                    result.get("won"),
                )

            continue

        if market in {
            "over_under",
            "team_goals",
        }:
            if not isinstance(result, dict):
                continue

            for line_result in result.values():
                if market == "team_goals":
                    if isinstance(line_result, dict):
                        for item in line_result.values():
                            if isinstance(item, dict):
                                _update_summary_entry(
                                    summary,
                                    f"{market}",
                                    item.get("won"),
                                )
                elif isinstance(line_result, dict):
                    _update_summary_entry(
                        summary,
                        f"{market} {line_result.get('line', '')}".strip(),
                        line_result.get("won"),
                    )


# ---------------------------------------------------------------------------
# Statistical enrichment
# ---------------------------------------------------------------------------


def _statistical_actuals(
    fixture: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Extract real statistical outcomes when present.

    Missing statistics remain missing. This function never estimates or
    fabricates historical corners/cards.
    """
    result: Dict[str, Any] = {}

    for key in (
        "corners",
        "cards",
        "yellow_cards",
        "red_cards",
        "shots",
        "shots_on_target",
        "possession",
        "fouls",
    ):
        if key in fixture and fixture[key] is not None:
            result[key] = fixture[key]

    statistics_data = fixture.get("statistics")

    if isinstance(statistics_data, dict):
        for key, value in statistics_data.items():
            if value is not None:
                result.setdefault(key, value)

    return result


def _prepare_statistical_enrichment(
    fixture: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Prepare real historical statistics for reporting.

    The backtest may report availability, but it must not convert absent
    historical data into fake outcomes.
    """
    actuals = _statistical_actuals(fixture)

    return {
        "available": bool(actuals),
        "data": actuals,
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_probability(
    probability: Any,
) -> str:
    """Format probability for human-readable backtest output."""
    value = _safe_float(probability)

    if value is None:
        return "N/A"

    if abs(value) <= 1:
        return f"{value:.1%}"

    return f"{value:.2f}"


def _print_market_prediction_block(
    market: str,
    prediction: Dict[str, Any],
) -> None:
    """Print one market prediction."""
    if not isinstance(prediction, dict):
        return

    pick = prediction.get("pick")
    probability = prediction.get("probability")

    print(
        f"  {market}: "
        f"{pick} "
        f"({_format_probability(probability)})"
    )


def _print_market_grading_block(
    market: str,
    prediction: Dict[str, Any],
) -> None:
    """Print one market prediction and its historical result."""
    if not isinstance(prediction, dict):
        return

    pick = prediction.get("pick")
    probability = prediction.get("probability")
    actual = prediction.get("actual")
    won = prediction.get("won")

    if won is True:
        status = "CORRECT"
    elif won is False:
        status = "WRONG"
    else:
        status = "UNGRADED"

    print(
        f"  {market}: "
        f"pick={pick} "
        f"prob={_format_probability(probability)} "
        f"actual={actual} "
        f"[{status}]"
    )


def _print_market_backtest_report(
    result: Dict[str, Any],
) -> None:
    """
    Print the complete market-by-market backtest report.

    This deliberately avoids collapsing different markets into one artificial
    accuracy number.
    """
    print()
    print("=== FULL MARKET BACKTEST REPORT ===")
    print()

    print(
        f"Fixtures graded: "
        f"{result.get('graded', 0)}"
    )

    print(
        f"Sample requested: "
        f"{result.get('sample_size', 0)}"
    )

    print()
    print("MARKET SUMMARY")
    print("-" * 72)
    print(
        f"{'Market':30}"
        f"{'Graded':>10}"
        f"{'Correct':>10}"
        f"{'Accuracy':>12}"
    )
    print("-" * 72)

    market_summary = result.get(
        "market_summary",
        {},
    )

    for market, summary in market_summary.items():
        if not isinstance(summary, dict):
            continue

        print(
            f"{market:30}"
            f"{summary.get('graded', 0):>10}"
            f"{summary.get('correct', 0):>10}"
            f"{summary.get('accuracy', 0.0):>11.1%}"
        )

    print("-" * 72)

    statistical_available = result.get(
        "statistical_fixtures",
        0,
    )

    statistical_total = result.get(
        "graded",
        0,
    )

    print()
    print("STATISTICAL DATA AVAILABILITY")
    print(
        f"Fixtures containing real historical statistical data: "
        f"{statistical_available}/{statistical_total}"
    )

    print()
    print("FIXTURE-BY-FIXTURE MARKET DETAIL")
    print("=" * 72)

    for index, entry in enumerate(
        result.get("log", []),
        start=1,
    ):
        fixture = entry.get("fixture", {})
        predictions = entry.get("graded_markets", {})

        home_team = (
            fixture.get("home_team")
            or "Unknown Home"
        )

        away_team = (
            fixture.get("away_team")
            or "Unknown Away"
        )

        date = fixture.get("date") or ""

        print()
        print(
            f"[{index}] "
            f"{home_team} vs {away_team}"
        )

        if date:
            print(f"  Date: {date}")

        home_goals = fixture.get("home_goals")
        away_goals = fixture.get("away_goals")

        if (
            home_goals is not None
            and away_goals is not None
        ):
            print(
                f"  Final score: "
                f"{home_goals}-{away_goals}"
            )

        for market, prediction in predictions.items():
            if market in {
                "match_result",
                "double_chance",
                "btts",
                "scoreline",
            }:
                _print_market_grading_block(
                    market,
                    prediction,
                )

            elif market == "over_under":
                if isinstance(prediction, dict):
                    for line, line_result in prediction.items():
                        _print_market_grading_block(
                            f"over_under {line}",
                            line_result,
                        )

            elif market == "team_goals":
                if isinstance(prediction, dict):
                    for team, team_result in prediction.items():
                        if not isinstance(
                            team_result,
                            dict,
                        ):
                            continue

                        for line, line_result in team_result.items():
                            _print_market_grading_block(
                                f"team_goals {team} {line}",
                                line_result,
                            )

        statistics_data = entry.get(
            "statistical_enrichment",
            {},
        )

        if statistics_data.get("available"):
            print(
                "  Historical statistics: "
                "AVAILABLE"
            )
        else:
            print(
                "  Historical statistics: "
                "NOT AVAILABLE"
            )

    print()
    print("=== END FULL MARKET BACKTEST REPORT ===")
    print()
# ---------------------------------------------------------------------------
# Prediction-engine integration
# ---------------------------------------------------------------------------


def _call_prediction_engine(
    fixture: Dict[str, Any],
    historical_data: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Call the prediction engine using the available public interface.

    The adapter intentionally supports the common interfaces used by the
    project so the backtest remains isolated from transport/UI concerns.
    """
    candidates = (
        "predict_fixture",
        "predict",
        "generate_prediction",
        "run_prediction",
    )

    last_error: Optional[Exception] = None

    for function_name in candidates:
        function = getattr(
            prediction_engine,
            function_name,
            None,
        )

        if not callable(function):
            continue

        calls = [
            lambda: function(
                fixture,
                historical_data=historical_data,
            ),
            lambda: function(
                fixture,
                historical_data,
            ),
            lambda: function(fixture),
        ]

        for call in calls:
            try:
                result = call()

                if isinstance(result, dict):
                    return result

            except TypeError as exc:
                last_error = exc
                continue

    if last_error is not None:
        raise last_error

    raise AttributeError(
        "prediction_engine does not expose a supported prediction "
        "function."
    )


# ---------------------------------------------------------------------------
# Historical fixture preparation
# ---------------------------------------------------------------------------


def _prepare_fixture_for_prediction(
    fixture: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Create the prediction input without modifying the original fixture.

    Historical result fields remain available for grading, while the
    prediction engine receives the same fixture information it expects.
    """
    return dict(fixture)


def _extract_prediction_markets(
    prediction: Dict[str, Any],
) -> Dict[str, Any]:
    """Extract the market container from a prediction-engine response."""
    if not isinstance(prediction, dict):
        return {}

    for key in (
        "markets",
        "prediction_markets",
        "predictions",
    ):
        value = prediction.get(key)

        if isinstance(value, dict):
            return value

    # Some prediction-engine responses place markets directly at the root.
    known_markets = {
        "match_result",
        "double_chance",
        "btts",
        "over_under",
        "team_goals",
        "scoreline",
        "scorelines",
        "correct_score",
    }

    direct = {
        key: value
        for key, value in prediction.items()
        if key in known_markets
    }

    return direct


# ---------------------------------------------------------------------------
# Backtest execution
# ---------------------------------------------------------------------------


def run_real_backtest(
    league_id: Any,
    season: Any,
    sample_size: int = 50,
) -> Dict[str, Any]:
    """
    Run the historical backtest.

    Parameters
    ----------
    league_id:
        Historical league identifier or name accepted by the project's
        historical-data layer.

    season:
        Historical season identifier.

    sample_size:
        Maximum number of historical fixtures to grade.

    Returns
    -------
    dict
        Backward-compatible result containing the primary 1X2 accuracy plus
        independent market summaries and the complete backtest log.
    """
    # Import lazily so importing this module does not unnecessarily trigger
    # the historical-data stack.
    try:
        import historical_data
    except ImportError:
        historical_data = None

    candidates: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Obtain historical fixtures
    # ------------------------------------------------------------------

    if historical_data is not None:
        loaders = (
            "load_historical_fixtures",
            "get_historical_fixtures",
            "fetch_historical_fixtures",
            "load_fixtures",
        )

        last_loader_error: Optional[Exception] = None

        for loader_name in loaders:
            loader = getattr(
                historical_data,
                loader_name,
                None,
            )

            if not callable(loader):
                continue

            calls = [
                lambda loader=loader: loader(
                    league_id,
                    season,
                ),
                lambda loader=loader: loader(
                    league_id=league_id,
                    season=season,
                ),
                lambda loader=loader: loader(
                    season=season,
                    league=league_id,
                ),
            ]

            loaded = False

            for call in calls:
                try:
                    result = call()

                    if isinstance(result, dict):
                        for key in (
                            "fixtures",
                            "matches",
                            "data",
                            "results",
                        ):
                            if isinstance(
                                result.get(key),
                                list,
                            ):
                                candidates = result[key]
                                loaded = True
                                break

                    elif isinstance(result, list):
                        candidates = result
                        loaded = True

                    if loaded:
                        break

                except TypeError as exc:
                    last_loader_error = exc
                    continue

            if loaded:
                break

        if not candidates and last_loader_error is not None:
            raise last_loader_error

    if not isinstance(candidates, list):
        candidates = []

    # Only historical fixtures with real final scores are eligible for
    # grading. Never manufacture outcomes for incomplete fixtures.
    candidates = [
        fixture
        for fixture in candidates
        if isinstance(fixture, dict)
        and _fixture_is_graded(fixture)
    ]

    # Keep deterministic ordering for reproducible backtests.
    candidates.sort(
        key=lambda fixture: (
            _extract_fixture_date(fixture) or "",
            _extract_fixture_id(fixture) or "",
        )
    )

    if sample_size is not None:
        try:
            requested_sample = max(
                0,
                int(sample_size),
            )
        except (TypeError, ValueError):
            requested_sample = len(candidates)

        if requested_sample:
            candidates = candidates[:requested_sample]

    # ------------------------------------------------------------------
    # Grade fixtures
    # ------------------------------------------------------------------

    log: List[Dict[str, Any]] = []

    market_summary = _new_market_summary()

    correct = 0
    graded = 0

    statistical_fixtures = 0

    for fixture in candidates:
        home_goals, away_goals = _extract_goals(
            fixture
        )

        fixture_record = {
            "fixture_id": _extract_fixture_id(
                fixture
            ),
            "date": _extract_fixture_date(
                fixture
            ),
            "home_team": _extract_home_team(
                fixture
            ),
            "away_team": _extract_away_team(
                fixture
            ),
            "home_goals": home_goals,
            "away_goals": away_goals,
        }

        prediction_input = _prepare_fixture_for_prediction(
            fixture
        )

        prediction = _call_prediction_engine(
            prediction_input
        )

        prediction_markets = _extract_prediction_markets(
            prediction
        )

        graded_markets = _grade_prediction_markets(
            prediction_markets,
            fixture,
        )

        # --------------------------------------------------------------
        # Primary 1X2 metric
        # --------------------------------------------------------------

        match_result = graded_markets.get(
            "match_result"
        )

        if isinstance(match_result, dict):
            match_won = match_result.get("won")

            if match_won is not None:
                graded += 1

                if match_won:
                    correct += 1

        # --------------------------------------------------------------
        # Independent market metrics
        # --------------------------------------------------------------

        _update_market_summary(
            market_summary,
            graded_markets,
        )

        # --------------------------------------------------------------
        # Statistical availability
        # --------------------------------------------------------------

        statistical_enrichment = (
            _prepare_statistical_enrichment(
                fixture
            )
        )

        if statistical_enrichment.get("available"):
            statistical_fixtures += 1

        # --------------------------------------------------------------
        # Full audit record
        # --------------------------------------------------------------

        log.append(
            {
                "fixture": fixture_record,
                "prediction": prediction,
                "prediction_markets": prediction_markets,
                "graded_markets": graded_markets,
                "statistical_enrichment": (
                    statistical_enrichment
                ),
            }
        )

    # ------------------------------------------------------------------
    # Result
    # ------------------------------------------------------------------

    result = {
        "graded": graded,
        "correct": correct,
        "accuracy": (
            correct / graded
            if graded
            else 0
        ),
        "sample_size": len(candidates),
        "league_id": league_id,
        "season": season,
        "market_summary": market_summary,
        "statistical_fixtures": statistical_fixtures,
        "log": log,
    }

    # ------------------------------------------------------------------
    # Persist audit log
    # ------------------------------------------------------------------

    timestamp = datetime.utcnow().strftime(
        "%Y%m%d_%H%M%S"
    )

    safe_league = (
        str(league_id)
        .replace("/", "_")
        .replace(" ", "_")
    )

    safe_season = (
        str(season)
        .replace("/", "_")
        .replace(" ", "_")
    )

    log_path = (
        BACKTEST_LOG_DIR
        / (
            f"backtest_"
            f"{safe_league}_"
            f"{safe_season}_"
            f"{timestamp}.json"
        )
    )

    try:
        with log_path.open(
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                result,
                handle,
                indent=2,
                ensure_ascii=False,
                default=str,
            )

        result["log_path"] = str(log_path)

    except OSError:
        # A reporting failure must not destroy an otherwise valid backtest
        # result.
        result["log_path"] = None

    # ------------------------------------------------------------------
    # Human-readable report
    # ------------------------------------------------------------------

    _print_market_backtest_report(
        result
    )

    return result


# ---------------------------------------------------------------------------
# Compatibility aliases
# ---------------------------------------------------------------------------


def run_backtest(
    league_id: Any,
    season: Any,
    sample_size: int = 50,
) -> Dict[str, Any]:
    """Compatibility wrapper for callers using the shorter function name."""
    return run_real_backtest(
        league_id=league_id,
        season=season,
        sample_size=sample_size,
    )


def backtest(
    league_id: Any,
    season: Any,
    sample_size: int = 50,
) -> Dict[str, Any]:
    """Compatibility wrapper for legacy callers."""
    return run_real_backtest(
        league_id=league_id,
        season=season,
        sample_size=sample_size,
    )


# ---------------------------------------------------------------------------
# Command-line entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Run the Football Agent historical backtest."
        )
    )

    parser.add_argument(
        "--league-id",
        default=None,
        help="Historical league identifier.",
    )

    parser.add_argument(
        "--league-name",
        default=None,
        help="Historical league name.",
    )

    parser.add_argument(
        "--season",
        required=True,
        help="Historical season.",
    )

    parser.add_argument(
        "--sample",
        type=int,
        default=50,
        help="Maximum number of fixtures to grade.",
    )

    args = parser.parse_args()

    league = (
        args.league_id
        if args.league_id is not None
        else args.league_name
    )

    if league is None:
        parser.error(
            "Either --league-id or --league-name is required."
        )

    output = run_real_backtest(
        league_id=league,
        season=args.season,
        sample_size=args.sample,
    )

    print(
        f"Backtest accuracy: "
        f"{output['accuracy']:.1%} "
        f"({output['correct']}/"
        f"{output['graded']})"
)
