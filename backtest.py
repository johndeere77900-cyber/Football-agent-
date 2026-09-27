"""
Backtesting and prediction-building helpers.
"""

import api_football
import confidence
import config
import poisson_model


def estimate_expected_goals_from_stats(team_stats_for, team_stats_against, league_avg_goals):
    if not isinstance(team_stats_for, dict):
        team_stats_for = {}
    if not isinstance(team_stats_against, dict):
        team_stats_against = {}

    goals_for_avg = team_stats_for.get("goals", {}).get("for", {}).get("average", {}).get("total")
    goals_against_avg = team_stats_against.get("goals", {}).get("against", {}).get("average", {}).get("total")

    try:
        goals_for_avg = float(goals_for_avg) if goals_for_avg not in (None, "") else league_avg_goals
    except (TypeError, ValueError):
        goals_for_avg = league_avg_goals

    try:
        goals_against_avg = float(goals_against_avg) if goals_against_avg not in (None, "") else league_avg_goals
    except (TypeError, ValueError):
        goals_against_avg = league_avg_goals

    return goals_for_avg / league_avg_goals, goals_against_avg / league_avg_goals


def estimate_recent_form_goals(team_id, league_avg_goals, last=None):
    last = last or config.RECENT_FORM_MATCHES
    matches = api_football.get_recent_form(team_id, last=last)

    goals_for, goals_against = [], []
    for m in matches:
        home_id = m["teams"]["home"]["id"]
        away_id = m["teams"]["away"]["id"]
        hg, ag = m["goals"]["home"], m["goals"]["away"]
        if hg is None or ag is None:
            continue
        if home_id == team_id:
            goals_for.append(hg)
            goals_against.append(ag)
        elif away_id == team_id:
            goals_for.append(ag)
            goals_against.append(hg)

    if not goals_for:
        return 1.0, 1.0

    avg_for = sum(goals_for) / len(goals_for)
    avg_against = sum(goals_against) / len(goals_for)
    return avg_for / league_avg_goals, avg_against / league_avg_goals


def estimate_head_to_head_goals(home_id, away_id, league_avg_goals, last=None):
    """
    How these two SPECIFIC teams have historically scored against each
    other - some teams just struggle against particular opponents
    regardless of general form. Small sample size, so this gets a modest
    weight in the final blend. Falls back to neutral if they've never
    met, or met too rarely to be meaningful.
    """
    last = last or config.HEAD_TO_HEAD_MATCHES
    matches = api_football.get_head_to_head(home_id, away_id, last=last)

    home_goals_for, away_goals_for = [], []
    for m in matches:
        m_home_id = m["teams"]["home"]["id"]
        m_away_id = m["teams"]["away"]["id"]
        hg, ag = m["goals"]["home"], m["goals"]["away"]
        if hg is None or ag is None:
            continue
        if m_home_id == home_id:
            home_goals_for.append(hg)
            away_goals_for.append(ag)
        elif m_away_id == home_id:
            home_goals_for.append(ag)
            away_goals_for.append(hg)

    if not home_goals_for:
        return 1.0, 1.0, 1.0, 1.0

    home_avg = sum(home_goals_for) / len(home_goals_for)
    away_avg = sum(away_goals_for) / len(away_goals_for)
    return (home_avg / league_avg_goals, 1.0, away_avg / league_avg_goals, 1.0)


def blend_three(season_ratio, recent_ratio, h2h_ratio):
    """Weighted blend of season, recent-form, and head-to-head signals."""
    return (
        season_ratio * config.SEASON_WEIGHT
        + recent_ratio * config.RECENT_FORM_WEIGHT
        + h2h_ratio * config.HEAD_TO_HEAD_WEIGHT
    )


