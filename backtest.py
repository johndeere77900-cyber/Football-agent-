"""
Backtesting and prediction-building helpers.

Historical backtesting uses the authoritative prediction_engine so that
production and historical predictions share the same mathematical path.

Historical feature construction is strictly cutoff-safe:
- completed matches only;
- timestamps strictly before the prediction cutoff;
- historical league average;
- historical season statistics;
- historical recent form;
- historical H2H;
- chronological historical Elo.

Market grading is performed after prediction generation:
- 1X2;
- Double Chance;
- Over/Under;
- BTTS;
- team goals;
- predicted top scoreline.

Historical corners/cards can optionally be enriched in batches through
API-Football. Missing statistical data is never fabricated.

The backtest report displays every market already produced by the
prediction engine. Reporting does not alter prediction mathematics,
market probabilities, grading, or production logic.
"""

from __future__ import annotations

import random

import api_football
import confidence
import config
import historical_elo
import historical_features
import historical_h2h
import market_grading
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
    """Compatibility wrapper around the authoritative configured blend."""
    return prediction_engine.blend_signal(
        season_ratio,
        recent_ratio,
        h2h_ratio,
    )


def estimate_avg_cards(
    team_stats,
    league_avg_cards=3.8,
):
    """Legacy current-statistics card estimator."""
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
    """Compatibility wrapper around historical feature calculation."""
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
    """Select a reproducible sample without changing global RNG state."""
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


def _fixture_id(match):
    value = (
        match
        .get("fixture", {})
        .get("id")
    )

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pick_single_distribution(
    probabilities,
    actual_distribution,
):
    """
    Grade the highest-probability outcome in one distribution.

    The full probability distribution remains untouched elsewhere.
    """
    if not isinstance(probabilities, dict):
        return None

    valid = {
        key: value
        for key, value in probabilities.items()
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
    }

    if not valid:
        return None

    selected = max(
        valid,
        key=valid.get,
    )

    actual = (
        actual_distribution.get(selected)
        if isinstance(actual_distribution, dict)
        else None
    )

    if not isinstance(actual, dict):
        return {
            "pick": selected,
            "probability": valid[selected],
            "won": None,
            "outcome": None,
        }

    return {
        "pick": selected,
        "probability": valid[selected],
        "won": (
            selected
            == actual.get("outcome")
            if actual.get("outcome") is not None
            else None
        ),
        "outcome": actual.get("outcome"),
    }


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

    selected = {}

    selected["match_result"] = _pick_single_distribution(
        prediction_markets.get("match_result"),
        outcomes.get("match_result"),
    )

    selected["double_chance"] = _pick_single_distribution(
        prediction_markets.get("double_chance"),
        outcomes.get("double_chance"),
    )

    selected["btts"] = _pick_single_distribution(
        prediction_markets.get("btts"),
        outcomes.get("btts"),
    )

    over_under_selected = {}

    predicted_over_under = prediction_markets.get(
        "over_under",
        {},
    )

    actual_over_under = outcomes.get(
        "over_under",
        {},
    )

    if isinstance(predicted_over_under, dict):
        for key in predicted_over_under:
            if not key.startswith("over_"):
                continue

            suffix = key[len("over_"):]
            under_key = f"under_{suffix}"

            picked = _pick_binary_line(
                predicted_over_under,
                actual_over_under,
                key,
                under_key,
            )

            if picked is not None:
                over_under_selected[suffix] = picked

    selected["over_under"] = over_under_selected

    team_goals_selected = {}

    predicted_team_goals = prediction_markets.get(
        "team_goals",
        {},
    )

    actual_team_goals = outcomes.get(
        "team_goals",
        {},
    )

    if isinstance(predicted_team_goals, dict):
        for prefix in ("home", "away"):
            for key in predicted_team_goals:
                start = f"{prefix}_over_"

                if not key.startswith(start):
                    continue

                suffix = key[len(start):]

                under_key = (
                    f"{prefix}_under_{suffix}"
                )

                picked = _pick_binary_line(
                    predicted_team_goals,
                    actual_team_goals,
                    key,
                    under_key,
                )

                if picked is not None:
                    team_goals_selected[
                        f"{prefix}_{suffix}"
                    ] = picked

    selected["team_goals"] = team_goals_selected

    top_scorelines = prediction_markets.get(
        "top_scorelines",
        [],
    )

    scoreline_actual = outcomes.get(
        "scoreline"
    )

    if isinstance(top_scorelines, list) and top_scorelines:
        first = top_scorelines[0]

        if isinstance(first, dict):
            predicted_score = first.get("score")
            probability = first.get("probability")

            if (
                predicted_score is not None
                and isinstance(
                    probability,
                    (int, float),
                )
                and not isinstance(
                    probability,
                    bool,
                )
            ):
                selected["scoreline"] = {
                    "pick": predicted_score,
                    "probability": probability,
                    "won": (
                        predicted_score
                        == scoreline_actual.get("outcome")
                        if isinstance(
                            scoreline_actual,
                            dict,
                        )
                        else None
                    ),
                    "outcome": (
                        scoreline_actual.get("outcome")
                        if isinstance(
                            scoreline_actual,
                            dict,
                        )
                        else None
                    ),
                }
            else:
                selected["scoreline"] = None
        else:
            selected["scoreline"] = None
    else:
        selected["scoreline"] = None

    return {
        "selected": selected,
        "outcomes": outcomes,
    }


