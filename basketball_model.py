"""
Basketball prediction model.

Uses team scoring/allowance statistics to estimate expected points and
derive moneyline and total-points probabilities.

Data-integrity rule:
missing or invalid team statistics are not replaced with fabricated
league averages. The prediction is marked insufficient instead.
"""

import math

import basketball_api
import config
import confidence


MARGIN_STD_DEV = 12.0
TOTAL_STD_DEV = 15.0
TOTAL_LINE = 224.5


def _valid_number(value):
    if isinstance(value, bool):
        return False

    try:
        value = float(value)
    except (TypeError, ValueError):
        return False

    return math.isfinite(value)


def _extract_scoring(stats):
    """
    Extract team scoring and defensive averages.

    Returns:
        (points_for, points_against)

    Returns (None, None) when required source data is missing or invalid.
    No artificial league-average fallback is used.
    """
    if not isinstance(stats, dict):
        return None, None

    try:
        points_for = stats[
            "points"
        ][
            "for"
        ][
            "average"
        ][
            "all"
        ]

        points_against = stats[
            "points"
        ][
            "against"
        ][
            "average"
        ][
            "all"
        ]
    except (
        KeyError,
        TypeError,
        AttributeError,
    ):
        return None, None

    if not _valid_number(points_for):
        return None, None

    if not _valid_number(points_against):
        return None, None

    points_for = float(points_for)
    points_against = float(points_against)

    if points_for < 0 or points_against < 0:
        return None, None

    return points_for, points_against


