"""
Telegram interface for the football/basketball research agent.

Architecture:
    Telegram
        -> Val Town webhook
        -> GitHub Actions workflow_dispatch
        -> this file (one message)
        -> Telegram response

IMPORTANT:
- No Telegram polling.
- No getUpdates.
- No background Telegram process.
- No Telegram phone/session dependency.
- One Telegram message is processed per GitHub Actions run.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests

import api_football
import basketball_api
import basketball_model
import config
import main as agent
import storage


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("TELEGRAM_TOKEN")
)

CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TELEGRAM_API = (
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    if TELEGRAM_TOKEN
    else ""
)

MEMORY_FILE = "telegram_memory.json"

WAIT_TEXT = "Ready. What would you like me to analyze?"


# ---------------------------------------------------------------------------
# Natural-language routing
# ---------------------------------------------------------------------------

GREETINGS = {
    "hi",
    "hello",
    "hey",
    "yo",
    "sup",
    "morning",
    "evening",
    "good morning",
    "good evening",
}

WEEKDAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
}

COMMAND_VERBS = {
    "analyze",
    "analyse",
    "backtest",
    "check",
    "compare",
    "find",
    "get",
    "give",
    "list",
    "research",
    "run",
    "show",
    "predict",
    "scan",
}

QUESTION_MARKERS = {
    "how",
    "what",
    "when",
    "where",
    "who",
    "which",
    "why",
    "is",
    "are",
    "can",
    "does",
    "do",
    "will",
    "has",
    "have",
}

SUPPORTED_COMMAND_TERMS = {
    "research",
    "analyze",
    "analyse",
    "predict",
    "find",
    "show",
    "list",
    "give",
    "get",
    "check",
    "compare",
    "scan",
    "pick",
    "picks",
    "game",
    "games",
    "match",
    "matches",
    "fixture",
    "fixtures",
    "football",
    "soccer",
    "basketball",
    "nba",
}


# ---------------------------------------------------------------------------
# Telegram transport
# ---------------------------------------------------------------------------

def send_message(text):
    """Send one response to the configured Telegram chat."""

    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Telegram credentials are not configured."
        )

    response = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": str(text),
            "parse_mode": "Markdown",
        },
        timeout=15,
    )

    try:
        data = response.json()
    except ValueError:
        data = {}

    if not response.ok or not data.get("ok"):
        raise RuntimeError(
            "Telegram sendMessage failed: "
            f"{response.status_code} {response.text[:400]}"
        )

    return data


# ---------------------------------------------------------------------------
# Persistent Telegram memory
# ---------------------------------------------------------------------------

def load_memory():
    """Load lightweight conversation state from the repository."""

    default = {
        "recent": [],
        "preference_counts": {
            "football": 0,
            "basketball": 0,
        },
    }

    if not os.path.exists(MEMORY_FILE):
        return default

    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as handle:
            memory = json.load(handle)
    except (OSError, ValueError, TypeError):
        return default

    if not isinstance(memory, dict):
        return default

    if not isinstance(memory.get("recent"), list):
        memory["recent"] = []

    if not isinstance(memory.get("preference_counts"), dict):
        memory["preference_counts"] = {
            "football": 0,
            "basketball": 0,
        }

    return memory


def save_memory(memory):
    """Persist Telegram state after processing the message."""

    with open(MEMORY_FILE, "w", encoding="utf-8") as handle:
        json.dump(
            memory,
            handle,
            ensure_ascii=False,
            indent=2,
        )


# ---------------------------------------------------------------------------
# Text parsing
# ---------------------------------------------------------------------------

def normalize_text(text):
    return re.sub(
        r"\s+",
        " ",
        str(text).strip().lower(),
    )


def parse_quantity(text, default=5):
    """Extract a numeric quantity from natural language."""

    match = re.search(
        r"\b(\d{1,3})\b",
        text,
    )

    if match:
        value = int(match.group(1))
        return value if value > 0 else default

    for token in re.findall(r"[a-z]+", text):
        if token in NUMBER_WORDS:
            return NUMBER_WORDS[token]

    return default


def mentions_league(text):
    normalized = normalize_text(text)

    for name, league_id in sorted(
        config.LEAGUE_NAME_TO_ID.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if name in normalized:
            return name, league_id

    return None, None


def detect_sport(text):
    normalized = normalize_text(text)

    if (
        "basketball" in normalized
        or re.search(r"\bnba\b", normalized)
    ):
        return "basketball"

    if (
        "football" in normalized
        or "soccer" in normalized
    ):
        return "football"

    return None


def resolve_date(text):
    """
    Resolve explicit dates and common natural-language dates.

    All dates are interpreted in UTC because the GitHub worker runs remotely.
    """

    normalized = normalize_text(text)

    today = datetime.now(timezone.utc).date()

    iso_match = re.search(
        r"\b(\d{4})-(\d{2})-(\d{2})\b",
        normalized,
    )

    if iso_match:
        value = iso_match.group(0)

        try:
            datetime.strptime(
                value,
                "%Y-%m-%d",
            )
        except ValueError as exc:
            raise ValueError(
                "The date in the message is invalid."
            ) from exc

        return value, value

    if "tomorrow" in normalized:
        return (
            (today + timedelta(days=1)).isoformat(),
            "tomorrow",
        )

    if (
        "today" in normalized
        or "tonight" in normalized
    ):
        return today.isoformat(), "today"

    for index, day_name in enumerate(WEEKDAYS):
        if re.search(
            rf"\b{day_name}\b",
            normalized,
        ):
            days_ahead = (
                index - today.weekday()
            ) % 7 or 7

            return (
                (today + timedelta(days=days_ahead)).isoformat(),
                day_name.title(),
            )

    return today.isoformat(), "today"


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

def classify_intent(text):
    normalized = normalize_text(text)

    if not normalized:
        return "UNKNOWN"

    if normalized in GREETINGS:
        return "GREETING"

    if normalized in {
        "/start",
        "/help",
        "help",
    }:
        return "GREETING"

    first_word = normalized.split(" ", 1)[0]

    question_phrases = (
        "how many",
        "how much",
        "what time",
        "when is",
        "when are",
        "where is",
        "who is",
        "which team",
        "is there",
        "are there",
        "is ",
        "are ",
        "does ",
        "do ",
        "can you tell",
        "tell me how many",
    )

    if "?" in normalized:
        return "QUESTION"

    if first_word in QUESTION_MARKERS:
        return "QUESTION"

    if any(
        phrase in normalized
        for phrase in question_phrases
    ):
        return "QUESTION"

    if first_word in COMMAND_VERBS:
        return "COMMAND"

    starters = (
        "i want you to",
        "i need you to",
        "please",
        "go and",
        "go ahead and",
        "run ",
        "look for",
        "find me",
        "give me",
        "show me",
        "get me",
        "check ",
        "research ",
        "analyze ",
        "analyse ",
        "compare ",
    )

    if any(
        normalized.startswith(prefix)
        for prefix in starters
    ):
        return "COMMAND"

    has_command_verb = any(
        re.search(
            rf"\b{re.escape(verb)}\b",
            normalized,
        )
        for verb in COMMAND_VERBS
    )

    has_subject = any(
        re.search(
            rf"\b{re.escape(term)}\b",
            normalized,
        )
        for term in SUPPORTED_COMMAND_TERMS
    )

    if has_command_verb and has_subject:
        return "COMMAND"

    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Football fixture helpers
# ---------------------------------------------------------------------------

def format_fixture_time(fixture):
    raw = fixture.get(
        "fixture",
        {},
    ).get("date")

    if not raw:
        return "time unavailable"

    try:
        parsed = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )

        return parsed.astimezone(
            timezone.utc
        ).strftime("%H:%M UTC")

    except (TypeError, ValueError):
        return "time unavailable"


def fixture_teams(fixture):
    teams = fixture.get(
        "teams",
        {},
    )

    home = teams.get(
        "home",
        {},
    ) or {}

    away = teams.get(
        "away",
        {},
    ) or {}

    return (
        str(home.get("name") or ""),
        str(away.get("name") or ""),
    )


def fixture_matches_team(
    fixture,
    team_query,
):
    query = normalize_text(team_query)

    home, away = fixture_teams(fixture)

    return (
        query in normalize_text(home)
        or query in normalize_text(away)
    )


def get_tracked_fixtures_for_date(date_str):
    fixtures = api_football.get_fixtures_by_date(
        date_str
    )

    if not isinstance(fixtures, list):
        return []

    return [
        fixture
        for fixture in fixtures
        if (
            isinstance(fixture, dict)
            and fixture.get("league", {}).get("id")
            in config.ALLOWED_LEAGUE_IDS
        )
    ]


# ---------------------------------------------------------------------------
# Factual questions
# ---------------------------------------------------------------------------

def handle_count_question(text):
    league_name, league_id = mentions_league(text)

    date_str, date_label = resolve_date(text)

    fixtures = get_tracked_fixtures_for_date(
        date_str
    )

    if league_id:
        fixtures = [
            fixture
            for fixture in fixtures
            if fixture.get("league", {}).get("id")
            == league_id
        ]

        label = league_name.title()
    else:
        label = "tracked"

    count = len(fixtures)

    if count == 0:
        send_message(
            f"📊 There are no {label} matches "
            f"scheduled for {date_label}."
        )
        return True

    noun = (
        "match"
        if count == 1
        else "matches"
    )

    send_message(
        f"📊 There are *{count}* "
        f"{label} {noun} scheduled "
        f"for {date_label}."
    )

    return True


def handle_schedule_question(text):
    normalized = normalize_text(text)

    if not any(
        phrase in normalized
        for phrase in (
            "what time",
            "when is",
            "when are",
            "playing",
            "kickoff",
            "kick-off",
        )
    ):
        return False

    stop_words = {
        "what",
        "time",
        "is",
        "are",
        "when",
        "will",
        "the",
        "a",
        "an",
        "playing",
        "play",
        "kickoff",
        "kick-off",
        "today",
        "tomorrow",
        "please",
        "me",
        "tell",
        "match",
        "game",
        "on",
        "at",
    }

    words = re.findall(
        r"[a-z0-9'-]+",
        normalized,
    )

    team_query = " ".join(
        word
        for word in words
        if word not in stop_words
    )

    if not team_query:
        return False

    date_str, date_label = resolve_date(text)

    dates_to_check = [date_str]

    if date_label == "today":
        dates_to_check.extend(
            (
                datetime.fromisoformat(date_str).date()
                + timedelta(days=i)
            ).isoformat()
            for i in range(1, 7)
        )

    found = []

    for candidate_date in dates_to_check:
        fixtures = get_tracked_fixtures_for_date(
            candidate_date
        )

        found.extend(
            fixture
            for fixture in fixtures
            if fixture_matches_team(
                fixture,
                team_query,
            )
        )

        if found and date_label == "today":
            break

    if not found:
        send_message(
            f"📅 I couldn't find a scheduled "
            f"tracked match for *{team_query}* "
            f"starting {date_label}."
        )
        return True

    lines = [
        f"📅 Matches for *{team_query}*:"
    ]

    for fixture in found[:5]:
        home, away = fixture_teams(
            fixture
        )

        league = fixture.get(
            "league",
            {},
        ).get(
            "name",
            "Unknown league",
        )

        fixture_date = fixture.get(
            "fixture",
            {},
        ).get(
            "date",
            "",
        )

        date_text = (
            fixture_date[:10]
            if fixture_date
            else "date unavailable"
        )

        lines.append(
            f"• {home} vs {away} — "
            f"{date_text}, "
            f"{format_fixture_time(fixture)} "
            f"({league})"
        )

    send_message(
        "\n".join(lines)
    )

    return True


def handle_live_question(text):
    normalized = normalize_text(text)

    if "live" not in normalized:
        return False

    sport = detect_sport(text) or "football"

    if sport == "football":
        today = datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d")

        fixtures = get_tracked_fixtures_for_date(
            today
        )

        live_statuses = set(
            agent.FOOTBALL_LIVE_STATUSES
        )

        live = [
            fixture
            for fixture in fixtures
            if fixture.get(
                "fixture",
                {},
            ).get(
                "status",
                {},
            ).get("short")
            in live_statuses
        ]

        if not live:
            send_message(
                "⚽ Nothing is live right now "
                "in the tracked football leagues."
            )
            return True

        lines = [
            f"⚽ *{len(live)} live "
            "football match(es):*"
        ]

        for fixture in live:
            home, away = fixture_teams(
                fixture
            )

            status = fixture.get(
                "fixture",
                {},
            ).get(
                "status",
                {},
            )

            elapsed = status.get(
                "elapsed",
                "?",
            )

            goals = fixture.get(
                "goals",
                {},
            )

            home_goals = (
                0
                if goals.get("home") is None
                else goals.get("home")
            )

            away_goals = (
                0
                if goals.get("away") is None
                else goals.get("away")
            )

            lines.append(
                f"• {home} "
                f"{home_goals}-{away_goals} "
                f"{away} ({elapsed}')"
            )

        send_message(
            "\n".join(lines)
        )

        return True

    today = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    league_ids = getattr(
        config,
        "ALLOWED_BASKETBALL_LEAGUE_IDS",
        [],
    )

    if not league_ids:
        send_message(
            "🏀 No tracked basketball league "
            "is configured."
        )
        return True

    league_id = league_ids[0]

    games = basketball_api.get_games_by_date(
        today,
        league_id,
    )

    finished_statuses = {
        "NS",
        "FT",
        "AOT",
        "CANC",
        "ABD",
    }

    live = [
        game
        for game in games
        if game.get(
            "status",
            {},
        ).get("short")
        not in finished_statuses
    ]

    if not live:
        send_message(
            "🏀 No tracked basketball games "
            "are live right now."
        )
        return True

    lines = [
        f"🏀 *{len(live)} live "
        "basketball game(s):*"
    ]

    for game in live:
        home = game.get(
            "teams",
            {},
        ).get(
            "home",
            {},
        ).get(
            "name",
            "Home",
        )

        away = game.get(
            "teams",
            {},
        ).get(
            "away",
            {},
        ).get(
            "name",
            "Away",
        )

        status = game.get(
            "status",
            {},
        ).get(
            "short",
            "LIVE",
        )

        lines.append(
            f"• {home} vs {away} ({status})"
        )

    send_message(
        "\n".join(lines)
    )

    return True


def handle_accuracy_question(text):
    normalized = normalize_text(text)

    if not any(
        term in normalized
        for term in (
            "accuracy",
            "track record",
            "graded predictions",
        )
    ):
        return False

    sport = detect_sport(text) or "football"

    storage.init_db()
    storage.init_basketball_db()

    if sport == "basketball":
        summary = storage.basketball_accuracy_summary()
    else:
        summary = storage.accuracy_summary()

    total = summary.get(
        "total_graded",
        0,
    )

    if total == 0:
        send_message(
            f"📈 There are no graded "
            f"{sport} predictions yet."
        )
        return True

    lines = [
        f"📈 *{sport.title()} prediction record:*",
        (
            f"Overall: "
            f"{summary['overall_accuracy']:.0%} "
            f"({total} graded)"
        ),
    ]

    for label, stats in summary.get(
        "by_confidence",
        {},
    ).items():
        lines.append(
            f"• {label}: "
            f"{stats['accuracy']:.0%} "
            f"({stats['count']})"
        )

    send_message(
        "\n".join(lines)
    )

    return True


# ---------------------------------------------------------------------------
# Research engine
# ---------------------------------------------------------------------------

def research_football(
    date_str,
    quantity,
    fetch_odds,
):
    fixtures = get_tracked_fixtures_for_date(
        date_str
    )

    finished = set(
        agent.FOOTBALL_FINISHED_STATUSES
    )

    fixtures = [
        fixture
        for fixture in fixtures
        if fixture.get(
            "fixture",
            {},
        ).get(
            "status",
            {},
        ).get("short")
        not in finished
    ]

    results = []

    for fixture in fixtures:
        try:
            league_id = fixture["league"]["id"]
            season = fixture["league"]["season"]

            league_avg = agent.get_league_avg_goals(
                league_id,
                season,
            )

            prediction = agent.predict_fixture(
                fixture,
                league_avg,
                fetch_odds,
            )

        except (
            KeyError,
            TypeError,
            ValueError,
            requests.RequestException,
        ) as exc:
            print(
                "Skipping football fixture due "
                f"to expected data/API issue: {exc}",
                flush=True,
            )
            continue

        if not isinstance(
            prediction,
            dict,
        ):
            continue

        safest = prediction.get(
            "safest"
        )

        probability = (
            safest.get("probability")
            if isinstance(safest, dict)
            else None
        )

        if (
            prediction.get("insufficient_data")
            or not safest
        ):
            continue

        if not isinstance(
            probability,
            (int, float),
        ):
            continue

        results.append(prediction)

    results.sort(
        key=lambda item: item["safest"]["probability"],
        reverse=True,
    )

    return results[:quantity]


def research_basketball(
    date_str,
    quantity,
):
    league_ids = getattr(
        config,
        "ALLOWED_BASKETBALL_LEAGUE_IDS",
        [],
    )

    if not league_ids:
        return []

    league_id = league_ids[0]

    games = basketball_api.get_games_by_date(
        date_str,
        league_id,
    )

    finished = set(
        getattr(
            agent,
            "BASKETBALL_FINISHED_STATUSES",
            {
                "FT",
                "AOT",
                "CANC",
                "ABD",
            },
        )
    )

    games = [
        game
        for game in games
        if game.get(
            "status",
            {},
        ).get("short")
        not in finished
    ]

    results = []

    for game in games:
        try:
            prediction = basketball_model.predict_game(
                game
            )

        except (
            KeyError,
            TypeError,
            ValueError,
            requests.RequestException,
        ) as exc:
            print(
                "Skipping basketball game due "
                f"to expected data/API issue: {exc}",
                flush=True,
            )
            continue

        if not isinstance(
            prediction,
            dict,
        ):
            continue

        safest = prediction.get(
            "safest"
        )

        probability = (
            safest.get("probability")
            if isinstance(safest, dict)
            else None
        )

        if (
            prediction.get("insufficient_data")
            or not safest
        ):
            continue

        if not isinstance(
            probability,
            (int, float),
        ):
            continue

        results.append(prediction)

    results.sort(
        key=lambda item: item["safest"]["probability"],
        reverse=True,
    )

    return results[:quantity]


def handle_research_command(text):
    normalized = normalize_text(text)

    research_terms = (
        "research",
        "analyze",
        "analyse",
        "predict",
        "find",
        "show",
        "list",
        "give",
        "get",
        "scan",
        "pick",
        "picks",
    )

    if not any(
        term in normalized
        for term in research_terms
    ):
        return False

    sport = detect_sport(text)

    if sport is None:
        send_message(
            "I understand the command, but please "
            "specify football or basketball."
        )
        return True

    quantity = parse_quantity(
        text,
        default=5,
    )

    if quantity > 100:
        send_message(
            "I can research up to 100 matches "
            "in one command."
        )
        return True

    date_str, date_label = resolve_date(
        text
    )

    fetch_odds = any(
        term in normalized
        for term in (
            "odds",
            "bookmaker",
            "market price",
            "price",
        )
    )

    send_message(
        f"🔎 Command received: researching "
        f"{quantity} {sport} match(es) "
        f"for {date_label}."
    )

    if sport == "football":
        results = research_football(
            date_str,
            quantity,
            fetch_odds,
        )
    else:
        results = research_basketball(
            date_str,
            quantity,
        )

    if not results:
        send_message(
            f"🔎 No {sport} predictions "
            f"could be produced for {date_label} "
            "from the available data."
        )
        return True

    emoji = (
        "⚽"
        if sport == "football"
        else "🏀"
    )

    lines = [
        f"{emoji} *Research results — {date_label}*",
        (
            f"Requested: {quantity} | "
            f"Produced: {len(results)}"
        ),
    ]

    for prediction in results:
        safest = prediction["safest"]

        home_team = prediction.get(
            "home_team",
            "Home",
        )

        away_team = prediction.get(
            "away_team",
            "Away",
        )

        label = safest.get(
            "label",
            "Selected market",
        )

        probability = safest.get(
            "probability"
        )

        lines.append(
            f"\n*{home_team} vs {away_team}*\n"
            f"• {label}: "
            f"{probability:.0%}"
        )

        if prediction.get("league"):
            lines.append(
                f"• League: "
                f"{prediction['league']}"
            )

    send_message(
        "\n".join(lines)
    )

    return True


# ---------------------------------------------------------------------------
# Question routing
# ---------------------------------------------------------------------------

def handle_question(text):
    if handle_accuracy_question(text):
        return True

    if handle_schedule_question(text):
        return True

    normalized = normalize_text(text)

    if (
        "how many" in normalized
        or "how many games" in normalized
        or "how many matches" in normalized
    ):
        return handle_count_question(text)

    if handle_live_question(text):
        return True

    return False


# ---------------------------------------------------------------------------
# Greeting/help routing
# ---------------------------------------------------------------------------

def handle_greeting(text):
    normalized = normalize_text(text)

    if normalized in {
        "/start",
        "/help",
        "help",
    }:
        send_message(
            "👋 I understand natural-language "
            "questions and commands.\n\n"
            "*Questions:* "
            "\"How many Premier League games "
            "are today?\" or "
            "\"What time is Arsenal playing?\"\n\n"
            "*Commands:* "
            "\"Research 10 football games today\" "
            "or "
            "\"Analyze tomorrow's NBA games.\"\n\n"
            f"Unsupported messages stay in WAIT STATE: "
            f"*{WAIT_TEXT}*"
        )

        return True

    if normalized in GREETINGS:
        send_message(WAIT_TEXT)
        return True

    return False


# ---------------------------------------------------------------------------
# Main message dispatcher
# ---------------------------------------------------------------------------

def handle_message(
    text,
    memory,
):
    """
    Process exactly one Telegram message.

    This function does not poll Telegram.
    """

    original = str(text or "").strip()

    if not original:
        send_message(WAIT_TEXT)
        return

    recent = memory.setdefault(
        "recent",
        [],
    )

    recent.append(original)

    memory["recent"] = recent[-20:]

    intent = classify_intent(
        original
    )

    print(
        f"Intent={intent} "
        f"message={original!r}",
        flush=True,
    )

    if intent == "GREETING":
        handle_greeting(original)
        return

    if intent == "QUESTION":
        if handle_question(original):
            return

        send_message(
            "I understand that as a question, "
            "but I don't have a supported "
            "data handler for it yet."
        )
        return

    if intent == "COMMAND":
        if handle_research_command(original):
            return

        send_message(
            "I understand that as a command, "
            "but it is outside the current "
            "football/basketball research "
            "capabilities."
        )
        return

    send_message(WAIT_TEXT)


# ---------------------------------------------------------------------------
# One-shot GitHub Actions entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    # Credentials are required for the worker to respond.
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Telegram credentials are not configured."
        )

    memory = load_memory()

    # IMPORTANT:
    # Val Town passes exactly one Telegram message
    # through GitHub Actions as TELEGRAM_MESSAGE.
    single_message = os.environ.get(
        "TELEGRAM_MESSAGE"
    )

    # Deliberately reject execution without a message.
    # This prevents the old polling architecture
    # from silently returning.
    if not single_message:
        raise RuntimeError(
            "Polling mode is disabled. "
            "Telegram messages must arrive "
            "through Val Town."
        )

    print(
        "Processing single Telegram message: "
        f"{single_message}",
        flush=True,
    )

    try:
        handle_message(
            single_message,
            memory,
        )
    finally:
        save_memory(memory)
