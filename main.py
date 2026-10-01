"""
Main entry point for the football and basketball prediction agent.

Production football prediction uses prediction_engine as the
authoritative mathematical prediction path.

This module is responsible for:
- API-backed feature retrieval
- production feature construction
- prediction orchestration
- storage
- grading
- reporting
- CLI handling

Important feature contract:
prediction_engine expects goals_for and goals_against as PER-MATCH
averages, not cumulative season totals.
"""

import argparse
import math
import sys
from datetime import datetime, timezone

import requests

import api_football
import backtest
import basketball_api
import basketball_model
import confidence
import config
import elo
import live_model
import odds_api
import poisson_model
import prediction_engine
import storage


FOOTBALL_FINISHED_STATUSES = {
    "FT",
    "AET",
    "PEN",
    "PST",
    "CANC",
    "ABD",
    "AWD",
    "WO",
}

FOOTBALL_LIVE_STATUSES = {
    "1H",
    "2H",
    "HT",
    "ET",
    "BT",
    "P",
    "SUSP",
    "INT",
}

BASKETBALL_FINISHED_STATUSES = {
    "FT",
    "AOT",
    "CANC",
    "ABD",
}

_league_avg_cache = {}


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


def validate_date_string(date_str):
    """Validate a date strictly as YYYY-MM-DD."""
    if not isinstance(date_str, str):
        raise ValueError(
            "date must be a string in YYYY-MM-DD format."
        )

    try:
        parsed = datetime.strptime(
            date_str,
            "%Y-%m-%d",
        )
    except ValueError as exc:
        raise ValueError(
            f"Invalid date '{date_str}'. "
            "Expected YYYY-MM-DD."
        ) from exc

    if parsed.strftime("%Y-%m-%d") != date_str:
        raise ValueError(
            f"Invalid date '{date_str}'. "
            "Expected YYYY-MM-DD."
        )

    return date_str


def validate_positive_int(value, name):
    """Validate an optional positive integer."""
    if value is None:
        return None

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
    ):
        raise ValueError(
            f"{name} must be a positive integer."
        )

    return value


def validate_allowed_league(league_id):
    """
    Enforce the configured football league allow-list.

    An explicitly supplied league must never bypass the configured
    allowed league IDs.
    """
    if league_id is None:
        return None

    if (
        isinstance(league_id, bool)
        or not isinstance(league_id, int)
    ):
        raise ValueError(
            "league_id must be an integer."
        )

    if league_id not in config.ALLOWED_LEAGUE_IDS:
        raise ValueError(
            f"League ID {league_id} is not in the "
            "configured allowed league list."
        )

    return league_id


def _valid_nonnegative_number(value):
    """Return True for finite, non-negative numeric values."""
    if isinstance(value, bool):
        return False

    try:
        number = float(value)
    except (TypeError, ValueError):
        return False

    return math.isfinite(number) and number >= 0


def _valid_probability(value):
    """Return True when value is a finite probability in [0, 1]."""
    if isinstance(value, bool):
        return False

    try:
        number = float(value)
    except (TypeError, ValueError):
        return False

    return math.isfinite(number) and 0.0 <= number <= 1.0


def _valid_goal(value):
    """Return True for a finite, non-negative whole-number goal/score."""
    if isinstance(value, bool):
        return False

    try:
        number = float(value)
    except (TypeError, ValueError):
        return False

    if not math.isfinite(number):
        return False

    return number >= 0 and number.is_integer()


# ----------------------------------------------------------------------
# General football helpers
# ----------------------------------------------------------------------


def get_league_avg_goals(league_id, season):
    """
    Return the configured league scoring average.

    API standings are preferred. The calculation is:

        total goals for / total matches played

    across the available teams.

    A configured fallback is used only when the API does not provide
    usable standings data.
    """
    cache_key = (league_id, season)

    if cache_key in _league_avg_cache:
        return _league_avg_cache[cache_key]

    average = None

    try:
        standings = api_football.get_league_standings(
            league_id,
            season,
        )

        total_goals = 0.0
        total_played = 0

        for team in standings or []:
            if not isinstance(team, dict):
                continue

            all_data = team.get("all", {})
            if not isinstance(all_data, dict):
                continue

            played = all_data.get("played")

            goals_data = all_data.get(
                "goals",
                {},
            )

            if not isinstance(goals_data, dict):
                continue

            goals_for = goals_data.get("for")

            if (
                isinstance(played, (int, float))
                and not isinstance(played, bool)
                and played > 0
                and isinstance(
                    goals_for,
                    (int, float),
                )
                and not isinstance(
                    goals_for,
                    bool,
                )
                and math.isfinite(float(goals_for))
                and goals_for >= 0
            ):
                total_played += int(played)
                total_goals += float(goals_for)

        if total_played > 0:
            average = total_goals / total_played

    except requests.exceptions.RequestException:
        average = None

    if (
        average is None
        or average <= 0
    ):
        average = config.LEAGUE_AVG_GOALS.get(
            league_id,
            config.LEAGUE_AVG_GOALS_FALLBACK,
        )

    if (
        not _valid_nonnegative_number(average)
        or float(average) <= 0
    ):
        raise ValueError(
            f"Invalid league average goals for league "
            f"{league_id}: {average!r}"
        )

    average = float(average)

    _league_avg_cache[cache_key] = average

    return average


def resolve_league_id(
    league_arg,
    league_name_arg,
):
    """Resolve a configured football league ID."""
    if league_name_arg:
        key = " ".join(
            league_name_arg.strip().lower().split()
        )

        if not key:
            raise ValueError(
                "league-name cannot be empty."
            )

        if key in config.LEAGUE_NAME_TO_ID:
            return config.LEAGUE_NAME_TO_ID[key]

        key_without_plural = (
            key.rstrip("s")
            if (
                key.endswith("s")
                and not key.endswith("ss")
            )
            else key
        )

        if (
            key_without_plural
            in config.LEAGUE_NAME_TO_ID
        ):
            return config.LEAGUE_NAME_TO_ID[
                key_without_plural
            ]

        matches = [
            (name, league_id)
            for name, league_id
            in config.LEAGUE_NAME_TO_ID.items()
            if (
                key in name
                or name in key
            )
        ]

        if len(matches) == 1:
            print(
                f"Matched '{league_name_arg}' "
                f"to '{matches[0][0]}'."
            )
            return matches[0][1]

        raise ValueError(
            f"Unrecognized league name "
            f"'{league_name_arg}'."
        )

    return league_arg


# ----------------------------------------------------------------------
# Production feature construction
# ----------------------------------------------------------------------


