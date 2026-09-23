"""
Basketball prediction model.

Basketball scores are much higher and more consistent than football goals,
so instead of the Poisson goals model used for football, this uses each
team's average points scored/allowed to estimate an expected final score,
then converts the point differential into a win probability using a
normal-distribution approximation (a standard technique in basketball
analytics - NBA game margins are roughly normally distributed).
"""

import math

import basketball_api
import config
import confidence

# Fallback used when a team's stats aren't available yet (e.g. very early
# season) - roughly a typical modern NBA team's points per game.
LEAGUE_AVG_POINTS = 113.0

# Standard deviation of NBA game margins, used to convert a point
# differential into a win probability. ~12 points is a commonly cited
# figure for NBA scoring margin variability.
MARGIN_STD_DEV = 12.0

# Standard deviation used for the combined total-points estimate.
TOTAL_STD_DEV = 15.0

# Default over/under line for total points, when nothing more specific
# is available.
TOTAL_LINE = 224.5


def _extract_scoring(stats):
    """
    Pulls average points-for and points-against out of an API-Basketball
    team statistics response. Falls back to the league average if the
    data isn't available (common early in a season).
    """
    try:
        points_for = float(stats.get("points", {}).get("for", {}).get("average", {}).get("all"))
    except (TypeError, ValueError, AttributeError):
        points_for = LEAGUE_AVG_POINTS

    try:
        points_against = float(stats.get("points", {}).get("against", {}).get("average", {}).get("all"))
    except (TypeError, ValueError, AttributeError):
        points_against = LEAGUE_AVG_POINTS

    return points_for, points_against


def _win_probability(point_diff):
    """
    Converts an expected point differential into a win probability using
    the normal cumulative distribution function (via math.erf).
    """
    return 0.5 * (1 + math.erf(point_diff / (MARGIN_STD_DEV * math.sqrt(2))))


def _over_probability(expected_total, line):
    """Probability the combined score goes over a given line."""
    diff = expected_total - line
    return 0.5 * (1 + math.erf(diff / (TOTAL_STD_DEV * math.sqrt(2))))


def predict_game(game):
    """
    Takes a single game dict (as returned by basketball_api.get_games_by_date)
    and returns predicted markets + confidence flag.
    """
    home_team = game["teams"]["home"]
    away_team = game["teams"]["away"]
    league = game["league"]
    league_id = league["id"]
    season = league["season"]

    home_stats = basketball_api.get_team_statistics(home_team["id"], league_id, season)
    away_stats = basketball_api.get_team_statistics(away_team["id"], league_id, season)

    home_for, home_against = _extract_scoring(home_stats)
    away_for, away_against = _extract_scoring(away_stats)

    expected_home = (home_for + away_against) / 2 + config.BASKETBALL_HOME_ADVANTAGE_POINTS
    expected_away = (away_for + home_against) / 2

    point_diff = expected_home - expected_away
    p_home = _win_probability(point_diff)
    p_away = 1 - p_home

    total_expected = expected_home + expected_away
    over_prob = _over_probability(total_expected, TOTAL_LINE)

    markets = {
        "expected_points": {
            "home": round(expected_home, 1),
            "away": round(expected_away, 1),
        },
        "moneyline": {
            "home_win": p_home,
            "away_win": p_away,
        },
        "total_points": {
            "line": TOTAL_LINE,
            "expected_total": round(total_expected, 1),
            "over": over_prob,
            "under": 1 - over_prob,
        },
    }

    conf = confidence.confidence_flag(markets["moneyline"])

    return {
        "game_id": game["id"],
        "date": game["date"],
        "home_team": home_team["name"],
        "away_team": away_team["name"],
        "league": league.get("name", "NBA"),
        "markets": markets,
        "confidence": conf,
    }


def print_prediction(pred):
    m = pred["markets"]
    c = pred["confidence"]
    print(f"\n{pred['home_team']} vs {pred['away_team']}  ({pred['league']})")
    print(f"  Expected points: {m['expected_points']['home']} - {m['expected_points']['away']}")
    print(f"  Moneyline:       Home {m['moneyline']['home_win']:.0%} | "
          f"Away {m['moneyline']['away_win']:.0%}")
    print(f"  Total points:    Line {m['total_points']['line']} | "
          f"Expected {m['total_points']['expected_total']} | "
          f"Over {m['total_points']['over']:.0%} | Under {m['total_points']['under']:.0%}")
    print(f"  Confidence:      {c['emoji']} {c['label']}  "
          f"(pick: {c['top_pick']}, {c['top_probability']:.0%})")
