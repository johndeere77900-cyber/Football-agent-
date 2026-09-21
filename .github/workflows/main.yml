"""
Main entry point: run this daily (manually, or on a schedule via GitHub
Actions) to get predictions for a given day's matches.

Usage:
    python3 main.py --date 2026-09-27 --league 39
    python3 main.py --date 2026-09-27          (all leagues that day)
    python3 main.py --grade                    (check results of past predictions)
    python3 main.py --accuracy                 (see the track record so far)
"""

import argparse
import sys

import api_football
import backtest
import confidence
import storage
import poisson_model

LEAGUE_AVG_GOALS_FALLBACK = 1.4  # used if we can't compute a league average


def get_league_avg_goals(league_id, season):
    """
    Rough league-average goals per team per game. A more precise version
    would average this across all teams in the league standings; this
    fallback keeps the API call count down for the free tier.
    """
    return LEAGUE_AVG_GOALS_FALLBACK


def predict_fixture(fixture, league_avg_goals):
    home_team = fixture["teams"]["home"]
    away_team = fixture["teams"]["away"]
    league = fixture["league"]

    home_stats = api_football.get_team_statistics(home_team["id"], league["id"], league["season"])
    away_stats = api_football.get_team_statistics(away_team["id"], league["id"], league["season"])

    home_attack, home_defense = backtest.estimate_expected_goals_from_stats(
        home_stats, home_stats, league_avg_goals)
    away_attack, away_defense = backtest.estimate_expected_goals_from_stats(
        away_stats, away_stats, league_avg_goals)

    markets, conf = backtest.predict_match(
        home_attack, home_defense, away_attack, away_defense, league_avg_goals)

    return {
        "fixture_id": fixture["fixture"]["id"],
        "date": fixture["fixture"]["date"],
        "home_team": home_team["name"],
        "away_team": away_team["name"],
        "league": league["name"],
        "markets": markets,
        "confidence": conf,
    }


def print_prediction(pred):
    m = pred["markets"]
    c = pred["confidence"]
    print(f"\n{pred['home_team']} vs {pred['away_team']}  ({pred['league']})")
    print(f"  Expected goals: {m['expected_goals']['home']} - {m['expected_goals']['away']}")
    print(f"  Win/Draw/Loss:  Home {m['match_result']['home_win']:.0%} | "
          f"Draw {m['match_result']['draw']:.0%} | Away {m['match_result']['away_win']:.0%}")
    print(f"  Over/Under 2.5: Over {m['over_under']['over_2_5']:.0%} | "
          f"Under {m['over_under']['under_2_5']:.0%}")
    print(f"  BTTS:           Yes {m['btts']['yes']:.0%} | No {m['btts']['no']:.0%}")
    print(f"  Top scoreline:  {m['top_scorelines'][0]['score']} "
          f"({m['top_scorelines'][0]['probability']:.0%})")
    print(f"  Confidence:     {c['emoji']} {c['label']}  "
          f"(pick: {c['top_pick']}, {c['top_probability']:.0%})")


def run_daily(date_str, league_id=None):
    storage.init_db()
    fixtures = api_football.get_fixtures_by_date(date_str, league_id)

    if not fixtures:
        print(f"No fixtures found for {date_str}.")
        return

    print(f"Found {len(fixtures)} fixture(s) for {date_str}.")

    for fixture in fixtures:
        try:
            league_avg_goals = get_league_avg_goals(
                fixture["league"]["id"], fixture["league"]["season"])
            pred = predict_fixture(fixture, league_avg_goals)
            print_prediction(pred)

            storage.save_prediction(
                fixture_id=pred["fixture_id"],
                match_date=pred["date"],
                home_team=pred["home_team"],
                away_team=pred["away_team"],
                league=pred["league"],
                markets=pred["markets"],
                confidence=pred["confidence"],
            )
        except Exception as e:
            print(f"  Skipped a fixture due to an error: {e}")


def run_grading():
    """Check pending predictions against real results, and update the log."""
    storage.init_db()
    pending = storage.get_pending_fixtures()

    if not pending:
        print("No pending predictions to grade.")
        return

    for fixture_id, match_date, home_team, away_team in pending:
        result = api_football.get_fixture_result(fixture_id)
        if not result or result["fixture"]["status"]["short"] != "FT":
            continue

        home_goals = result["goals"]["home"]
        away_goals = result["goals"]["away"]
        storage.record_result(fixture_id, home_goals, away_goals)
        print(f"Graded: {home_team} {home_goals}-{away_goals} {away_team}")


def run_accuracy_report():
    storage.init_db()
    summary = storage.accuracy_summary()

    if summary["total_graded"] == 0:
        print("No graded predictions yet - run --grade after some matches finish.")
        return

    print(f"Total graded predictions: {summary['total_graded']}")
    print(f"Overall accuracy (top pick correct): {summary['overall_accuracy']:.1%}")
    print("\nAccuracy by confidence level:")
    for label, stats in summary.get("by_confidence", {}).items():
        print(f"  {label}: {stats['accuracy']:.1%} ({stats['count']} predictions)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Football prediction agent")
    parser.add_argument("--date", help="Date to fetch fixtures for, YYYY-MM-DD")
    parser.add_argument("--league", type=int, help="Optional league ID to filter by")
    parser.add_argument("--grade", action="store_true", help="Grade past predictions against results")
    parser.add_argument("--accuracy", action="store_true", help="Show accuracy track record")
    args = parser.parse_args()

    if args.grade:
        run_grading()
    elif args.accuracy:
        run_accuracy_report()
    elif args.date:
        run_daily(args.date, args.league)
    else:
        parser.print_help()
        sys.exit(1)
