"""
Main entry point for the football and basketball prediction agent.

Football production prediction uses prediction_engine for the mathematical
prediction path. API-backed feature retrieval happens here; the engine
itself performs no API calls.
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

        for team in standings:
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
                played
                and goals_for is not None
                and played > 0
            ):
                ratios.append(
                    goals_for / played
                )

        if ratios:
            avg = sum(ratios) / len(ratios)

    except Exception:
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
            if key.endswith("s")
            and not key.endswith("ss")
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

        print(
            f"Unrecognized league name "
            f"'{league_name_arg}'. "
            f"Known names: "
            f"{', '.join(config.LEAGUE_NAME_TO_ID.keys())}"
        )

        sys.exit(1)

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
    """
    Adapt current API season statistics to the
    prediction-engine feature contract.
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
    """
    Build a current recent-form snapshot from
    completed API fixtures.
    """

    matches = api_football.get_recent_form(
        team_id,
        last=last,
    )

    goals_for = []
    goals_against = []

    for match in matches or []:
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
    """
    Build H2H history from the requested
    home-team perspective.
    """

    matches = api_football.get_head_to_head(
        home_id,
        away_id,
        last=last,
    )

    goals_for = []
    goals_against = []

    for match in matches or []:
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


def _estimate_avg_cards(
    team_stats,
    league_avg_cards=3.8,
):
    if not isinstance(team_stats, dict):
        return None

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

        if (
            fixtures_played
            and fixtures_played > 0
        ):
            return (
                total_yellow
                / fixtures_played
            )

    except (
        TypeError,
        AttributeError,
    ):
        pass

    return league_avg_cards


