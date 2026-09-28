"""
Backtesting and prediction-building helpers.

Historical backtesting uses the authoritative prediction_engine so that
the mathematical prediction path is shared with production.

Historical feature construction is strictly cutoff-safe:
- completed matches only;
- timestamps strictly before the prediction cutoff;
- historical league average;
- historical season statistics;
- historical recent form;
- historical H2H;
- chronological historical Elo.

The legacy API-backed helper functions remain available for the existing
production path until main.py is migrated to prediction_engine.
"""

from __future__ import annotations

import random

import api_football
import confidence
import config
import historical_elo
import historical_features
import historical_h2h
import poisson_model
import prediction_engine


def estimate_expected_goals_from_stats(
    team_stats_for,
    team_stats_against,
    league_avg_goals,
):
    """Convert current API team statistics into attack/defence ratios."""
    if not isinstance(team_stats_for, dict):
        team_stats_for = {}

    if not isinstance(team_stats_against, dict):
        team_stats_against = {}

    if league_avg_goals <= 0:
        raise ValueError(
            "league_avg_goals must be positive."
        )

    goals_for_avg = (
        team_stats_for
        .get("goals", {})
        .get("for", {})
        .get("average", {})
        .get("total")
    )

    goals_against_avg = (
        team_stats_against
        .get("goals", {})
        .get("against", {})
        .get("average", {})
        .get("total")
    )

    try:
        goals_for_avg = (
            float(goals_for_avg)
            if goals_for_avg not in (None, "")
            else league_avg_goals
        )
    except (TypeError, ValueError):
        goals_for_avg = league_avg_goals

    try:
        goals_against_avg = (
            float(goals_against_avg)
            if goals_against_avg not in (None, "")
            else league_avg_goals
        )
    except (TypeError, ValueError):
        goals_against_avg = league_avg_goals

    return (
        goals_for_avg / league_avg_goals,
        goals_against_avg / league_avg_goals,
    )


def estimate_recent_form_goals(
    team_id,
    league_avg_goals,
    last=None,
):
    """
    Legacy production helper.

    Historical backtesting must not call this function because it retrieves
    current API history rather than reconstructing history as of a past
    cutoff.
    """
    if league_avg_goals <= 0:
        raise ValueError(
            "league_avg_goals must be positive."
        )

    last = last or config.RECENT_FORM_MATCHES
    matches = api_football.get_recent_form(
        team_id,
        last=last,
    )

    goals_for = []
    goals_against = []

    for match in matches:
        home_id = match["teams"]["home"]["id"]
        away_id = match["teams"]["away"]["id"]
        home_goals = match["goals"]["home"]
        away_goals = match["goals"]["away"]

        if home_goals is None or away_goals is None:
            continue

        if home_id == team_id:
            goals_for.append(home_goals)
            goals_against.append(away_goals)

        elif away_id == team_id:
            goals_for.append(away_goals)
            goals_against.append(home_goals)

    if not goals_for:
        return 1.0, 1.0

    avg_for = sum(goals_for) / len(goals_for)
    avg_against = sum(goals_against) / len(goals_against)

    return (
        avg_for / league_avg_goals,
        avg_against / league_avg_goals,
    )


def estimate_head_to_head_goals(
    home_id,
    away_id,
    league_avg_goals,
    last=None,
):
    """
    Legacy production H2H helper.

    Historical backtesting uses historical_h2h instead so that only
    pre-cutoff meetings are visible.
    """
    if league_avg_goals <= 0:
        raise ValueError(
            "league_avg_goals must be positive."
        )

    last = last or config.HEAD_TO_HEAD_MATCHES

    matches = api_football.get_head_to_head(
        home_id,
        away_id,
        last=last,
    )

    home_goals_for = []
    away_goals_for = []

    for match in matches:
        match_home_id = match["teams"]["home"]["id"]
        home_goals = match["goals"]["home"]
        away_goals = match["goals"]["away"]

        if home_goals is None or away_goals is None:
            continue

        if match_home_id == home_id:
            home_goals_for.append(home_goals)
            away_goals_for.append(away_goals)

        elif match["teams"]["away"]["id"] == home_id:
            home_goals_for.append(away_goals)
            away_goals_for.append(home_goals)

    if not home_goals_for:
        return 1.0, 1.0, 1.0, 1.0

    home_avg = sum(home_goals_for) / len(home_goals_for)
    away_avg = sum(away_goals_for) / len(away_goals_for)

    return (
        home_avg / league_avg_goals,
        1.0,
        away_avg / league_avg_goals,
        1.0,
    )


def blend_three(
    season_ratio,
    recent_ratio,
    h2h_ratio,
):
    """
    Compatibility wrapper around the authoritative configured blend.
    """
    return prediction_engine.blend_signal(
        season_ratio,
        recent_ratio,
        h2h_ratio,
    )


