"""
Optional odds comparison via The Odds API - kept separate and opt-in per
match, since its free tier (500 requests/month) is tighter than
API-Football's, and you don't need it for every single prediction.
"""

import requests

import config


def get_odds_for_match(sport_key, home_team, away_team, regions="uk,eu"):
    """
    Fetches current bookmaker odds for a match and returns the market's
    implied probabilities for comparison against the model's own numbers.

    sport_key examples: 'soccer_epl', 'soccer_spain_la_liga' - The Odds API
    documents the full list of sport keys.
    """
    url = f"{config.ODDS_API_BASE_URL}/sports/{sport_key}/odds"
    params = {
        "apiKey": config.ODDS_API_KEY,
        "regions": regions,
        "markets": "h2h,totals",
        "oddsFormat": "decimal",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    events = resp.json()

    for event in events:
        if _teams_match(event, home_team, away_team):
            return _summarize_odds(event)

    return None


def _teams_match(event, home_team, away_team):
    return (
        home_team.lower() in event.get("home_team", "").lower()
        and away_team.lower() in event.get("away_team", "").lower()
    )


def _decimal_to_implied_prob(decimal_odds):
    return 1 / decimal_odds


def _summarize_odds(event):
    """Averages odds across bookmakers and converts to implied probabilities."""
    home_odds, draw_odds, away_odds = [], [], []

    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market["key"] != "h2h":
                continue
            for outcome in market["outcomes"]:
                if outcome["name"] == event["home_team"]:
                    home_odds.append(outcome["price"])
                elif outcome["name"] == event["away_team"]:
                    away_odds.append(outcome["price"])
                elif outcome["name"] == "Draw":
                    draw_odds.append(outcome["price"])

    def avg(lst):
        return sum(lst) / len(lst) if lst else None

    avg_home, avg_draw, avg_away = avg(home_odds), avg(draw_odds), avg(away_odds)

    result = {"bookmakers_counted": len(event.get("bookmakers", []))}
    if avg_home:
        result["implied_home_win"] = _decimal_to_implied_prob(avg_home)
    if avg_draw:
        result["implied_draw"] = _decimal_to_implied_prob(avg_draw)
    if avg_away:
        result["implied_away_win"] = _decimal_to_implied_prob(avg_away)

    return result
