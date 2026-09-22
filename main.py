"""
Main entry point: run this daily (manually, or on a schedule via GitHub
Actions) to get predictions for a given day's matches.

Usage:
    python3 main.py --date 2026-09-27 --league-name "Premier League" --limit 15
    python3 main.py --limit 10                      (today, top leagues, capped at 10)
    python3 main.py --date 2026-09-28                (tomorrow, all allowed leagues)
    python3 main.py --grade                          (check results of past predictions)
    python3 main.py --accuracy                       (see the track record so far)
    python3 main.py --cleanup                        (remove old non-target-league predictions)
"""

import argparse
import sys
from datetime import datetime, timezone

import requests

import api_football
import backtest
import confidence
import config
import storage
import poisson_model

LEAGUE_AVG_GOALS_FALLBACK = 1.4  # used if we can't compute a league average


def get_league_avg_goals(league_id, season):
    return LEAGUE_AVG_GOALS_FALLBACK


def resolve_league_id(league_arg, league_name_arg):
    """
    Matches a typed league name to a known league, forgiving common typing
    variations: extra spaces, trailing 's', different capitalization, or
    typing just part of the name (e.g. 'nations' matches 'nations league').
    """
    if league_name_arg:
        key = " ".join(league_name_arg.strip().lower().split())  # collapse extra spaces
        key_no_trailing_s = key.rstrip("s") if key.endswith("s") and not key.endswith("ss") else key

        # Exact match first
        if key in config.LEAGUE_NAME_TO_ID:
            return config.LEAGUE_NAME_TO_ID[key]
        if key_no_trailing_s in config.LEAGUE_NAME_TO_ID:
            return config.LEAGUE_NAME_TO_ID[key_no_trailing_s]

        # Partial match: typed text is contained in a known name, or vice versa
        matches = [
            (name, league_id) for name, league_id in config.LEAGUE_NAME_TO_ID.items()
            if key in name or name in key
        ]
        if len(matches) == 1:
            print(f"Matched '{league_name_arg}' to '{matches[0][0]}'.")
            return matches[0][1]

        print(f"Unrecognized league name '{league_name_arg}'. "
              f"Known names: {', '.join(config.LEAGUE_NAME_TO_ID.keys())}")
        sys.exit(1)
    return league_arg


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


def run_daily(date_str, league_id=None, limit=None):
    storage.init_db()
    fixtures = api_football.get_fixtures_by_date(date_str, league_id)

    if league_id is None:
        fixtures = [f for f in fixtures if f["league"]["id"] in config.ALLOWED_LEAGUE_IDS]

    if not fixtures:
        print(f"No fixtures found for {date_str} matching your criteria.")
        return

    if limit:
        fixtures = fixtures[:limit]

    print(f"Found {len(fixtures)} fixture(s) for {date_str} (showing up to {limit or 'all'}).")

    quota_hit = False
    predicted_count = 0

    for fixture in fixtures:
        if quota_hit:
            print("  Skipping remaining matches - daily API quota appears exhausted.")
            break
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
            predicted_count += 1
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                print(f"  Skipped a fixture: daily API quota exhausted (429).")
                quota_hit = True
            else:
                print(f"  Skipped a fixture due to an API error: {e}")
        except Exception as e:
            print(f"  Skipped a fixture due to an error: {e}")

    print(f"\nSuccessfully predicted {predicted_count} of {len(fixtures)} fixture(s).")


def run_grading():
    """
    Check pending predictions against real results, and update the log.
    Each match is graded independently - if one fails (e.g. a rate limit
    or a transient API error), the rest still get processed instead of the
    whole grading step crashing.
    """
    storage.init_db()
    pending = storage.get_pending_fixtures()

    if not pending:
        print("No pending predictions to grade.")
        return

    graded_count = 0
    quota_hit = False

    for fixture_id, match_date, home_team, away_team in pending:
        if quota_hit:
            print(f"  Skipping remaining grading - daily API quota appears exhausted.")
            break
        try:
            result = api_football.get_fixture_result(fixture_id)
            if not result or result["fixture"]["status"]["short"] != "FT":
                continue

            home_goals = result["goals"]["home"]
            away_goals = result["goals"]["away"]
            storage.record_result(fixture_id, home_goals, away_goals)
            print(f"Graded: {home_team} {home_goals}-{away_goals} {away_team}")
            graded_count += 1
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                print(f"  Could not grade {home_team} vs {away_team}: "
                      f"daily API quota exhausted (429).")
                quota_hit = True
            else:
                print(f"  Could not grade {home_team} vs {away_team}: {e}")
        except Exception as e:
            print(f"  Could not grade {home_team} vs {away_team}: {e}")

    print(f"\nGraded {graded_count} of {len(pending)} pending fixture(s).")


def run_cleanup():
    """Remove old predictions from leagues outside your current tracked list."""
    storage.init_db()
    keep_keywords = [
        "Premier League", "La Liga", "Serie A", "Bundesliga", "Ligue 1",
        "Champions League", "Europa League", "Nations League",
        "World Cup", "Euro",
    ]
    deleted, total = storage.cleanup_non_target_leagues(keep_keywords)
    print(f"Removed {deleted} of {total} predictions from leagues outside your current tracked list.")
    print(f"Kept {total - deleted} prediction(s) from your tracked leagues.")


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
    parser.add_argument("--date", help="Date to fetch fixtures for, YYYY-MM-DD (defaults to today)")
    parser.add_argument("--league", type=int, help="League ID to filter by (numeric)")
    parser.add_argument("--league-name", help="League name to filter by, e.g. 'Premier League'")
    parser.add_argument("--limit", type=int, help="Max number of matches to predict, e.g. 15")
    parser.add_argument("--grade", action="store_true", help="Grade past predictions against results")
    parser.add_argument("--accuracy", action="store_true", help="Show accuracy track record")
    parser.add_argument("--cleanup", action="store_true", help="Remove old predictions from non-target leagues")
    args = parser.parse_args()

    if args.grade:
        run_grading()
    elif args.accuracy:
        run_accuracy_report()
    elif args.cleanup:
        run_cleanup()
    else:
        date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        league_id = resolve_league_id(args.league, args.league_name)
        run_daily(date_str, league_id, args.limit)
