"""
Main entry point for the football and basketball prediction agent.

Production football prediction uses prediction_engine.
This module handles orchestration, API-backed feature retrieval,
CLI validation, storage, and presentation.
"""

import argparse
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
import prediction_engine
import storage
import poisson_model


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
            "date must be a string in YYYY-MM-DD format"
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

    An explicitly supplied --league value must not bypass
    the configured allowed league IDs.
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


# ----------------------------------------------------------------------
# General football helpers
# ----------------------------------------------------------------------

def get_league_avg_goals(league_id, season):
    cache_key = (league_id, season)

    if cache_key in _league_avg_cache:
        return _league_avg_cache[cache_key]

    avg = None

    try:
        standings = api_football.get_league_standings(
            league_id,
            season,
        )

        ratios = []

        for team in standings or []:
            played = (
                team
                .get("all", {})
                .get("played")
            )

            goals_for = (
                team
                .get("all", {})
                .get("goals", {})
                .get("for")
            )

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
                and goals_for >= 0
            ):
                ratios.append(
                    goals_for / played
                )

        if ratios:
            avg = sum(ratios) / len(ratios)

    except requests.exceptions.RequestException:
        avg = None

    if avg is None:
        avg = config.LEAGUE_AVG_GOALS.get(
            league_id,
            config.LEAGUE_AVG_GOALS_FALLBACK,
        )

    _league_avg_cache[cache_key] = avg

    return avg


def resolve_league_id(
    league_arg,
    league_name_arg,
):
    if league_name_arg:
        key = " ".join(
            league_name_arg.strip().lower().split()
        )

        key_no_trailing_s = (
            key.rstrip("s")
            if (
                key.endswith("s")
                and not key.endswith("ss")
            )
            else key
        )

        if key in config.LEAGUE_NAME_TO_ID:
            return config.LEAGUE_NAME_TO_ID[key]

        if (
            key_no_trailing_s
            in config.LEAGUE_NAME_TO_ID
        ):
            return config.LEAGUE_NAME_TO_ID[
                key_no_trailing_s
            ]

        matches = [
            (name, league_id)
            for name, league_id
            in config.LEAGUE_NAME_TO_ID.items()
            if key in name or name in key
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


def _valid_goal(value):
    if isinstance(value, bool):
        return False

    try:
        return float(value) >= 0
    except (TypeError, ValueError):
        return False


def _extract_average_goals(
    team_stats,
    side,
):
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

        if value < 0:
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
    except (TypeError, ValueError):
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
        fixtures_played <= 0
        or goals_for is None
        or goals_against is None
    ):
        return None

    return {
        "matches": fixtures_played,
        "goals_for": (
            goals_for * fixtures_played
        ),
        "goals_against": (
            goals_against * fixtures_played
        ),
        "source": name,
    }


def _recent_feature(
    team_id,
    last,
):
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
            not _valid_goal(home_goals)
            or not _valid_goal(away_goals)
        ):
            continue

        if home.get("id") == team_id:
            goals_for.append(
                float(home_goals)
            )
            goals_against.append(
                float(away_goals)
            )

        elif away.get("id") == team_id:
            goals_for.append(
                float(away_goals)
            )
            goals_against.append(
                float(home_goals)
            )

    if not goals_for:
        return None

    return {
        "matches": len(goals_for),
        "goals_for": sum(goals_for),
        "goals_against": sum(goals_against),
    }


def _h2h_feature(
    home_id,
    away_id,
    last,
):
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
            not _valid_goal(home_goals)
            or not _valid_goal(away_goals)
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

    return {
        "meetings": len(goals_for),
        "goals_for": sum(goals_for),
        "goals_against": sum(goals_against),
    }


