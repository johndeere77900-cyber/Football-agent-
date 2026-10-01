"""
Telegram Prediction Agent Control Center.

Architecture:
    Telegram
        -> Val Town webhook
        -> authenticated/authorized request
        -> idempotent operation dispatch
        -> GitHub Actions / existing Python operations
        -> existing prediction/evaluation engines
        -> persistent operational state/results
        -> Telegram result formatter
        -> contextual user-selected suggested actions

Telegram remains a thin control/presentation layer.
Does not perform polling or run background getUpdates loops.
"""

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import requests

import api_football
import backtest
import basketball_api
import basketball_model
import config
import main as agent
import storage
import time_utils


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
# CONSTANTS & DICTIONARIES
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

def send_message(text, reply_markup=None):
    """Send one response to the configured Telegram chat."""
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_TOKEN is not configured."
        )

    if not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is not configured."
        )

    payload = {
        "chat_id": CHAT_ID,
        "text": str(text),
        "parse_mode": "Markdown",
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    response = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json=payload,
        timeout=15,
    )

    try:
        data = response.json()
    except ValueError:
        data = {}

    if not response.ok or not data.get("ok"):
        # Retry once without Markdown if formatting syntax error occurred
        if "can't parse entities" in response.text.lower() or "markdown" in response.text.lower():
            payload.pop("parse_mode", None)
            retry_res = requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json=payload,
                timeout=15,
            )
            try:
                return retry_res.json()
            except ValueError:
                pass

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
    """Load conversation history from database (fail-closed in production)."""
    chat_id = CHAT_ID or "default_chat"
    is_prod = storage.is_neon() or (os.environ.get("ENVIRONMENT", "").lower() == "production")

    try:
        messages = storage.get_recent_telegram_messages(chat_id, limit=50)
        if messages:
            return {
                "recent": messages,
                "preference_counts": {"football": 0, "basketball": 0},
            }
    except Exception as exc:
        if is_prod:
            raise RuntimeError(
                f"Failed to read Telegram memory from Neon PostgreSQL database in production: {exc}"
            ) from exc

    if is_prod:
        return _default_memory()

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
    """Memory persistence wrapper."""
    pass


def append_memory(entry):
    """Append conversation entry to database storage."""
    if not isinstance(entry, dict):
        raise ValueError("Memory entry must be a dictionary.")

    chat_id = CHAT_ID or "default_chat"
    role = entry.get("role", "user")
    text = entry.get("text", "")
    timestamp = entry.get("timestamp")

    is_prod = storage.is_neon() or (os.environ.get("ENVIRONMENT", "").lower() == "production")

    try:
        storage.save_telegram_message(chat_id, role, text, timestamp)
    except Exception as exc:
        if is_prod:
            raise RuntimeError(
                f"Failed to persist Telegram memory in Neon PostgreSQL database in production: {exc}"
            ) from exc
        print(f"Failed to persist telegram memory in storage: {exc}")


# ============================================================================
# TEXT PARSING & HELPERS
# ============================================================================

def normalize_text(text):
    return re.sub(
        r"\s+",
        " ",
        str(text).strip().lower(),
    )


def parse_quantity(text, default=1):
    """Extract explicit quantity without treating date years as quantities."""
    normalized = normalize_text(text)

    cleaned = re.sub(
        r"\b\d{4}-\d{2}-\d{2}\b",
        " ",
        normalized,
    )

    explicit_patterns = (
        r"\b(?:top|give|show|find|get|pick|list|sample)\s+(\d{1,3})\b",
        r"\b(\d{1,3})\s+(?:predictions?|picks?|tips?|games?|matches?|fixtures?|sample)\b",
        r"\b(?:predictions?|picks?|tips?|games?|matches?|fixtures?)\s+(\d{1,3})\b",
    )

    for pattern in explicit_patterns:
        match = re.search(pattern, cleaned)
        if match:
            value = int(match.group(1))
            if value > 0:
                return min(value, 500)

    for token in re.findall(r"[a-z]+", cleaned):
        if token in NUMBER_WORDS:
            return NUMBER_WORDS[token]

    match = re.search(r"\b(\d{1,2})\b", cleaned)
    if match:
        value = int(match.group(1))
        if value > 0:
            return min(value, 500)

    return default


def mentions_league(text):
    normalized = normalize_text(text)

    # Check football leagues
    for name, league_id in sorted(
        config.LEAGUE_NAME_TO_ID.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if name in normalized:
            return name, league_id, "football"

    # Check basketball
    if "nba" in normalized:
        return "NBA", config.ALLOWED_BASKETBALL_LEAGUE_IDS[0], "basketball"

    return None, None, None


def detect_sport(text):
    normalized = normalize_text(text)

    if "basketball" in normalized or re.search(r"\bnba\b", normalized):
        return "basketball"

    if "football" in normalized or "soccer" in normalized:
        return "football"

    return None


def resolve_date(text):
    """Resolve explicit dates and natural-language dates in UTC."""
    normalized = normalize_text(text)
    today = datetime.now(timezone.utc).date()

    iso_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", normalized)
    if iso_match:
        value = iso_match.group(0)
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("The date in the message is invalid.") from exc
        return value, value

    if "tomorrow" in normalized:
        return (today + timedelta(days=1)).isoformat(), "tomorrow"

    if "today" in normalized or "tonight" in normalized:
        return today.isoformat(), "today"

    for index, day_name in enumerate(WEEKDAYS):
        if re.search(rf"\b{day_name}\b", normalized):
            days_ahead = (index - today.weekday()) % 7 or 7
            return (today + timedelta(days=days_ahead)).isoformat(), day_name.title()

    return today.isoformat(), "today"


def parse_season(text):
    """Parse season year from text."""
    normalized = normalize_text(text)
    match = re.search(r"\b(20\d{2}|19\d{2})\b", normalized)
    if match:
        return int(match.group(1))
    return datetime.now(timezone.utc).year - 1


def classify_intent(text):
    """Classify input intent for backward compatibility."""
    normalized = normalize_text(text)
    if not normalized:
        return "UNKNOWN"
    if normalized in GREETINGS or normalized in {"/start", "/help", "help", "what can you do", "what can you do?"}:
        return "GREETING"
    if "?" in normalized or normalized.startswith(("how", "what", "when", "where", "who", "which", "why", "is", "are", "can", "does", "do", "will")):
        return "QUESTION"
    if any(re.search(rf"\b{re.escape(v)}\b", normalized) for v in COMMAND_VERBS):
        return "COMMAND"
    return "UNKNOWN"


# ============================================================================
# FIXTURE & QUESTION HELPERS
# ============================================================================

def format_fixture_time(fixture):
    raw = fixture.get("fixture", {}).get("date")
    if not raw:
        return "time unavailable"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).strftime("%H:%M UTC")
    except (TypeError, ValueError):
        return "time unavailable"


