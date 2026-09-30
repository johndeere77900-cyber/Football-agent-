"""
Telegram prediction-agent interface.

Architecture:
    Telegram
        -> Val Town webhook
        -> GitHub Actions workflow_dispatch
        -> this file (one message)
        -> Telegram response

Design:
- Prediction is the primary capability.
- Research is secondary/supporting.
- No Telegram polling.
- No getUpdates.
- No background Telegram process.
- Predictions are persisted through storage.py.
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


# ============================================================================
# CONFIGURATION
# ============================================================================

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


# ============================================================================
# LANGUAGE / INTENT CONSTANTS
# ============================================================================

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


# ============================================================================
# TELEGRAM TRANSPORT
# ============================================================================

def send_message(text):
    """Send one response to the configured Telegram chat."""

    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_TOKEN is not configured."
        )

    if not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is not configured."
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


# ============================================================================
# PERSISTENT TELEGRAM MEMORY
# ============================================================================

def _default_memory():
    return {
        "recent": [],
        "preference_counts": {
            "football": 0,
            "basketball": 0,
        },
    }


def load_memory():
    """Load conversation history from database (fallback to json file if empty)."""
    chat_id = CHAT_ID or "default_chat"
    try:
        messages = storage.get_recent_telegram_messages(chat_id, limit=50)
        if messages:
            return {
                "recent": messages,
                "preference_counts": {"football": 0, "basketball": 0},
            }
    except Exception:
        pass

    default = _default_memory()
    if not os.path.exists(MEMORY_FILE):
        return default

    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as handle:
            memory = json.load(handle)
        if isinstance(memory, dict) and isinstance(memory.get("recent"), list):
            return memory
    except Exception:
        pass

    return default


def save_memory(memory):
    """
    Memory persistence is handled through storage.save_telegram_message.

    Historical telegram_memory.json is kept intact on disk and not overwritten at runtime.
    """
    pass


def append_memory(entry):
    """Append one conversation entry to database storage."""
    if not isinstance(entry, dict):
        raise ValueError("Memory entry must be a dictionary.")

    chat_id = CHAT_ID or "default_chat"
    role = entry.get("role", "user")
    text = entry.get("text", "")
    timestamp = entry.get("timestamp")

    try:
        storage.save_telegram_message(chat_id, role, text, timestamp)
    except Exception as exc:
        print(f"Failed to persist telegram memory in storage: {exc}")


# ============================================================================
# TEXT PARSING
# ============================================================================

def normalize_text(text):
    return re.sub(
        r"\s+",
        " ",
        str(text).strip().lower(),
    )


def parse_quantity(text, default=1):
    """
    Extract an explicit quantity without accidentally reading the year,
    month, or day from an ISO date as the requested number of predictions.
    """

    normalized = normalize_text(text)

    # Remove ISO dates before looking for standalone numbers.
    cleaned = re.sub(
        r"\b\d{4}-\d{2}-\d{2}\b",
        " ",
        normalized,
    )

    # Prefer explicit quantity phrases.
    explicit_patterns = (
        r"\b(?:top|give|show|find|get|pick|list)\s+(\d{1,3})\b",
        r"\b(\d{1,3})\s+(?:predictions?|picks?|tips?|games?|matches?|fixtures?)\b",
        r"\b(?:predictions?|picks?|tips?|games?|matches?|fixtures?)\s+(\d{1,3})\b",
    )

    for pattern in explicit_patterns:
        match = re.search(
            pattern,
            cleaned,
        )

        if match:
            value = int(match.group(1))

            if value > 0:
                return min(value, 50)

    # Number words.
    for token in re.findall(
        r"[a-z]+",
        cleaned,
    ):
        if token in NUMBER_WORDS:
            return NUMBER_WORDS[token]

    # Only use a bare number when it is not obviously unrelated.
    match = re.search(
        r"\b(\d{1,2})\b",
        cleaned,
    )

    if match:
        value = int(match.group(1))

        if value > 0:
            return min(value, 50)

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

    Dates are interpreted in UTC because the GitHub worker runs remotely.
    """

    normalized = normalize_text(text)

    today = datetime.now(
        timezone.utc
    ).date()

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
            (
                today
                + timedelta(days=1)
            ).isoformat(),
            "tomorrow",
        )

    if (
        "today" in normalized
        or "tonight" in normalized
    ):
        return (
            today.isoformat(),
            "today",
        )

    for index, day_name in enumerate(WEEKDAYS):
        if re.search(
            rf"\b{day_name}\b",
            normalized,
        ):
            days_ahead = (
                index
                - today.weekday()
            ) % 7 or 7

            return (
                (
                    today
                    + timedelta(days=days_ahead)
                ).isoformat(),
                day_name.title(),
            )

    return (
        today.isoformat(),
        "today",
    )


