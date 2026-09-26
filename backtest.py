"""
Backtesting and prediction-building helpers shared between live predictions
and the synthetic self-test.
"""

import elo
import poisson_model
import confidence
import api_football


def estimate_expected_goals_from_stats(team_stats_for, team_stats_against, league_avg_goals):
    if not isinstance(team_stats_for, dict):
        team_stats_for = {}
    if not isinstance(team_stats_against, dict):
        team_stats_against = {}

    goals_for_avg = (
        team_stats_for.get("goals", {}).get("for", {}).get("average", {}).get("total")
    )
    goals_against_avg = (
        team_stats_against.get("goals", {}).get("against", {}).get("average", {}).get("total")
    )

    try:
        goals_for_avg = float(goals_for_avg) if goals_for_avg not in (None, "") else league_avg_goals
    except (TypeError, ValueError):
        goals_for_avg = league_avg_goals

    try:
        goals_against_avg = float(goals_against_avg) if goals_against_avg not in (None, "") else league_avg_goals
    except (TypeError, ValueError):
        goals_against_avg = league_avg_goals

    attack_ratio = goals_for_avg / league_avg_goals
    defense_ratio = goals_against_avg / league_avg_goals

    return attack_ratio, defense_ratio


def estimate_avg_cards(team_stats, league_avg_cards=3.8):
    """
    Pulls a team's average cards-per-game from their statistics response.
    Falls back to a typical league-average if unavailable. 3.8 is a
    reasonable rough baseline across major European leagues.
    """
    if not isinstance(team_stats, dict):
        return league_avg_cards
    try:
        yellow = team_stats.get("cards", {}).get("yellow", {})
        total_yellow = sum(
            v.get("total") or 0 for v in yellow.values() if isinstance(v, dict)
        )
        fixtures_played = team_stats.get("fixtures", {}).get("played", {}).get("total")
        if fixtures_played and fixtures_played > 0:
            return total_yellow / fixtures_played
    except (TypeError, AttributeError):
        pass
    return league_avg_cards


def predict_match(home_attack, home_defense, away_attack, away_defense, league_avg_goals):
    home_xg = poisson_model.expected_goals(home_attack, away_defense, league_avg_goals)
    away_xg = poisson_model.expected_goals(away_attack, home_defense, league_avg_goals)

    markets = poisson_model.market_probabilities(home_xg, away_xg)
    conf = confidence.confidence_flag(markets["match_result"])

    return markets, conf


def run_synthetic_selftest():
    league_avg_goals = 1.4
    home_attack, home_defense = 1.6, 0.7
    away_attack, away_defense = 0.7, 1.4

    markets, conf = predict_match(home_attack, home_defense, away_attack, away_defense, league_avg_goals)

    print("=== Synthetic self-test: strong home side vs weak away side ===")
    print(f"Expected goals -> Home: {markets['expected_goals']['home']}, "
          f"Away: {markets['expected_goals']['away']}")
    print(f"Match result -> Home {markets['match_result']['home_win']:.1%}, "
          f"Draw {markets['match_result']['draw']:.1%}, "
          f"Away {markets['match_result']['away_win']:.1%}")
    print(f"Over 2.5 goals: {markets['over_under']['over_2_5']:.1%}")
    print(f"BTTS Yes: {markets['btts']['yes']:.1%}")
    print("Top scorelines:")
    for s in markets["top_scorelines"]:
        print(f"   {s['score']}  ({s['probability']:.1%})")
    print(f"Confidence: {conf['emoji']} {conf['label']} "
          f"(top pick: {conf['top_pick']} at {conf['top_probability']:.1%}, "
          f"gap: {conf['gap']:.1%})")

    total_prob = sum(markets["match_result"].values())
    assert 0.99 <= total_prob <= 1.01, f"Probabilities don't sum to 1: {total_prob}"
    assert markets["match_result"]["home_win"] > markets["match_result"]["away_win"], \
        "Strong home team should be favoured over weak away team"

    print("\nSelf-test passed: math checks out.")
    return markets, conf


if __name__ == "__main__":
    run_synthetic_selftest()