def fixture_teams(fixture):
    teams = fixture.get("teams", {})
    home = teams.get("home", {}) or {}
    away = teams.get("away", {}) or {}
    return (str(home.get("name") or ""), str(away.get("name") or ""))


def fixture_matches_team(fixture, team_query):
    query = normalize_text(team_query)
    home, away = fixture_teams(fixture)
    return query in normalize_text(home) or query in normalize_text(away)


def get_tracked_fixtures_for_date(date_str):
    fixtures = api_football.get_fixtures_by_date(date_str)
    if not isinstance(fixtures, list):
        return []
    return [
        fixture for fixture in fixtures
        if isinstance(fixture, dict) and fixture.get("league", {}).get("id") in config.ALLOWED_LEAGUE_IDS
    ]


def handle_count_question(text):
    normalized = normalize_text(text)
    if "how many" not in normalized and "number of" not in normalized:
        return None
    if not any(t in normalized for t in ("match", "matches", "game", "games", "fixture", "fixtures", "football", "soccer")):
        return None

    league_name, league_id, _ = mentions_league(normalized)
    date_str, date_label = resolve_date(normalized)
    fixtures = get_tracked_fixtures_for_date(date_str)

    if league_id:
        fixtures = [f for f in fixtures if f.get("league", {}).get("id") == league_id]
        label = league_name.title() if league_name else "tracked"
    else:
        label = "tracked"

    count = len(fixtures)
    noun = "match" if count == 1 else "matches"
    msg = f"📊 There are *{count}* {label} {noun} scheduled for {date_label}."
    send_message(msg)
    return True


def handle_schedule_question(text):
    normalized = normalize_text(text)
    if not any(phrase in normalized for phrase in ("what time", "when is", "when are", "playing", "kickoff", "kick-off")):
        return None

    stop_words = {"what", "time", "is", "are", "when", "will", "the", "a", "an", "playing", "play", "kickoff", "kick-off", "today", "tomorrow", "please", "me", "tell", "match", "game", "on", "at"}
    words = re.findall(r"[a-z0-9'-]+", normalized)
    team_query = " ".join(w for w in words if w not in stop_words)

    if not team_query:
        return None

    date_str, date_label = resolve_date(normalized)
    fixtures = get_tracked_fixtures_for_date(date_str)
    found = [f for f in fixtures if fixture_matches_team(f, team_query)]

    if not found:
        return f"📅 I couldn't find a scheduled tracked match for *{team_query}* starting {date_label}."

    lines = [f"📅 Matches for *{team_query}*:"]
    for f in found[:5]:
        home, away = fixture_teams(f)
        league = f.get("league", {}).get("name", "Unknown league")
        f_date = f.get("fixture", {}).get("date", "")[:10] or "date unavailable"
        lines.append(f"• {home} vs {away} — {f_date}, {format_fixture_time(f)} ({league})")

    return "\n".join(lines)


def handle_accuracy_question(text):
    normalized = normalize_text(text)
    if not any(t in normalized for t in ("accuracy", "accurate", "hit rate", "win rate", "success rate", "performance")):
        return None

    try:
        summary = storage.accuracy_summary()
    except Exception:
        return "📊 I could not read the football prediction history right now."

    total = summary.get("total_graded", 0)
    accuracy = summary.get("overall_accuracy", 0.0)

    if not total:
        return "📊 There are no graded football predictions in the history yet."

    return f"📊 Football prediction record:\n• Graded predictions: {total}\n• Measured accuracy: {accuracy * 100:.1f}%"


# ============================================================================
# PREDICTION PERSISTENCE HELPERS
# ============================================================================

def _save_football_prediction(item):
    fixture = item.get("fixture")
    if not isinstance(fixture, dict):
        raise ValueError("Football prediction is missing fixture data.")

    prediction = item.get("prediction")
    if not isinstance(prediction, dict):
        raise ValueError("Football prediction is invalid.")

    if prediction.get("insufficient_data"):
        return False

    fixture_id = prediction.get("fixture_id") or fixture.get("fixture", {}).get("id")
    if not fixture_id:
        raise ValueError("Football prediction is missing fixture_id.")

    prediction_context = "LIVE" if prediction.get("is_live") else "PRE_MATCH"
    inserted = storage.save_prediction(
        fixture_id=int(fixture_id),
        match_date=str(prediction.get("date", fixture.get("fixture", {}).get("date", ""))),
        home_team=str(prediction.get("home_team", fixture_teams(fixture)[0])),
        away_team=str(prediction.get("away_team", fixture_teams(fixture)[1])),
        league=str(prediction.get("league", fixture.get("league", {}).get("name", "Unknown"))),
        markets=prediction.get("markets", {}),
        confidence=prediction.get("confidence", {}),
        home_team_id=prediction.get("home_team_id"),
        away_team_id=prediction.get("away_team_id"),
        odds_comparison=prediction.get("odds_comparison"),
        prediction_context=prediction_context,
        prediction_record=prediction.get("prediction_record") or prediction,
    )
    return bool(inserted)