def _validate_probability(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError(
            "Probability must be a finite number between 0 and 1."
        )

    return float(value)


def _win_probability(point_diff):
    if not _valid_number(point_diff):
        raise ValueError(
            "point_diff must be a finite number."
        )

    probability = 0.5 * (
        1
        + math.erf(
            float(point_diff)
            / (
                MARGIN_STD_DEV
                * math.sqrt(2)
            )
        )
    )

    return _validate_probability(
        probability
    )


def _over_probability(expected_total, line):
    if not _valid_number(expected_total):
        raise ValueError(
            "expected_total must be a finite number."
        )

    if not _valid_number(line):
        raise ValueError(
            "line must be a finite number."
        )

    diff = (
        float(expected_total)
        - float(line)
    )

    probability = 0.5 * (
        1
        + math.erf(
            diff
            / (
                TOTAL_STD_DEV
                * math.sqrt(2)
            )
        )
    )

    return _validate_probability(
        probability
    )


def build_basketball_safest_candidates(
    markets,
):
    """Flatten all supported basketball market outcomes."""
    if not isinstance(markets, dict):
        raise ValueError(
            "markets must be a dictionary."
        )

    moneyline = markets.get(
        "moneyline"
    )
    total_points = markets.get(
        "total_points"
    )

    if not isinstance(
        moneyline,
        dict,
    ):
        raise ValueError(
            "moneyline market is missing."
        )

    if not isinstance(
        total_points,
        dict,
    ):
        raise ValueError(
            "total_points market is missing."
        )

    candidates = [
        (
            "Home Win",
            _validate_probability(
                moneyline["home_win"]
            ),
        ),
        (
            "Away Win",
            _validate_probability(
                moneyline["away_win"]
            ),
        ),
        (
            (
                f"Over "
                f"{total_points['line']} "
                f"Points"
            ),
            _validate_probability(
                total_points["over"]
            ),
        ),
        (
            (
                f"Under "
                f"{total_points['line']} "
                f"Points"
            ),
            _validate_probability(
                total_points["under"]
            ),
        ),
    ]

    return candidates
def _insufficient_prediction(
    game,
    reason,
):
    """
    Return a capability-honest result when required source data is absent.

    No probabilities or fabricated expected scores are produced.
    """
    teams = game.get(
        "teams",
        {},
    )

    home_team = teams.get(
        "home",
        {},
    )

    away_team = teams.get(
        "away",
        {},
    )

    league = game.get(
        "league",
        {},
    )

    return {
        "game_id": game.get("id"),
        "date": game.get("date"),
        "home_team": home_team.get(
            "name"
        ),
        "away_team": away_team.get(
            "name"
        ),
        "league": league.get(
            "name",
            "NBA",
        ),
        "insufficient_data": True,
        "reason": reason,
        "markets": None,
        "confidence": None,
        "safest": None,
    }


def predict_game(game, home_stats_override=None, away_stats_override=None):
    if not isinstance(game, dict):
        raise ValueError(
            "game must be a dictionary."
        )

    teams = game.get(
        "teams"
    )

    if not isinstance(
        teams,
        dict,
    ):
        raise ValueError(
            "game is missing teams."
        )

    home_team = teams.get(
        "home"
    )

    away_team = teams.get(
        "away"
    )

    if not isinstance(
        home_team,
        dict,
    ):
        raise ValueError(
            "game is missing the home team."
        )

    if not isinstance(
        away_team,
        dict,
    ):
        raise ValueError(
            "game is missing the away team."
        )

    league = game.get(
        "league"
    )

    if not isinstance(
        league,
        dict,
    ):
        raise ValueError(
            "game is missing league information."
        )

    league_id = league.get(
        "id"
    )

    season = league.get(
        "season"
    )

    home_id = home_team.get(
        "id"
    )

    away_id = away_team.get(
        "id"
    )

    if (
        isinstance(home_id, bool)
        or not isinstance(home_id, int)
        or home_id <= 0
    ):
        raise ValueError(
            "Invalid home team ID."
        )

    if (
        isinstance(away_id, bool)
        or not isinstance(away_id, int)
        or away_id <= 0
    ):
        raise ValueError(
            "Invalid away team ID."
        )

    if (
        isinstance(league_id, bool)
        or not isinstance(league_id, int)
        or league_id <= 0
    ):
        raise ValueError(
            "Invalid league ID."
        )

    if (
        isinstance(season, bool)
        or not isinstance(season, int)
        or season <= 0
    ):
        raise ValueError(
            "Invalid season."
        )

    if home_stats_override is not None and away_stats_override is not None:
        home_stats = home_stats_override
        away_stats = away_stats_override
    else:
        home_stats = (
            basketball_api.get_team_statistics(
                home_id,
                league_id,
                season,
            )
        )

        away_stats = (
            basketball_api.get_team_statistics(
                away_id,
                league_id,
                season,
            )
        )

    (
        home_for,
        home_against,
    ) = _extract_scoring(
        home_stats
    )

    (
        away_for,
        away_against,
    ) = _extract_scoring(
        away_stats
    )

    if (
        home_for is None
        or home_against is None
    ):
        return _insufficient_prediction(
            game,
            "Missing or invalid home-team scoring statistics.",
        )

    if (
        away_for is None
        or away_against is None
    ):
        return _insufficient_prediction(
            game,
            "Missing or invalid away-team scoring statistics.",
        )

    home_advantage = getattr(
        config,
        "BASKETBALL_HOME_ADVANTAGE_POINTS",
        0.0,
    )

    if not _valid_number(
        home_advantage
    ):
        raise ValueError(
            "BASKETBALL_HOME_ADVANTAGE_POINTS "
            "must be a finite number."
        )

    expected_home = (
        (
            home_for
            + away_against
        )
        / 2
        + float(home_advantage)
    )

    expected_away = (
        home_against
        + away_for
    ) / 2

    if (
        not _valid_number(
            expected_home
        )
        or not _valid_number(
            expected_away
        )
        or expected_home < 0
        or expected_away < 0
    ):
        return _insufficient_prediction(
            game,
            "Unable to construct valid expected points.",
        )

    point_diff = (
        expected_home
        - expected_away
    )

    p_home = _win_probability(
        point_diff
    )

    p_away = _validate_probability(
        1.0 - p_home
    )

    total_expected = (
        expected_home
        + expected_away
    )

    over_prob = _over_probability(
        total_expected,
        TOTAL_LINE,
    )

    under_prob = _validate_probability(
        1.0 - over_prob
    )

    markets = {
        "expected_points": {
            "home": round(
                expected_home,
                1,
            ),
            "away": round(
                expected_away,
                1,
            ),
        },
        "moneyline": {
            "home_win": p_home,
            "away_win": p_away,
        },
        "total_points": {
            "line": TOTAL_LINE,
            "expected_total": round(
                total_expected,
                1,
            ),
            "over": over_prob,
            "under": under_prob,
        },
    }

    conf = confidence.confidence_flag(
        markets["moneyline"]
    )

    safest = confidence.safest_pick(
        build_basketball_safest_candidates(
            markets
        )
    )

    return {
        "game_id": game["id"],
        "date": game["date"],
        "home_team": home_team["name"],
        "away_team": away_team["name"],
        "league": league.get(
            "name",
            "NBA",
        ),
        "insufficient_data": False,
        "markets": markets,
        "confidence": conf,
        "safest": safest,
    }


def print_prediction(pred):
    if not isinstance(
        pred,
        dict,
    ):
        raise ValueError(
            "Prediction must be a dictionary."
        )

    if pred.get(
        "insufficient_data"
    ):
        print(
            f"\n{pred.get('home_team')} "
            f"vs {pred.get('away_team')} "
            f"({pred.get('league')})"
        )
        print(
            "  Prediction unavailable: "
            f"{pred.get('reason')}"
        )
        return

    m = pred["markets"]
    c = pred["confidence"]
    s = pred["safest"]

    print(
        f"\n{pred['home_team']} vs "
        f"{pred['away_team']} "
        f"({pred['league']})"
    )

    print(
        "  Expected points: "
        f"{m['expected_points']['home']} - "
        f"{m['expected_points']['away']}"
    )

    print(
        "  Moneyline:       "
        f"Home "
        f"{m['moneyline']['home_win']:.0%} | "
        f"Away "
        f"{m['moneyline']['away_win']:.0%}"
    )

    print(
        "  Total points:    "
        f"Line "
        f"{m['total_points']['line']} | "
        f"Expected "
        f"{m['total_points']['expected_total']} | "
        f"Over "
        f"{m['total_points']['over']:.0%} | "
        f"Under "
        f"{m['total_points']['under']:.0%}"
    )

    print(
        "  Confidence:      "
        f"{c['emoji']} "
        f"{c['label']} "
        f"(pick: "
        f"{c['top_pick']}, "
        f"{c['top_probability']:.0%})"
    )

    if s:
        print(
            "  >>> SAFEST PICK: "
            f"{s['label']} "
            f"({s['probability']:.0%}) <<<"
    )