def estimate_avg_cards(
    team_stats,
    league_avg_cards=3.8,
):
    """Estimate average yellow cards from current API statistics."""
    if not isinstance(team_stats, dict):
        return league_avg_cards

    try:
        yellow = (
            team_stats
            .get("cards", {})
            .get("yellow", {})
        )

        total_yellow = sum(
            value.get("total") or 0
            for value in yellow.values()
            if isinstance(value, dict)
        )

        fixtures_played = (
            team_stats
            .get("fixtures", {})
            .get("played", {})
            .get("total")
        )

        if fixtures_played and fixtures_played > 0:
            return total_yellow / fixtures_played

    except (TypeError, AttributeError):
        pass

    return league_avg_cards


def predict_match(
    home_attack,
    home_defense,
    away_attack,
    away_defense,
    league_avg_goals,
):
    """
    Compatibility wrapper for the shared mathematical prediction engine.

    This helper has no H2H/Elo context, so it supplies neutral H2H values.
    """
    features = {
        "home_attack": home_attack,
        "home_defence": home_defense,
        "away_attack": away_attack,
        "away_defence": away_defense,
        "league_avg_goals": league_avg_goals,
    }

    result = prediction_engine.predict_from_features(
        features
    )

    conf = confidence.confidence_flag(
        result["markets"]["match_result"]
    )

    return result["markets"], conf


def run_synthetic_selftest():
    """Run a small deterministic mathematical self-test."""
    league_avg_goals = 1.4

    markets, conf = predict_match(
        home_attack=1.6,
        home_defense=0.7,
        away_attack=0.7,
        away_defense=1.4,
        league_avg_goals=league_avg_goals,
    )

    print(
        "=== Synthetic self-test: "
        "strong home side vs weak away side ==="
    )

    print(
        f"Expected goals -> "
        f"Home: {markets['expected_goals']['home']}, "
        f"Away: {markets['expected_goals']['away']}"
    )

    total_prob = sum(
        markets["match_result"].values()
    )

    assert 0.99 <= total_prob <= 1.01, (
        f"Probabilities don't sum to 1: {total_prob}"
    )

    assert (
        markets["match_result"]["home_win"]
        > markets["match_result"]["away_win"]
    )

    print("Self-test passed: math checks out.")

    return markets, conf


def _compute_stats_as_of(
    all_fixtures,
    team_id,
    cutoff,
):
    """
    Compatibility wrapper around the authoritative historical feature
    calculation.

    This remains available for existing tests/callers, but run_real_backtest
    no longer uses it directly.
    """
    result = historical_features.team_goal_averages(
        all_fixtures,
        team_id,
        cutoff,
    )

    if result is None:
        return None, None

    return (
        result["goals_for"],
        result["goals_against"],
    )


def _filter_candidates_by_minimum_history(
    finished_fixtures,
    all_fixtures,
    minimum_matches,
):
    """
    Keep fixtures where BOTH participating teams have enough valid,
    completed, pre-cutoff history.
    """
    if (
        not isinstance(minimum_matches, int)
        or isinstance(minimum_matches, bool)
    ):
        raise ValueError(
            "minimum_matches must be a non-negative integer."
        )

    if minimum_matches < 0:
        raise ValueError(
            "minimum_matches must be a non-negative integer."
        )

    eligible = []

    for fixture in finished_fixtures:
        fixture_date = (
            fixture
            .get("fixture", {})
            .get("date", "")
        )

        if not fixture_date:
            continue

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

        if home_id is None or away_id is None:
            continue

        if historical_features.fixture_has_minimum_history(
            all_fixtures,
            home_id,
            away_id,
            fixture_date,
            minimum_matches=minimum_matches,
        ):
            eligible.append(fixture)

    return sorted(
        eligible,
        key=lambda fixture: fixture["fixture"]["date"],
    )


def _sample_backtest_candidates(
    candidates,
    sample_size,
    seed=42,
):
    """
    Select a reproducible sample without changing global random state.
    """
    if (
        not isinstance(sample_size, int)
        or isinstance(sample_size, bool)
    ):
        raise ValueError(
            "sample_size must be a positive integer."
        )

    if sample_size <= 0:
        raise ValueError(
            "sample_size must be a positive integer."
        )

    candidates = list(candidates)

    if len(candidates) <= sample_size:
        return candidates.copy()

    rng = random.Random(seed)

    return rng.sample(
        candidates,
        sample_size,
    )


def _historical_prediction_for_fixture(
    all_fixtures,
    match,
    min_prior_matches,
):
    """
    Build a complete leakage-safe prediction for one historical fixture.

    Every feature is reconstructed from the same fixture cutoff.
    """
    cutoff = (
        match
        .get("fixture", {})
        .get("date")
    )

    if not cutoff:
        return None

    home_id = (
        match
        .get("teams", {})
        .get("home", {})
        .get("id")
    )

    away_id = (
        match
        .get("teams", {})
        .get("away", {})
        .get("id")
    )

    if home_id is None or away_id is None:
        return None

    league_avg_goals = (
        historical_features.historical_league_avg_goals(
            all_fixtures,
            cutoff,
        )
    )

    if league_avg_goals is None or league_avg_goals <= 0:
        return None

    historical_snapshot = (
        historical_features.historical_feature_snapshot(
            all_fixtures,
            home_id,
            away_id,
            cutoff,
            minimum_matches=min_prior_matches,
        )
    )

    if historical_snapshot is None:
        return None

    recent_snapshot = (
        historical_features.fixture_recent_form(
            all_fixtures,
            home_id,
            away_id,
            cutoff,
            window=config.RECENT_FORM_MATCHES,
            minimum_matches=min_prior_matches,
        )
    )

    if recent_snapshot is None:
        return None

    h2h_snapshot = (
        historical_h2h.historical_h2h_snapshot(
            all_fixtures,
            home_id,
            away_id,
            cutoff,
            window=config.HEAD_TO_HEAD_MATCHES,
            minimum_matches=0,
        )
    )

    elo_snapshot = (
        historical_elo.fixture_elo_snapshot(
            all_fixtures,
            home_id,
            away_id,
            cutoff,
        )
    )

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
        "cutoff": cutoff,
        "league_avg_goals": league_avg_goals,
        "historical_snapshot": historical_snapshot,
        "recent_snapshot": recent_snapshot,
        "h2h_snapshot": h2h_snapshot,
        "elo_snapshot": elo_snapshot,
    }


