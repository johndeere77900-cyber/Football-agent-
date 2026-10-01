"""
Football market outcome grading.

This module contains pure, deterministic grading functions.

Rules:
- Never fabricate missing results.
- Goal-derived markets are graded from final goals.
- Cards/corners require actual supplied statistics.
- Unsupported or unavailable markets return None.
- Market names are explicit and stable for backtesting/reporting.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
import historical_match_policy


def _valid_goal(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    )


def _result_from_goals(home_goals: Any, away_goals: Any) -> Optional[str]:
    if not _valid_goal(home_goals) or not _valid_goal(away_goals):
        return None

    if home_goals > away_goals:
        return "home_win"

    if home_goals < away_goals:
        return "away_win"

    return "draw"


def _binary(value: bool) -> Dict[str, Any]:
    return {
        "outcome": "yes" if value else "no",
        "won": value,
    }


def grade_match_result(
    home_goals: Any,
    away_goals: Any,
) -> Optional[Dict[str, Any]]:
    result = _result_from_goals(home_goals, away_goals)

    if result is None:
        return None

    return {
        "outcome": result,
        "won": True,
    }


def grade_double_chance(
    home_goals: Any,
    away_goals: Any,
) -> Optional[Dict[str, Dict[str, Any]]]:
    result = _result_from_goals(home_goals, away_goals)

    if result is None:
        return None

    return {
        "home_or_draw": {
            "outcome": "home_or_draw",
            "won": result in {"home_win", "draw"},
        },
        "away_or_draw": {
            "outcome": "away_or_draw",
            "won": result in {"away_win", "draw"},
        },
        "home_or_away": {
            "outcome": "home_or_away",
            "won": result in {"home_win", "away_win"},
        },
    }


def grade_over_under(
    home_goals: Any,
    away_goals: Any,
    lines=(1.5, 2.5, 3.5, 4.5, 5.5),
) -> Optional[Dict[str, Dict[str, Any]]]:
    if not _valid_goal(home_goals) or not _valid_goal(away_goals):
        return None

    total = home_goals + away_goals
    results = {}

    for line in lines:
        key_line = str(line).replace(".", "_")

        results[f"over_{key_line}"] = {
            "outcome": "over" if total > line else "under",
            "won": total > line,
        }

        results[f"under_{key_line}"] = {
            "outcome": "under" if total < line else "over",
            "won": total < line,
        }

    return results


def grade_btts(
    home_goals: Any,
    away_goals: Any,
) -> Optional[Dict[str, Dict[str, Any]]]:
    if not _valid_goal(home_goals) or not _valid_goal(away_goals):
        return None

    yes = home_goals >= 1 and away_goals >= 1

    return {
        "yes": _binary(yes),
        "no": _binary(not yes),
    }


def grade_team_goals(
    home_goals: Any,
    away_goals: Any,
    lines=(0.5, 1.5, 2.5),
) -> Optional[Dict[str, Dict[str, Any]]]:
    if not _valid_goal(home_goals) or not _valid_goal(away_goals):
        return None

    results = {}

    for line in lines:
        key_line = str(line).replace(".", "_")

        results[f"home_over_{key_line}"] = {
            "outcome": "over" if home_goals > line else "under",
            "won": home_goals > line,
        }

        results[f"home_under_{key_line}"] = {
            "outcome": "under" if home_goals < line else "over",
            "won": home_goals < line,
        }

        results[f"away_over_{key_line}"] = {
            "outcome": "over" if away_goals > line else "under",
            "won": away_goals > line,
        }

        results[f"away_under_{key_line}"] = {
            "outcome": "under" if away_goals < line else "over",
            "won": away_goals < line,
        }

    return results


def grade_scoreline(
    home_goals: Any,
    away_goals: Any,
) -> Optional[Dict[str, Any]]:
    if not _valid_goal(home_goals) or not _valid_goal(away_goals):
        return None

    return {
        "outcome": f"{int(home_goals)}-{int(away_goals)}",
        "won": True,
    }


def grade_goal_markets(
    home_goals: Any,
    away_goals: Any,
) -> Dict[str, Any]:
    """
    Grade every goal-derived market currently produced by poisson_model.
    """
    return {
        "match_result": grade_match_result(
            home_goals,
            away_goals,
        ),
        "double_chance": grade_double_chance(
            home_goals,
            away_goals,
        ),
        "over_under": grade_over_under(
            home_goals,
            away_goals,
        ),
        "btts": grade_btts(
            home_goals,
            away_goals,
        ),
        "team_goals": grade_team_goals(
            home_goals,
            away_goals,
        ),
        "scoreline": grade_scoreline(
            home_goals,
            away_goals,
        ),
    }


def _extract_statistic_value(
    statistics: Any,
    names: set[str],
) -> Optional[float]:
    if not isinstance(statistics, list):
        return None

    for item in statistics:
        if not isinstance(item, dict):
            continue

        name = str(item.get("type", "")).strip().lower()

        if name not in names:
            continue

        value = item.get("value")

        if value in (None, ""):
            return None

        if isinstance(value, str):
            value = value.replace("%", "").strip()

        try:
            number = float(value)
        except (TypeError, ValueError):
            return None

        if number < 0:
            return None

        return number

    return None


def extract_fixture_statistics(
    fixture: Dict[str, Any],
) -> Dict[str, Optional[float]]:
    """
    Extract corner/card statistics from an enriched API-Football fixture.

    The exact API statistic labels are normalized here.

    This function does not invent missing values.
    """
    statistics = fixture.get("statistics")

    if not isinstance(statistics, list):
        return {
            "home_corners": None,
            "away_corners": None,
            "home_yellow_cards": None,
            "away_yellow_cards": None,
            "home_red_cards": None,
            "away_red_cards": None,
        }

    by_team = {}

    for team_block in statistics:
        if not isinstance(team_block, dict):
            continue

        team_id = (
            team_block
            .get("team", {})
            .get("id")
        )

        if team_id is not None:
            by_team[team_id] = team_block.get(
                "statistics",
                [],
            )

    home_id = (
        fixture
        .get("teams", {})
        .get("home", {})
        .get("id")
    )

    away_id = (
        fixture
        .get("teams", {})
        .get("away", {})
        .get("id")
    )

    home_stats = by_team.get(home_id, [])
    away_stats = by_team.get(away_id, [])

    return {
        "home_corners": _extract_statistic_value(
            home_stats,
            {"corner kicks", "corners"},
        ),
        "away_corners": _extract_statistic_value(
            away_stats,
            {"corner kicks", "corners"},
        ),
        "home_yellow_cards": _extract_statistic_value(
            home_stats,
            {"yellow cards"},
        ),
        "away_yellow_cards": _extract_statistic_value(
            away_stats,
            {"yellow cards"},
        ),
        "home_red_cards": _extract_statistic_value(
            home_stats,
            {"red cards"},
        ),
        "away_red_cards": _extract_statistic_value(
            away_stats,
            {"red cards"},
        ),
    }


def grade_statistical_markets(
    fixture: Dict[str, Any],
    corner_lines=(7.5, 8.5, 9.5, 10.5),
    card_lines=(2.5, 3.5, 4.5, 5.5),
) -> Dict[str, Any]:
    """
    Grade corners/cards only when actual fixture statistics exist.

    No fallback averages are used.
    """
    stats = extract_fixture_statistics(fixture)

    result = {
        "corners": {},
        "cards": {},
    }

    home_corners = stats["home_corners"]
    away_corners = stats["away_corners"]

    if home_corners is not None and away_corners is not None:
        total_corners = home_corners + away_corners

        for line in corner_lines:
            key = str(line).replace(".", "_")

            result["corners"][f"over_{key}"] = {
                "outcome": (
                    "over"
                    if total_corners > line
                    else "under"
                ),
                "won": total_corners > line,
            }

            result["corners"][f"under_{key}"] = {
                "outcome": (
                    "under"
                    if total_corners < line
                    else "over"
                ),
                "won": total_corners < line,
            }

    home_yellow = stats["home_yellow_cards"]
    away_yellow = stats["away_yellow_cards"]
    home_red = stats["home_red_cards"]
    away_red = stats["away_red_cards"]

    if (
        home_yellow is not None
        and away_yellow is not None
        and home_red is not None
        and away_red is not None
    ):
        total_cards = (
            home_yellow
            + away_yellow
            + home_red
            + away_red
        )

        for line in card_lines:
            key = str(line).replace(".", "_")

            result["cards"][f"over_{key}"] = {
                "outcome": (
                    "over"
                    if total_cards > line
                    else "under"
                ),
                "won": total_cards > line,
            }

            result["cards"][f"under_{key}"] = {
                "outcome": (
                    "under"
                    if total_cards < line
                    else "over"
                ),
                "won": total_cards < line,
            }

    return result


def grade_fixture_markets(
    fixture: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Produce the complete currently gradeable market outcome record using centralized match policy goals.
    """
    reg_goals = historical_match_policy.get_regulation_goals(fixture)
    if reg_goals is not None:
        home_goals, away_goals = reg_goals
    else:
        goals = historical_match_policy.get_football_match_goals(fixture)
        if goals is not None:
            home_goals, away_goals = goals
        else:
            home_goals = fixture.get("goals", {}).get("home")
            away_goals = fixture.get("goals", {}).get("away")

    return {
        "goal_markets": grade_goal_markets(
            home_goals,
            away_goals,
        ),
        "statistical_markets": grade_statistical_markets(
            fixture,
        ),
    }
