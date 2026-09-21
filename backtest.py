"""
Backtesting: run the model against matches that have ALREADY finished,
using only the data that would have been available beforehand, then check
the prediction against what actually happened.

This is how we find out whether the model is any good BEFORE relying on it
for upcoming matches.
"""

import elo
import poisson_model
import confidence
import api_football


def estimate_expected_goals_from_stats(team_stats_for, team_stats_against, league_avg_goals):
    """
    Pulls goals-for/against averages out of an API-Football team statistics
    response and converts them into the attack/defense ratios the Poisson
    model needs.
    """
    goals_for_avg = team_stats_for.get("goals", {}).get("for", {}).get("average", {}).get("total")
    goals_against_avg = team_stats_against.get("goals", {}).get("against", {}).get("average", {}).get("total")

    goals_for_avg = float(goals_for_avg) if goals_for_avg else league_avg_goals
    goals_against_avg = float(goals_against_avg) if goals_against_avg else league_avg_goals

    attack_ratio = goals_for_avg / league_avg_goals
    defense_ratio = goals_against_avg / league_avg_goals

    return attack_ratio, defense_ratio


def predict_match(home_attack, home_defense, away_attack, away_defense, league_avg_goals):
    """
    Core prediction step shared by both live predictions and backtesting.
    *_attack / *_defense are ratios relative to league average (1.0 = average).
    """
    home_xg = poisson_model.expected_goals(home_attack, away_defense, league_avg_goals)
    away_xg = poisson_model.expected_goals(away_attack, home_defense, league_avg_goals)

    markets = poisson_model.market_probabilities(home_xg, away_xg)
    conf = confidence.confidence_flag(markets["match_result"])

    return markets, conf


def run_synthetic_selftest():
    """
    Quick sanity check using made-up but realistic numbers - no API calls,
    no API key required. Confirms the math pipeline behaves as expected
    before we ever touch real data.

    Scenario: a strong home team (scores more, concedes less than average)
    vs a weak away team (scores less, concedes more than average).
    """
    league_avg_goals = 1.4  # a fairly typical league-wide average

    # Strong home team: scores 60% more than average, concedes 30% less
    home_attack, home_defense = 1.6, 0.7
    # Weak away team: scores 30% less than average, concedes 40% more
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


def backtest_finished_fixture(fixture_id, home_team_id, away_team_id, league_id, season, league_avg_goals):
    """
    Real backtest against one already-finished match. Uses the team's
    SEASON stats (an approximation - a true point-in-time backtest needs
    the paid historical endpoint).
    """
    home_stats = api_football.get_team_statistics(home_team_id, league_id, season)
    away_stats = api_football.get_team_statistics(away_team_id, league_id, season)

    home_attack, home_defense = estimate_expected_goals_from_stats(home_stats, home_stats, league_avg_goals)
    away_attack, away_defense = estimate_expected_goals_from_stats(away_stats, away_stats, league_avg_goals)

    markets, conf = predict_match(home_attack, home_defense, away_attack, away_defense, league_avg_goals)

    result = api_football.get_fixture_result(fixture_id)
    actual_home_goals = result["goals"]["home"] if result else None
    actual_away_goals = result["goals"]["away"] if result else None

    return {
        "predicted_markets": markets,
        "confidence": conf,
        "actual_home_goals": actual_home_goals,
        "actual_away_goals": actual_away_goals,
    }


if __name__ == "__main__":
    run_synthetic_selftest()