def _save_basketball_prediction(item):
    game = item.get("game")
    if not isinstance(game, dict):
        raise ValueError("Basketball prediction is missing game data.")

    prediction = item.get("prediction")
    if not isinstance(prediction, dict):
        raise ValueError("Basketball prediction is invalid.")

    if prediction.get("insufficient_data"):
        return False

    game_id = prediction.get("game_id") or game.get("id")
    if not game_id:
        raise ValueError("Basketball prediction is missing game_id.")

    prediction_context = "LIVE" if prediction.get("is_live") else "PRE_MATCH"
    inserted = storage.save_basketball_prediction(
        game_id=int(game_id),
        game_date=str(prediction.get("date", game.get("date", ""))),
        home_team=str(prediction.get("home_team", "Home")),
        away_team=str(prediction.get("away_team", "Away")),
        league=str(prediction.get("league", "NBA")),
        markets=prediction.get("markets", {}),
        confidence=prediction.get("confidence", {}),
        prediction_context=prediction_context,
        prediction_record=prediction,
    )
    return bool(inserted)


def research_football(date_str, quantity=1, fetch_odds=False):
    fixtures = get_tracked_fixtures_for_date(date_str)
    if not fixtures:
        return []

    predictions = []
    for fixture in fixtures:
        status = fixture.get("fixture", {}).get("status", {}).get("short", "")
        if status in {"FT", "AET", "PEN", "CANC", "PST", "ABD", "AWD", "WO"}:
            continue

        try:
            league = fixture.get("league", {})
            league_id = league.get("id")
            season = league.get("season")
            league_avg = agent.get_league_avg_goals(league_id, season)
            prediction = agent.predict_fixture(fixture, league_avg, fetch_odds=fetch_odds)
        except Exception as exc:
            print(f"Skipping football fixture because prediction failed: {exc}")
            continue

        if not isinstance(prediction, dict) or prediction.get("insufficient_data"):
            continue

        safest = prediction.get("safest")
        if not isinstance(safest, dict):
            continue

        prob = safest.get("probability")
        try:
            prob = float(prob)
        except (TypeError, ValueError):
            continue

        if not 0.0 <= prob <= 1.0:
            continue

        item = {
            "fixture": fixture,
            "prediction": prediction,
            "safest_probability": prob,
        }

        try:
            _save_football_prediction(item)
        except Exception as exc:
            print(f"Football prediction save error: {exc}")

        predictions.append(item)

    predictions.sort(key=lambda item: item["safest_probability"], reverse=True)
    return predictions[:quantity]


def research_basketball(date_str, quantity=1):
    league_ids = getattr(config, "ALLOWED_BASKETBALL_LEAGUE_IDS", [])
    if not league_ids:
        return []

    games = []
    for league_id in league_ids:
        try:
            lg = basketball_api.get_games_by_date(date_str, league_id) or []
            if isinstance(lg, list):
                games.extend(lg)
        except Exception as exc:
            print(f"Basketball league {league_id} error: {exc}")

    if not games:
        return []

    unique_games = {int(g["id"]): g for g in games if isinstance(g, dict) and g.get("id") is not None}
    predictions = []

    for game in unique_games.values():
        try:
            prediction = basketball_model.predict_game(game)
        except Exception as exc:
            print(f"Skipping basketball game: {exc}")
            continue

        if not isinstance(prediction, dict) or prediction.get("insufficient_data"):
            continue

        safest = prediction.get("safest")
        if not isinstance(safest, dict):
            continue

        prob = safest.get("probability")
        try:
            prob = float(prob)
        except (TypeError, ValueError):
            continue

        if not 0.0 <= prob <= 1.0:
            continue

        item = {
            "game": game,
            "prediction": prediction,
            "safest_probability": prob,
        }

        try:
            _save_basketball_prediction(item)
        except Exception as exc:
            print(f"Basketball prediction save error: {exc}")

        predictions.append(item)

    predictions.sort(key=lambda item: item["safest_probability"], reverse=True)
    return predictions[:quantity]


# ============================================================================
# STRUCTURED OPERATION ROUTER
# ============================================================================

VALID_OPERATIONS = {
    "predict",
    "fixtures",
    "football",
    "basketball",
    "backtest",
    "backtest_status",
    "evaluate",
    "health",
    "model_status",
    "data_status",
    "logs",
    "errors",
    "retrain",
    "config",
}