def _actual_match_result(match):
    """Convert final goals into the canonical 1X2 outcome key."""
    actual_home = (
        match
        .get("goals", {})
        .get("home")
    )

    actual_away = (
        match
        .get("goals", {})
        .get("away")
    )

    if not isinstance(actual_home, (int, float)):
        return None

    if isinstance(actual_home, bool):
        return None

    if not isinstance(actual_away, (int, float)):
        return None

    if isinstance(actual_away, bool):
        return None

    if actual_home > actual_away:
        return "home_win"

    if actual_home < actual_away:
        return "away_win"

    return "draw"


def run_real_backtest(
    league_id,
    season,
    sample_size=20,
    min_prior_matches=5,
    sample_seed=42,
):
    """
    Run a chronological historical backtest using the authoritative
    prediction engine.

    The API is used only once to retrieve the league fixture dataset.
    Historical features are then calculated locally from stored/fetched
    fixtures, preventing per-fixture feature API calls.
    """
    if not isinstance(league_id, int):
        raise ValueError("league_id must be an integer.")

    if not isinstance(season, int):
        raise ValueError("season must be an integer.")

    all_fixtures = api_football.get_league_fixtures(
        league_id,
        season,
    )

    if not isinstance(all_fixtures, list):
        raise ValueError(
            "League fixture response must be a list."
        )

    print(
        f"DEBUG: fetched {len(all_fixtures)} total fixtures "
        f"for league {league_id}, season {season}"
    )

    finished = sorted(
        [
            fixture
            for fixture in all_fixtures
            if (
                fixture
                .get("fixture", {})
                .get("status", {})
                .get("short")
                == "FT"
            )
        ],
        key=lambda fixture: (
            fixture
            .get("fixture", {})
            .get("date", "")
        ),
    )

    print(
        f"DEBUG: {len(finished)} of those are finished (FT)"
    )

    candidates = _filter_candidates_by_minimum_history(
        finished,
        all_fixtures,
        min_prior_matches,
    )

    print(
        f"DEBUG: {len(candidates)} candidates "
        f"with {min_prior_matches} prior matches "
        f"per team"
    )

    candidates = _sample_backtest_candidates(
        candidates,
        sample_size,
        seed=sample_seed,
    )

    print(
        f"DEBUG: {len(candidates)} candidates selected "
        f"for backtest (seed={sample_seed})"
    )

    correct = 0
    graded = 0
    log = []

    for match in candidates:
        result = _historical_prediction_for_fixture(
            all_fixtures,
            match,
            min_prior_matches,
        )

        if result is None:
            continue

        actual = _actual_match_result(match)

        if actual is None:
            continue

        prediction = result["prediction"]
        markets = prediction["markets"]

        predicted = max(
            markets["match_result"],
            key=markets["match_result"].get,
        )

        is_correct = predicted == actual

        graded += 1
        correct += int(is_correct)

        log.append(
            {
                "fixture_id": (
                    match
                    .get("fixture", {})
                    .get("id")
                ),
                "date": result["cutoff"],
                "match": (
                    f"{match['teams']['home']['name']} "
                    f"{match['goals']['home']}-"
                    f"{match['goals']['away']} "
                    f"{match['teams']['away']['name']}"
                ),
                "predicted": predicted,
                "actual": actual,
                "correct": is_correct,
                "probabilities": dict(
                    markets["match_result"]
                ),
                "expected_goals": dict(
                    prediction["expected_goals"]
                ),
                "league_avg_goals": (
                    result["league_avg_goals"]
                ),
                "h2h_available": (
                    prediction["features"]
                    .get("h2h_available", False)
                ),
                "home_elo": (
                    result["elo_snapshot"]["home_rating"]
                ),
                "away_elo": (
                    result["elo_snapshot"]["away_rating"]
                ),
            }
        )

    return {
        "graded": graded,
        "correct": correct,
        "accuracy": (
            correct / graded
            if graded
            else 0
        ),
        "sample_size": len(candidates),
        "min_prior_matches": min_prior_matches,
        "sample_seed": sample_seed,
        "log": log,
    }


if __name__ == "__main__":
    run_synthetic_selftest()