def _new_market_summary():
    return {
        "match_result": {
            "graded": 0,
            "correct": 0,
            "accuracy": 0.0,
        },
        "double_chance": {
            "graded": 0,
            "correct": 0,
            "accuracy": 0.0,
        },
        "over_under": {},
        "btts": {
            "graded": 0,
            "correct": 0,
            "accuracy": 0.0,
        },
        "team_goals": {},
        "scoreline": {
            "graded": 0,
            "correct": 0,
            "accuracy": 0.0,
        },
    }


def _update_summary_entry(
    summary,
    key,
    selected,
):
    if not isinstance(selected, dict):
        return

    won = selected.get("won")

    if won not in (True, False):
        return

    entry = summary.setdefault(
        key,
        {
            "graded": 0,
            "correct": 0,
            "accuracy": 0.0,
        },
    )

    entry["graded"] += 1

    if won:
        entry["correct"] += 1

    entry["accuracy"] = (
        entry["correct"]
        / entry["graded"]
        if entry["graded"]
        else 0.0
    )


def _update_market_summary(
    summary,
    selected,
):
    if not isinstance(selected, dict):
        return

    _update_summary_entry(
        summary,
        "match_result",
        selected.get("match_result"),
    )

    _update_summary_entry(
        summary,
        "double_chance",
        selected.get("double_chance"),
    )

    _update_summary_entry(
        summary,
        "btts",
        selected.get("btts"),
    )

    _update_summary_entry(
        summary,
        "scoreline",
        selected.get("scoreline"),
    )

    for line, result in (
        selected
        .get("over_under", {})
        .items()
    ):
        _update_summary_entry(
            summary["over_under"],
            line,
            result,
        )

    for line, result in (
        selected
        .get("team_goals", {})
        .items()
    ):
        _update_summary_entry(
            summary["team_goals"],
            line,
            result,
        )


def _statistical_actuals(
    enriched_fixture,
):
    """
    Return actual corners/cards only when enrichment supplied them.

    No prediction is created here because the current prediction engine
    does not yet generate historical corner/card probability distributions.
    """
    if not isinstance(enriched_fixture, dict):
        return {
            "corners": {},
            "cards": {},
        }

    graded = market_grading.grade_statistical_markets(
        enriched_fixture
    )

    return {
        "corners": graded.get(
            "corners",
            {},
        ),
        "cards": graded.get(
            "cards",
            {},
        ),
    }


def _prepare_statistical_enrichment(
    candidates,
    enrich_statistics,
):
    if not isinstance(enrich_statistics, bool):
        raise ValueError(
            "enrich_statistics must be a boolean."
        )

    if not enrich_statistics:
        return {}, None

    fixture_ids = [
        _fixture_id(match)
        for match in candidates
    ]

    fixture_ids = [
        fixture_id
        for fixture_id in fixture_ids
        if fixture_id is not None
    ]

    if not fixture_ids:
        return {}, None

    try:
        enriched = api_football.get_enriched_fixtures(
            fixture_ids
        )

        if not isinstance(enriched, dict):
            return {}, (
                "Fixture enrichment returned "
                "an invalid result."
            )

        return enriched, None

    except Exception as exc:
        return {}, str(exc)