def estimate_avg_cards(team_stats, league_avg_cards=3.8):
    if not isinstance(team_stats, dict):
        return league_avg_cards
    try:
        yellow = team_stats.get("cards", {}).get("yellow", {})
        total_yellow = sum(v.get("total") or 0 for v in yellow.values() if isinstance(v, dict))
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
    total_prob = sum(markets["match_result"].values())
    assert 0.99 <= total_prob <= 1.01, f"Probabilities don't sum to 1: {total_prob}"
    assert markets["match_result"]["home_win"] > markets["match_result"]["away_win"]
    print("Self-test passed: math checks out.")
    return markets, conf


def _compute_stats_as_of(all_fixtures, team_id, cutoff_date_str):
    goals_for, goals_against = [], []
    for f in all_fixtures:
        if f["fixture"]["date"][:10] >= cutoff_date_str:
            continue
        if f["fixture"]["status"]["short"] != "FT":
            continue
        home_id = f["teams"]["home"]["id"]
        away_id = f["teams"]["away"]["id"]
        hg, ag = f["goals"]["home"], f["goals"]["away"]
        if hg is None or ag is None:
            continue
        if home_id == team_id:
            goals_for.append(hg)
            goals_against.append(ag)
        elif away_id == team_id:
            goals_for.append(ag)
            goals_against.append(hg)

    if not goals_for:
        return None, None
    return sum(goals_for) / len(goals_for), sum(goals_against) / len(goals_for)


def run_real_backtest(league_id, season, sample_size=20, min_prior_matches=5):
    all_fixtures = api_football.get_league_fixtures(league_id, season)
    print(f"DEBUG: fetched {len(all_fixtures)} total fixtures for league {league_id}, season {season}")

    finished = sorted(
        [f for f in all_fixtures if f["fixture"]["status"]["short"] == "FT"],
        key=lambda f: f["fixture"]["date"]
    )
    print(f"DEBUG: {len(finished)} of those are finished (FT)")

    candidates = finished[min_prior_matches * 2:]
    print(f"DEBUG: {len(candidates)} candidates after skipping early-season matches")

    if len(candidates) > sample_size:
        step = max(len(candidates) // sample_size, 1)
        candidates = candidates[::step][:sample_size]
    print(f"DEBUG: {len(candidates)} candidates selected for backtest")

    league_avg_goals = config.LEAGUE_AVG_GOALS.get(league_id, config.LEAGUE_AVG_GOALS_FALLBACK)

    correct = 0
    graded = 0
    log = []

    for match in candidates:
        cutoff = match["fixture"]["date"][:10]
        home_id = match["teams"]["home"]["id"]
        away_id = match["teams"]["away"]["id"]

        home_for, home_against = _compute_stats_as_of(all_fixtures, home_id, cutoff)
        away_for, away_against = _compute_stats_as_of(all_fixtures, away_id, cutoff)
        if home_for is None or away_for is None:
            continue

        home_attack, home_defense = home_for / league_avg_goals, home_against / league_avg_goals
        away_attack, away_defense = away_for / league_avg_goals, away_against / league_avg_goals

        home_xg = poisson_model.expected_goals(home_attack, away_defense, league_avg_goals, is_home=True)
        away_xg = poisson_model.expected_goals(away_attack, home_defense, league_avg_goals, is_home=False)
        markets = poisson_model.market_probabilities(home_xg, away_xg)
        predicted = max(markets["match_result"], key=markets["match_result"].get)

        ah, aw = match["goals"]["home"], match["goals"]["away"]
        actual = "home_win" if ah > aw else "away_win" if ah < aw else "draw"

        graded += 1
        is_correct = predicted == actual
        correct += int(is_correct)

        log.append({
            "match": f"{match['teams']['home']['name']} {ah}-{aw} {match['teams']['away']['name']}",
            "predicted": predicted, "actual": actual, "correct": is_correct,
        })

    return {
        "graded": graded,
        "correct": correct,
        "accuracy": correct / graded if graded else 0,
        "log": log,
    }


if __name__ == "__main__":
    run_synthetic_selftest()