# ============================================================================
# INTENT CLASSIFICATION
# ============================================================================

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
        "what can you do",
        "what can you do?",
    }:
        return "GREETING"

    first_word = normalized.split(
        " ",
        1,
    )[0]

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

    has_sport = (
        detect_sport(normalized)
        is not None
    )

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


# ============================================================================
# FOOTBALL FIXTURE HELPERS
# ============================================================================

def format_fixture_time(fixture):
    raw = (
        fixture
        .get("fixture", {})
        .get("date")
    )

    if not raw:
        return "time unavailable"

    try:
        parsed = datetime.fromisoformat(
            raw.replace(
                "Z",
                "+00:00",
            )
        )

        return parsed.astimezone(
            timezone.utc
        ).strftime("%H:%M UTC")

    except (
        TypeError,
        ValueError,
    ):
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
    query = normalize_text(
        team_query
    )

    home, away = fixture_teams(
        fixture
    )

    return (
        query in normalize_text(home)
        or query in normalize_text(away)
    )


def get_tracked_fixtures_for_date(
    date_str,
):
    fixtures = api_football.get_fixtures_by_date(
        date_str
    )

    if not isinstance(
        fixtures,
        list,
    ):
        return []

    return [
        fixture
        for fixture in fixtures
        if (
            isinstance(fixture, dict)
            and fixture.get(
                "league",
                {},
            ).get("id")
            in config.ALLOWED_LEAGUE_IDS
        )
    ]


# ============================================================================
# FACTUAL QUESTION HANDLERS
# ============================================================================