def _format_probability(value):
    """Format a probability for human-readable backtest output."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "n/a"

    return f"{value:.0%}"


def _print_market_prediction_block(markets):
    """
    Print every market actually produced by prediction_engine.

    This function is presentation-only. It does not alter probabilities,
    selections, grading, or prediction logic.
    """
    if not isinstance(markets, dict):
        return

    print("    MATCH RESULT:")

    match_result = markets.get(
        "match_result",
        {},
    )

    if isinstance(match_result, dict):
        for key, label in (
            ("home_win", "Home Win"),
            ("draw", "Draw"),
            ("away_win", "Away Win"),
        ):
            if key in match_result:
                print(
                    f"      {label}: "
                    f"{_format_probability(match_result[key])}"
                )

    for section, title in (
        ("double_chance", "DOUBLE CHANCE"),
        ("btts", "BTTS"),
    ):
        values = markets.get(section)

        if isinstance(values, dict) and values:
            print(f"    {title}:")

            for key, value in values.items():
                print(
                    f"      {key}: "
                    f"{_format_probability(value)}"
                )

    values = markets.get(
        "over_under",
        {},
    )

    if isinstance(values, dict) and values:
        print("    OVER/UNDER:")

        for key, value in values.items():
            print(
                f"      {key}: "
                f"{_format_probability(value)}"
            )

    values = markets.get(
        "team_goals",
        {},
    )

    if isinstance(values, dict) and values:
        print("    TEAM GOALS:")

        for key, value in values.items():
            print(
                f"      {key}: "
                f"{_format_probability(value)}"
            )

    scorelines = markets.get(
        "top_scorelines",
        [],
    )

    if isinstance(scorelines, list) and scorelines:
        print("    TOP SCORELINES:")

        for item in scorelines:
            if not isinstance(item, dict):
                continue

            score = item.get("score")
            probability = item.get("probability")

            if score is not None:
                print(
                    f"      {score}: "
                    f"{_format_probability(probability)}"
                )

    for section, title in (
        ("cards", "CARDS"),
        ("corners", "CORNERS"),
    ):
        values = markets.get(section)

        if isinstance(values, dict) and values:
            print(f"    {title}:")

            for key, value in values.items():
                print(
                    f"      {key}: "
                    f"{_format_probability(value)}"
                )


def _print_market_grading_block(market_grading):
    """Print selected market results without changing grading."""
    if not isinstance(market_grading, dict):
        return

    selected = market_grading.get(
        "selected",
        {},
    )

    if not isinstance(selected, dict):
        return

    print("    SELECTED MARKET RESULTS:")

    def print_selected(label, result):
        if not isinstance(result, dict):
            return

        pick = result.get("pick")
        won = result.get("won")

        if pick is None:
            return

        if won is True:
            mark = "✓"
        elif won is False:
            mark = "✗"
        else:
            mark = "-"

        probability = _format_probability(
            result.get("probability")
        )

        print(
            f"      {mark} {label}: "
            f"{pick} ({probability})"
        )

    print_selected(
        "Match result",
        selected.get("match_result"),
    )

    print_selected(
        "Double chance",
        selected.get("double_chance"),
    )

    print_selected(
        "BTTS",
        selected.get("btts"),
    )

    print_selected(
        "Scoreline",
        selected.get("scoreline"),
    )

    for line, result in (
        selected
        .get("over_under", {})
        .items()
    ):
        print_selected(
            f"Over/Under {line}",
            result,
        )

    for line, result in (
        selected
        .get("team_goals", {})
        .items()
    ):
        print_selected(
            f"Team goals {line}",
            result,
        )


def _print_market_backtest_report(result):
    """
    Print the complete market-level backtest report.

    Accuracy is intentionally kept separate by market. Different markets
    are never combined into a single artificial overall accuracy.
    """
    print(
        "\n=== FULL MARKET BACKTEST REPORT ==="
    )

    print(
        f"Fixtures graded: {result['graded']} "
        f"(sample requested: {result['sample_size']})"
    )

    print("\nMARKET SUMMARY")
    print(
        "  Market | Graded | Correct | Accuracy"
    )

    def print_summary(label, entry):
        if (
            not isinstance(entry, dict)
            or not entry.get("graded")
        ):
            return

        print(
            f"  {label} | "
            f"{entry['graded']} | "
            f"{entry['correct']} | "
            f"{entry['accuracy']:.1%}"
        )

    summary = result.get(
        "market_summary",
        {},
    )

    print_summary(
        "Match Result (1X2)",
        summary.get("match_result"),
    )

    print_summary(
        "Double Chance",
        summary.get("double_chance"),
    )

    print_summary(
        "BTTS",
        summary.get("btts"),
    )

    print_summary(
        "Scoreline",
        summary.get("scoreline"),
    )

    for line, entry in (
        summary
        .get("over_under", {})
        .items()
    ):
        print_summary(
            f"Over/Under {line}",
            entry,
        )

    for line, entry in (
        summary
        .get("team_goals", {})
        .items()
    ):
        print_summary(
            f"Team Goals {line}",
            entry,
        )

    available = result.get(
        "statistical_data_available",
        {},
    )

    if (
        available.get("corners")
        or available.get("cards")
    ):
        print(
            "\nSTATISTICAL DATA AVAILABLE"
        )

        if available.get("corners"):
            print(
                "  Corners actuals available: "
                f"{available['corners']}"
            )

        if available.get("cards"):
            print(
                "  Cards actuals available: "
                f"{available['cards']}"
            )

    print(
        "\nFIXTURE-BY-FIXTURE MARKET DETAIL"
    )

    for entry in result.get(
        "log",
        [],
    ):
        print(
            f"\n  {entry['match']}"
        )

        _print_market_prediction_block(
            entry.get(
                "markets",
                {},
            )
        )

        _print_market_grading_block(
            entry.get(
                "market_grading",
                {},
            )
        )


def run_real_backtest(
    league_id,
    season,
    sample_size=20,
    min_prior_matches=5,
    sample_seed=42,
    enrich_statistics=False,
):
    """
    Run a chronological historical backtest.

    API behaviour:
    - One league-fixture retrieval for the historical dataset.
    - Optional batched fixture enrichment for corners/cards.
    - No per-fixture feature API requests.
    - No fallback/fabricated historical statistical values.

    The full prediction probability distributions remain in each log row.
    Market-level grading is stored separately from the predictions.
    """
    if (
        not isinstance(league_id, int)
        or isinstance(league_id, bool)
    ):
        raise ValueError(
            "league_id must be an integer."
        )

    if (
        not isinstance(season, int)
        or isinstance(season, bool)
    ):
        raise ValueError(
            "season must be an integer."
        )

    if not isinstance(enrich_statistics, bool):
        raise ValueError(
            "enrich_statistics must be a boolean."
        )

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

    enriched_fixtures, enrichment_error = (
        _prepare_statistical_enrichment(
            candidates,
            enrich_statistics,
        )
    )

    if enrichment_error:
        print(
            "WARNING: historical fixture enrichment "
            f"unavailable: {enrichment_error}"
        )

    market_summary = _new_market_summary()

    statistical_data_available = {
        "corners": 0,
        "cards": 0,
    }

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

        market_grade = _grade_prediction_markets(
            markets,
            match,
        )

        _update_market_summary(
            market_summary,
            market_grade["selected"],
        )

        fixture_id = _fixture_id(match)

        enriched_fixture = (
            enriched_fixtures.get(fixture_id)
            if fixture_id is not None
            else None
        )

        statistical_actuals = _statistical_actuals(
            enriched_fixture
        )

        if statistical_actuals["corners"]:
            statistical_data_available["corners"] += 1

        if statistical_actuals["cards"]:
            statistical_data_available["cards"] += 1

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
                "markets": markets,
                "market_grading": {
                    "selected": market_grade[
                        "selected"
                    ],
                    "outcomes": market_grade[
                        "outcomes"
                    ],
                    "statistical_actuals": (
                        statistical_actuals
                    ),
                },
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

    result = {
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
        "statistics_enriched": enrich_statistics,
        "statistical_enrichment_error": enrichment_error,
        "statistical_data_available": (
            statistical_data_available
        ),
        "market_summary": market_summary,
        "log": log,
    }

    _print_market_backtest_report(result)

    return result


if __name__ == "__main__":
    run_synthetic_selftest()