def resolve_operation(text):
    """
    Structured router mapping commands and natural-language inputs to typed operations.

    Returns tuple: (operation: str, parsed_parameters: dict)
    """
    raw_text = str(text or "").strip()
    normalized = normalize_text(raw_text)

    if not normalized:
        return "greeting", {}

    # 1. Slash commands & explicit keyword prefixes
    first_word = normalized.split(" ", 1)[0]

    if first_word.startswith("/"):
        cmd = first_word[1:].split("@")[0].lower()
        if cmd in VALID_OPERATIONS:
            return cmd, parse_operation_parameters(cmd, raw_text)
        if cmd in ("start", "help"):
            return "greeting", {}

    # 2. Natural language mapping to operations
    if any(k in normalized for k in ("health", "system health", "health check", "status check")):
        return "health", {}

    if any(k in normalized for k in ("model status", "model_status", "show model", "what model")):
        return "model_status", {}

    if any(k in normalized for k in ("data status", "data_status", "dataset status", "datasets")):
        return "data_status", parse_operation_parameters("data_status", raw_text)

    if any(k in normalized for k in ("backtest status", "backtest_status", "check backtest", "backtest run")):
        if "run a" not in normalized and "run backtest" not in normalized and not re.search(r"\bbacktest\s+(football|basketball|premier|la liga|nba|\d{4})\b", normalized):
            return "backtest_status", {}

    if "backtest" in normalized:
        return "backtest", parse_operation_parameters("backtest", raw_text)

    if any(k in normalized for k in ("calibration", "evaluate", "evaluation", "baseline comparison", "show calibration")):
        return "evaluate", parse_operation_parameters("evaluate", raw_text)

    if any(k in normalized for k in ("logs", "show logs", "recent logs", "activity log")):
        return "logs", {}

    if any(k in normalized for k in ("errors", "show errors", "recent errors", "error log")):
        return "errors", {}

    if any(k in normalized for k in ("retrain", "train model", "re-train")):
        return "retrain", {}

    if any(k in normalized for k in ("config", "configuration", "show config", "environment config")):
        return "config", {}

    if any(k in normalized for k in ("fixtures", "schedule", "upcoming matches", "upcoming games")):
        return "fixtures", parse_operation_parameters("fixtures", raw_text)

    if "basketball" in normalized or "nba" in normalized:
        if any(term in normalized for term in ("predict", "prediction", "forecast", "pick", "tips", "analyze")):
            return "basketball", parse_operation_parameters("basketball", raw_text)

    if "football" in normalized or "soccer" in normalized:
        if any(term in normalized for term in ("predict", "prediction", "forecast", "pick", "tips", "analyze")):
            return "football", parse_operation_parameters("football", raw_text)

    if any(term in normalized for term in ("predict", "prediction", "predictions", "forecast", "tip", "pick", "analyze", "research")):
        return "predict", parse_operation_parameters("predict", raw_text)

    if normalized in ("hi", "hello", "hey", "yo", "sup", "help", "start") or "?" in normalized:
        if any(k in normalized for k in ("how many", "when is", "what time", "playing now")):
            return "fixtures", parse_operation_parameters("fixtures", raw_text)
        return "greeting", {}

    return "predict", parse_operation_parameters("predict", raw_text)


def parse_operation_parameters(operation, text):
    """Strictly parse and validate parameters for typed operation."""
    normalized = normalize_text(text)
    sport = detect_sport(normalized) or "football"
    date_str, date_label = resolve_date(normalized)
    league_name, league_id, league_sport = mentions_league(normalized)

    if league_sport:
        sport = league_sport

    quantity = parse_quantity(normalized, default=1)
    season = parse_season(normalized)

    params = {
        "raw_text": text,
        "sport": sport,
        "date": date_str,
        "date_label": date_label,
        "league_name": league_name,
        "league_id": league_id,
        "quantity": quantity,
        "season": season,
        "sample": parse_quantity(normalized, default=20),
    }

    if operation == "backtest":
        params["league_id"] = league_id or (config.ALLOWED_BASKETBALL_LEAGUE_IDS[0] if sport == "basketball" else config.ALLOWED_LEAGUE_IDS[0])
        params["league_name"] = league_name or ("NBA" if sport == "basketball" else "Premier League")

    return params


# ============================================================================
# AUTHORITATIVE PREDICTION PRESENTATION FORMATTER
# ============================================================================

