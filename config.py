"""
Configuration for the football prediction agent.

Fill in your own API keys below (or set them as environment variables
with the same names, which is safer if you ever share this code).
"""

import os

# --- API Keys -------------------------------------------------------------
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "PUT_YOUR_API_FOOTBALL_KEY_HERE")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "PUT_YOUR_ODDS_API_KEY_HERE")

# --- API endpoints ----------------------------------------------------------
API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"

# --- Model settings ---------------------------------------------------------
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

# --- League restriction ---------------------------------------------------
ALLOWED_LEAGUE_IDS = [
    39,   # Premier League (England)
    140,  # La Liga (Spain)
    135,  # Serie A (Italy)
    78,   # Bundesliga (Germany)
    61,   # Ligue 1 (France)
    2,    # UEFA Champions League
    3,    # UEFA Europa League
    5,    # UEFA Nations League
    1,    # World Cup
    4,    # Euro Championship
]

# Lets you type a league by name instead of its numeric ID. Includes common
# full/short names so typing the official name works too.
LEAGUE_NAME_TO_ID = {
    "premier league": 39,
    "english premier league": 39,
    "epl": 39,
    "la liga": 140,
    "spanish la liga": 140,
    "serie a": 135,
    "italian serie a": 135,
    "bundesliga": 78,
    "german bundesliga": 78,
    "ligue 1": 61,
    "french ligue 1": 61,
    "champions league": 2,
    "uefa champions league": 2,
    "ucl": 2,
    "europa league": 3,
    "uefa europa league": 3,
    "uel": 3,
    "nations league": 5,
    "uefa nations league": 5,
    "world cup": 1,
    "fifa world cup": 1,
    "euro championship": 4,
    "uefa euro championship": 4,
    "european championship": 4,
    "euros": 4,
}