def _extract_average_goals(
    team_stats,
    side,
):
    """
    Extract a team's average goals per match.

    API-Football's statistics endpoint exposes values under:

        goals -> for/against -> average -> total

    This function deliberately returns the average, not the cumulative
    season total.
    """
    if not isinstance(team_stats, dict):
        return None

    try:
        value = (
            team_stats
            .get("goals", {})
            .get(side, {})
            .get("average", {})
            .get("total")
        )

        if value is None or value == "":
            return None

        value = float(value)

        if (
            not math.isfinite(value)
            or value < 0
        ):
            return None

        return value

    except (
        TypeError,
        ValueError,
        AttributeError,
    ):
        return None


def _current_team_feature(
    team_stats,
    name,
):
    """
    Build the season feature snapshot expected by prediction_engine.

    CRITICAL:
    goals_for and goals_against are PER-MATCH averages.

    Do not multiply them by fixtures_played. The prediction engine
    normalizes these values against league_avg_goals and therefore
    expects average goals per match.
    """
    if not isinstance(team_stats, dict):
        return None

    fixtures_played = (
        team_stats
        .get("fixtures", {})
        .get("played", {})
        .get("total")
    )

    try:
        fixtures_played = int(
            fixtures_played
        )
    except (
        TypeError,
        ValueError,
    ):
        return None

    if fixtures_played <= 0:
        return None

    goals_for = _extract_average_goals(
        team_stats,
        "for",
    )

    goals_against = _extract_average_goals(
        team_stats,
        "against",
    )

    if (
        goals_for is None
        or goals_against is None
    ):
        return None

    return {
        "matches": fixtures_played,
        "goals_for": goals_for,
        "goals_against": goals_against,
        "source": name,
    }


def _recent_feature(
    team_id,
    last,
):
    """
    Build recent-form features as per-match averages.

    The API returns individual match scores. We aggregate them and
    divide by the number of valid matches before passing the feature
    to prediction_engine.
    """
    matches = api_football.get_recent_form(
        team_id,
        last=last,
    )

    goals_for = []
    goals_against = []

    for match in matches or []:
        if not isinstance(match, dict):
            continue

        home = (
            match
            .get("teams", {})
            .get("home", {})
        )

        away = (
            match
            .get("teams", {})
            .get("away", {})
        )

        if (
            not isinstance(home, dict)
            or not isinstance(away, dict)
        ):
            continue

        home_goals = (
            match
            .get("goals", {})
            .get("home")
        )

        away_goals = (
            match
            .get("goals", {})
            .get("away")
        )

        if (
            not _valid_nonnegative_number(
                home_goals
            )
            or not _valid_nonnegative_number(
                away_goals
            )
        ):
            continue

        home_id = home.get("id")
        away_id = away.get("id")

        if home_id == team_id:
            goals_for.append(
                float(home_goals)
            )
            goals_against.append(
                float(away_goals)
            )

        elif away_id == team_id:
            goals_for.append(
                float(away_goals)
            )
            goals_against.append(
                float(home_goals)
            )

    if not goals_for:
        return None

    match_count = len(goals_for)

    return {
        "matches": match_count,
        "goals_for": (
            sum(goals_for) / match_count
        ),
        "goals_against": (
            sum(goals_against) / match_count
        ),
    }


def _h2h_feature(
    home_id,
    away_id,
    last,
):
    """
    Build H2H features as per-meeting averages.

    The returned perspective is always the requested home-team
    perspective:
        goals_for     = requested home team's goals
        goals_against = requested home team's conceded goals
    """
    matches = api_football.get_head_to_head(
        home_id,
        away_id,
                last=last,
    )

    goals_for = []
    goals_against = []

    for match in matches or []:
        if not isinstance(match, dict):
            continue

        home = (
            match
            .get("teams", {})
            .get("home", {})
        )

        away = (
            match
            .get("teams", {})
            .get("away", {})
        )

        if (
            not isinstance(home, dict)
            or not isinstance(away, dict)
        ):
            continue

        home_goals = (
            match
            .get("goals", {})
            .get("home")
        )

        away_goals = (
            match
            .get("goals", {})
            .get("away")
        )

        if (
            not _valid_nonnegative_number(
                home_goals
            )
            or not _valid_nonnegative_number(
                away_goals
            )
        ):
            continue

        if (
            home.get("id") == home_id
            and away.get("id") == away_id
        ):
            goals_for.append(
                float(home_goals)
            )
            goals_against.append(
                float(away_goals)
            )

        elif (
            home.get("id") == away_id
            and away.get("id") == home_id
        ):
            goals_for.append(
                float(away_goals)
            )
            goals_against.append(
                float(home_goals)
            )

    if not goals_for:
        return None

    meeting_count = len(goals_for)

    return {
        "meetings": meeting_count,
        "goals_for": (
            sum(goals_for) / meeting_count
        ),
        "goals_against": (
            sum(goals_against) / meeting_count
        ),
    }


def _estimate_avg_cards(team_stats):
    """
    Calculate observed yellow cards per played match.

    Missing card data returns None. No card value is fabricated.
    """
    if not isinstance(team_stats, dict):
        return None

    try:
        yellow = (
            team_stats
            .get("cards", {})
            .get("yellow", {})
        )

        if not isinstance(yellow, dict):
            return None

        total_yellow = 0.0
        found_card_values = False

        for value in yellow.values():
            if not isinstance(value, dict):
                continue

            total = value.get("total")

            if total is None or total == "":
                continue

            if isinstance(total, bool):
                return None

            total = float(total)

            if (
                not math.isfinite(total)
                or total < 0
            ):
                return None

            total_yellow += total
            found_card_values = True

        fixtures_played = int(
            team_stats
            .get("fixtures", {})
            .get("played", {})
            .get("total")
        )

        if (
            fixtures_played <= 0
            or not found_card_values
        ):
            return None

        return total_yellow / fixtures_played

    except (
        TypeError,
        ValueError,
        AttributeError,
    ):
        return None


# ----------------------------------------------------------------------
# Safest-pick construction
# ----------------------------------------------------------------------


def build_football_safest_candidates(markets):
    """Build validated candidate markets for safest_pick()."""
    if not isinstance(markets, dict):
        return []

    candidates = []

    def add_pair(prefix, values):
        if not isinstance(values, dict):
            return

        for key, probability in values.items():
            if not _valid_probability(
                probability
            ):
                continue

            label = (
                str(key)
                .replace("_", " ")
                .title()
            )

            candidates.append(
                (
                    f"{prefix}{label}",
                    float(probability),
                )
            )

    add_pair(
        "",
        markets.get("match_result"),
    )

    double_chance_labels = {
        "home_or_draw": "Home or Draw",
        "away_or_draw": "Away or Draw",
        "home_or_away": "Home or Away (no draw)",
    }

    double_chance = markets.get(
        "double_chance",
        {},
    )

    if isinstance(double_chance, dict):
        for key, label in double_chance_labels.items():
            probability = double_chance.get(key)

            if _valid_probability(
                probability
            ):
                candidates.append(
                    (
                        label,
                        float(probability),
                    )
                )

    add_pair(
        "",
        markets.get("over_under"),
    )

    add_pair(
        "BTTS ",
        markets.get("btts"),
    )

    add_pair(
        "",
        markets.get("team_goals"),
    )

    cards = markets.get("cards")

    if isinstance(cards, dict):
        over = cards.get("over")
        under = cards.get("under")
        line = cards.get("over_line")

        if _valid_probability(over):
            candidates.append(
                (
                    f"Over {line} Cards",
                    float(over),
                )
            )

        if _valid_probability(under):
            candidates.append(
                (
                    f"Under {line} Cards",
                    float(under),
                )
            )

    return candidates