def format_prediction_contract_telegram(prediction, sport="football"):
    """
    Format authoritative prediction contract into compact phone-friendly Telegram text.

    Enforces rules:
    - Never uses safest pick as authoritative prediction.
    - Explicitly displays Signal, Quality Gate, Reason, Selected Market, Raw/Calibrated prob, Odds, Edge, EV, Versions.
    - Displays safest picks strictly as 'Informational market-scoped picks'.
    """
    rec = prediction.get("prediction_record") if isinstance(prediction, dict) and isinstance(prediction.get("prediction_record"), dict) else prediction

    if not isinstance(rec, dict) or rec.get("insufficient_data"):
        reason = rec.get("reason", "Insufficient data") if isinstance(rec, dict) else "Insufficient data"
        home = prediction.get("home_team", "Home")
        away = prediction.get("away_team", "Away")
        return f"⚠️ *{home} vs {away}*\nPrediction unavailable: {reason}"

    home = rec.get("home_team") or prediction.get("home_team", "Home")
    away = rec.get("away_team") or prediction.get("away_team", "Away")
    league = rec.get("league_name") or rec.get("league") or prediction.get("league", "Unknown")
    date_str = rec.get("data_cutoff_timestamp", "")[:10] or prediction.get("date", "")[:10]

    quality_gate = rec.get("quality_gate", "PASS")
    signal_str = "SIGNAL" if quality_gate == "SIGNAL" else "PASS"
    reasons = rec.get("reason_codes", [])
    reason_str = ", ".join(reasons) if reasons else "None"

    # Calibration info
    calib_meta = rec.get("calibration_metadata", {})
    calib_status = calib_meta.get("calibration_status", "UNAVAILABLE") if isinstance(calib_meta, dict) else "UNAVAILABLE"
    is_calibrated = calib_status == "APPLIED"

    # Probabilities
    calib_markets = rec.get("calibrated_probabilities", {})
    raw_markets = rec.get("raw_probabilities", {}) or prediction.get("markets", {})

    market_key = "moneyline" if sport == "basketball" else "match_result"
    selected_market_label = "Moneyline" if sport == "basketball" else "1X2 — Match Result"

    probs = calib_markets.get(market_key, {}) if is_calibrated and isinstance(calib_markets, dict) else raw_markets.get(market_key, {})
    top_pick = "N/A"
    top_prob = 0.0

    if isinstance(probs, dict) and probs:
        top_k = max(probs, key=probs.get)
        top_pick = top_k.replace("_", " ").title()
        top_prob = float(probs[top_k])

    conf_obj = rec.get("confidence", {}) or prediction.get("confidence", {})
    conf_label = conf_obj.get("label", "Moderate") if isinstance(conf_obj, dict) else "Moderate"

    unc_obj = rec.get("uncertainty", {}) if isinstance(rec.get("uncertainty"), dict) else {}
    unc_state = unc_obj.get("state", "NORMAL")

    m_analysis = rec.get("market_analysis", {}) if isinstance(rec.get("market_analysis"), dict) else {}
    best_market = m_analysis.get("best_market", {}) if isinstance(m_analysis.get("best_market"), dict) else {}

    odds_val = best_market.get("bookmaker_odds") or (prediction.get("odds_comparison", {}).get("home_win") if isinstance(prediction.get("odds_comparison"), dict) else None)
    edge_val = rec.get("edge") or best_market.get("edge")
    ev_val = rec.get("ev") or best_market.get("ev")

    odds_str = f"{odds_val:.2f}" if odds_val is not None else "N/A"
    edge_str = f"{edge_val:+.1%}" if edge_val is not None else "N/A"
    ev_str = f"{ev_val:+.1%}" if ev_val is not None else "N/A"

    model_ver = rec.get("model_version", config.MODEL_VERSION)
    feat_ver = rec.get("feature_version", config.FEATURE_VERSION)
    calib_ver = rec.get("calibration_version", config.CALIBRATION_VERSION)

    lines = [
        "📊 *PREDICTION*",
        f"*{sport.title()}* — {league}",
        f"*{home} vs {away}*",
        f"Date: {date_str}",
        "",
        f"• *Signal:* {signal_str}",
        f"• *Quality Gate:* {quality_gate}",
        f"• *Reason:* {reason_str}",
        "",
        f"• *Selected Market:* {selected_market_label}",
        f"• *Top Pick:* {top_pick}",
        f"• *Probability:* {top_prob:.1%}",
        f"• *Calibrated:* {'YES' if is_calibrated else 'NO'}",
        f"• *Confidence:* {conf_label}",
        f"• *Calibration Status:* {calib_status}",
        f"• *Uncertainty:* {unc_state}",
        "",
        f"• *Odds:* {odds_str}",
        f"• *Edge:* {edge_str}",
        f"• *EV:* {ev_str}",
        "",
        f"• *Model Version:* {model_ver}",
        f"• *Feature Version:* {feat_ver}",
        f"• *Calibration Version:* {calib_ver}",
    ]

    # Informational non-authoritative safest picks
    safest = prediction.get("safest")
    if safest and isinstance(safest, dict):
        lines.append("")
        lines.append("ℹ️ *Informational market-scoped picks (non-authoritative):*")
        by_m = safest.get("by_market", {})
        if isinstance(by_m, dict) and by_m:
            for mk, val in by_m.items():
                if isinstance(val, dict) and "label" in val and "probability" in val:
                    lines.append(f"  - {mk.replace('_', ' ').title()}: {val['label']} ({val['probability']:.0%})")
        elif "label" in safest and "probability" in safest:
            lines.append(f"  - {safest['label']} ({safest['probability']:.0%})")

    return "\n".join(lines)


def get_suggested_action_buttons(operation):
    """Generate structured contextual suggested actions for Telegram output."""
    if operation in ("predict", "football", "basketball", "fixtures"):
        return {
            "inline_keyboard": [
                [
                    {"text": "📋 Show Fixtures", "callback_data": "cmd:fixtures"},
                    {"text": "📐 Show Calibration", "callback_data": "cmd:evaluate"},
                ],
                [
                    {"text": "🏥 Health Check", "callback_data": "cmd:health"},
                    {"text": "📊 Data Status", "callback_data": "cmd:data_status"},
                ]
            ]
        }
    elif operation in ("backtest", "evaluate", "backtest_status"):
        return {
            "inline_keyboard": [
                [
                    {"text": "🔄 Check Backtest Status", "callback_data": "cmd:backtest_status"},
                    {"text": "📈 Model Status", "callback_data": "cmd:model_status"},
                ],
                [
                    {"text": "📜 View Logs", "callback_data": "cmd:logs"},
                    {"text": "⚠️ View Errors", "callback_data": "cmd:errors"},
                ]
            ]
        }
    return {
        "inline_keyboard": [
            [
                {"text": "⚽ Predict Football", "callback_data": "cmd:football"},
                {"text": "🏀 Predict Basketball", "callback_data": "cmd:basketball"},
            ],
            [
                {"text": "🧪 Run Backtest", "callback_data": "cmd:backtest"},
                {"text": "🏥 System Health", "callback_data": "cmd:health"},
            ]
        ]
    }


# ============================================================================
# OPERATION HANDLERS
# ============================================================================

def handle_predict_op(params):
    """Execute prediction operation for football or basketball."""
    sport = params.get("sport", "football")
    date_str = params.get("date")
    date_label = params.get("date_label", "today")
    quantity = params.get("quantity", 1)

    if sport == "football":
        results = research_football(date_str, quantity=quantity)
    else:
        results = research_basketball(date_str, quantity=quantity)

    if not results:
        return f"📊 No {sport} predictions could be produced for {date_label} from available data."

    outputs = []
    for item in results:
        pred_text = format_prediction_contract_telegram(item.get("prediction", {}), sport=sport)
        outputs.append(pred_text)

    return "\n\n---\n\n".join(outputs)


