"""
Telegram bot brain - checks for new messages, understands what you're
asking for, and either answers instantly or runs a real prediction.

This runs on a GitHub Actions schedule (every few minutes) rather than
instantly, since we're not using an always-on server. A short delay is
the honest trade-off for staying fully free.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

import api_football
import basketball_api
import basketball_model
import config
import main as agent

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

OFFSET_FILE = "telegram_offset.txt"
MEMORY_FILE = "telegram_memory.json"


def send_message(text):
    requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": CHAT_ID, "text": text}, timeout=15)


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


def handle_count_question(text, memory):
    """'how many premier league games today' -> instant count, no prediction."""
    for name, league_id in config.LEAGUE_NAME_TO_ID.items():
        if name in text:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            fixtures = api_football.get_fixtures_by_date(today, league_id)
            send_message(f"{len(fixtures)} {name.title()} match(es) today.")
            return True
    return False


def handle_strong_picks(text, memory):
    """'find me 9 strong football games' -> real predictions, sorted by safest pick."""
    match = re.search(r"(\d+)\s*(strong\s*)?(football|soccer|basketball)?", text)
    if not match:
        return False

    n = int(match.group(1))
    sport = match.group(3)
    if not sport:
        sport = top_preference(memory)
        memory["recent"].append(f"assumed sport: {sport}")
    else:
        memory["preference_counts"][sport if sport != "soccer" else "football"] = \
            memory["preference_counts"].get(sport if sport != "soccer" else "football", 0) + 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    send_message(f"Working on {n} {sport} pick(s) - give me a moment...")

    results = []
    if sport in ("football", "soccer"):
        fixtures = api_football.get_fixtures_by_date(today)
        fixtures = [f for f in fixtures if f["league"]["id"] in config.ALLOWED_LEAGUE_IDS]
        fixtures = [f for f in fixtures if f["fixture"]["status"]["short"] not in agent.FOOTBALL_FINISHED_STATUSES]
        for fixture in fixtures[:25]:
            try:
                pred = agent.predict_fixture(fixture, agent.LEAGUE_AVG_GOALS_FALLBACK)
                if not pred["insufficient_data"] and pred["safest"]:
                    results.append((f"{pred['home_team']} vs {pred['away_team']}", pred["safest"]))
            except Exception:
                continue
    else:
        games = basketball_api.get_games_by_date(today, config.ALLOWED_BASKETBALL_LEAGUE_IDS[0])
        games = [g for g in games if g.get("status", {}).get("short") not in agent.BASKETBALL_FINISHED_STATUSES]
        for game in games[:25]:
            try:
                pred = basketball_model.predict_game(game)
                if pred["safest"]:
                    results.append((f"{pred['home_team']} vs {pred['away_team']}", pred["safest"]))
            except Exception:
                continue

    results.sort(key=lambda r: r[1]["probability"], reverse=True)
    top_n = results[:n]

    if not top_n:
        send_message("No strong picks found - not enough matches with reliable data right now.")
        return True

    lines = [f"Top {len(top_n)} strongest pick(s):\n"]
    for matchup, safest in top_n:
        lines.append(f"{matchup}\n  {safest['label']} ({safest['probability']:.0%})")
    send_message("\n\n".join(lines))
    return True


def handle_message(text, memory):
    text = text.lower().strip()
    memory["recent"].append(text)
    memory["recent"] = memory["recent"][-10:]

    if "how many" in text:
        if handle_count_question(text, memory):
            return

    if re.search(r"\d+", text) and ("game" in text or "match" in text or "pick" in text):
        if handle_strong_picks(text, memory):
            return

    if "accuracy" in text:
        agent.run_accuracy_report()
        send_message("Check the Actions log for your full accuracy report (Telegram summary coming soon).")
        return

    send_message(
        "I can find football or basketball picks - try \"find me 5 strong football games\" "
        "or \"how many premier league games today\"."
    )


if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram credentials not configured, skipping.")
        sys.exit(0)

    memory = load_memory()
    updates = get_updates()

    for update in updates:
        message = update.get("message")
        if not message or not message.get("text"):
            continue
        if str(message["chat"]["id"]) != CHAT_ID:
            continue
        handle_message(message["text"], memory)

    save_memory(memory)