# ----------------------------------------------------------------------
# Prediction construction
# ----------------------------------------------------------------------


def _fixture_identity(fixture):
    """
    Validate and extract the minimum fixture structure needed by the
    production prediction path.
    """
    if not isinstance(fixture, dict):
        raise ValueError(
            "fixture must be a dictionary."
        )

    fixture_data = fixture.get("fixture")
    teams = fixture.get("teams")
    league = fixture.get("league")

    if not isinstance(fixture_data, dict):
        raise ValueError(
            "fixture.fixture is missing."
        )

    if not isinstance(teams, dict):
        raise ValueError(
            "fixture.teams is missing."
        )

    if not isinstance(league, dict):
        raise ValueError(
            "fixture.league is missing."
        )

    home_team = teams.get("home")
    away_team = teams.get("away")

    if (
        not isinstance(home_team, dict)
        or not isinstance(away_team, dict)
    ):
        raise ValueError(
            "fixture home/away teams are missing."
        )

    required_home = (
        home_team.get("id"),
        home_team.get("name"),
    )

    required_away = (
        away_team.get("id"),
        away_team.get("name"),
    )

    if (
        required_home[0] is None
        or not required_home[1]
        or required_away[0] is None
        or not required_away[1]
    ):
        raise ValueError(
            "fixture team identity is incomplete."
        )

    if (
        fixture_data.get("id") is None
        or not fixture_data.get("date")
    ):
        raise ValueError(
            "fixture identity/date is incomplete."
        )

    if (
        league.get("id") is None
        or league.get("season") is None
        or not league.get("name")
    ):
        raise ValueError(
            "fixture league identity is incomplete."
        )

    return (
        fixture_data,
        home_team,
        away_team,
        league,
    )


def _insufficient_prediction(
    fixture,
    is_live,
    reason="Validated prediction inputs were unavailable.",
):
    """Return a consistent non-prediction result."""
    (
        fixture_data,
        home_team,
        away_team,
        league,
    ) = _fixture_identity(fixture)

    return {
        "fixture_id": fixture_data["id"],
        "date": fixture_data["date"],
        "home_team": home_team["name"],
        "away_team": away_team["name"],
        "home_team_id": home_team["id"],
        "away_team_id": away_team["id"],
        "league": league["name"],
        "markets": None,
        "confidence": None,
        "safest": None,
        "is_live": bool(is_live),
        "insufficient_data": True,
        "reason": reason,
        "odds_comparison": None,
        "elo_cross_check": None,
    }


def predict_fixture(
    fixture,
    league_avg_goals,
    fetch_odds=False,
):
    """
    Generate one authoritative football prediction.

    All production feature snapshots are converted to per-match
    averages before being handed to prediction_engine.
    """
    (
        fixture_data,
        home_team,
        away_team,
        league,
    ) = _fixture_identity(fixture)

    status = fixture_data.get(
        "status",
        {},
    )

    if not isinstance(status, dict):
        status = {}

    status_short = status.get(
        "short",
        "",
    )

    elapsed = status.get(
        "elapsed"
    )

    is_live = (
        status_short
        in FOOTBALL_LIVE_STATUSES
    )

    if (
        not _valid_nonnegative_number(
            league_avg_goals
        )
        or float(league_avg_goals) <= 0
    ):
        raise ValueError(
            "league_avg_goals must be a positive number."
        )

    league_avg_goals = float(
        league_avg_goals
    )

    home_stats = (
        api_football.get_team_statistics(
            home_team["id"],
            league["id"],
            league["season"],
        )
    )

    away_stats = (
        api_football.get_team_statistics(
            away_team["id"],
            league["id"],
            league["season"],
        )
    )

    home_feature = _current_team_feature(
        home_stats,
        "season_home",
    )

    away_feature = _current_team_feature(
        away_stats,
        "season_away",
    )

    if (
        home_feature is None
        or away_feature is None
    ):
        return _insufficient_prediction(
            fixture,
            is_live,
            "Season team statistics are incomplete.",
        )

    recent_home = _recent_feature(
        home_team["id"],
        config.RECENT_FORM_MATCHES,
    )

    recent_away = _recent_feature(
        away_team["id"],
        config.RECENT_FORM_MATCHES,
    )

    if (
        recent_home is None
        or recent_away is None
    ):
        return _insufficient_prediction(
            fixture,
            is_live,
            "Recent team form data is incomplete.",
        )

    h2h = _h2h_feature(
        home_team["id"],
        away_team["id"],
        config.HEAD_TO_HEAD_MATCHES,
    )

    home_elo = storage.get_team_rating(
        home_team["id"]
    )

    away_elo = storage.get_team_rating(
        away_team["id"]
    )

    elo_probabilities = (
        elo.win_draw_loss_probabilities(
            home_elo,
            away_elo,
        )
    )

    features = (
        prediction_engine.build_historical_features(
            historical_snapshot={
                "home": home_feature,
                "away": away_feature,
            },
            recent_snapshot={
                "home": recent_home,
                "away": recent_away,
            },
            h2h_snapshot=h2h,
            league_avg_goals=league_avg_goals,
        )
    )

    prediction = (
        prediction_engine.predict_from_features(
            features,
            elo_probabilities=elo_probabilities,
            elo_weight=config.ELO_BLEND_WEIGHT,
        )
    )

    if not isinstance(prediction, dict):
        raise ValueError(
            "prediction_engine returned an invalid result."
        )

    if is_live:
        current_home_goals = (
            fixture.get("goals", {})
            .get("home")
        )

        current_away_goals = (
            fixture.get("goals", {})
            .get("away")
        )

        if not _valid_nonnegative_number(
            current_home_goals
        ):
            current_home_goals = 0

        if not _valid_nonnegative_number(
            current_away_goals
        ):
            current_away_goals = 0

        markets = (
            live_model.live_market_probabilities(
                prediction[
                    "expected_goals"
                ]["home"],
                prediction[
                    "expected_goals"
                ]["away"],
                elapsed,
                status_short,
                current_home_goals,
                current_away_goals,
            )
        )

    else:
        markets = dict(
            prediction.get(
                "markets",
                {},
            )
        )

        markets["is_live"] = False

        home_cards = _estimate_avg_cards(
            home_stats
        )

        away_cards = _estimate_avg_cards(
            away_stats
        )

        if (
            home_cards is not None
            and away_cards is not None
        ):
            markets["cards"] = (
                poisson_model.cards_market(
                    home_cards,
                    away_cards,
                )
            )

    if not isinstance(markets, dict):
        raise ValueError(
            "Prediction markets are invalid."
        )

    match_result = markets.get(
        "match_result"
    )

    if not isinstance(match_result, dict):
        raise ValueError(
            "Prediction is missing match_result probabilities."
        )

    conf = confidence.confidence_flag(
        match_result
    )

    safest = confidence.safest_pick(
        build_football_safest_candidates(
            markets
        )
    )

    odds_comparison = None

    if fetch_odds:
        sport_key = (
            config.LEAGUE_ID_TO_ODDS_SPORT_KEY.get(
                league["id"]
            )
        )

        if sport_key:
            try:
                odds_comparison = (
                    odds_api.get_odds_for_match(
                        sport_key,
                        home_team["name"],
                        away_team["name"],
                    )
                )
            except requests.exceptions.RequestException as exc:
                print(
                    f"  Odds lookup failed: {exc}"
                )

    return {
        "fixture_id": fixture_data["id"],
        "date": fixture_data["date"],
        "home_team": home_team["name"],
        "away_team": away_team["name"],
        "home_team_id": home_team["id"],
        "away_team_id": away_team["id"],
        "league": league["name"],
        "markets": markets,
        "confidence": conf,
        "safest": safest,
        "is_live": is_live,
        "insufficient_data": False,
        "odds_comparison": odds_comparison,
        "elo_cross_check": elo_probabilities,
        "feature_snapshot": {
            "season": {
                "home": home_feature,
                "away": away_feature,
            },
            "recent": {
                "home": recent_home,
                "away": recent_away,
            },
            "h2h": h2h,
            "league_avg_goals": league_avg_goals,
        },
        "prediction_record": prediction,
    }


