"""
Main entry point: run this daily (manually, or on a schedule via GitHub
Actions) to get predictions for a given day's matches.

Usage (football - default):
    python3 main.py --date 2026-09-27 --league-name "Premier League" --limit 15
    python3 main.py --limit 10
    python3 main.py --grade
    python3 main.py --accuracy
    python3 main.py --cleanup

Usage (basketball):
    python3 main.py --sport basketball --date 2026-09-27 --limit 5
    python3 main.py --sport basketball --grade
    python3 main.py --sport basketball --accuracy
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
import live_model
import storage
import poisson_model

LEAGUE_AVG_GOALS_FALLBACK = 1.4

FOOTBALL_FINISHED_STATUSES = {"FT", "AET", "PEN", "PST", "CANC", "ABD", "AWD", "WO"}
FOOTBALL_LIVE_STATUSES = {"1H", "2H", "HT", "ET", "BT", "P", "SUSP", "INT"}
BASKETBALL_FINISHED_STATUSES = {"FT", "AOT", "CANC", "ABD"}


def get_league_avg_goals(league_id, season):
    return LEAGUE_AVG_GOALS_FALLBACK


def resolve_league_id(league_arg, league_name_arg):
    if league_name_arg:
        key = " ".join(league_name_arg.strip().lower().split())
        key_no_trailing_s = key.rstrip("s") if key.endswith("s") and not key.endswith("ss") else key

        if key in config.LEAGUE_NAME_TO_ID:
            return config.LEAGUE_NAME_TO_ID[key]
        if key_no_trailing_s in config.LEAGUE_NAME_TO_ID:
            return config.LEAGUE_NAME_TO_ID[key_no_trailing_s]

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


def build_football_safest_candidates(m):
    return [
        ("Home Win", m["match_result"]["home_win"]),
        ("Draw", m["match_result"]["draw"]),
        ("Away Win", m["match_result"]["away_win"]),
        ("Home or Draw", m["double_chance"]["home_or_draw"]),
        ("Away or Draw", m["double_chance"]["away_or_draw"]),
        ("Home or Away (no draw)", m["double_chance"]["home_or_away"]),
        ("Over 1.5 Goals", m["over_under"]["over_1_5"]),
        ("Under 1.5 Goals", m["over_under"]["under_1_5"]),
        ("Over 2.5 Goals", m["over_under"]["over_2_5"]),
        ("Under 2.5 Goals", m["over_under"]["under_2_5"]),
        ("Over 3.5 Goals", m["over_under"]["over_3_5"]),
        ("Under 3.5 Goals", m["over_under"]["under_3_5"]),
        ("BTTS Yes", m["btts"]["yes"]),
        ("BTTS No", m["btts"]["no"]),
    ]


def predict_fixture(fixture, league_avg_goals):
    home_team = fixture["teams"]["home"]
    away_team = fixture["teams"]["away"]
    league = fixture["league"]
    status_short = fixture["fixture"]["status"]["short"]
    elapsed = fixture["fixture"]["status"].get("elapsed")
    is_live = status_short in FOOTBALL_LIVE_STATUSES

    home_stats = api_football.get_team_statistics(home_team["id"], league["id"], league["season"])
    away_stats = api_football.get_team_statistics(away_team["id"], league["id"], league["season"])
    insufficient_data = not home_stats and not away_stats

    home_attack, home_defense = backtest.estimate_expected_goals_from_stats(
        home_stats, home_stats, league_avg_goals)
    away_attack, away_defense = backtest.estimate_expected_goals_from_stats(
        away_stats, away_stats, league_avg_goals)

    home_xg = poisson_model.expected_goals(home_attack, away_defense, league_avg_goals)
    away_xg = poisson_model.expected_goals(away_attack, home_defense, league_avg_goals)

    if is_live:
        current_home_goals = fixture["goals"]["home"] or 0
        current_away_goals = fixture["goals"]["away"] or 0
        markets = live_model.live_market_probabilities(
            home_xg, away_xg, elapsed, status_short, current_home_goals, current_away_goals)
    else:
        markets = poisson_model.market_probabilities(home_xg, away_xg)
        markets["is_live"] = False

    conf = confidence.confidence_flag(markets["match_result"])
    safest = confidence.safest_pick(build_football_safest_candidates(markets))

    return {
        "fixture_id": fixture["fixture"]["id"],
        "date": fixture["fixture"]["date"],
        "home_team": home_team["name"],
        "away_team": away_team["name"],
        "league": league["name"],
        "markets": markets,
        "confidence": conf,
        "safest": safest,
        "is_live": is_live,
        "insufficient_data": insufficient_data,
    }


def print_prediction(pred):
    m = pred["markets"]
    c = pred["confidence"]
    s = pred["safest"]
    print(f"\n{pred['home_team']} vs {pred['away_team']}  ({pred['league']})")

    if pred["is_live"]:
        print(f"  \U0001F534 LIVE - {m['minutes_elapsed']}' "
              f"(est. {m['minutes_remaining_estimate']} min remaining)")
        cs = m["current_score"]
        print(f"  Current score: {cs['home']} - {cs['away']}")
        eg = m["expected_additional_goals"]
        print(f"  Expected additional goals: {eg['home']} - {eg['away']}")
    else:
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
    if s:
        print(f"  >>> SAFEST PICK: {s['label']} ({s['probability']:.0%}) <<<")


def run_daily(date_str, league_id=None, limit=None):
    storage.init_db()
    fixtures = api_football.get_fixtures_by_date(date_str, league_id)

    if league_id is None:
        fixtures = [f for f in fixtures if f["league"]["id"] in config.ALLOWED_LEAGUE_IDS]

    fixtures = [f for f in fixtures if f["fixture"]["status"]["short"] not in FOOTBALL_FINISHED_STATUSES]

    if not fixtures:
        print(f"No fixtures found for {date_str} matching your criteria.")
        return

    if limit:
        fixtures = fixtures[:limit]

    print(f"Found {len(fixtures)} fixture(s) for {date_str} (showing up to {limit or 'all'}).")

    quota_hit = False
    predicted_count = 0
    skipped_no_data = 0

    for fixture in fixtures:
        if quota_hit:
            print("  Skipping remaining matches - daily API quota appears exhausted.")
            break
        try:
            league_avg_goals = get_league_avg_goals(
                fixture["league"]["id"], fixture["league"]["season"])
            pred = predict_fixture(fixture, league_avg_goals)

            if pred["insufficient_data"]:
                print(f"\n{pred['home_team']} vs {pred['away_team']}  ({pred['league']})")
                print("  Skipped: not enough team data available for a real prediction.")
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

    print(f"\nSuccessfully predicted {predicted_count} of {len(fixtures)} fixture(s) "
          f"({skipped_no_data} skipped due to insufficient data).")


def run_grading():
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


# --- Basketball --------------------------------------------------------

def run_daily_basketball(date_str, limit=None):
    storage.init_basketball_db()
    games = basketball_api.get_games_by_date(date_str, config.ALLOWED_BASKETBALL_LEAGUE_IDS[0])

    games = [g for g in games if g.get("status", {}).get("short") not in BASKETBALL_FINISHED_STATUSES]

    if not games:
        print(f"No basketball games found for {date_str}.")
        return

    if limit:
        games = games[:limit]

    print(f"Found {len(games)} basketball game(s) for {date_str} (showing up to {limit or 'all'}).")

    quota_hit = False
    predicted_count = 0

    for game in games:
        if quota_hit:
            print("  Skipping remaining games - daily API quota appears exhausted.")
            break
        try:
            pred = basketball_model.predict_game(game)
            basketball_model.print_prediction(pred)

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
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                print(f"  Skipped a game: daily API quota exhausted (429).")
                quota_hit = True
            else:
                print(f"  Skipped a game due to an API error: {e}")
        except Exception as e:
            print(f"  Skipped a game due to an error: {e}")

    print(f"\nSuccessfully predicted {predicted_count} of {len(games)} game(s).")


def run_grading_basketball():
    storage.init_basketball_db()
    pending = storage.get_pending_basketball_games()

    if not pending:
        print("No pending basketball predictions to grade.")
        return

    graded_count = 0
    quota_hit = False

    for game_id, game_date, home_team, away_team in pending:
        if quota_hit:
            print("  Skipping remaining grading - daily API quota appears exhausted.")
            break
        try:
            result = basketball_api.get_game_result(game_id)
            if not result or result.get("status", {}).get("short") != "FT":
                continue

            home_points = result["scores"]["home"]["total"]
            away_points = result["scores"]["away"]["total"]
            storage.record_basketball_result(game_id, home_points, away_points)
            print(f"Graded: {home_team} {home_points}-{away_points} {away_team}")
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

    print(f"\nGraded {graded_count} of {len(pending)} pending game(s).")


def run_accuracy_report_basketball():
    storage.init_basketball_db()
    summary = storage.basketball_accuracy_summary()

    if summary["total_graded"] == 0:
        print("No graded basketball predictions yet - run --sport basketball --grade after some games finish.")
        return

    print(f"Total graded basketball predictions: {summary['total_graded']}")
    print(f"Overall accuracy (top pick correct): {summary['overall_accuracy']:.1%}")
    print("\nAccuracy by confidence level:")
    for label, stats in summary.get("by_confidence", {}).items():
        print(f"  {label}: {stats['accuracy']:.1%} ({stats['count']} predictions)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sports prediction agent")
    parser.add_argument("--sport", choices=["football", "basketball"], default="football")
    parser.add_argument("--date", help="Date to fetch fixtures/games for, YYYY-MM-DD (defaults to today)")
    parser.add_argument("--league", type=int, help="League ID to filter by (numeric, football only)")
    parser.add_argument("--league-name", help="League name to filter by (football only)")
    parser.add_argument("--limit", type=int, help="Max number of matches/games to predict")
    parser.add_argument("--grade", action="store_true")
    parser.add_argument("--accuracy", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()

    if args.sport == "basketball":
        if args.grade:
            run_grading_basketball()
        elif args.accuracy:
            run_accuracy_report_basketball()
        else:
            date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            run_daily_basketball(date_str, args.limit)
    else:
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
