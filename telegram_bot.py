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

PREDICTION_TERMS = {
    "prediction",
    "predictions",
    "forecast",
    "forecasts",
    "tip",
    "tips",
    "pick",
    "picks",
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

    has_prediction_term = any(
        re.search(
            rf"\b{re.escape(term)}\b",
            normalized,
        )
        for term in PREDICTION_TERMS
    )

    has_sport = detect_sport(normalized) is not None

    has_prediction_context = (
        has_prediction_term
        and (
            has_sport
            or any(
                word in normalized
                for word in (
                    "game",
                    "games",
                    "match",
                    "matches",
                    "fixture",
                    "fixtures",
                    "today",
                    "tomorrow",
                    "tonight",
                )
            )
        )
    )

    if (
        (has_command_verb and has_subject)
        or has_prediction_context
    ):
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
    # ---------------------------------------------------------------------------
# Live and accuracy questions
# ---------------------------------------------------------------------------

def handle_live_question(text):
    normalized = normalize_text(text)

    wants_live = any(
        phrase in normalized
        for phrase in (
            "live",
            "live now",
            "playing now",
            "playing live",
            "currently playing",
            "what is happening now",
            "whats happening now",
        )
    )

    if not wants_live:
        return None

    football_live = []
    basketball_live = []

    try:
        football_live = api_football.get_live_fixtures() or []
    except Exception as exc:
        print(f"Live football lookup failed: {exc}")

    try:
        basketball_live = basketball_api.get_live_games() or []
    except Exception as exc:
        print(f"Live basketball lookup failed: {exc}")

    if not football_live and not basketball_live:
        return (
            "🔴 There are no live tracked football or basketball games "
            "available right now."
        )

    lines = ["🔴 Live games right now:"]

    for fixture in football_live:
        home, away = fixture_teams(fixture)
        if home and away:
            lines.append(f"⚽ {home} vs {away}")

    for game in basketball_live:
        home = (
            game.get("home_team")
            or game.get("homeTeam")
            or game.get("home")
            or "Home"
        )
        away = (
            game.get("away_team")
            or game.get("awayTeam")
            or game.get("away")
            or "Away"
        )
        lines.append(f"🏀 {home} vs {away}")

    return "\n".join(lines)


def handle_accuracy_question(text):
    normalized = normalize_text(text)

    accuracy_terms = (
        "accuracy",
        "accurate",
        "hit rate",
        "win rate",
        "success rate",
        "how well",
        "how good",
        "performance",
    )

    if not any(term in normalized for term in accuracy_terms):
        return None

    try:
        summary = storage.get_prediction_summary()
    except Exception as exc:
        print(f"Accuracy summary failed: {exc}")
        return (
            "📊 I could not read the prediction history right now."
        )

    if not summary:
        return (
            "📊 There are no graded predictions in the history yet, "
            "so there is no measured accuracy to report."
        )

    total = summary.get("total", 0)
    graded = summary.get("graded", 0)
    correct = summary.get("correct", 0)

    if not graded:
        return (
            f"📊 I have {total} recorded predictions, but none have been "
            "graded yet, so there is no measured accuracy yet."
        )

    accuracy = (correct / graded) * 100

    return (
        f"📊 Prediction record:\n"
        f"• Recorded predictions: {total}\n"
        f"• Graded predictions: {graded}\n"
        f"• Correct: {correct}\n"
        f"• Measured accuracy: {accuracy:.1f}%"
    )


# ---------------------------------------------------------------------------
# Prediction / research engine
# ---------------------------------------------------------------------------

def research_football(date_str, quantity=1, fetch_odds=False):
    fixtures = get_tracked_fixtures_for_date(date_str)

    if not fixtures:
        return []

    predictions = []

    for fixture in fixtures:
        status = (
            fixture.get("fixture", {})
            .get("status", {})
            .get("short", "")
        )

        if status in {
            "FT",
            "AET",
            "PEN",
            "CANC",
            "PST",
            "ABD",
            "AWD",
            "WO",
        }:
            continue

        try:
            league = fixture.get("league", {})
            league_id = league.get("id")
            season = league.get("season")

            league_avg = agent.get_league_avg_goals(
                league_id,
                season,
            )

            prediction = agent.predict_fixture(
                fixture,
                league_avg,
                fetch_odds=fetch_odds,
            )

        except (
            KeyError,
            TypeError,
            ValueError,
            RuntimeError,
            requests.RequestException,
        ) as exc:
            print(
                f"Skipping football fixture because prediction failed: "
                f"{exc}"
            )
            continue

        if not prediction:
            continue

        safest = prediction.get("safest")
        safest_probability = prediction.get("safest_probability")

        if not safest or safest_probability is None:
            continue

        try:
            safest_probability = float(safest_probability)
        except (TypeError, ValueError):
            continue

        predictions.append(
            {
                "fixture": fixture,
                "prediction": prediction,
                "safest_probability": safest_probability,
            }
        )

    predictions.sort(
        key=lambda item: item["safest_probability"],
        reverse=True,
    )

    return predictions[:quantity]


def research_basketball(date_str, quantity=1):
    games = basketball_api.get_games_for_date(date_str) or []

    if not games:
        return []

    predictions = []

    for game in games:
        try:
            prediction = basketball_model.predict_game(game)
        except (
            KeyError,
            TypeError,
            ValueError,
            RuntimeError,
            requests.RequestException,
        ) as exc:
            print(
                f"Skipping basketball game because prediction failed: "
                f"{exc}"
            )
            continue

        if not prediction:
            continue

        safest = prediction.get("safest")
        safest_probability = prediction.get("safest_probability")

        if not safest or safest_probability is None:
            continue

        try:
            safest_probability = float(safest_probability)
        except (TypeError, ValueError):
            continue

        predictions.append(
            {
                "game": game,
                "prediction": prediction,
                "safest_probability": safest_probability,
            }
        )

    predictions.sort(
        key=lambda item: item["safest_probability"],
        reverse=True,
    )

    return predictions[:quantity]


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

    if not any(term in normalized for term in research_terms):
        return None

    sport = detect_sport(normalized)

    if sport is None:
        return (
            "I can make predictions for football or basketball. "
            "Tell me which sport you want."
        )

    quantity = parse_quantity(normalized, default=1)

    date_str, date_label = resolve_date(normalized)

    fetch_odds = any(
        phrase in normalized
        for phrase in (
            "odds",
            "betting odds",
            "bookmaker",
            "bookmakers",
            "market",
            "markets",
        )
    )

    print(
        f"Prediction command received: sport={sport}, "
        f"date={date_str}, quantity={quantity}, odds={fetch_odds}"
    )

    send_telegram_message(
        "Command received. I’m checking the available fixtures and "
        "running the prediction pipeline now."
    )

    if sport == "football":
        results = research_football(
            date_str,
            quantity=quantity,
            fetch_odds=fetch_odds,
        )
    else:
        results = research_basketball(
            date_str,
            quantity=quantity,
        )

    if not results:
        return (
            f"🔎 No {sport} predictions could be produced for "
            f"{date_label} from the available data."
        )

    lines = [
        f"📊 {sport.title()} predictions for {date_label}:"
    ]

    for index, item in enumerate(results, start=1):
        prediction = item["prediction"]

        if sport == "football":
            home, away = fixture_teams(item["fixture"])

            if not home:
                home = "Home"

            if not away:
                away = "Away"

            title = f"{home} vs {away}"

        else:
            game = item["game"]

            home = (
                game.get("home_team")
                or game.get("homeTeam")
                or game.get("home")
                or "Home"
            )

            away = (
                game.get("away_team")
                or game.get("awayTeam")
                or game.get("away")
                or "Away"
            )

            title = f"{home} vs {away}"

        safest = prediction.get("safest", "N/A")
        probability = item["safest_probability"] * 100

        lines.append(
            f"\n{index}. {title}\n"
            f"Prediction: {safest}\n"
            f"Probability: {probability:.1f}%"
        )

        confidence = prediction.get("confidence")

        if confidence is not None:
            lines.append(f"Confidence: {confidence}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# General question / greeting handlers
# ---------------------------------------------------------------------------

def handle_question(text):
    result = handle_accuracy_question(text)

    if result is not None:
        return result

    result = handle_schedule_question(text)

    if result is not None:
        return result

    result = handle_count_question(text)

    if result is not None:
        return result

    result = handle_live_question(text)

    if result is not None:
        return result

    return (
        "I understand that as a question, but I don't have a supported "
        "data handler for it yet."
    )


def handle_greeting(text):
    normalized = normalize_text(text)

    if normalized in GREETINGS:
        return (
            "Hello. I’m ready to analyze football or basketball fixtures "
            "and produce predictions."
        )

    if normalized in {
        "help",
        "/help",
        "what can you do",
        "what can you do?",
    }:
        return (
            "I can analyze football and basketball fixtures, produce "
            "predictions, check schedules and live games, and report "
            "prediction-history results."
        )

    return None


def handle_message(text):
    append_memory(
        {
            "role": "user",
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )

    intent = classify_intent(text)

    print(f"Classified intent: {intent}")

    if intent == "GREETING":
        response = handle_greeting(text)

    elif intent == "QUESTION":
        response = handle_question(text)

        if response is None:
            response = (
                "I understand that as a question, but I don't have a "
                "supported data handler for it yet."
            )

    elif intent == "COMMAND":
        response = handle_research_command(text)

        if response is None:
            response = (
                "I understand that as a prediction command, but it is "
                "outside the current football/basketball prediction "
                "capabilities."
            )

    else:
        response = (
            "I can work with natural-language prediction requests. "
            "Tell me which football or basketball predictions you want."
        )

    append_memory(
        {
            "role": "assistant",
            "text": response,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )

    return response


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_TOKEN is required."
        )

    if not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is required."
        )

    message = os.environ.get("TELEGRAM_MESSAGE", "").strip()

    if not message:
        raise RuntimeError(
            "TELEGRAM_MESSAGE is required."
        )

    # This worker is deliberately single-message based.
    # Telegram polling/getUpdates is not used here.
    response = handle_message(message)

    send_message(response)

    save_memory(load_memory())