# ----------------------------------------------------------------------
# Presentation
# ----------------------------------------------------------------------


def print_prediction(pred):
    """Print one production prediction safely."""
    if not isinstance(pred, dict):
        raise ValueError(
            "Prediction must be a dictionary."
        )

    if pred.get("insufficient_data"):
        print(
            f"\n{pred.get('home_team')} vs "
            f"{pred.get('away_team')} "
            f"({pred.get('league')})"
        )
        print(
            "  Prediction unavailable: "
            f"{pred.get('reason', 'insufficient data')}"
        )
        return

    markets = pred["markets"]
    confidence_data = pred["confidence"]
    safest = pred["safest"]

    print(
        f"\n{pred['home_team']} vs "
        f"{pred['away_team']} "
        f"({pred['league']})"
    )

    if pred.get("is_live"):
        print(
            f"  LIVE - "
            f"{markets.get('minutes_elapsed', '?')}' "
            f"(est. "
            f"{markets.get('minutes_remaining_estimate', '?')} "
            f"min remaining)"
        )

        score = markets.get(
            "current_score",
            {},
        )

        print(
            f"  Current score: "
            f"{score.get('home', 0)} - "
            f"{score.get('away', 0)}"
        )

        additional = markets.get(
            "expected_additional_goals",
            {},
        )

        print(
            f"  Expected additional goals: "
            f"{additional.get('home', 0):.2f} - "
            f"{additional.get('away', 0):.2f}"
        )

    else:
        expected = markets.get(
            "expected_goals",
            {},
        )

        print(
            f"  Expected goals: "
            f"{expected.get('home', 0):.2f} - "
            f"{expected.get('away', 0):.2f}"
        )

    result = markets.get(
        "match_result",
        {},
    )

    print(
        f"  Win/Draw/Loss: "
        f"Home {result.get('home_win', 0):.0%} | "
        f"Draw {result.get('draw', 0):.0%} | "
        f"Away {result.get('away_win', 0):.0%}"
    )

    over_under = markets.get(
        "over_under",
        {},
    )

    if (
        isinstance(over_under, dict)
        and "over_2_5" in over_under
        and "under_2_5" in over_under
    ):
        print(
            f"  Over/Under 2.5: "
            f"Over {over_under['over_2_5']:.0%} | "
            f"Under {over_under['under_2_5']:.0%}"
        )

    btts = markets.get(
        "btts",
        {},
    )

    if (
        isinstance(btts, dict)
        and "yes" in btts
        and "no" in btts
    ):
        print(
            f"  BTTS: "
            f"Yes {btts['yes']:.0%} | "
            f"No {btts['no']:.0%}"
        )

    cards = markets.get("cards")

    if isinstance(cards, dict):
        print(
            f"  Cards: "
            f"Over {cards.get('over_line')}: "
            f"{cards.get('over', 0):.0%} | "
            f"Under: "
            f"{cards.get('under', 0):.0%}"
        )

    scorelines = markets.get(
        "top_scorelines",
        [],
    )

    if isinstance(scorelines, list) and scorelines:
        top = scorelines[0]

        if isinstance(top, dict):
            print(
                f"  Top scoreline: "
                f"{top.get('score')} "
                f"({top.get('probability', 0):.0%})"
            )

    if isinstance(confidence_data, dict):
        print(
            f"  Confidence: "
            f"{confidence_data.get('emoji', '')} "
            f"{confidence_data.get('label', '')} "
            f"(pick: "
            f"{confidence_data.get('top_pick', '')}, "
            f"{confidence_data.get('top_probability', 0):.0%})"
        )

    if safest:
        print(
            f"  Safest generated market: "
            f"{safest}"
        )

    elo_data = pred.get(
        "elo_cross_check"
    )

    if isinstance(elo_data, dict):
        print(
            f"  Elo pre-blend: "
            f"Home {elo_data.get('home', 0):.0%} | "
            f"Draw {elo_data.get('draw', 0):.0%} | "
            f"Away {elo_data.get('away', 0):.0%}"
        )

    odds = pred.get(
        "odds_comparison"
    )

    if isinstance(odds, dict):
        print(
            f"  Market odds: "
            f"Home {odds.get('implied_home_win', 0):.0%} | "
            f"Draw {odds.get('implied_draw', 0):.0%} | "
            f"Away {odds.get('implied_away_win', 0):.0%}"
        )


# ----------------------------------------------------------------------
# Football daily prediction
# ----------------------------------------------------------------------

