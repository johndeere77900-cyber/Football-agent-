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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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


def _fixture_date(fixture: Dict[str, Any]) -> str:
    return str(fixture.get("fixture", {}).get("date", ""))


def _fixture_id(fixture: Dict[str, Any]) -> Optional[Any]:
    return fixture.get("fixture", {}).get("id")


def _home_id(fixture: Dict[str, Any]) -> Optional[Any]:
    return fixture.get("teams", {}).get("home", {}).get("id")


def _away_id(fixture: Dict[str, Any]) -> Optional[Any]:
    return fixture.get("teams", {}).get("away", {}).get("id")


def _home_name(fixture: Dict[str, Any]) -> str:
    return str(fixture.get("teams", {}).get("home", {}).get("name", "Unknown Home"))


def _away_name(fixture: Dict[str, Any]) -> str:
    return str(fixture.get("teams", {}).get("away", {}).get("name", "Unknown Away"))


def _goals(fixture: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    goals = fixture.get("goals", {})
    home = goals.get("home")
    away = goals.get("away")
    if not _valid_goal(home) or not _valid_goal(away):
        return None, None
    return int(home), int(away)


def _is_finished(fixture: Dict[str, Any]) -> bool:
    return fixture.get("fixture", {}).get("status", {}).get("short") == "FT"


def _fixture_is_gradeable(fixture: Dict[str, Any]) -> bool:
    home, away = _goals(fixture)
    return home is not None and away is not None


def _normalise_probability_distribution(value: Any) -> Dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: Dict[str, float] = {}
    for key, probability in value.items():
        number = _safe_float(probability)
        if number is not None:
            result[str(key)] = number
    return result


def _probability_pick(distribution: Any) -> Optional[Tuple[str, float]]:
    cleaned = _normalise_probability_distribution(distribution)
    if not cleaned:
        return None
    key = max(cleaned, key=cleaned.get)
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
    return sum(goals_for) / len(goals_for), sum(goals_against) / len(goals_against)


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
        raise ValueError("sample_size must be a positive integer.")

    original = list(candidates)
    if sample_size >= len(original):
        return original

    rng = random.Random(seed)
    return rng.sample(original, sample_size)


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
        raise ValueError("minimum_matches must be a non-negative integer.")

    ordered = sorted(
        [fixture for fixture in finished_fixtures if _fixture_is_gradeable(fixture)],
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
        raise ValueError("min_prior_matches must be a non-negative integer.")

    home_id = _home_id(fixture)
    away_id = _away_id(fixture)
    cutoff = _fixture_date(fixture)

    if home_id is None or away_id is None or not cutoff:
        return None

    historical_snapshot = historical_features.historical_feature_snapshot(
        fixtures,
        home_id,
        away_id,
        cutoff,
        minimum_matches=min_prior_matches,
    )
    recent_snapshot = historical_features.fixture_recent_form(
        fixtures,
        home_id,
        away_id,
        cutoff,
        window=config.RECENT_FORM_MATCHES,
        minimum_matches=min_prior_matches,
    )

    if historical_snapshot is None or recent_snapshot is None:
        return None

    league_avg_goals = historical_features.historical_league_avg_goals(
        fixtures,
        cutoff,
    )
    if league_avg_goals is None or league_avg_goals <= 0:
        return None

    h2h_snapshot = historical_h2h.historical_h2h_snapshot(
        fixtures,
        home_id,
        away_id,
        cutoff,
        window=config.HEAD_TO_HEAD_MATCHES,
        minimum_matches=0,
    )

    elo_snapshot = historical_elo.fixture_elo_snapshot(
        fixtures,
        home_id,
        away_id,
        cutoff,
    )

    prediction = prediction_engine.predict_historical_fixture(
        historical_snapshot=historical_snapshot,
        recent_snapshot=recent_snapshot,
        h2h_snapshot=h2h_snapshot,
        league_avg_goals=league_avg_goals,
        home_elo=elo_snapshot["home_rating"],
        away_elo=elo_snapshot["away_rating"],
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


def _actual_match_result(fixture: Dict[str, Any]) -> Optional[str]:
    home, away = _goals(fixture)
    if home is None or away is None:
        return None
    if home > away:
        return "home_win"
    if home < away:
        return "away_win"
    return "draw"


def _actual_double_chance(fixture: Dict[str, Any]) -> Optional[str]:
    result = _actual_match_result(fixture)
    if result == "home_win" or result == "draw":
        return "home_or_draw"
    if result == "away_win":
        return "away_or_draw"
    return None


def _actual_btts(fixture: Dict[str, Any]) -> Optional[str]:
    home, away = _goals(fixture)
    if home is None or away is None:
        return None
    return "yes" if home >= 1 and away >= 1 else "no"


def _actual_binary_total(total: Optional[float], line: Any) -> Optional[str]:
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
        "won": pick == actual if actual is not None else None,
    }


def _grade_prediction_markets(
    prediction_markets: Dict[str, Any],
    fixture: Dict[str, Any],
) -> Dict[str, Any]:
    """Return selected predictions plus independent actual market outcomes."""
    selected: Dict[str, Any] = {}
    outcomes: Dict[str, Any] = {}

    home, away = _goals(fixture)
    total = (home + away) if home is not None and away is not None else None

    # 1X2
    selected_1x2 = _pick_and_grade(
        prediction_markets.get("match_result"),
        _actual_match_result(fixture),
    )
    if selected_1x2 is not None:
        selected["match_result"] = selected_1x2
    outcomes["match_result"] = market_grading.grade_match_result(home, away)

    # Double Chance
    selected_dc = _pick_and_grade(
        prediction_markets.get("double_chance"),
        _actual_double_chance(fixture),
    )
    if selected_dc is not None:
        selected["double_chance"] = selected_dc
    outcomes["double_chance"] = market_grading.grade_double_chance(home, away)

    # BTTS
    selected_btts = _pick_and_grade(
        prediction_markets.get("btts"),
        _actual_btts(fixture),
    )
    if selected_btts is not None:
        selected["btts"] = selected_btts
    outcomes["btts"] = market_grading.grade_btts(home, away)

    # Over / Under.  The prediction engine uses flat keys such as over_2_5.
    over_under_selected: Dict[str, Any] = {}
    over_under_outcomes = market_grading.grade_over_under(home, away)
    over_under = prediction_markets.get("over_under", {})
    if isinstance(over_under, dict):
        lines: Dict[str, Tuple[float, Dict[str, float]]] = {}
        for key, value in over_under.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if not (str(key).startswith("over_") or str(key).startswith("under_")):
                continue
            parts = str(key).split("_", 1)
            if len(parts) != 2:
                continue
            try:
                line = float(parts[1].replace("_", "."))
            except ValueError:
                continue
            lines.setdefault(str(line), (line, {}))[1][str(key)] = float(value)
        for line_key, (line, distribution) in lines.items():
            actual = _actual_binary_total(total, line)
            picked = _pick_and_grade(distribution, actual)
            if picked is not None:
                over_under_selected[line_key] = picked
    if over_under_selected:
        selected["over_under"] = over_under_selected
    outcomes["over_under"] = over_under_outcomes or {}

    # Team goals.  The prediction engine also uses flat keys.
    team_selected: Dict[str, Any] = {}
    team_goals = prediction_markets.get("team_goals", {})
    if isinstance(team_goals, dict):
        lines: Dict[str, Tuple[float, Dict[str, float]]] = {}
        for key, value in team_goals.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            key_text = str(key)
            parts = key_text.rsplit("_", 2)
            if len(parts) != 3 or parts[0] not in {"home", "away"}:
                continue
            try:
                line = float(f"{parts[1]}.{parts[2]}")
            except ValueError:
                continue
            prefix = parts[0]
            line_key = f"{prefix}_{parts[1]}_{parts[2]}"
            lines.setdefault(line_key, (line, {}))[1][key_text] = float(value)
        for line_key, (line, distribution) in lines.items():
            team = line_key.split("_", 1)[0]
            actual_goals = home if team == "home" else away
            actual = _actual_binary_total(actual_goals, line)
            picked = _pick_and_grade(distribution, actual)
            if picked is not None:
                team_selected[line_key] = picked
    if team_selected:
        selected["team_goals"] = team_selected
    outcomes["team_goals"] = market_grading.grade_team_goals(home, away) or {}

    # Correct score / top scorelines.
    scorelines = prediction_markets.get("top_scorelines")
    if not isinstance(scorelines, list):
        scorelines = prediction_markets.get("scoreline")
    if isinstance(scorelines, list) and scorelines:
        clean_scores = {
            str(item.get("score")): _safe_float(item.get("probability"))
            for item in scorelines
            if isinstance(item, dict) and item.get("score") is not None
            and _safe_float(item.get("probability")) is not None
        }
        actual_score = f"{home}-{away}" if home is not None and away is not None else None
        picked = _pick_and_grade(clean_scores, actual_score)
        if picked is not None:
            selected["scoreline"] = picked
    elif isinstance(scorelines, dict):
        actual_score = f"{home}-{away}" if home is not None and away is not None else None
        picked = _pick_and_grade(scorelines, actual_score)
        if picked is not None:
            selected["scoreline"] = picked
    outcomes["scoreline"] = market_grading.grade_scoreline(home, away)

    return {
        "selected": selected,
        "outcomes": outcomes,
    }


# ---------------------------------------------------------------------------
# Statistical enrichment
# ---------------------------------------------------------------------------


def _statistical_actuals(fixture: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return only real corners/cards present in an enriched API fixture."""
    stats = market_grading.extract_fixture_statistics(fixture)
    corners: Dict[str, Any] = {}
    cards: Dict[str, Any] = {}

    if stats["home_corners"] is not None and stats["away_corners"] is not None:
        corners = {
            "home": stats["home_corners"],
            "away": stats["away_corners"],
            "total": stats["home_corners"] + stats["away_corners"],
        }

    if all(stats[key] is not None for key in (
        "home_yellow_cards", "away_yellow_cards",
        "home_red_cards", "away_red_cards",
    )):
        cards = {
            "home_yellow": stats["home_yellow_cards"],
            "away_yellow": stats["away_yellow_cards"],
            "home_red": stats["home_red_cards"],
            "away_red": stats["away_red_cards"],
            "total": (
                stats["home_yellow_cards"]
                + stats["away_yellow_cards"]
                + stats["home_red_cards"]
                + stats["away_red_cards"]
            ),
        }

    return {"corners": corners, "cards": cards}


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
# Summary/reporting
# ---------------------------------------------------------------------------


def _new_market_summary() -> Dict[str, Any]:
    """Create a summary that supports both aggregate and line-specific markets."""
    return {}


def _summary_entry() -> Dict[str, Any]:
    return {"graded": 0, "correct": 0, "accuracy": 0.0}


def _update_summary_bucket(bucket: Dict[str, Any], won: Optional[bool]) -> None:
    if won is None:
        return
    bucket["graded"] += 1
    if won:
        bucket["correct"] += 1
    bucket["accuracy"] = bucket["correct"] / bucket["graded"]


def _update_market_summary(
    summary: Dict[str, Any],
    selected: Dict[str, Any],
) -> None:
    if not isinstance(selected, dict):
        return

    for market in ("match_result", "double_chance", "btts", "scoreline"):
        item = selected.get(market)
        if isinstance(item, dict):
            bucket = summary.setdefault(market, _summary_entry())
            _update_summary_bucket(bucket, item.get("won"))

    over_under = selected.get("over_under")
    if isinstance(over_under, dict):
        market_bucket = summary.setdefault("over_under", {})
        for line, item in over_under.items():
            if not isinstance(item, dict):
                continue
            line_bucket = market_bucket.setdefault(str(line), _summary_entry())
            _update_summary_bucket(line_bucket, item.get("won"))

    team_goals = selected.get("team_goals")
    if isinstance(team_goals, dict):
        market_bucket = summary.setdefault("team_goals", {})
        for line, item in team_goals.items():
            if not isinstance(item, dict):
                continue
            line_bucket = market_bucket.setdefault(str(line), _summary_entry())
            _update_summary_bucket(line_bucket, item.get("won"))


def _format_probability(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "N/A"
    return f"{number:.1%}" if abs(number) <= 1 else f"{number:.2f}"


def _print_market_summary(summary: Dict[str, Any]) -> None:
    print("\nMARKET SUMMARY")
    print("-" * 72)
    print(f"{'Market':30}{'Graded':>10}{'Correct':>10}{'Accuracy':>12}")
    print("-" * 72)
    for market in ("match_result", "double_chance", "btts", "scoreline"):
        item = summary.get(market)
        if not isinstance(item, dict) or "graded" not in item:
            continue
        print(
            f"{market:30}"
            f"{item.get('graded', 0):>10}"
            f"{item.get('correct', 0):>10}"
            f"{item.get('accuracy', 0.0):>11.1%}"
        )

    for market in ("over_under", "team_goals"):
        lines = summary.get(market, {})
        if not isinstance(lines, dict):
            continue
        for line, item in lines.items():
            if not isinstance(item, dict) or "graded" not in item:
                continue
            label = f"{market} {line}"
            print(
                f"{label:30}"
                f"{item.get('graded', 0):>10}"
                f"{item.get('correct', 0):>10}"
                f"{item.get('accuracy', 0.0):>11.1%}"
            )
    print("-" * 72)


def _print_backtest_report(result: Dict[str, Any]) -> None:
    print("\n=== FULL MARKET BACKTEST REPORT ===")
    print(f"Fixtures fetched: {result['fixtures_fetched']}")
    print(f"Finished fixtures: {result['finished_fixtures']}")
    print(f"Eligible candidates: {result['eligible_candidates']}")
    print(f"Sample requested: {result['requested_sample']}")
    print(f"Fixtures actually graded: {result['graded']}")
    print(f"1X2: {result['market_summary'].get('match_result', _summary_entry())}")
    print(f"Double Chance: {result['market_summary'].get('double_chance', _summary_entry())}")
    print(f"BTTS: {result['market_summary'].get('btts', _summary_entry())}")
    print(f"Over/Under: {result['market_summary'].get('over_under', {})}")
    print(f"Team Goals: {result['market_summary'].get('team_goals', {})}")
    print(f"Scoreline: {result['market_summary'].get('scoreline', _summary_entry())}")
    print(
        "Real statistical availability: "
        f"corners={result['statistical_data_available']['corners']}, "
        f"cards={result['statistical_data_available']['cards']}"
    )
    print(f"Statistics enriched: {result['statistics_enriched']}")
    print(f"No zero-fixture regression: {'PASS' if result['graded'] > 0 else 'FAIL'}")
    _print_market_summary(result["market_summary"])
    print("=== END FULL MARKET BACKTEST REPORT ===\n")


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
    if isinstance(league_id, bool) or not isinstance(league_id, int):
        raise ValueError("league_id must be an integer.")
    if isinstance(season, bool) or not isinstance(season, int) or season < 1900:
        raise ValueError("season must be a valid integer year.")
    if isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size <= 0:
        raise ValueError("sample_size must be a positive integer.")
    if isinstance(min_prior_matches, bool) or not isinstance(min_prior_matches, int) or min_prior_matches < 0:
        raise ValueError("min_prior_matches must be a non-negative integer.")
    if enrich_statistics not in (True, False):
        raise ValueError("enrich_statistics must be a boolean.")
    if sample_seed is not None and (
        isinstance(sample_seed, bool) or not isinstance(sample_seed, int)
    ):
        raise ValueError("sample_seed must be an integer or None.")

    fixtures = api_football.get_league_fixtures(league_id, season)
    if not isinstance(fixtures, list):
        fixtures = []

    fixtures = [fixture for fixture in fixtures if isinstance(fixture, dict)]
    finished = [fixture for fixture in fixtures if _is_finished(fixture)]
    finished.sort(key=_fixture_date)

    eligible = _filter_candidates_by_minimum_history(
        finished,
        fixtures,
        minimum_matches=min_prior_matches,
    )

    selected = _sample_backtest_candidates(
        eligible,
        sample_size,
        seed=sample_seed,
    )
    selected.sort(key=_fixture_date)

    enriched_by_id: Dict[int, Dict[str, Any]] = {}
    statistics_enriched = False
    statistical_data_available = {"corners": 0, "cards": 0}

    if enrich_statistics and selected:
        statistics_enriched = True
        fixture_ids = [
            _fixture_id(fixture)
            for fixture in selected
            if _fixture_id(fixture) is not None
        ]
        enriched = api_football.get_enriched_fixtures(fixture_ids)
        if isinstance(enriched, dict):
            enriched_by_id = {
                int(key): value
                for key, value in enriched.items()
                if isinstance(value, dict) and str(key).lstrip("-").isdigit()
            }

    log: List[Dict[str, Any]] = []
    market_summary = _new_market_summary()
    correct = 0
    graded = 0

    for candidate in selected:
        fixture_id = _fixture_id(candidate)
        enriched_fixture = enriched_by_id.get(int(fixture_id)) if fixture_id is not None else None
        fixture_for_stats = _merge_enriched_fixture(candidate, enriched_fixture)

        historical = _historical_prediction_for_fixture(
            fixtures,
            candidate,
            min_prior_matches=min_prior_matches,
        )
        if historical is None:
            continue

        prediction = historical["prediction"]
        prediction_markets = prediction.get("markets", {})
        market_grading = _grade_prediction_markets(
            prediction_markets,
            candidate,
        )
        selected_markets = market_grading["selected"]

        primary = selected_markets.get("match_result")
        primary_won = primary.get("won") if isinstance(primary, dict) else None
        if primary_won is not None:
            graded += 1
            if primary_won:
                correct += 1

        _update_market_summary(market_summary, selected_markets)

        statistical_actuals = _statistical_actuals(fixture_for_stats)
        if statistical_actuals["corners"]:
            statistical_data_available["corners"] += 1
        if statistical_actuals["cards"]:
            statistical_data_available["cards"] += 1

        match_result = _actual_match_result(candidate)
        predicted = primary.get("pick") if isinstance(primary, dict) else None
        entry = {
            "fixture_id": fixture_id,
            "match": f"{_home_name(candidate)} vs {_away_name(candidate)}",
            "date": _fixture_date(candidate),
            "home_team": _home_name(candidate),
            "away_team": _away_name(candidate),
            "correct": bool(primary_won) if primary_won is not None else False,
            "predicted": predicted,
            "actual": match_result,
            "probabilities": prediction_markets.get("match_result", {}),
            "market_grading": {
                "selected": selected_markets,
                "outcomes": market_grading["outcomes"],
                "statistical_actuals": statistical_actuals,
            },
            "prediction": prediction,
            "historical_features": {
                "historical_snapshot": historical["historical_snapshot"],
                "recent_snapshot": historical["recent_snapshot"],
                "h2h_snapshot": historical["h2h_snapshot"],
                "elo_snapshot": historical["elo_snapshot"],
                "league_avg_goals": historical["league_avg_goals"],
            },
        }
        log.append(entry)

    result = {
        "fixtures_fetched": len(fixtures),
        "finished_fixtures": len(finished),
        "eligible_candidates": len(eligible),
        "requested_sample": sample_size,
        "selected": len(selected),
        "sample_size": len(selected),
        "graded": graded,
        "correct": correct,
        "accuracy": correct / graded if graded else 0.0,
        "league_id": league_id,
        "season": season,
        "min_prior_matches": min_prior_matches,
        "sample_seed": sample_seed,
        "statistics_enriched": statistics_enriched,
        "statistical_data_available": statistical_data_available,
        "market_summary": market_summary,
        "log": log,
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = BACKTEST_LOG_DIR / f"backtest_{league_id}_{season}_{timestamp}.json"
    try:
        log_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        result["log_path"] = str(log_path)
    except OSError:
        result["log_path"] = None

    _print_backtest_report(result)
    return result


# Compatibility aliases.
def run_backtest(league_id: Any, season: Any, sample_size: int = 50, **kwargs: Any) -> Dict[str, Any]:
    return run_real_backtest(league_id, season, sample_size, **kwargs)


def backtest(league_id: Any, season: Any, sample_size: int = 50, **kwargs: Any) -> Dict[str, Any]:
    return run_real_backtest(league_id, season, sample_size, **kwargs)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Football Agent historical backtest.")
    parser.add_argument("--league-id", type=int, default=None)
    parser.add_argument("--league-name", default=None)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--sample", type=int, default=50)
    args = parser.parse_args()

    league_id = args.league_id
    if league_id is None and args.league_name:
        # Direct backtest.py use accepts the common configured league name.
        league_names = {
            "premier league": 39,
            "la liga": 140,
            "serie a": 135,
            "bundesliga": 78,
            "ligue 1": 61,
        }
        league_id = league_names.get(args.league_name.strip().lower())

    if league_id is None:
        parser.error("A supported --league-id or --league-name is required.")

    result = run_real_backtest(league_id, args.season, args.sample)
    print(f"Backtest accuracy: {result['accuracy']:.1%} ({result['correct']}/{result['graded']})")