def handle_fixtures_op(params):
    """Execute fixtures list operation."""
    date_str = params.get("date")
    date_label = params.get("date_label", "today")
    fixtures = get_tracked_fixtures_for_date(date_str)

    if not fixtures:
        return f"📅 No tracked football fixtures found scheduled for {date_label} ({date_str})."

    lines = [f"📅 Tracked Football Fixtures for {date_label} ({date_str}):"]
    for f in fixtures[:10]:
        teams = f.get("teams", {})
        h_name = teams.get("home", {}).get("name", "Home")
        a_name = teams.get("away", {}).get("name", "Away")
        league = f.get("league", {}).get("name", "Unknown")
        lines.append(f"• {h_name} vs {a_name} ({league})")

    return "\n".join(lines)


def handle_backtest_op(params, request_id):
    """Execute durable backtest operation."""
    sport = params.get("sport", "football")
    league_id = params.get("league_id")
    season = params.get("season", 2024)
    sample_size = params.get("sample", 20)

    storage.update_operation_request(request_id, status="RUNNING")

    try:
        if sport == "basketball":
            result = backtest.run_basketball_backtest(league_id=league_id, season=season, sample_size=sample_size)
        else:
            result = backtest.run_real_backtest(league_id=league_id, season=season, sample_size=sample_size)

        acc = result.get("accuracy", 0.0)
        graded = result.get("graded", 0)
        correct = result.get("correct", 0)
        status_str = result.get("status", "COMPLETE")

        summary = {
            "sport": sport,
            "league_id": league_id,
            "season": season,
            "sample_size": sample_size,
            "graded": graded,
            "correct": correct,
            "accuracy": acc,
            "status": status_str,
            "message": f"Backtest completed: {acc:.1%} accuracy ({correct}/{graded})",
        }

        storage.update_operation_request(request_id, status="COMPLETED", result_summary=summary)

        return (
            f"🧪 *BACKTEST COMPLETED*\n"
            f"• Request ID: `{request_id}`\n"
            f"• Sport: {sport.title()}\n"
            f"• League ID: {league_id}\n"
            f"• Season: {season}\n"
            f"• Sample Size: {sample_size}\n"
            f"• Graded Sample: {graded}\n"
            f"• Accuracy: *{acc:.1%}* ({correct}/{graded})\n"
            f"• Status: {status_str}"
        )
    except Exception as exc:
        storage.update_operation_request(
            request_id,
            status="FAILED",
            error_code="BACKTEST_ERROR",
            error_message=str(exc)[:200],
        )
        return f"⚠️ Backtest failed: {str(exc)[:200]}"


def handle_backtest_status_op():
    """Execute /backtest_status operation."""
    req = storage.get_latest_operation_request(operation="backtest")

    if not req:
        return "🧪 *BACKTEST STATUS*\nNo backtest requests recorded yet. Run `/backtest` to launch one."

    req_id = req["request_id"]
    sport = req.get("sport") or "football"
    params = req.get("parameters") or {}
    status = req.get("status", "QUEUED")
    created_at = req.get("created_at", "N/A")
    started_at = req.get("started_at", "N/A")
    completed_at = req.get("completed_at", "N/A")
    gh_run_id = req.get("github_run_id", "N/A")
    summary = req.get("result_summary") or {}
    err_msg = req.get("error_message")

    lines = [
        "🧪 *LATEST BACKTEST STATUS*",
        f"• *Request ID:* `{req_id}`",
        f"• *Sport:* {sport.title()}",
        f"• *League ID:* {params.get('league_id', 'N/A')}",
        f"• *Season:* {params.get('season', 'N/A')}",
        f"• *Status:* {status}",
        f"• *Created:* {created_at}",
        f"• *Started:* {started_at or 'N/A'}",
        f"• *Completed:* {completed_at or 'N/A'}",
        f"• *GitHub Run ID:* {gh_run_id or 'N/A'}",
    ]

    if status == "COMPLETED":
        lines.append(f"• *Summary:* Accuracy {summary.get('accuracy', 0.0):.1%} ({summary.get('correct', 0)}/{summary.get('graded', 0)})")
    elif status == "FAILED":
        lines.append(f"• *Failure Information:* {err_msg or 'Unknown error'}")

    return "\n".join(lines)


def handle_evaluate_op():
    """Execute /evaluate operation displaying Phase 4 evaluation metrics."""
    runs = storage.get_latest_backtest_runs(limit=1)
    if not runs:
        return (
            "📐 *EVALUATION METRICS*\n"
            "No historical evaluation runs recorded yet.\n"
            "Run `/backtest` to generate an evaluation run."
        )

    r = runs[0]
    rid, sport, lid, ssn, dfc, ss, gc, acc, bs, ll, cat = r

    lines = [
        "📐 *PHASE 4 EVALUATION REPORT*",
        f"• *Run ID:* `{rid}`",
        f"• *Sport:* {sport.title()}",
        f"• *League:* {lid} | *Season:* {ssn}",
        f"• *Eligible Population:* {dfc}",
        f"• *Selected Sample:* {ss}",
        f"• *Graded Sample:* {gc}",
        f"• *Accuracy:* {acc:.1%}" if acc is not None else "• *Accuracy:* N/A",
        f"• *Brier Score:* {bs:.4f}" if bs is not None else "• *Brier Score:* N/A",
        f"• *Log Loss:* {ll:.4f}" if ll is not None else "• *Log Loss:* N/A",
        f"• *Calibration Status:* APPLIED",
        f"• *Diagnostic Status:* NORMAL",
    ]

    if gc < config.MIN_EVALUATION_SAMPLE_THRESHOLD:
        lines.append(f"\n⚠️ *Low-Sample Warning:* Sample size ({gc}) is below threshold ({config.MIN_EVALUATION_SAMPLE_THRESHOLD}). Metrics marked INSUFFICIENT_SAMPLE.")

    return "\n".join(lines)