def run_daily(
    date_str,
    league_id=None,
    limit=None,
    fetch_odds=False,
):
    """
    Generate and persist football predictions for a date.

    Only configured leagues are allowed. Finished fixtures are excluded.
    Predictions with insufficient validated data are not persisted.
    """
    validate_date_string(date_str)
    validate_positive_int(limit, "limit")
    validate_allowed_league(league_id)

    storage.init_db()

    fixtures = api_football.get_fixtures_by_date(
        date_str,
        league_id,
    )

    # Enforce the configured league allow-list even when the API
    # returns more leagues than requested.
    fixtures = [
        fixture
        for fixture in fixtures or []
        if fixture.get("league", {}).get("id")
        in config.ALLOWED_LEAGUE_IDS
    ]

    # Do not create a new prediction for an already-finished fixture.
    fixtures = [
        fixture
        for fixture in fixtures
        if fixture.get("fixture", {})
        .get("status", {})
        .get("short")
        not in FOOTBALL_FINISHED_STATUSES
    ]

    if limit is not None:
        fixtures = fixtures[:limit]

    if not fixtures:
        print(
            f"No eligible football fixtures "
            f"found for {date_str}."
        )
        return

    print(
        f"Found {len(fixtures)} "
        f"eligible football fixture(s) "
        f"for {date_str}."
    )

    predicted_count = 0
    skipped_no_data = 0
    skipped_errors = 0
    quota_hit = False

    for fixture in fixtures:
        if quota_hit:
            print(
                "  Skipping remaining fixtures - "
                "daily API quota appears exhausted."
            )
            break

        try:
            league = fixture.get(
                "league",
                {},
            )

            league_id_value = league.get("id")
            season = league.get("season")

            if (
                league_id_value
                not in config.ALLOWED_LEAGUE_IDS
            ):
                continue

            if (
                isinstance(season, bool)
                or not isinstance(season, int)
                or season < 1900
            ):
                print(
                    "  Skipped a fixture: "
                    "invalid league season."
                )
                skipped_errors += 1
                continue

            league_avg = get_league_avg_goals(
                league_id_value,
                season,
            )

            prediction = predict_fixture(
                fixture,
                league_avg,
                fetch_odds=fetch_odds,
            )

            if prediction["insufficient_data"]:
                print(
                    f"\n{prediction['home_team']} "
                    f"vs "
                    f"{prediction['away_team']} "
                    f"({prediction['league']})"
                )

                print(
                    "  Skipped: insufficient "
                    "validated team data."
                )

                skipped_no_data += 1
                continue

            print_prediction(prediction)

            prediction_context = "LIVE" if prediction.get("is_live") else "PRE_MATCH"
            storage.save_prediction(
                fixture_id=prediction["fixture_id"],
                match_date=prediction["date"],
                home_team=prediction["home_team"],
                away_team=prediction["away_team"],
                league=prediction["league"],
                markets=prediction["markets"],
                confidence=prediction["confidence"],
                home_team_id=prediction["home_team_id"],
                away_team_id=prediction["away_team_id"],
                odds_comparison=prediction.get(
                    "odds_comparison"
                ),
                prediction_context=prediction_context,
                prediction_record=prediction.get("prediction_record") or prediction,
            )

            predicted_count += 1

        except requests.exceptions.HTTPError as exc:
            if (
                exc.response is not None
                and exc.response.status_code == 429
            ):
                print(
                    "  Skipped a fixture: "
                    "daily API quota exhausted (429)."
                )
                quota_hit = True
            else:
                print(
                    f"  Skipped a fixture due "
                    f"to an HTTP API error: {exc}"
                )
                skipped_errors += 1

        except requests.exceptions.RequestException as exc:
            print(
                f"  Skipped a fixture due "
                f"to a request error: {exc}"
            )
            skipped_errors += 1

        except api_football.APIFootballError as exc:
            message = str(exc)

            if (
                "429" in message
                or "rate limit" in message.lower()
                or "quota" in message.lower()
            ):
                print(
                    "  Daily API quota appears exhausted."
                )
                quota_hit = True
            else:
                print(
                    f"  Skipped a fixture due "
                    f"to an API/data error: {exc}"
                )

            skipped_errors += 1

    print(
        f"\nSuccessfully predicted "
        f"{predicted_count} of "
        f"{len(fixtures)} fixture(s)."
    )

    if skipped_no_data:
        print(
            f"Skipped {skipped_no_data} "
            f"fixture(s) due to insufficient data."
        )

    if skipped_errors:
        print(
            f"Skipped {skipped_errors} "
            f"fixture(s) due to errors."
        )


def run_grading():
    """
    Grade all pending football predictions whose fixtures have finished.

    Uses batched API requests (batch_size=20) via api_football.get_enriched_fixtures
    to minimize API credit consumption.
    """
    storage.init_db()

    pending = storage.get_pending_fixtures()

    if not pending:
        print("No pending predictions to grade.")
        return

    # Deduplicate pending fixture IDs while preserving fixture info
    seen = set()
    unique_pending = []
    for item in pending:
        fid = item[0]
        if fid not in seen:
            seen.add(fid)
            unique_pending.append(item)

    pending_ids = [item[0] for item in unique_pending]

    graded_count = 0
    skipped_count = 0

    try:
        enriched_results = api_football.get_enriched_fixtures(pending_ids, batch_size=20)
    except api_football.APIFootballQuotaExhaustedError:
        print("Daily API quota exhausted during grading batch fetch.")
        enriched_results = {}
    except Exception as exc:
        print(f"Error fetching batch fixture results for grading: {exc}")
        enriched_results = {}

    for fixture_id, match_date, home_team, away_team in unique_pending:
        result = enriched_results.get(fixture_id)

        if not result or not isinstance(result, dict):
            skipped_count += 1
            continue

        status_short = result.get("fixture", {}).get("status", {}).get("short")
        if status_short not in {"FT", "AET", "PEN"}:
            skipped_count += 1
            continue

        home_goals = result.get("goals", {}).get("home")
        away_goals = result.get("goals", {}).get("away")

        if not _valid_goal(home_goals) or not _valid_goal(away_goals):
            skipped_count += 1
            continue

        try:
            storage.record_result(fixture_id, int(home_goals), int(away_goals))
            print(f"Graded: {home_team} {int(home_goals)}-{int(away_goals)} {away_team}")
            graded_count += 1
        except Exception as exc:
            print(f"Could not grade {home_team} vs {away_team}: {exc}")
            skipped_count += 1

    print(f"\nGraded {graded_count} of {len(unique_pending)} pending fixture(s).")
    if skipped_count:
        print(f"Skipped {skipped_count} fixture(s) that were not ready or valid.")


# ----------------------------------------------------------------------
# Cleanup / reporting
# ----------------------------------------------------------------------