def _estimate_avg_cards(team_stats):
    """
    Calculate observed yellow cards per played match.

    No fabricated league-wide/default card value is returned.
    Missing or invalid card data returns None.
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

            if total < 0:
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

        return (
            total_yellow / fixtures_played
        )

    except (
        TypeError,
        ValueError,
        AttributeError,
    ):
        return None


def build_football_safest_candidates(markets):
    candidates = []

    def add_pair(prefix, values):
        if not isinstance(values, dict):
            return

        for key, probability in values.items():
            if (
                isinstance(
                    probability,
                    (int, float),
                )
                and not isinstance(
                    probability,
                    bool,
                )
            ):
                label = (
                    str(key)
                    .replace("_", " ")
                    .title()
                )

                candidates.append(
                    (
                        f"{prefix}{label}",
                        probability,
                    )
                )

    add_pair(
        "",
        markets.get("match_result"),
    )

    double_chance_labels = {
        "home_or_draw": "Home or Draw",
        "away_or_draw": "Away or Draw",
        "home_or_away": (
            "Home or Away (no draw)"
        ),
    }

    double_chance = markets.get(
        "double_chance",
        {},
    )

    for key, label in (
        double_chance_labels.items()
    ):
        probability = double_chance.get(key)

        if (
            isinstance(
                probability,
                (int, float),
            )
            and not isinstance(
                probability,
                bool,
            )
        ):
            candidates.append(
                (
                    label,
                    probability,
                )
            )

    add_pair(
        "",
        markets.get("over_under"),
    )

    add_pair(
        "BTTS ",
        {
            key: value
            for key, value
            in markets.get(
                "btts",
                {},
            ).items()
        },
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

        if (
            isinstance(over, (int, float))
            and not isinstance(over, bool)
        ):
            candidates.append(
                (
                    f"Over {line} Cards",
                    over,
                )
            )

        if (
            isinstance(under, (int, float))
            and not isinstance(under, bool)
        ):
            candidates.append(
                (
                    f"Under {line} Cards",
                    under,
                )
            )

    return candidates
    
    def _insufficient_prediction(
    fixture,
    is_live,
):
    home_team = fixture["teams"]["home"]
    away_team = fixture["teams"]["away"]
    league = fixture["league"]

    return {
        "fixture_id": fixture["fixture"]["id"],
        "date": fixture["fixture"]["date"],
        "home_team": home_team["name"],
        "away_team": away_team["name"],
        "home_team_id": home_team["id"],
        "away_team_id": away_team["id"],
        "league": league["name"],
        "markets": None,
        "confidence": None,
        "safest": None,
        "is_live": is_live,
        "insufficient_data": True,
        "odds_comparison": None,
        "elo_cross_check": None,
    }


def predict_fixture(
    fixture,
    league_avg_goals,
    fetch_odds=False,
):
    home_team = fixture["teams"]["home"]
    away_team = fixture["teams"]["away"]
    league = fixture["league"]

    status = fixture["fixture"]["status"]
    status_short = status["short"]
    elapsed = status.get("elapsed")

    is_live = (
        status_short
        in FOOTBALL_LIVE_STATUSES
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

    if is_live:
        current_home_goals = (
            fixture
            .get("goals", {})
            .get("home")
        )

        current_away_goals = (
            fixture
            .get("goals", {})
            .get("away")
        )

        if not _valid_goal(
            current_home_goals
        ):
            current_home_goals = 0

        if not _valid_goal(
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
            prediction["markets"]
        )

        markets["is_live"] = False

        home_cards = _estimate_avg_cards(
            home_stats
        )

        away_cards = _estimate_avg_cards(
            away_stats
        )

        # Never manufacture card data.
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

    conf = confidence.confidence_flag(
        markets["match_result"]
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
                    f"  (Odds lookup failed: {exc})"
                )

    return {
        "fixture_id": fixture["fixture"]["id"],
        "date": fixture["fixture"]["date"],
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
    }


def print_prediction(pred):
    markets = pred["markets"]
    confidence_data = pred["confidence"]
    safest = pred["safest"]

    print(
        f"\n{pred['home_team']} vs "
        f"{pred['away_team']} "
        f"({pred['league']})"
    )

    if pred["is_live"]:
        print(
            f"  LIVE - "
            f"{markets['minutes_elapsed']}' "
            f"(est. "
            f"{markets['minutes_remaining_estimate']} "
            f"min remaining)"
        )

        score = markets["current_score"]

        print(
            f"  Current score: "
            f"{score['home']} - "
            f"{score['away']}"
        )

        additional = (
            markets["expected_additional_goals"]
        )

        print(
            f"  Expected additional goals: "
            f"{additional['home']} - "
            f"{additional['away']}"
        )

    else:
        expected = markets["expected_goals"]

        print(
            f"  Expected goals: "
            f"{expected['home']} - "
            f"{expected['away']}"
        )

    result = markets["match_result"]

    print(
        f"  Win/Draw/Loss: "
        f"Home {result['home_win']:.0%} | "
        f"Draw {result['draw']:.0%} | "
        f"Away {result['away_win']:.0%}"
    )

    over_under = markets.get(
        "over_under",
        {},
    )

    if "over_2_5" in over_under:
        print(
            f"  Over/Under 2.5: "
            f"Over "
            f"{over_under['over_2_5']:.0%} | "
            f"Under "
            f"{over_under['under_2_5']:.0%}"
        )

    btts = markets.get("btts", {})

    if (
        "yes" in btts
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
            f"Over {cards['over_line']}: "
            f"{cards['over']:.0%} | "
            f"Under: "
            f"{cards['under']:.0%}"
        )

    scorelines = markets.get(
        "top_scorelines",
        [],
    )

    if scorelines:
        top = scorelines[0]

        print(
            f"  Top scoreline: "
            f"{top['score']} "
            f"({top['probability']:.0%})"
        )

    if confidence_data:
        print(
            f"  Confidence: "
            f"{confidence_data['emoji']} "
            f"{confidence_data['label']} "
            f"(pick: "
            f"{confidence_data['top_pick']}, "
            f"{confidence_data['top_probability']:.0%})"
        )

    if safest:
        print(
            f"  Safest generated market: "
            f"{safest}"
        )

    elo_data = pred.get(
        "elo_cross_check"
    )

    if elo_data:
        print(
            f"  Elo pre-blend: "
            f"Home {elo_data['home']:.0%} | "
            f"Draw {elo_data['draw']:.0%} | "
            f"Away {elo_data['away']:.0%}"
        )

    if pred.get("odds_comparison"):
        odds = pred["odds_comparison"]

        print(
            f"  Market odds: "
            f"Home "
            f"{odds.get('implied_home_win', 0):.0%} | "
            f"Draw "
            f"{odds.get('implied_draw', 0):.0%} | "
            f"Away "
            f"{odds.get('implied_away_win', 0):.0%}"
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
    validate_date_string(date_str)
    validate_positive_int(limit, "limit")
    validate_allowed_league(league_id)

    storage.init_db()

    fixtures = api_football.get_fixtures_by_date(
        date_str,
        league_id,
    )

    # Always enforce the configured allow-list.
    fixtures = [
        fixture
        for fixture in fixtures or []
        if fixture.get("league", {}).get("id")
        in config.ALLOWED_LEAGUE_IDS
    ]

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

            if not isinstance(
                season,
                int,
            ):
                continue

            league_avg = (
                get_league_avg_goals(
                    league_id_value,
                    season,
                )
            )

            prediction = predict_fixture(
                fixture,
                league_avg,
                fetch_odds=fetch_odds,
            )

            if prediction[
                "insufficient_data"
            ]:
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

            print_prediction(
                prediction
            )

            storage.save_prediction(
                fixture_id=prediction[
                    "fixture_id"
                ],
                match_date=prediction[
                    "date"
                ],
                home_team=prediction[
                    "home_team"
                ],
                away_team=prediction[
                    "away_team"
                ],
                league=prediction[
                    "league"
                ],
                markets=prediction[
                    "markets"
                ],
                confidence=prediction[
                    "confidence"
                ],
                home_team_id=prediction[
                    "home_team_id"
                ],
                away_team_id=prediction[
                    "away_team_id"
                ],
                odds_comparison=prediction.get(
                    "odds_comparison"
                ),
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
                    f"to an API error: {exc}"
                )

        except requests.exceptions.RequestException as exc:
            print(
                f"  Skipped a fixture due "
                f"to a request error: {exc}"
            )

    print(
        f"\nSuccessfully predicted "
        f"{predicted_count} of "
        f"{len(fixtures)} fixture(s) "
        f"({skipped_no_data} skipped due "
        f"to insufficient data)."
    )


def run_grading():
    storage.init_db()

    pending = storage.get_pending_fixtures()

    if not pending:
        print(
            "No pending predictions to grade."
        )
        return

    graded_count = 0
    quota_hit = False

    for (
        fixture_id,
        match_date,
        home_team,
        away_team,
    ) in pending:

        if quota_hit:
            break

        try:
            result = (
                api_football.get_fixture_result(
                    fixture_id
                )
            )

            if (
                not result
                or result["fixture"]["status"]["short"]
                != "FT"
            ):
                continue

            home_goals = result["goals"]["home"]
            away_goals = result["goals"]["away"]

            if (
                not _valid_goal(home_goals)
                or not _valid_goal(away_goals)
            ):
                continue

            storage.record_result(
                fixture_id,
                home_goals,
                away_goals,
            )

            print(
                f"Graded: "
                f"{home_team} "
                f"{home_goals}-"
                f"{away_goals} "
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

        except requests.exceptions.RequestException as exc:
            print(
                f"Could not grade "
                f"{home_team} vs "
                f"{away_team}: {exc}"
            )

    print(
        f"\nGraded {graded_count} of "
        f"{len(pending)} pending fixture(s)."
    )


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
        league_id = (
            config.ALLOWED_LEAGUE_IDS[0]
        )

    validate_allowed_league(
        league_id
    )

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

    if result["graded"] == 0:
        print(
            "No matches could be backtested."
        )
        return

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


def run_find_league(name):
    results = api_football.search_leagues(
        name
    )

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
        league = result["league"]
        country = result.get(
            "country",
            {},
        ).get(
            "name",
            "",
        )

        print(
            f"  ID {league['id']}: "
            f"{league['name']} "
            f"({country})"
        )


def run_check_coverage(league_id):
    validate_allowed_league(
        league_id
    )

    seasons = (
        api_football.get_league_coverage(
            league_id
        )
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

    games = (
        basketball_api.get_games_by_date(
            date_str,
            config.ALLOWED_BASKETBALL_LEAGUE_IDS[
                0
            ],
        )
    )

    games = [
        game
        for game in games or []
        if game.get(
            "status",
            {},
        ).get("short")
        not in BASKETBALL_FINISHED_STATUSES
    ]

    if not games:
        print(
            f"No basketball games found "
            f"for {date_str}."
        )
        return

    if limit is not None:
        games = games[:limit]

    print(
        f"Found {len(games)} "
        f"basketball game(s) for "
        f"{date_str}."
    )

    quota_hit = False
    predicted_count = 0

    for game in games:
        if quota_hit:
            break

        try:
            prediction = (
                basketball_model.predict_game(
                    game
                )
            )

            basketball_model.print_prediction(
                prediction
            )

            storage.save_basketball_prediction(
                game_id=prediction["game_id"],
                game_date=prediction["date"],
                home_team=prediction["home_team"],
                away_team=prediction["away_team"],
                league=prediction["league"],
                markets=prediction["markets"],
                confidence=prediction["confidence"],
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

        except requests.exceptions.RequestException as exc:
            print(
                f"  Skipped a game due "
                f"to a request error: {exc}"
            )

    print(
        f"\nSuccessfully predicted "
        f"{predicted_count} of "
        f"{len(games)} game(s)."
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

            if (
                not result
                or result.get(
                    "status",
                    {},
                ).get("short")
                != "FT"
            ):
                continue

            home_points = (
                result["scores"]
                ["home"]["total"]
            )

            away_points = (
                result["scores"]
                ["away"]["total"]
            )

            if (
                not _valid_goal(home_points)
                or not _valid_goal(away_points)
            ):
                continue

            storage.record_basketball_result(
                game_id,
                home_points,
                away_points,
            )

            print(
                f"Graded: "
                f"{home_team} "
                f"{home_points}-"
                f"{away_points} "
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

        except requests.exceptions.RequestException as exc:
            print(
                f"Could not grade "
                f"{home_team} vs "
                f"{away_team}: {exc}"
            )

    print(
        f"\nGraded {graded_count} of "
        f"{len(pending)} pending game(s)."
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
    )

    parser.add_argument(
        "--accuracy",
        action="store_true",
    )

    parser.add_argument(
        "--cleanup",
        action="store_true",
        help=(
            "Request cleanup. "
            "Deletion requires --confirm-cleanup."
        ),
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
        "--season",
        type=int,
        help="Season year for backtest.",
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
        validate_positive_int(
            args.limit,
            "limit",
        )

        validate_positive_int(
            args.sample,
            "sample",
        )

        if args.date is not None:
            validate_date_string(
                args.date
            )

        if (
            args.league is not None
            and args.sport == "football"
        ):
            validate_allowed_league(
                args.league
            )

        if args.find_league:
            run_find_league(
                args.find_league
            )
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

            validate_allowed_league(
                league_id
            )

            season = (
                args.season
                if args.season is not None
                else (
                    datetime.now(
                        timezone.utc
                    ).year
                    - 1
                )
            )

            run_raw_debug(
                league_id,
                season,
            )
            return 0

        if args.sport == "basketball":
            if args.grade:
                run_grading_basketball()

            elif args.accuracy:
                run_accuracy_report_basketball()

            else:
                date_str = (
                    args.date
                    or datetime.now(
                        timezone.utc
                    ).strftime("%Y-%m-%d")
                )

                validate_date_string(
                    date_str
                )

                run_daily_basketball(
                    date_str,
                    args.limit,
                )

            return 0

        if args.grade:
            run_grading()
            return 0

        if args.accuracy:
            run_accuracy_report()
            return 0

        if args.cleanup:
            run_cleanup(
                confirm=args.confirm_cleanup
            )
            return 0

        if args.backtest:
            league_id = resolve_league_id(
                args.league,
                args.league_name,
            )

            if league_id is not None:
                validate_allowed_league(
                    league_id
                )

            run_backtest_command(
                league_id,
                args.season,
                args.sample,
            )
            return 0

        date_str = (
            args.date
            or datetime.now(
                timezone.utc
            ).strftime("%Y-%m-%d")
        )

        validate_date_string(
            date_str
        )

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


if __name__ == "__main__":
    sys.exit(main())