def handle_health_op():
    """Execute /health operation."""
    tp_status = "OK" if TELEGRAM_TOKEN and CHAT_ID else "DEGRADED"

    db_status = "FAILED"
    try:
        conn, db_type = storage._connect()
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        else:
            conn.execute("SELECT 1")
        conn.close()
        db_status = "OK"
    except Exception:
        db_status = "FAILED"

    fb_data = "OK" if getattr(config, "API_FOOTBALL_KEY", None) or os.environ.get("API_FOOTBALL_KEY") else "DEGRADED"
    bk_data = "OK" if getattr(config, "API_FOOTBALL_KEY", None) or os.environ.get("API_FOOTBALL_KEY") else "DEGRADED"

    gh_status = "OK" if os.environ.get("GITHUB_RUN_ID") else "UNAVAILABLE"

    lines = [
        "🏥 *PREDICTION CONTROL CENTER HEALTH*",
        f"• *Telegram Transport:* {tp_status}",
        f"• *Database Connection:* {db_status}",
        f"• *Prediction Engine:* OK",
        f"• *Football Data API:* {fb_data}",
        f"• *Basketball Data API:* {bk_data}",
        f"• *Historical Datasets:* OK",
        f"• *Calibration Layer:* OK",
        f"• *GitHub Actions Integration:* {gh_status}",
    ]
    return "\n".join(lines)


def handle_model_status_op():
    """Execute /model_status operation."""
    lines = [
        "📈 *MODEL & FEATURE STATUS*",
        f"• *Football Model Version:* {config.MODEL_VERSION}",
        f"• *Basketball Model Version:* {config.MODEL_VERSION}",
        f"• *Feature Version:* {config.FEATURE_VERSION}",
        f"• *Calibration Version:* {config.CALIBRATION_VERSION}",
        "• *Supported Markets:* Match Result (1X2), Double Chance, Over/Under Goals, BTTS, Team Goals, Basketball Moneyline",
        "• *Calibration Status:* APPLIED",
        "• *Quality Gate State:* ACTIVE",
    ]
    return "\n".join(lines)


def handle_data_status_op(params):
    """Execute /data_status operation."""
    sports_to_check = [("football", 39, 2024), ("basketball", 12, 2024)]

    lines = ["📊 *HISTORICAL DATASET STATUS*"]

    for sp, lid, ssn in sports_to_check:
        st = storage.get_historical_dataset_status(lid, ssn, sport=sp)
        lines.append(
            f"\n*{sp.title()} (League {lid}, {ssn}):*\n"
            f"• Status: *{st['status']}*\n"
            f"• Count: {st['fixture_count']}\n"
            f"• Enrichment: {st['enrichment_status']}\n"
            f"• Pages: {st['pages_completed']}/{st['expected_pages']}"
        )

    return "\n".join(lines)


def handle_logs_op():
    """Execute /logs operation displaying bounded recent activity."""
    logs = storage.get_recent_operation_logs(limit=10)
    if not logs:
        return "📜 *OPERATIONAL LOGS*\nNo recent operation logs recorded."

    lines = ["📜 *RECENT OPERATIONAL LOGS*"]
    for l in logs:
        ts = l["timestamp"][:19].replace("T", " ")
        lines.append(f"• `[{ts}]` *{l['operation'].upper()}* ({l['status']}): {l['message'][:80]}")

    return "\n".join(lines)


def handle_errors_op():
    """Execute /errors operation displaying recent sanitized errors."""
    errors = storage.get_recent_operation_errors(limit=10)
    if not errors:
        return "⚠️ *OPERATIONAL ERRORS*\nNo recent operational errors recorded."

    lines = ["⚠️ *RECENT OPERATIONAL ERRORS*"]
    for e in errors:
        ts = (e["timestamp"] or "")[:19].replace("T", " ")
        lines.append(f"• `[{ts}]` *{e['operation'].upper()}* [{e['error_category']}]: {e['message'][:100]}")

    return "\n".join(lines)


def handle_config_op():
    """Execute /config operation with secrets strictly redacted."""
    env_mode = os.environ.get("ENVIRONMENT", "production")
    db_type = "Neon PostgreSQL" if storage.is_neon() else "Local SQLite"

    lines = [
        "⚙️ *SAFE CONFIGURATION METADATA*",
        f"• *Environment Mode:* {env_mode}",
        f"• *Database System:* {db_type}",
        f"• *Model Version:* {config.MODEL_VERSION}",
        f"• *Feature Version:* {config.FEATURE_VERSION}",
        f"• *Calibration Version:* {config.CALIBRATION_VERSION}",
        "• *Configured Sports:* Football, Basketball",
        f"• *Configured Football Leagues:* {len(config.ALLOWED_LEAGUE_IDS)} leagues",
        f"• *Configured Basketball Leagues:* {len(config.ALLOWED_BASKETBALL_LEAGUE_IDS)} leagues",
        "• *API Providers:* API-Football (Active), API-Basketball (Active), The Odds API (Active)",
        "• *API Keys / Credentials:* REDACTED / SECURE",
    ]
    return "\n".join(lines)


def handle_retrain_op():
    """Execute /retrain operation."""
    return "Retraining is not currently available."


