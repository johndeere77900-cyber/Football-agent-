"""
Leakage-safe historical feature reconstruction for basketball.

Extracts team scoring and defensive averages, recent form, and team history
strictly from completed games prior to the prediction cutoff timestamp.

No future games or same-timestamp target games are included.
"""

from typing import Any, Dict, List, Optional, Tuple
import historical_match_policy


def _game_date(game: Dict[str, Any]) -> str:
    return str(game.get("date", ""))


def _is_before_cutoff(game: Dict[str, Any], cutoff: str) -> bool:
    return _game_date(game) < cutoff


def prior_completed_games(games: List[Dict[str, Any]], cutoff: str) -> List[Dict[str, Any]]:
    """Return all completed basketball games strictly before cutoff timestamp."""
    eligible = [
        game
        for game in games
        if historical_match_policy.is_finished_match(game, sport="basketball")
        and _is_before_cutoff(game, cutoff)
    ]
    return sorted(eligible, key=_game_date)


def team_scoring_averages(games: List[Dict[str, Any]], team_id: int, cutoff: str) -> Optional[Dict[str, float]]:
    """
    Calculate average points for and against a team prior to cutoff.
    Returns dict: {"matches": count, "points_for": avg_for, "points_against": avg_against} or None.
    """
    prior = prior_completed_games(games, cutoff)
    points_for = []
    points_against = []

    for game in prior:
        teams = game.get("teams", {})
        h_id = teams.get("home", {}).get("id")
        a_id = teams.get("away", {}).get("id")

        if team_id not in (h_id, a_id):
            continue

        h_pts, a_pts = historical_match_policy.get_basketball_match_points(game)
        if h_pts is None or a_pts is None:
            continue

        if h_id == team_id:
            points_for.append(h_pts)
            points_against.append(a_pts)
        else:
            points_for.append(a_pts)
            points_against.append(h_pts)

    if not points_for:
        return None

    matches = len(points_for)
    return {
        "matches": matches,
        "points_for": sum(points_for) / matches,
        "points_against": sum(points_against) / matches,
    }


def reconstruct_basketball_team_stats(games: List[Dict[str, Any]], team_id: int, cutoff: str) -> Optional[Dict[str, Any]]:
    """
    Reconstruct the team statistics payload expected by basketball_model._extract_scoring().
    """
    avgs = team_scoring_averages(games, team_id, cutoff)
    if avgs is None:
        return None

    return {
        "points": {
            "for": {
                "average": {
                    "all": avgs["points_for"],
                }
            },
            "against": {
                "average": {
                    "all": avgs["points_against"],
                }
            },
        }
    }


def game_has_minimum_history(
    games: List[Dict[str, Any]],
    home_team_id: int,
    away_team_id: int,
    cutoff: str,
    minimum_matches: int = 5,
) -> bool:
    """Validate that both home and away teams have at least minimum_matches prior completed games."""
    home_avgs = team_scoring_averages(games, home_team_id, cutoff)
    away_avgs = team_scoring_averages(games, away_team_id, cutoff)

    if home_avgs is None or home_avgs["matches"] < minimum_matches:
        return False
    if away_avgs is None or away_avgs["matches"] < minimum_matches:
        return False

    return True