def build_football_safest_candidates(m):
    """
    Flatten every currently generated football
    market outcome.
    """

    candidates = []

    def add_pair(
        prefix,
        values,
    ):
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
                    key
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
        m.get("match_result"),
    )

    double_chance_labels = {
        "home_or_draw": "Home or Draw",
        "away_or_draw": "Away or Draw",
        "home_or_away": (
            "Home or Away (no draw)"
        ),
    }

    for key, label in (
        double_chance_labels.items()
    ):
        if key in m.get(
            "double_chance",
            {},
        ):
            candidates.append(
                (
                    label,
                    m["double_chance"][key],
                )
            )

    add_pair(
        "",
        m.get("over_under"),
    )

    btts = m.get("btts", {})

    for key, probability in btts.items():
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
                    f"BTTS {key.title()}",
                    probability,
                )
            )

    add_pair(
        "",
        m.get("team_goals"),
    )

    cards = m.get("cards")

    if isinstance(cards, dict):
        if isinstance(
            cards.get("over"),
            (int, float),
        ):
            candidates.append(
                (
                    f"Over {cards.get('over_line')} Cards",
                    cards["over"],
                )
            )

        if isinstance(
            cards.get("under"),
            (int, float),
        ):
            candidates.append(
                (
                    f"Under {cards.get('over_line')} Cards",
                    cards["under"],
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
    """
    Predict one football fixture using the shared
    production prediction engine.
    """

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

    # Both season-stat snapshots are required.
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

    # Recent form is a configured production signal,
    # so it cannot silently be fabricated.
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
            or 0
        )

        current_away_goals = (
            fixture
            .get("goals", {})
            .get("away")
            or 0
        )

        markets = (
            live_model.live_market_probabilities(
                prediction["expected_goals"]["home"],
                prediction["expected_goals"]["away"],
                elapsed,
                status_short,
                current_home_goals,
                current_away_goals,
            )
        )

    else:
        markets = prediction["markets"]

        markets["is_live"] = False

        markets["cards"] = (
            poisson_model.cards_market(
                _estimate_avg_cards(
                    home_stats
                ),
                _estimate_avg_cards(
                    away_stats
                ),
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
            except Exception as exc:
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
    m = pred["markets"]
    c = pred["confidence"]
    s = pred["safest"]

    print(
        f"\n{pred['home_team']} vs "
        f"{pred['away_team']}  "
        f"({pred['league']})"
    )

    if pred["is_live"]:
        print(
            f"  🔴 LIVE - "
            f"{m['minutes_elapsed']}' "
            f"(est. "
            f"{m['minutes_remaining_estimate']} "
            f"min remaining)"
        )

        cs = m["current_score"]

        print(
            f"  Current score: "
            f"{cs['home']} - {cs['away']}"
        )

        eg = m["expected_additional_goals"]

        print(
            f"  Expected additional goals: "
            f"{eg['home']} - {eg['away']}"
        )

    else:
        print(
            f"  Expected goals: "
            f"{m['expected_goals']['home']} - "
            f"{m['expected_goals']['away']}"
        )

    print(
        f"  Win/Draw/Loss:  "
        f"Home {m['match_result']['home_win']:.0%} | "
        f"Draw {m['match_result']['draw']:.0%} | "
        f"Away {m['match_result']['away_win']:.0%}"
        f"  (Elo blended in)"
    )

    if "over_2_5" in m.get(
        "over_under",
        {},
    ):
        print(
            f"  Over/Under 2.5: "
            f"Over "
            f"{m['over_under']['over_2_5']:.0%} | "
            f"Under "
            f"{m['over_under']['under_2_5']:.0%}"
        )

    print(
        f"  BTTS:           "
        f"Yes {m['btts']['yes']:.0%} | "
        f"No {m['btts']['no']:.0%}"
    )

    if "cards" in m:
        print(
            f"  Cards:          "
            f"Over {m['cards']['over_line']}: "
            f"{m['cards']['over']:.0%} | "
            f"Under: "
            f"{m['cards']['under']:.0%}"
        )

    if m.get("top_scorelines"):
        print(
            f"  Top scoreline:  "
            f"{m['top_scorelines'][0]['score']} "
            f"("
            f"{m['top_scorelines'][0]['probability']:.0%}"
            f")"
        )

    print(
        f"  Confidence:     "
        f"{c['emoji']} {c['label']} "
        f"(pick: {c['top_pick']}, "
        f"{c['top_probability']:.0%})"
    )

    ec = pred.get("elo_cross_check")

    if ec:
        print(
            f"  Elo (pre-blend): "
            f"Home {ec['home']:.0%} | "
            f"Draw {ec['draw']:.0%} | "
            f"Away {ec['away']:.0%}"
        )

    if pred.get("odds_comparison"):
        oc = pred["odds_comparison"]

        print(
            f"  Market odds:    "
            f"Home "
            f"{oc.get('implied_home_win', 0):.0%} | "
            f"Draw "
            f"{oc.get('implied_draw', 0):.0%} | "
            f"Away "
            f"{oc.get('implied_away_win', 0):.0%} "
            f"("
            f"{oc.get('bookmakers_counted', 0)} "
            f"bookmakers)"
        )

    if s:
        print(
            f"  >>> SAFEST PICK: "
            f"{s['label']} "
            f"({s['probability']:.0%}) <<<"
        )


def run_daily(
    date_str,
    league_id=None,
    limit=None,
    fetch_odds=False,
):
    storage.init_db()

    fixtures = (
        api_football.get_fixtures_by_date(
            date_str,
            league_id,
        )
    )

    if league_id is None:
        fixtures = [
            f
            for f in fixtures
            if f["league"]["id"]
            in config.ALLOWED_LEAGUE_IDS
        ]

    fixtures = [
        f
        for f in fixtures
        if f["fixture"]["status"]["short"]
        not in FOOTBALL_FINISHED_STATUSES
    ]

    if not fixtures:
        print(
            f"No fixtures found for "
            f"{date_str} matching your criteria."
        )
        return

    if limit:
        fixtures = fixtures[:limit]

    print(
        f"Found {len(fixtures)} fixture(s) "
        f"for {date_str} "
        f"(showing up to "
        f"{limit or 'all'})."
    )

    quota_hit = False
    predicted_count = 0
    skipped_no_data = 0

    for fixture in fixtures:
        if quota_hit:
            print(
                "  Skipping remaining matches - "
                "daily API quota appears exhausted."
            )
            break

        try:
            league_avg_goals = (
                get_league_avg_goals(
                    fixture["league"]["id"],
                    fixture["league"]["season"],
                )
            )

            pred = predict_fixture(
                fixture,
                league_avg_goals,
                fetch_odds,
            )

            if pred["insufficient_data"]:
                print(
                    f"\n{pred['home_team']} vs "
                    f"{pred['away_team']}  "
                    f"({pred['league']})"
                )

                print(
                    "  Skipped: not enough "
                    "team data available for "
                    "a real prediction."
                )

                skipped_no_data += 1
                continue

            print_prediction(pred)

            storage.save_prediction(
                fixture_id=pred["fixture_id"],
                match_date=pred["date"],
                home_team=pred["home_team"],
                away_team=pred["away_team"],
                league=pred["league"],
                markets=pred["markets"],
                confidence=pred["confidence"],
                home_team_id=pred["home_team_id"],
                away_team_id=pred["away_team_id"],
                odds_comparison=pred.get(
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

        except Exception as exc:
            print(
                f"  Skipped a fixture due "
                f"to an error: {exc}"
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
            print(
                "  Skipping remaining grading - "
                "daily API quota appears exhausted."
            )
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

            storage.record_result(
                fixture_id,
                home_goals,
                away_goals,
            )

            print(
                f"Graded: {home_team} "
                f"{home_goals}-{away_goals} "
                f"{away_team}"
            )

            graded_count += 1

        except requests.exceptions.HTTPError as exc:
            if (
                exc.response is not None
                and exc.response.status_code == 429
            ):
                print(
                    f"  Could not grade "
                    f"{home_team} vs "
                    f"{away_team}: "
                    f"daily API quota "
                    f"exhausted (429)."
                )
                quota_hit = True
            else:
                print(
                    f"  Could not grade "
                    f"{home_team} vs "
                    f"{away_team}: {exc}"
                )

        except Exception as exc:
            print(
                f"  Could not grade "
                f"{home_team} vs "
                f"{away_team}: {exc}"
            )

    print(
        f"\nGraded {graded_count} of "
        f"{len(pending)} pending fixture(s)."
    )


def run_cleanup():
    storage.init_db()

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
        f"predictions from leagues outside "
        f"your current tracked list."
    )

    print(
        f"Kept {total - deleted} "
        f"prediction(s) from your tracked leagues."
    )


def run_accuracy_report():
    storage.init_db()

    summary = storage.accuracy_summary()

    if summary["total_graded"] == 0:
        print(
            "No graded predictions yet - "
            "run --grade after some matches finish."
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
    if not league_id:
        league_id = (
            config.ALLOWED_LEAGUE_IDS[0]
        )

    if not season:
        season = (
            datetime.now(
                timezone.utc
            ).year - 1
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
            "No matches could be backtested - "
            "try a different league or season."
        )
        return

    print(
        f"\nBacktest accuracy: "
        f"{result['accuracy']:.1%} "
        f"({result['correct']}/"
        f"{result['graded']})"
    )

    predicted_counts = {
        "home_win": 0,
        "draw": 0,
        "away_win": 0,
    }

    actual_counts = {
        "home_win": 0,
        "draw": 0,
        "away_win": 0,
    }

    for entry in result["log"]:
        predicted_counts[
            entry["predicted"]
        ] += 1

        actual_counts[
            entry["actual"]
        ] += 1

    total = len(result["log"])

    print(
        "\nWhat the model predicted "
        "vs what actually happened:"
    )

    print(
        f"  {'Outcome':<12} "
        f"{'Predicted':<20} "
        f"{'Actual':<20}"
    )

    for outcome in (
        "home_win",
        "draw",
        "away_win",
    ):
        print(
            f"  {outcome:<12} "
            f"{predicted_counts[outcome]}/"
            f"{total} "
            f"("
            f"{predicted_counts[outcome]/total:.0%}"
            f")"
            f"{'':<8}"
            f"{actual_counts[outcome]}/"
            f"{total} "
            f"("
            f"{actual_counts[outcome]/total:.0%}"
            f")"
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
    seasons = (
        api_football.get_league_coverage(
            league_id
        )
    )

    if not seasons:
        print(
            f"No season data found "
            f"for league {league_id} at all."
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

        fixtures_covered = coverage.get(
            "fixtures",
            {},
        )

        print(
            f"  Year {year} "
            f"{'(current)' if current else ''}: "
            f"fixtures events="
            f"{fixtures_covered.get('events')}, "
            f"stats="
            f"{fixtures_covered.get('statistics_fixtures')}, "
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
        f"  parameters sent back: "
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
        for game in games
        if game.get(
            "status",
            {},
        ).get(
            "short"
        )
        not in BASKETBALL_FINISHED_STATUSES
    ]

    if not games:
        print(
            f"No basketball games found "
            f"for {date_str}."
        )
        return

    if limit:
        games = games[:limit]

    print(
        f"Found {len(games)} "
        f"basketball game(s) for "
        f"{date_str} "
        f"(showing up to "
        f"{limit or 'all'})."
    )

    quota_hit = False
    predicted_count = 0

    for game in games:
        if quota_hit:
            print(
                "  Skipping remaining games - "
                "daily API quota appears exhausted."
            )
            break

        try:
            pred = basketball_model.predict_game(
                game
            )

            basketball_model.print_prediction(
                pred
            )

            storage.save_basketball_prediction(
                game_id=pred["game_id"],
                game_date=pred["date"],
                home_team=pred["home_team"],
                away_team=pred["away_team"],
                league=pred["league"],
                markets=pred["markets"],
                confidence=pred["confidence"],
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

        except Exception as exc:
            print(
                f"  Skipped a game due "
                f"to an error: {exc}"
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
            print(
                "  Skipping remaining grading - "
                "daily API quota appears exhausted."
            )
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
                    f"  Could not grade "
                    f"{home_team} vs "
                    f"{away_team}: "
                    f"daily API quota "
                    f"exhausted (429)."
                )
                quota_hit = True
            else:
                print(
                    f"  Could not grade "
                    f"{home_team} vs "
                    f"{away_team}: {exc}"
                )

        except Exception as exc:
            print(
                f"  Could not grade "
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
            "predictions yet - run "
            "--sport basketball --grade "
            "after some games finish."
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


if __name__ == "__main__":
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
        help=(
            "Date to fetch fixtures/games "
            "for, YYYY-MM-DD "
            "(defaults to today)"
        ),
    )

    parser.add_argument(
        "--league",
        type=int,
        help=(
            "League ID to filter by "
            "(numeric, football only)"
        ),
    )

    parser.add_argument(
        "--league-name",
        help=(
            "League name to filter by "
            "(football only)"
        ),
    )

    parser.add_argument(
        "--limit",
        type=int,
        help=(
            "Max number of matches/games "
            "to predict"
        ),
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
    )

    parser.add_argument(
        "--with-odds",
        action="store_true",
        help=(
            "Also fetch bookmaker odds "
            "for comparison"
        ),
    )

    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Run a real historical backtest",
    )

    parser.add_argument(
        "--season",
        type=int,
        help=(
            "Season year for backtest, "
            "e.g. 2025"
        ),
    )

    parser.add_argument(
        "--sample",
        type=int,
        default=20,
        help=(
            "Number of matches to sample "
            "for backtest"
        ),
    )

    parser.add_argument(
        "--find-league",
        help=(
            "Search API-Football for a "
            "league's correct ID by name"
        ),
    )

    parser.add_argument(
        "--check-coverage",
        type=int,
        help=(
            "Check what seasons/data are "
            "available for a league ID"
        ),
    )

    parser.add_argument(
        "--raw-debug",
        action="store_true",
        help=(
            "Dump the full raw API response "
            "for diagnosis"
        ),
    )

    args = parser.parse_args()

    if args.find_league:
        run_find_league(
            args.find_league
        )
        sys.exit(0)

    if args.check_coverage:
        run_check_coverage(
            args.check_coverage
        )
        sys.exit(0)

    if args.raw_debug:
        run_raw_debug(
            args.league or 39,
            args.season or 2025,
        )
        sys.exit(0)

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

            run_daily_basketball(
                date_str,
                args.limit,
            )

    else:
        if args.grade:
            run_grading()

        elif args.accuracy:
            run_accuracy_report()

        elif args.cleanup:
            run_cleanup()

        elif args.backtest:
            league_id = resolve_league_id(
                args.league,
                args.league_name,
            )

            run_backtest_command(
                league_id,
                args.season,
                args.sample,
            )

        else:
            date_str = (
                args.date
                or datetime.now(
                    timezone.utc
                ).strftime("%Y-%m-%d")
            )

            league_id = resolve_league_id(
                args.league,
                args.league_name,
            )

            run_daily(
                date_str,
                league_id,
                args.limit,
                args.with_odds,
    )
