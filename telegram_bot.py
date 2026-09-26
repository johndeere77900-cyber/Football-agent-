"""
Telegram bot brain - understands what you're asking for and either answers
instantly or runs a real prediction. Can run two ways: process a single
message passed directly (via the Val Town webhook path), or poll for new
messages (the older, now-disabled scheduled approach).
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

import api_football
import basketball_api
import basketball_model
import config
import main as agent
import odds_api
import storage

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

OFFSET_FILE = "telegram_offset.txt"
MEMORY_FILE = "telegram_memory.json"

GREETINGS = {"hi", "hello", "hey", "yo", "sup", "what's up", "whats up", "morning", "evening"}
STRONG_PICK_WORDS = {"strong", "safe", "sure", "best", "good", "solid", "reliable", "confident"}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def send_message(text):
    resp = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
        timeout=15,
    )
    if not resp.ok:
        print(f"Telegram send failed: {resp.status_code} {resp.text}")


def get_updates():
    offset = 0
    if os.path.exists(OFFSET_FILE):
        with open(OFFSET_FILE) as f:
            offset = int(f.read().strip() or 0)

    resp = requests.get(f"{TELEGRAM_API}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=15)
    data = resp.json()
    updates = data.get("result", [])

    if updates:
        new_offset = updates[-1]["update_id"] + 1
        with open(OFFSET_FILE, "w") as f:
            f.write(str(new_offset))

    return updates


def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE) as f:
            return json.load(f)
    return {"preference_counts": {"football": 0, "basketball": 0}, "recent": []}


def save_memory(memory):
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f)


def top_preference(memory):
    counts = memory.get("preference_counts", {})
    return "basketball" if counts.get("basketball", 0) > counts.get("football", 0) else "football"


def mentions_league(text):
    for name, league_id in config.LEAGUE_NAME_TO_ID.items():
        if name in text:
            return name, league_id
    return None, None


def resolve_date(text):
    today = datetime.now(timezone.utc).date()

    iso_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso_match:
        return iso_match.group(0), iso_match.group(0)

    if "tomorrow" in text:
        d = today + timedelta(days=1)
        return d.strftime("%Y-%m-%d"), "tomorrow"

    for i, day_name in enumerate(WEEKDAYS):
        if day_name in text:
            days_ahead = (i - today.weekday()) % 7
            days_ahead = days_ahead or 7
            d = today + timedelta(days=days_ahead)
            return d.strftime("%Y-%m-%d"), day_name.title()

    return today.strftime("%Y-%m-%d"), "today"


def handle_count_question(text, memory):
    name, league_id = mentions_league(text)
    if not league_id:
        return False
    date_str, date_label = resolve_date(text)
    fixtures = api_football.get_fixtures_by_date(date_str, league_id)
    if fixtures:
        send_message(f"📊 There {'is' if len(fixtures)==1 else 'are'} *{len(fixtures)}* {name.title()} "
                     f"match{'es' if len(fixtures)!=1 else ''} {date_label}.")
    else:
        send_message(f"📊 No {name.title()} matches scheduled {date_label}.")
    return True


def handle_live_question(text, memory):
    if "live" not in text:
        return False

    sport = "basketball" if ("basketball" in text or "nba" in text) else "football"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sport_emoji = "🏀" if sport == "basketball" else "⚽"

    if sport == "football":
        fixtures = api_football.get_fixtures_by_date(today)
        fixtures = [f for f in fixtures if f["league"]["id"] in config.ALLOWED_LEAGUE_IDS]
        live = [f for f in fixtures if f["fixture"]["status"]["short"] in agent.FOOTBALL_LIVE_STATUSES]
        if not live:
            send_message(f"{sport_emoji} Nothing live right now in your tracked leagues.")
            return True
        lines = [f"{sport_emoji} *{len(live)} live match(es):*\n"]
        for f in live[:10]:
            home, away = f["teams"]["home"]["name"], f["teams"]["away"]["name"]
            elapsed = f["fixture"]["status"].get("elapsed", "?")
            hg, ag = f["goals"]["home"] or 0, f["goals"]["away"] or 0
            lines.append(f"{home} {hg}-{ag} {away} ({elapsed}')")
        send_message("\n".join(lines))
    else:
        games = basketball_api.get_games_by_date(today, config.ALLOWED_BASKETBALL_LEAGUE_IDS[0])
        live = [g for g in games if g.get("status", {}).get("short") not in
                ("NS", "FT", "AOT", "CANC", "ABD")]
        if not live:
            send_message(f"{sport_emoji} No NBA games live right now.")
            return True
        lines = [f"{sport_emoji} *{len(live)} live game(s):*\n"]
        for g in live[:10]:
            home, away = g["teams"]["home"]["name"], g["teams"]["away"]["name"]
            lines.append(f"{home} vs {away}")
        send_message("\n".join(lines))

    return True


def handle_strong_picks(text, memory, n_default=5):
    number_match = re.search(r"\d+", text)
    n = int(number_match.group()) if number_match else n_default
    fetch_odds = "odds" in text or "bookmaker" in text or "market" in text
    date_str, date_label = resolve_date(text)

    if "basketball" in text or "nba" in text:
        sport = "basketball"
    elif "football" in text or "soccer" in text:
        sport = "football"
    else:
        sport = top_preference(memory)

    memory["preference_counts"][sport] = memory["preference_counts"].get(sport, 0) + 1

    sport_emoji = "🏀" if sport == "basketball" else "⚽"
    send_message(f"{sport_emoji} On it - looking for {n} good {sport} pick(s) for {date_label}, one moment...")

    results = []
    if sport == "football":
        fixtures = api_football.get_fixtures_by_date(date_str)
        fixtures = [f for f in fixtures if f["league"]["id"] in config.ALLOWED_LEAGUE_IDS]
        fixtures = [f for f in fixtures if f["fixture"]["status"]["short"] not in agent.FOOTBALL_FINISHED_STATUSES]
        for fixture in fixtures[:12]:
            try:
                league_avg = agent.get_league_avg_goals(fixture["league"]["id"], fixture["league"]["season"])
                pred = agent.predict_fixture(fixture, league_avg, fetch_odds)
                if not pred["insufficient_data"] and pred["safest"]:
                    results.append(pred)
            except Exception:
                continue
    else:
        games = basketball_api.get_games_by_date(date_str, config.ALLOWED_BASKETBALL_LEAGUE_IDS[0])
        games = [g for g in games if g.get("status", {}).get("short") not in agent.BASKETBALL_FINISHED_STATUSES]
        for game in games[:12]:
            try:
                pred = basketball_model.predict_game(game)
                if pred["safest"]:
                    results.append(pred)
            except Exception:
                continue

    results.sort(key=lambda r: r["safest"]["probability"], reverse=True)
    top_n = results[:n]

    if not top_n:
        send_message(
            f"{sport_emoji} Nothing solid for {date_label} - either there's nothing scheduled, "
            "or the teams playing don't have enough data yet. Try a different day!"
        )
        return

    lines = [f"{sport_emoji} *Here's what looks good for {date_label}:*\n"]
    for pred in top_n:
        safest = pred["safest"]
        emoji = "🟢" if safest["probability"] >= 0.75 else "🟡" if safest["probability"] >= 0.6 else "🔴"
        line = f"*{pred['home_team']} vs {pred['away_team']}* ({pred['league']})\n  {emoji} {safest['label']} ({safest['probability']:.0%})"
        if pred.get("odds_comparison"):
            oc = pred["odds_comparison"]
            line += f"\n  Market: Home {oc.get('implied_home_win', 0):.0%} | Away {oc.get('implied_away_win', 0):.0%}"
        lines.append(line)
    send_message("\n\n".join(lines))


def handle_accuracy_question(text, memory):
    if "accuracy" not in text and "track record" not in text and "how good" not in text:
        return False

    sport = "basketball" if ("basketball" in text or "nba" in text) else "football"
    storage.init_db()
    storage.init_basketball_db()
    summary = storage.basketball_accuracy_summary() if sport == "basketball" else storage.accuracy_summary()

    if summary["total_graded"] == 0:
        send_message(f"📈 No graded {sport} predictions yet - check back after some matches finish.")
        return True

    lines = [f"📈 *{sport.title()} track record:*\n",
             f"Overall: {summary['overall_accuracy']:.0%} ({summary['total_graded']} graded)\n"]
    for label, stats in summary.get("by_confidence", {}).items():
        emoji = {"High": "🟢", "Moderate": "🟡", "Toss-up": "🔴"}.get(label, "")
        lines.append(f"{emoji} {label}: {stats['accuracy']:.0%} ({stats['count']})")
    send_message("\n".join(lines))
    return True


def handle_start(memory):
    send_message(
        "👋 Hey, good to see you! I'm your football & basketball buddy.\n\n"
        "Ask me stuff like:\n"
        "• \"what's good today\" or \"find me some strong picks\"\n"
        "• \"any solid basketball games tomorrow\"\n"
        "• \"how many premier league games this saturday\"\n"
        "• \"any live football games\"\n"
        "• \"what's my accuracy so far\"\n\n"
        "I check in every few minutes, so I might take a moment to reply."
    )


def handle_message(text, memory):
    text = text.lower().strip()
    memory["recent"].append(text)
    memory["recent"] = memory["recent"][-10:]

    if text in ("/start", "/help", "help") or text in GREETINGS:
        handle_start(memory)
        return

    if handle_accuracy_question(text, memory):
        return

    if handle_live_question(text, memory):
        return

    if "how many" in text or ("how much" in text and "game" in text):
        if handle_count_question(text, memory):
            return

    has_number = bool(re.search(r"\d+", text))
    mentions_games = any(w in text for w in ["game", "match", "pick", "fixture"])
    mentions_strong = any(w in text for w in STRONG_PICK_WORDS)

    if mentions_games or (mentions_strong and (has_number or "today" in text or "tonight" in text
                                                or "tomorrow" in text or any(d in text for d in WEEKDAYS))):
        handle_strong_picks(text, memory)
        return

    send_message(
        "Hmm, not sure I caught that one 🤔 Try something like \"what's good tomorrow\" "
        "or \"any live games right now\"!"
    )


if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram credentials not configured, skipping.")
        sys.exit(0)

    memory = load_memory()

    single_message = os.environ.get("TELEGRAM_MESSAGE")
    if single_message:
        print(f"Processing single message: {single_message}")
        handle_message(single_message, memory)
        save_memory(memory)
        sys.exit(0)

    updates = get_updates()
    for update in updates:
        message = update.get("message")
        if not message or not message.get("text"):
            continue
        if str(message["chat"]["id"]) != CHAT_ID:
            continue
        handle_message(message["text"], memory)

    save_memory(memory)
