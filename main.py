"""
Main entry point: run this daily (manually, or on a schedule via GitHub
Actions) to get predictions for a given day's matches.
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
import storage
import poisson_model

FOOTBALL_FINISHED_STATUSES = {"FT", "AET", "PEN", "PST", "CANC", "ABD", "AWD", "WO"}
FOOTBALL_LIVE_STATUSES = {"1H", "2H", "HT", "ET", "BT", "P", "SUSP", "INT"}
BASKETBALL_FINISHED_STATUSES = {"FT", "AOT", "CANC", "ABD"}

_league_avg_cache = {}


def get_league_avg_goals(league_id, season):
    """
    Calculates a real, live league-average-goals figure from current
    standings, caching it per run so we don't re-fetch for every match in
    the same league. Falls back to a fixed estimate if standings aren't
    available (early season, or a competition without a standings endpoint).
    """
    cache_key = (league_id, season)
    if cache_key in _league_avg_cache:
        return _league_avg_cache[cache_key]

    avg = None
    try:
        standings = api_football.get_league_standings(league_id, season)
        ratios = []
        for team in standings:
            played = team.get("all", {}).get("played")
            goals_for = team.get("all", {}).get("goals", {}).get("for")
            if played and goals_for is not None and played > 0:
                ratios.append(goals_for / played)
        if ratios:
            avg = sum(ratios) / len(ratios)
    except Exception:
        avg = None

    if avg is None:
        avg = config.LEAGUE_AVG_GOALS.get(league_id, config.LEAGUE_AVG_GOALS_FALLBACK)

    _league_avg_cache[cache_key] = avg
    return avg


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
    candidates = [
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
    if "cards" in m:
        candidates.append((f"Over {m['cards']['over_line']} Cards", m["cards"]["over"]))
        candidates.append((f"Under {m['cards']['over_line']} Cards", m["cards"]["under"]))
    return candidates


def _blend_elo_into_match_result(match_result, elo_probs, weight):
    blended = {
        "home_win": match_result["home_win"] * (1 - weight) + elo_probs["home"] * weight,
        "draw": match_result["draw"] * (1 - weight) + elo_probs["draw"] * weight,
        "away_win": match_result["away_win"] * (1 - weight) + elo_probs["away"] * weight,
    }
    total = sum(blended.values())
    return {k: v / total for k, v in blended.items()}


def predict_fixture(fixture, league_avg_goals, fetch_odds=False):
    home_team = fixture["teams"]["home"]
    away_team = fixture["teams"]["away"]
    league = fixture["league"]
    status_short = fixture["fixture"]["status"]["short"]
    elapsed = fixture["fixture"]["status"].get("elapsed")
    is_live = status_short in FOOTBALL_LIVE_STATUSES

    home_stats = api_football.get_team_statistics(home_team["id"], league["id"], league["season"])
    away_stats = api_football.get_team_statistics(away_team["id"], league["id"], league["season"])
    insufficient_data = not home_stats and not away_stats

    home_season_a, home_season_d = backtest.estimate_expected_goals_from_stats(home_stats, home_stats, league_avg_goals)
    away_season_a, away_season_d = backtest.estimate_expected_goals_from_stats(away_stats, away_stats, league_avg_goals)

    home_recent_a, home_recent_d = backtest.estimate_recent_form_goals(home_team["id"], league_avg_goals)
    away_recent_a, away_recent_d = backtest.estimate_recent_form_goals(away_team["id"], league_avg_goals)

    h2h_home_a, h2h_home_d, h2h_away_a, h2h_away_d = backtest.estimate_head_to_head_goals(
        home_team["id"], away_team["id"], league_avg_goals)

    home_attack = backtest.blend_three(home_season_a, home_recent_a, h2h_home_a)
    home_defense = backtest.blend_three(home_season_d, home_recent_d, h2h_home_d)
    away_attack = backtest.blend_three(away_season_a, away_recent_a, h2h_away_a)
    away_defense = backtest.blend_three(away_season_d, away_recent_d, h2h_away_d)

    home_xg = poisson_model.expected_goals(home_attack, away_defense, league_avg_goals, is_home=True)
    away_xg = poisson_model.expected_goals(away_attack, home_defense, league_avg_goals, is_home=False)

    home_cards_avg = backtest.estimate_avg_cards(home_stats)
    away_cards_avg = backtest.estimate_avg_cards(away_stats)

    home_elo = storage.get_team_rating(home_team["id"])
    away_elo = storage.get_team_rating(away_team["id"])
    elo_cross_check = elo.win_draw_loss_probabilities(home_elo, away_elo)

    if is_live:
        current_home_goals = fixture["goals"]["home"] or 0
        current_away_goals = fixture["goals"]["away"] or 0
        markets = live_model.live_market_probabilities(
            home_xg, away_xg, elapsed, status_short, current_home_goals, current_away_goals)
    else:
        markets = poisson_model.market_probabilities(home_xg, away_xg)
        markets["is_live"] = False
        markets["cards"] = poisson_model.cards_market(home_cards_avg, away_cards_avg)

        # Blend Elo's independent view into the main match-result probabilities
        markets["match_result"] = _blend_elo_into_match_result(
            markets["match_result"], elo_cross_check, config.ELO_BLEND_WEIGHT)
        markets["double_chance"] = {
            "home_or_draw": markets["match_result"]["home_win"] + markets["match_result"]["draw"],
            "away_or_draw": markets["match_result"]["away_win"] + markets["match_result"]["draw"],
            "home_or_away": markets["match_result"]["home_win"] + markets["match_result"]["away_win"],
        }

    conf = confidence.confidence_flag(markets["match_result"])
    safest = confidence.safest_pick(build_football_safest_candidates(markets))

    odds_comparison = None
    if fetch_odds:
        sport_key = config.LEAGUE_ID_TO_ODDS_SPORT_KEY.get(league["id"])
        if sport_key:
            try:
                odds_comparison = odds_api.get_odds_for_match(sport_key, home_team["name"], away_team["name"])
            except Exception as e:
                print(f"  (Odds lookup failed: {e})")

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
        "insufficient_data": insufficient_data,
        "odds_comparison": odds_comparison,
        "elo_cross_check": elo_cross_check,
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
          f"Draw {m['match_result']['draw']:.0%} | Away {m['match_result']['away_win']:.0%}"
          f"  (Elo blended in)")
    print(f"  Over/Under 2.5: Over {m['over_under']['over_2_5']:.0%} | "
          f"Under {m['over_under']['under_2_5']:.0%}")
    print(f"  BTTS:           Yes {m['btts']['yes']:.0%} | No {m['btts']['no']:.0%}")

    if "cards" in m:
        print(f"  Cards:          Over {m['cards']['over_line']}: {m['cards']['over']:.0%} | "
              f"Under: {m['cards']['under']:.0%}")

    print(f"  Top scoreline:  {m['top_scorelines'][0]['score']} "
          f"({m['top_scorelines'][0]['probability']:.0%})")
    print(f"  Confidence:     {c['emoji']} {c['label']}  "
          f"(pick: {c['top_pick']}, {c['top_probability']:.0%})")

    ec = pred.get("elo_cross_check")
    if ec:
        print(f"  Elo (pre-blend): Home {ec['home']:.0%} | Draw {ec['draw']:.0%} | Away {ec['away']:.0%}")

    if pred.get("odds_comparison"):
        oc = pred["odds_comparison"]
        print(f"  Market odds:    Home {oc.get('implied_home_win', 0):.0%} | "
              f"Draw {oc.get('implied_draw', 0):.0%} | Away {oc.get('implied_away_win', 0):.0%} "
              f"({oc.get('bookmakers_counted', 0)} bookmakers)")

    if s:
        print(f"  >>> SAFEST PICK: {s['label']} ({s['probability']:.0%}) <<<")


def run_daily(date_str, league_id=None, limit=None, fetch_odds=False):
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
            league_avg_goals = get_league_avg_goals(fixture["league"]["id"], fixture["league"]["season"])
            pred = predict_fixture(fixture, league_avg_goals, fetch_odds)

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
                home_team_id=pred["home_team_id"],
                away_team_id=pred["away_team_id"],
                odds_comparison=pred.get("odds_comparison"),
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


def run_backtest_command(league_id, season, sample_size=20):
    if not league_id:
        league_id = config.ALLOWED_LEAGUE_IDS[0]
    if not season:
        season = datetime.now(timezone.utc).year - 1

    print(f"Running backtest: league {league_id}, season {season}, sample size {sample_size}...")
    result = backtest.run_real_backtest(league_id, season, sample_size)

    if result["graded"] == 0:
        print("No matches could be backtested - try a different league or season.")
        return

    print(f"\nBacktest accuracy: {result['accuracy']:.1%} ({result['correct']}/{result['graded']})")
    print("\nSample results:")
    for entry in result["log"][:10]:
        mark = "✓" if entry["correct"] else "✗"
        print(f"  {mark} {entry['match']} | predicted: {entry['predicted']}")


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
    parser.add_argument("--with-odds", action="store_true", help="Also fetch bookmaker odds for comparison")
    parser.add_argument("--backtest", action="store_true", help="Run a real historical backtest")
    parser.add_argument("--season", type=int, help="Season year for backtest, e.g. 2025")
    parser.add_argument("--sample", type=int, default=20, help="Number of matches to sample for backtest")
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
        elif args.backtest:
            league_id = resolve_league_id(args.league, args.league_name)
            run_backtest_command(league_id, args.season, args.sample)
        else:
            date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            league_id = resolve_league_id(args.league, args.league_name)
            run_daily(date_str, league_id, args.limit, args.with_odds)