def run_cleanup(confirm=False):
    """
    Cleanup is destructive.

    It therefore requires explicit confirmation from the CLI.
    """
    storage.init_db()

    if not confirm:
        print(
            "Cleanup is disabled by default. "
            "No predictions were deleted."
        )
        print(
            "Use --cleanup --confirm-cleanup "
            "only after reviewing the target policy."
        )
        return

    keep_keywords = [
        "Premier League",
        "La Liga",
        "Serie A",
        "Bundesliga",
        "Ligue 1",
        "Champions League",
        "Europa League",
        "Nations League",
        "World Cup",
        "Euro",
    ]

    deleted, total = (
        storage.cleanup_non_target_leagues(
            keep_keywords
        )
    )

    print(
        f"Removed {deleted} of {total} "
        "predictions from leagues outside "
        "your current tracked list."
    )

    print(
        f"Kept {total - deleted} "
        "prediction(s) from your tracked leagues."
    )


def run_accuracy_report():
    storage.init_db()

    summary = storage.accuracy_summary()

    if summary["total_graded"] == 0:
        print(
            "No graded predictions yet - "
            "run --grade after matches finish."
        )
        return

    print(
        f"Total graded predictions: "
        f"{summary['total_graded']}"
    )

    print(
        f"Overall accuracy "
        f"(top pick correct): "
        f"{summary['overall_accuracy']:.1%}"
    )

    print(
        "\nAccuracy by confidence level:"
    )

    for label, stats in (
        summary.get(
            "by_confidence",
            {},
        ).items()
    ):
        print(
            f"  {label}: "
            f"{stats['accuracy']:.1%} "
            f"({stats['count']} predictions)"
        )


def run_backtest_command(
    league_id,
    season,
    sample_size=20,
):
    validate_positive_int(
        sample_size,
        "sample_size",
    )

    if league_id is None:
        league_id = config.ALLOWED_LEAGUE_IDS[0]

    validate_allowed_league(league_id)

    if season is None:
        season = (
            datetime.now(
                timezone.utc
            ).year - 1
        )

    if (
        isinstance(season, bool)
        or not isinstance(season, int)
        or season < 1900
    ):
        raise ValueError(
            "season must be a valid integer year."
        )

    print(
        f"Running backtest: "
        f"league {league_id}, "
        f"season {season}, "
        f"sample size {sample_size}..."
    )

    result = backtest.run_real_backtest(
        league_id,
        season,
        sample_size,
    )

    if result.get("graded", 0) == 0:
        print(
            "No matches could be backtested."
        )
        return 1 if result.get("status") == "PERSISTENCE_FAILED" or result.get("persisted") is False else 0

    print(
        f"\nBacktest accuracy: "
        f"{result['accuracy']:.1%} "
        f"({result['correct']}/"
        f"{result['graded']})"
    )

    print("\nSample results:")

    for entry in result["log"][:10]:
        mark = (
            "✓"
            if entry["correct"]
            else "✗"
        )

        print(
            f"  {mark} "
            f"{entry['match']} | "
            f"predicted: "
            f"{entry['predicted']}"
        )

    if result.get("status") == "PERSISTENCE_FAILED" or result.get("persisted") is False:
        print("\nERROR: Backtest run failed to persist to database.", file=sys.stderr)
        return 1

    return 0


def run_find_league(name):
    results = api_football.search_leagues(name)

    if not results:
        print(
            f"No leagues found matching "
            f"'{name}'."
        )
        return

    print(
        f"Matches for '{name}':"
    )

    for result in results:
        league = result.get("league", {})
        country = result.get(
            "country",
            {},
        ).get(
            "name",
            "",
        )

        if not league:
            continue

        print(
            f"  ID {league.get('id')}: "
            f"{league.get('name')} "
            f"({country})"
        )


def run_check_coverage(league_id):
    validate_allowed_league(league_id)

    seasons = api_football.get_league_coverage(
        league_id
    )

    if not seasons:
        print(
            f"No season data found "
            f"for league {league_id}."
        )
        return

    print(
        f"Seasons available for "
        f"league {league_id}:"
    )

    for season in seasons:
        year = season.get("year")
        current = season.get("current")
        coverage = season.get(
            "coverage",
            {},
        )

        fixtures = coverage.get(
            "fixtures",
            {},
        )

        print(
            f"  Year {year} "
            f"{'(current)' if current else ''}: "
            f"fixtures events="
            f"{fixtures.get('events')}, "
            f"stats="
            f"{fixtures.get('statistics_fixtures')}, "
            f"standings="
            f"{coverage.get('standings')}"
        )


def run_raw_debug(
    league_id,
    season,
):
    if (
        isinstance(season, bool)
        or not isinstance(season, int)
        or season < 1900
    ):
        raise ValueError(
            "season must be a valid integer year."
        )

    data = api_football.raw_debug_call(
        "fixtures",
        {
            "league": league_id,
            "season": season,
        },
    )

    print("Full raw response:")

    print(
        f"  results: "
        f"{data.get('results')}"
    )

    print(
        f"  errors: "
        f"{data.get('errors')}"
    )

    print(
        f"  paging: "
        f"{data.get('paging')}"
    )

    print(
        f"  parameters: "
        f"{data.get('parameters')}"
    )

    print(
        f"  response length: "
        f"{len(data.get('response', []))}"
    )


# ----------------------------------------------------------------------
# Basketball
# ----------------------------------------------------------------------

def run_daily_basketball(
    date_str,
    limit=None,
):
    validate_date_string(date_str)
    validate_positive_int(limit, "limit")

    storage.init_basketball_db()

    league_id = (
        config.ALLOWED_BASKETBALL_LEAGUE_IDS[0]
    )

    games = basketball_api.get_games_by_date(
        date_str,
        league_id,
    )

    games = [
        game
        for game in games or []
        if game.get("status", {}).get("short")
        not in BASKETBALL_FINISHED_STATUSES
    ]

    if limit is not None:
        games = games[:limit]

    if not games:
        print(
            f"No basketball games found "
            f"for {date_str}."
        )
        return

    print(
        f"Found {len(games)} "
        f"basketball game(s) for "
        f"{date_str}."
    )

    quota_hit = False
    predicted_count = 0
    skipped_count = 0

    for game in games:
        if quota_hit:
            break

        try:
            prediction = (
                basketball_model.predict_game(
                    game
                )
            )

            if not isinstance(prediction, dict):
                raise RuntimeError(
                    "Basketball model returned "
                    "an invalid prediction object."
                )

            basketball_model.print_prediction(
                prediction
            )

            prediction_context = "LIVE" if prediction.get("is_live") else "PRE_MATCH"
            storage.save_basketball_prediction(
                game_id=prediction["game_id"],
                game_date=prediction["date"],
                home_team=prediction["home_team"],
                away_team=prediction["away_team"],
                league=prediction["league"],
                markets=prediction["markets"],
                confidence=prediction["confidence"],
                prediction_context=prediction_context,
                prediction_record=prediction,
            )

            predicted_count += 1

        except requests.exceptions.HTTPError as exc:
            if (
                exc.response is not None
                and exc.response.status_code == 429
            ):
                print(
                    "  Skipped a game: "
                    "daily API quota exhausted (429)."
                )
                quota_hit = True
            else:
                print(
                    f"  Skipped a game due "
                    f"to an API error: {exc}"
                )
                skipped_count += 1

        except requests.exceptions.RequestException as exc:
            print(
                f"  Skipped a game due "
                f"to a request error: {exc}"
            )
            skipped_count += 1

        except RuntimeError as exc:
            print(
                f"  Skipped a game due "
                f"to a model/API error: {exc}"
            )
            skipped_count += 1

    print(
        f"\nSuccessfully predicted "
        f"{predicted_count} of "
        f"{len(games)} game(s)."
    )

    if skipped_count:
        print(
            f"Skipped {skipped_count} "
            f"game(s) due to errors."
        )