def handle_count_question(text):
    """
    Only handle actual count questions.

    This prevents the count handler from stealing unrelated questions.
    """

    normalized = normalize_text(text)

    count_intent = (
        "how many" in normalized
        or "number of" in normalized
        or re.search(
            r"\bhow many\b",
            normalized,
        )
    )

    if not count_intent:
        return None

    if not any(
        term in normalized
        for term in (
            "match",
            "matches",
            "game",
            "games",
            "fixture",
            "fixtures",
            "football",
            "soccer",
        )
    ):
        return None

    league_name, league_id = mentions_league(
        normalized
    )

    date_str, date_label = resolve_date(
        normalized
    )

    fixtures = get_tracked_fixtures_for_date(
        date_str
    )

    if league_id:
        fixtures = [
            fixture
            for fixture in fixtures
            if fixture.get(
                "league",
                {},
            ).get("id") == league_id
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
        return None

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
        return None

    date_str, date_label = resolve_date(
        normalized
    )

    dates_to_check = [date_str]

    if date_label == "today":
        dates_to_check.extend(
            (
                datetime.fromisoformat(
                    date_str
                ).date()
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
        return (
            f"📅 I couldn't find a scheduled tracked "
            f"match for *{team_query}* starting "
            f"{date_label}."
        )

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

    return "\n".join(lines)
    # ============================================================================
# LIVE / ACCURACY QUESTIONS
# ============================================================================

def handle_live_question(text):
    normalized = normalize_text(text)

    if not any(
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
    ):
        return None

    football_live = []
    basketball_live = []

    # These are optional capabilities. The bot must not crash merely because
    # a live endpoint has not yet been implemented in an API wrapper.
    football_live_function = getattr(
        api_football,
        "get_live_fixtures",
        None,
    )

    if callable(football_live_function):
        try:
            football_live = (
                football_live_function()
                or []
            )
        except Exception as exc:
            print(
                f"Live football lookup failed: {exc}"
            )

    basketball_live_function = getattr(
        basketball_api,
        "get_live_games",
        None,
    )

    if callable(basketball_live_function):
        try:
            basketball_live = (
                basketball_live_function()
                or []
            )
        except Exception as exc:
            print(
                f"Live basketball lookup failed: {exc}"
            )

    if not football_live and not basketball_live:
        return (
            "🔴 I couldn't retrieve any live tracked games "
            "from the currently available live-data endpoints."
        )

    lines = [
        "🔴 Live games right now:"
    ]

    for fixture in football_live:
        home, away = fixture_teams(
            fixture
        )

        if home and away:
            lines.append(
                f"⚽ {home} vs {away}"
            )

    for game in basketball_live:
        if not isinstance(
            game,
            dict,
        ):
            continue

        teams = game.get(
            "teams",
            {},
        )

        if isinstance(
            teams,
            dict,
        ):
            home_data = teams.get(
                "home",
                {},
            ) or {}

            away_data = teams.get(
                "away",
                {},
            ) or {}

            home = (
                home_data.get("name")
                or "Home"
            )

            away = (
                away_data.get("name")
                or "Away"
            )

        else:
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

        lines.append(
            f"🏀 {home} vs {away}"
        )

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

    if not any(
        term in normalized
        for term in accuracy_terms
    ):
        return None

    try:
        summary = storage.accuracy_summary()

    except Exception as exc:
        print(
            f"Accuracy summary failed: {exc}"
        )

        return (
            "📊 I could not read the football "
            "prediction history right now."
        )

    total = summary.get(
        "total_graded",
        0,
    )

    accuracy = summary.get(
        "overall_accuracy",
        0.0,
    )

    if not total:
        return (
            "📊 There are no graded football predictions "
            "in the history yet."
        )

    return (
        "📊 Football prediction record:\n"
        f"• Graded predictions: {total}\n"
        f"• Measured accuracy: {accuracy * 100:.1f}%"
    )


# ============================================================================
# PREDICTION PERSISTENCE HELPERS
# ============================================================================

def _save_football_prediction(item):
    fixture = item.get("fixture")

    if not isinstance(
        fixture,
        dict,
    ):
        raise ValueError(
            "Football prediction is missing fixture data."
        )

    prediction = item.get(
        "prediction"
    )

    if not isinstance(
        prediction,
        dict,
    ):
        raise ValueError(
            "Football prediction is invalid."
        )

    if prediction.get(
        "insufficient_data"
    ):
        return False

    fixture_id = prediction.get(
        "fixture_id"
    )

    if fixture_id is None:
        fixture_id = (
            fixture
            .get("fixture", {})
            .get("id")
        )

    if not fixture_id:
        raise ValueError(
            "Football prediction is missing fixture_id."
        )

    prediction_context = "LIVE" if prediction.get("is_live") else "PRE_MATCH"
    inserted = storage.save_prediction(
        fixture_id=int(fixture_id),
        match_date=str(
            prediction.get(
                "date",
                fixture.get(
                    "fixture",
                    {},
                ).get(
                    "date",
                    "",
                ),
            )
        ),
        home_team=str(
            prediction.get(
                "home_team",
                fixture_teams(fixture)[0],
            )
        ),
        away_team=str(
            prediction.get(
                "away_team",
                fixture_teams(fixture)[1],
            )
        ),
        league=str(
            prediction.get(
                "league",
                fixture.get(
                    "league",
                    {},
                ).get(
                    "name",
                    "Unknown",
                ),
            )
        ),
        markets=prediction.get(
            "markets",
            {},
        ),
        confidence=prediction.get(
            "confidence",
            {},
        ),
        home_team_id=prediction.get(
            "home_team_id"
        ),
        away_team_id=prediction.get(
            "away_team_id"
        ),
        odds_comparison=prediction.get(
            "odds_comparison"
        ),
        prediction_context=prediction_context,
    )

    return bool(inserted)


def _save_basketball_prediction(item):
    game = item.get("game")

    if not isinstance(
        game,
        dict,
    ):
        raise ValueError(
            "Basketball prediction is missing game data."
        )

    prediction = item.get(
        "prediction"
    )

    if not isinstance(
        prediction,
        dict,
    ):
        raise ValueError(
            "Basketball prediction is invalid."
        )

    if prediction.get(
        "insufficient_data"
    ):
        return False

    game_id = prediction.get(
        "game_id"
    )

    if game_id is None:
        game_id = game.get(
            "id"
        )

    if not game_id:
        raise ValueError(
            "Basketball prediction is missing game_id."
        )

    prediction_context = "LIVE" if prediction.get("is_live") else "PRE_MATCH"
    inserted = storage.save_basketball_prediction(
        game_id=int(game_id),
        game_date=str(
            prediction.get(
                "date",
                game.get(
                    "date",
                    "",
                ),
            )
        ),
        home_team=str(
            prediction.get(
                "home_team",
                "Home",
            )
        ),
        away_team=str(
            prediction.get(
                "away_team",
                "Away",
            )
        ),
        league=str(
            prediction.get(
                "league",
                "NBA",
            )
        ),
        markets=prediction.get(
            "markets",
            {},
        ),
        confidence=prediction.get(
            "confidence",
            {},
        ),
        prediction_context=prediction_context,
    )

    return bool(inserted)


# ============================================================================
# FOOTBALL PREDICTIONS
# ============================================================================

def research_football(
    date_str,
    quantity=1,
    fetch_odds=False,
):
    fixtures = get_tracked_fixtures_for_date(
        date_str
    )

    if not fixtures:
        return []

    predictions = []

    for fixture in fixtures:
        status = (
            fixture
            .get("fixture", {})
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
            league = fixture.get(
                "league",
                {},
            )

            league_id = league.get(
                "id"
            )

            season = league.get(
                "season"
            )

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
                "Skipping football fixture because "
                f"prediction failed: {exc}"
            )
            continue

        if not isinstance(
            prediction,
            dict,
        ):
            continue

        if prediction.get(
            "insufficient_data"
        ):
            continue

        safest = prediction.get(
            "safest"
        )

        if not isinstance(
            safest,
            dict,
        ):
            continue

        probability = safest.get(
            "probability"
        )

        try:
            probability = float(
                probability
            )
        except (
            TypeError,
            ValueError,
        ):
            continue

        if not 0.0 <= probability <= 1.0:
            continue

        item = {
            "fixture": fixture,
            "prediction": prediction,
            "safest_probability": probability,
        }

        # Persist every valid Telegram prediction immediately.
        try:
            _save_football_prediction(
                item
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            RuntimeError,
        ) as exc:
            print(
                "Football prediction was generated but "
                f"could not be persisted: {exc}"
            )
            continue

        predictions.append(item)

    predictions.sort(
        key=lambda item: item[
            "safest_probability"
        ],
        reverse=True,
    )

    return predictions[:quantity]


# ============================================================================
# BASKETBALL PREDICTIONS
# ============================================================================

def research_basketball(
    date_str,
    quantity=1,
):
    league_ids = getattr(
        config,
        "ALLOWED_BASKETBALL_LEAGUE_IDS",
        [],
    )

    if not league_ids:
        return []

    games = []

    # Keep API calls constrained to the configured basketball leagues.
    for league_id in league_ids:
        try:
            league_games = (
                basketball_api.get_games_by_date(
                    date_str,
                    league_id,
                )
                or []
            )

        except (
            KeyError,
            TypeError,
            ValueError,
            RuntimeError,
            requests.RequestException,
        ) as exc:
            print(
                f"Basketball league {league_id} lookup failed: {exc}"
            )
            continue

        if isinstance(
            league_games,
            list,
        ):
            games.extend(
                league_games
            )

    if not games:
        return []

    # Prevent duplicate games if an API response repeats an ID.
    unique_games = {}
    for game in games:
        if not isinstance(
            game,
            dict,
        ):
            continue

        game_id = game.get(
            "id"
        )

        if game_id is None:
            continue

        unique_games[int(game_id)] = game

    predictions = []

    for game in unique_games.values():
        try:
            prediction = (
                basketball_model.predict_game(
                    game
                )
            )

        except (
            KeyError,
            TypeError,
            ValueError,
            RuntimeError,
            requests.RequestException,
        ) as exc:
            print(
                "Skipping basketball game because "
                f"prediction failed: {exc}"
            )
            continue

        if not isinstance(
            prediction,
            dict,
        ):
            continue

        if prediction.get(
            "insufficient_data"
        ):
            continue

        safest = prediction.get(
            "safest"
        )

        if not isinstance(
            safest,
            dict,
        ):
            continue

        probability = safest.get(
            "probability"
        )

        try:
            probability = float(
                probability
            )
        except (
            TypeError,
            ValueError,
        ):
            continue

        if not 0.0 <= probability <= 1.0:
            continue

        item = {
            "game": game,
            "prediction": prediction,
            "safest_probability": probability,
        }

        try:
            _save_basketball_prediction(
                item
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            RuntimeError,
        ) as exc:
            print(
                "Basketball prediction was generated but "
                f"could not be persisted: {exc}"
            )
            continue

        predictions.append(item)

    predictions.sort(
        key=lambda item: item[
            "safest_probability"
        ],
        reverse=True,
    )

    return predictions[:quantity]


# ============================================================================
# PREDICTION COMMAND HANDLER
# ============================================================================

def handle_prediction_command(text):
    normalized = normalize_text(
        text
    )

    if not any(
        term in normalized
        for term in (
            "predict",
            "prediction",
            "predictions",
            "forecast",
            "forecast",
            "tip",
            "tips",
            "pick",
            "picks",
            "analyze",
            "analyse",
            "research",
        )
    ):
        return None

    sport = detect_sport(
        normalized
    )

    if sport is None:
        return (
            "I can make predictions for football or basketball. "
            "Tell me which sport you want."
        )

    quantity = parse_quantity(
        normalized,
        default=1,
    )

    date_str, date_label = resolve_date(
        normalized
    )

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
        "Prediction command received: "
        f"sport={sport}, "
        f"date={date_str}, "
        f"quantity={quantity}, "
        f"odds={fetch_odds}"
    )

    # No redundant "Command received" Telegram message.
    # The Val Town webhook already acknowledges receipt and the final
    # prediction response is sent once by main.

    try:
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

    except requests.RequestException as exc:
        print(
            f"Prediction API request failed: {exc}"
        )

        return (
            "⚠️ The prediction data provider failed while "
            "retrieving the required data. No prediction was "
            "returned from incomplete data."
        )

    except RuntimeError as exc:
        print(
            f"Prediction pipeline failed: {exc}"
        )

        return (
            "⚠️ The prediction pipeline could not complete "
            "because a required data source failed."
        )

    if not results:
        return (
            f"📊 No {sport} predictions could be produced "
            f"for {date_label} from the available data."
        )

    lines = [
        f"📊 {sport.title()} predictions for {date_label}:"
    ]

    for index, item in enumerate(
        results,
        start=1,
    ):
        prediction = item[
            "prediction"
        ]

        if sport == "football":
            home, away = fixture_teams(
                item["fixture"]
            )

        else:
            prediction_game = item[
                "prediction"
            ]

            home = prediction_game.get(
                "home_team",
                "Home",
            )

            away = prediction_game.get(
                "away_team",
                "Away",
            )

        title = (
            f"{home} vs {away}"
        )

        safest = prediction.get(
            "safest"
        )

        if isinstance(
            safest,
            dict,
        ):
            safest_label = safest.get(
                "label",
                "N/A",
            )

            probability = safest.get(
                "probability",
                item[
                    "safest_probability"
                ],
            )

        else:
            safest_label = "N/A"
            probability = item[
                "safest_probability"
            ]

        try:
            probability = float(
                probability
            )
        except (
            TypeError,
            ValueError,
        ):
            probability = item[
                "safest_probability"
            ]

        lines.append(
            f"\n{index}. {title}\n"
            f"Prediction: {safest_label}\n"
            f"Probability: {probability * 100:.1f}%"
        )

        confidence_data = prediction.get(
            "confidence"
        )

        if isinstance(
            confidence_data,
            dict,
        ):
            confidence_label = (
                confidence_data.get(
                    "label"
                )
            )

            if confidence_label:
                lines.append(
                    f"Confidence: {confidence_label}"
                )

    return "\n".join(lines)


# ============================================================================
# BACKWARD-COMPATIBLE RESEARCH COMMAND NAME
# ============================================================================

def handle_research_command(text):
    """
    Research remains supported, but prediction routing is authoritative.

    This alias keeps the existing message router/API contract intact.
    """

    return handle_prediction_command(
        text
    )


# ============================================================================
# GENERAL QUESTION / GREETING HANDLERS
# ============================================================================

def handle_question(text):
    result = handle_accuracy_question(
        text
    )

    if result is not None:
        return result

    result = handle_schedule_question(
        text
    )

    if result is not None:
        return result

    result = handle_count_question(
        text
    )

    if result is not None:
        return result

    result = handle_live_question(
        text
    )

    if result is not None:
        return result

    return (
        "I understand that as a question, but I don't "
        "have a supported data handler for it yet."
    )


def handle_greeting(text):
    normalized = normalize_text(
        text
    )

    if normalized in GREETINGS:
        return (
            "Hello. I’m ready to analyze football or "
            "basketball fixtures and produce predictions."
        )

    if normalized in {
        "help",
        "/help",
        "what can you do",
        "what can you do?",
    }:
        return (
            "I can predict football and basketball fixtures, "
            "check schedules and live games, and report the "
            "measured accuracy of graded football predictions."
        )

    return None


# ============================================================================
# MESSAGE ROUTER
# ============================================================================

def handle_message(text):
    text = str(
        text or ""
    ).strip()

    if not text:
        return (
            "Please send a football or basketball prediction request."
        )

    append_memory(
        {
            "role": "user",
            "text": text,
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
        }
    )

    intent = classify_intent(
        text
    )

    print(
        f"Classified intent: {intent}"
    )

    try:
        if intent == "GREETING":
            response = handle_greeting(
                text
            )

        elif intent == "QUESTION":
            response = handle_question(
                text
            )

        elif intent == "COMMAND":
            response = handle_prediction_command(
                text
            )

        else:
            response = (
                "I can work with natural-language prediction "
                "requests. Tell me which football or basketball "
                "predictions you want."
            )

    except (
        requests.RequestException,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        print(
            f"Message handling failed: {exc}"
        )

        response = (
            "⚠️ I couldn't complete that request because a "
            "required data source or prediction step failed. "
            "No unsupported prediction was returned."
        )

    if response is None:
        response = (
            "I couldn't produce a response for that request."
        )

    response = str(
        response
    )

    append_memory(
        {
            "role": "assistant",
            "text": response,
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
        }
    )

    return response


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_TOKEN is required."
        )

    if not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is required."
        )

    message = os.environ.get(
        "TELEGRAM_MESSAGE",
        "",
    ).strip()

    if not message:
        raise RuntimeError(
            "TELEGRAM_MESSAGE is required."
        )

    # One-message worker only.
    # Telegram polling/getUpdates is intentionally not used.
    response = handle_message(
        message
    )

    send_message(
        response
    )

    # Ensure the final in-memory state is flushed.
    save_memory(
        load_memory()
)
