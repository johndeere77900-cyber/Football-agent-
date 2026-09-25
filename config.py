"""
Configuration for the football and basketball prediction agent.
"""

import os

# --- API Keys -------------------------------------------------------------
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "PUT_YOUR_API_FOOTBALL_KEY_HERE")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "PUT_YOUR_ODDS_API_KEY_HERE")

# --- API endpoints ----------------------------------------------------------
API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
API_BASKETBALL_BASE_URL = "https://v1.basketball.api-sports.io"
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"

# --- Model settings (football) ----------------------------------------------
RECENT_FORM_MATCHES = 8
HEAD_TO_HEAD_SEASONS_BACK = 3
RECENT_FORM_WEIGHT = 0.55
MAX_GOALS_GRID = 10

# --- Confidence flag thresholds ----------------------------------------------
CONFIDENCE_HIGH_GAP = 0.20
CONFIDENCE_MODERATE_GAP = 0.08

# --- Storage ------------------------------------------------------------
DB_PATH = os.environ.get("FOOTBALL_AGENT_DB", "predictions.db")

# --- Caching --------------------------------------------------------------
CACHE_TTL_HOURS = 20
CACHE_DIR = ".api_cache"

# --- League restriction (football) -----------------------------------------
ALLOWED_LEAGUE_IDS = [
    39, 140, 135, 78, 61, 2, 3, 5, 1, 4,
]

LEAGUE_NAME_TO_ID = {
    "premier league": 39, "english premier league": 39, "epl": 39,
    "la liga": 140, "spanish la liga": 140,
    "serie a": 135, "italian serie a": 135,
    "bundesliga": 78, "german bundesliga": 78,
    "ligue 1": 61, "french ligue 1": 61,
    "champions league": 2, "uefa champions league": 2, "ucl": 2,
    "europa league": 3, "uefa europa league": 3, "uel": 3,
    "nations league": 5, "uefa nations league": 5,
    "world cup": 1, "fifa world cup": 1,
    "euro championship": 4, "uefa euro championship": 4,
    "european championship": 4, "euros": 4,
}

# Maps our internal league IDs to The Odds API's own naming for each
# competition. Leagues not listed here simply won't have odds available
# (e.g. Nations League isn't covered by The Odds API).
LEAGUE_ID_TO_ODDS_SPORT_KEY = {
    39: "soccer_epl",
    140: "soccer_spain_la_liga",
    135: "soccer_italy_serie_a",
    78: "soccer_germany_bundesliga",
    61: "soccer_france_ligue_one",
    2: "soccer_uefa_champs_league",
    3: "soccer_uefa_europa_league",
    1: "soccer_fifa_world_cup",
}

# --- League restriction (basketball) ----------------------------------------
ALLOWED_BASKETBALL_LEAGUE_IDS = [12]
BASKETBALL_HOME_ADVANTAGE_POINTS = 3.0
BASKETBALL_ODDS_SPORT_KEY = "basketball_nba"