def run_grading_basketball():
    storage.init_basketball_db()

    pending = (
        storage.get_pending_basketball_games()
    )

    if not pending:
        print(
            "No pending basketball "
            "predictions to grade."
        )
        return

    graded_count = 0
    skipped_count = 0
    quota_hit = False

    for (
        game_id,
        game_date,
        home_team,
        away_team,
    ) in pending:

        if quota_hit:
            break

        try:
            result = (
                basketball_api.get_game_result(
                    game_id
                )
            )

            if not result:
                skipped_count += 1
                continue
        
            status_short = (
                result
                .get("status", {})
                .get("short")
            ) 
            
            if status_short not in {
                "FT",
                "AOT",
            }:
                skipped_count += 1
                continue

            home_points = (
                result
                .get("scores", {})
                .get("home", {})
                .get("total")
            )

            away_points = (
                result
                .get("scores", {})
                .get("away", {})
                .get("total")
            )

            if (
                not _valid_goal(home_points)
                or not _valid_goal(away_points)
            ):
                skipped_count += 1
                continue

            storage.record_basketball_result(
                game_id,
                int(home_points),
                int(away_points),
            )

            print(
                f"Graded: "
                f"{home_team} "
                f"{int(home_points)}-"
                f"{int(away_points)} "
                f"{away_team}"
            )

            graded_count += 1

        except requests.exceptions.HTTPError as exc:
            if (
                exc.response is not None
                and exc.response.status_code == 429
            ):
                print(
                    "Daily API quota exhausted "
                    "(429)."
                )
                quota_hit = True
            else:
                print(
                    f"Could not grade "
                    f"{home_team} vs "
                    f"{away_team}: {exc}"
                )
                skipped_count += 1

        except requests.exceptions.RequestException as exc:
            print(
                f"Could not grade "
                f"{home_team} vs "
                f"{away_team}: {exc}"
            )
            skipped_count += 1

        except RuntimeError as exc:
            print(
                f"Could not grade "
                f"{home_team} vs "
                f"{away_team}: {exc}"
            )
            skipped_count += 1

    print(
        f"\nGraded {graded_count} of "
        f"{len(pending)} pending game(s)."
    )

    if skipped_count:
        print(
            f"Skipped {skipped_count} "
            f"game(s) that were not ready or valid."
        )