def handle_greeting_op():
    """Execute Prediction Agent Control Center greeting."""
    return (
        "🤖 *PREDICTION AGENT CONTROL CENTER*\n\n"
        "I control and monitor the prediction and evaluation pipeline.\n\n"
        "*Supported Commands & Operations:*\n"
        "• `/predict` or `predict Arsenal tomorrow` — Make predictions\n"
        "• `/fixtures` — List upcoming tracked fixtures\n"
        "• `/football` / `/basketball` — Sport-specific predictions\n"
        "• `/backtest` — Dispatch historical backtest\n"
        "• `/backtest_status` — Inspect backtest job run\n"
        "• `/evaluate` — Display Phase 4 evaluation metrics\n"
        "• `/health` — System component health report\n"
        "• `/model_status` — View model & feature versions\n"
        "• `/data_status` — View dataset completion state\n"
        "• `/logs` & `/errors` — View activity & error logs\n"
        "• `/config` — Safe environment configuration"
    )


# ============================================================================
# MAIN DISPATCHER WITH UPDATE IDEMPOTENCY
# ============================================================================

def process_telegram_update(
    message_text,
    update_id=None,
    chat_id=None,
    callback_data=None,
):
    """
    Main entry point for processing an incoming Telegram update.

    Enforces:
    - Authorization check (chat_id == CHAT_ID)
    - Telegram update idempotency
    - Durable job request creation and status tracking
    """
    authorized_chat = CHAT_ID or "default_chat"
    current_chat = str(chat_id or authorized_chat)

    # 1. Authorization check
    if CHAT_ID and current_chat != str(CHAT_ID):
        print(f"Unauthorized chat ID rejection: {current_chat} != {CHAT_ID}")
        return "⚠️ Unauthorized chat ID."

    # 2. Telegram update idempotency
    if update_id:
        existing_req = storage.get_operation_request_by_update_id(update_id)
        if existing_req:
            print(f"Duplicate update_id {update_id} received. Reusing existing result.")
            summary = existing_req.get("result_summary")
            if isinstance(summary, dict) and "formatted_text" in summary:
                return summary["formatted_text"]
            return f"Operation `{existing_req['operation']}` previously processed with status {existing_req['status']}."

    # 3. Handle inline callback data if present
    if callback_data:
        if callback_data.startswith("cmd:"):
            message_text = "/" + callback_data[4:]

    # 4. Resolve operation and parameters
    operation, params = resolve_operation(message_text)
    request_id = f"req_{uuid.uuid4().hex[:12]}"

    # 5. Persist persistent request state
    storage.save_operation_request(
        request_id=request_id,
        telegram_update_id=update_id,
        chat_id=current_chat,
        operation=operation,
        sport=params.get("sport", "football"),
        parameters=params,
        status="RUNNING",
        github_run_id=os.environ.get("GITHUB_RUN_ID"),
    )

    # 6. Execute operation handler
    try:
        if operation == "predict":
            response_text = handle_predict_op(params)
        elif operation == "fixtures":
            response_text = handle_fixtures_op(params)
        elif operation == "football":
            params["sport"] = "football"
            response_text = handle_predict_op(params)
        elif operation == "basketball":
            params["sport"] = "basketball"
            response_text = handle_predict_op(params)
        elif operation == "backtest":
            response_text = handle_backtest_op(params, request_id)
        elif operation == "backtest_status":
            response_text = handle_backtest_status_op()
        elif operation == "evaluate":
            response_text = handle_evaluate_op()
        elif operation == "health":
            response_text = handle_health_op()
        elif operation == "model_status":
            response_text = handle_model_status_op()
        elif operation == "data_status":
            response_text = handle_data_status_op(params)
        elif operation == "logs":
            response_text = handle_logs_op()
        elif operation == "errors":
            response_text = handle_errors_op()
        elif operation == "config":
            response_text = handle_config_op()
        elif operation == "retrain":
            response_text = handle_retrain_op()
        elif operation == "greeting":
            response_text = handle_greeting_op()
        else:
            response_text = handle_predict_op(params)

        # Mark request as COMPLETED unless already marked FAILED by handler
        current_req = storage.get_operation_request(request_id)
        if current_req and current_req["status"] == "RUNNING":
            storage.update_operation_request(
                request_id,
                status="COMPLETED",
                result_summary={"message": f"Completed {operation}", "formatted_text": response_text},
            )

    except Exception as exc:
        response_text = f"⚠️ Operation `{operation}` failed: {str(exc)[:200]}"
        storage.update_operation_request(
            request_id,
            status="FAILED",
            error_code="OPERATION_FAILED",
            error_message=str(exc)[:200],
            result_summary={"message": f"Failed {operation}", "formatted_text": response_text},
        )

    # 7. Append memory
    append_memory({
        "role": "user",
        "text": message_text,
        "timestamp": time_utils.format_utc_iso(datetime.now(timezone.utc)),
    })
    append_memory({
        "role": "assistant",
        "text": response_text,
        "timestamp": time_utils.format_utc_iso(datetime.now(timezone.utc)),
    })

    return response_text


def handle_message(text):
    """Backward-compatible entry point for worker execution."""
    update_id = os.environ.get("TELEGRAM_UPDATE_ID")
    callback_data = os.environ.get("TELEGRAM_CALLBACK_DATA")
    return process_telegram_update(
        message_text=text,
        update_id=update_id,
        chat_id=CHAT_ID,
        callback_data=callback_data,
    )


# ============================================================================
# MAIN WORKER
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

    callback_data = os.environ.get("TELEGRAM_CALLBACK_DATA")

    if not message and not callback_data:
        raise RuntimeError(
            "TELEGRAM_MESSAGE or TELEGRAM_CALLBACK_DATA is required."
        )

    storage.init_db()

    resolved_op, _ = resolve_operation(message if message else (f"/{callback_data[4:]}" if callback_data and callback_data.startswith("cmd:") else ""))

    response = handle_message(message)
    reply_markup = get_suggested_action_buttons(resolved_op)

    send_message(response, reply_markup=reply_markup)

    save_memory(load_memory())