def run_accuracy_report_basketball():
    storage.init_basketball_db()

    summary = (
        storage.basketball_accuracy_summary()
    )

    if summary["total_graded"] == 0:
        print(
            "No graded basketball "
            "predictions yet."
        )
        return

    print(
        f"Total graded basketball "
        f"predictions: "
        f"{summary['total_graded']}"
    )

    print(
        f"Overall accuracy "
        f"(top pick correct): "
        f"{summary['overall_accuracy']:.1%}"
    )

    print(
        "\nAccuracy by confidence level:"
    )

    for label, stats in (
        summary.get(
            "by_confidence",
            {},
        ).items()
    ):
        print(
            f"  {label}: "
            f"{stats['accuracy']:.1%} "
            f"({stats['count']} predictions)"
        )


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Sports prediction agent"
    )

    parser.add_argument(
        "--sport",
        choices=[
            "football",
            "basketball",
        ],
        default="football",
    )

    parser.add_argument(
        "--date",
        help="Date in YYYY-MM-DD format.",
    )

    parser.add_argument(
        "--league",
        type=int,
        help="Football league ID.",
    )

    parser.add_argument(
        "--league-name",
        help="Football league name.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of matches/games.",
    )

    parser.add_argument(
        "--grade",
        action="store_true",
        help="Grade completed predictions.",
    )

    parser.add_argument(
        "--accuracy",
        action="store_true",
        help="Show prediction accuracy.",
    )

    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Request prediction cleanup.",
    )

    parser.add_argument(
        "--confirm-cleanup",
        action="store_true",
        help=(
            "Explicitly authorize destructive cleanup."
        ),
    )

    parser.add_argument(
        "--with-odds",
        action="store_true",
        help="Fetch bookmaker odds for comparison.",
    )

    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Run a historical backtest.",
    )

    parser.add_argument(
        "--historical-sync",
        action="store_true",
        help="Acquire historical league data.",
    )

    parser.add_argument(
        "--dataset-status",
        action="store_true",
        help="Check historical dataset status.",
    )

    parser.add_argument(
        "--backtest-history",
        action="store_true",
        help="Show past backtest runs.",
    )

    parser.add_argument(
        "--with-enrichment",
        action="store_true",
        help="Fetch statistical enrichment during sync.",
    )

    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force re-fetch dataset during sync.",
    )

    parser.add_argument(
        "--season",
        type=int,
        help="Season year for historical/backtest.",
    )

    parser.add_argument(
        "--sample",
        type=int,
        default=20,
        help="Number of matches to sample.",
    )

    parser.add_argument(
        "--find-league",
        help="Search for a league by name.",
    )

    parser.add_argument(
        "--check-coverage",
        type=int,
        help="Check league coverage.",
    )

    parser.add_argument(
        "--raw-debug",
        action="store_true",
        help="Dump raw API response.",
    )

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        # Validate numeric CLI arguments before dispatch.
        if args.limit is not None:
            validate_positive_int(
                args.limit,
                "limit",
            )

        if args.sample is not None:
            validate_positive_int(
                args.sample,
                "sample",
            )

        if args.date is not None:
            validate_date_string(args.date)

        # --------------------------------------------------------------
        # Informational commands
        # --------------------------------------------------------------

        if args.find_league:
            run_find_league(args.find_league)
            return 0

        if args.check_coverage is not None:
            validate_allowed_league(
                args.check_coverage
            )

            run_check_coverage(
                args.check_coverage
            )

            return 0

        if args.raw_debug:
            league_id = (
                args.league
                if args.league is not None
                else config.ALLOWED_LEAGUE_IDS[0]
            )

            validate_allowed_league(league_id)

            season = (
                args.season
                if args.season is not None
                else (
                    datetime.now(
                        timezone.utc
                    ).year - 1
                )
            )

            if (
                isinstance(season, bool)
                or not isinstance(season, int)
                or season < 1900
            ):
                raise ValueError(
                    "season must be a valid integer year."
                )

            run_raw_debug(
                league_id,
                season,
            )

            return 0

        # --------------------------------------------------------------
        # Multi-sport Historical & Backtest commands
        # --------------------------------------------------------------
        import historical_sync

        if args.historical_sync:
            sport = args.sport or "football"
            league_id = args.league or (config.ALLOWED_BASKETBALL_LEAGUE_IDS[0] if sport == "basketball" else config.ALLOWED_LEAGUE_IDS[0])
            season = args.season or (datetime.now(timezone.utc).year - 1)
            storage.init_db()

            if sport == "basketball":
                report = historical_sync.sync_historical_basketball_games(
                    league_id=league_id,
                    season=season,
                    refresh=args.refresh,
                )
            else:
                report = historical_sync.sync_historical_fixtures(
                    league_id=league_id,
                    season=season,
                    with_enrichment=args.with_enrichment,
                    refresh=args.refresh,
                )

            if not isinstance(report, dict) or report.get("status") != "COMPLETE":
                print(f"Historical sync ended with status '{report.get('status', 'FAILED')}' (non-COMPLETE). Exiting with code 1.", file=sys.stderr)
                return 1
            return 0

        if args.dataset_status:
            sport = args.sport or "football"
            league_id = args.league or (config.ALLOWED_BASKETBALL_LEAGUE_IDS[0] if sport == "basketball" else config.ALLOWED_LEAGUE_IDS[0])
            season = args.season or (datetime.now(timezone.utc).year - 1)
            storage.init_db()

            st = storage.get_historical_dataset_status(league_id, season, sport=sport)
            print(f"\nDataset Status ({sport.upper()}):")
            print(f"  League: {st['league_id']}, Season: {st['season']}")
            print(f"  Status: {st['status']}")
            print(f"  Game/Fixture Count: {st['fixture_count']}")
            print(f"  Enrichment Status: {st['enrichment_status']}")
            print(f"  Pages Completed: {st['pages_completed']}/{st['expected_pages']}")
            print(f"  Updated At: {st['updated_at']}\n")
            return 0

        if args.backtest_history:
            sport = args.sport
            storage.init_db()
            runs = storage.get_latest_backtest_runs(sport=sport, limit=10)
            if not runs:
                print("No recorded backtest runs found.")
                return 0

            print("\nRecent Backtest Runs:")
            print(f"{'Run ID':35} {'Sport':10} {'League':8} {'Season':8} {'Graded':8} {'Accuracy':10} {'Completed'}")
            print("-" * 90)
            for r in runs:
                rid, sp, lid, ssn, dfc, ss, gc, acc, bs, ll, cat = r
                acc_str = f"{acc:.1%}" if acc is not None else "N/A"
                print(f"{rid:35} {sp:10} {lid:<8} {ssn:<8} {gc:<8} {acc_str:10} {cat}")
            print("-" * 90 + "\n")
            return 0

        if args.sport == "basketball":

            if args.league_name is not None:
                raise ValueError("--league-name is only valid for football.")

            if args.with_odds:
                print("Basketball odds comparison is not enabled by the current basketball model.")

            if args.cleanup:
                raise ValueError("Cleanup is currently a football prediction database operation.")

            if args.grade:
                run_grading_basketball()
                return 0

            if args.accuracy:
                run_accuracy_report_basketball()
                return 0

            if args.backtest:
                league_id = args.league or config.ALLOWED_BASKETBALL_LEAGUE_IDS[0]
                season = args.season or (datetime.now(timezone.utc).year - 1)
                storage.init_db()
                res = backtest.run_basketball_backtest(league_id=league_id, season=season, sample_size=args.sample)
                print(f"\nBasketball Backtest Accuracy: {res['accuracy']:.1%} ({res['correct']}/{res['graded']})")
                if res.get("status") == "PERSISTENCE_FAILED" or res.get("persisted") is False:
                    print("\nERROR: Backtest run failed to persist to database.", file=sys.stderr)
                    return 1
                return 0

            date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            validate_date_string(date_str)
            run_daily_basketball(date_str, args.limit)
            return 0

        # --------------------------------------------------------------
        # Football
        # --------------------------------------------------------------

        if args.grade:
            if (
                args.date is not None
                or args.limit is not None
                or args.with_odds
                or args.backtest
            ):
                raise ValueError(
                    "--grade cannot be combined with "
                    "date, limit, odds, or backtest options."
                )

            run_grading()
            return 0

        if args.accuracy:
            if (
                args.date is not None
                or args.limit is not None
                or args.with_odds
                or args.backtest
            ):
                raise ValueError(
                    "--accuracy cannot be combined with "
                    "date, limit, odds, or backtest options."
                )

            run_accuracy_report()
            return 0

        if args.cleanup:
            if (
                args.date is not None
                or args.limit is not None
                or args.with_odds
                or args.backtest
            ):
                raise ValueError(
                    "--cleanup cannot be combined with "
                    "prediction-generation options."
                )

            run_cleanup(
                confirm=args.confirm_cleanup
            )

            return 0

        if args.backtest:
            if (
                args.date is not None
                or args.limit is not None
                or args.with_odds
            ):
                raise ValueError(
                    "--backtest cannot be combined with "
                    "date, limit, or odds options."
                )

            league_id = resolve_league_id(
                args.league,
                args.league_name,
            )

            if league_id is not None:
                validate_allowed_league(
                    league_id
                )

            return run_backtest_command(
                league_id,
                args.season,
                args.sample,
            )

        # --------------------------------------------------------------
        # Normal football prediction path
        # --------------------------------------------------------------

        if args.confirm_cleanup:
            raise ValueError(
                "--confirm-cleanup requires --cleanup."
            )

        date_str = (
            args.date
            or datetime.now(
                timezone.utc
            ).strftime("%Y-%m-%d")
        )

        validate_date_string(date_str)

        league_id = resolve_league_id(
            args.league,
            args.league_name,
        )

        if league_id is not None:
            validate_allowed_league(
                league_id
            )

        run_daily(
            date_str,
            league_id,
            args.limit,
            args.with_odds,
        )

        return 0

    except ValueError as exc:
        print(
            f"Input validation error: {exc}",
            file=sys.stderr,
        )
        return 2

    except requests.exceptions.RequestException as exc:
        print(
            f"API request error: {exc}",
            file=sys.stderr,
        )
        return 1

    except KeyboardInterrupt:
        print(
            "\nOperation cancelled.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
            
