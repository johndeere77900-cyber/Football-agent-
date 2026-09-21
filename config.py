"""
Configuration for the football prediction agent.

Fill in your own API keys below (or set them as environment variables
with the same names, which is safer if you ever share this code).
"""

import os

# --- API Keys -------------------------------------------------------------
# API-Football: get a free key at https://dashboard.api-football.com
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "PUT_YOUR_API_FOOTBALL_KEY_HERE")

# The Odds API: get a free key at https://the-odds-api.com
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
# Only these competitions are checked each run - keeps API usage low and
# focuses predictions on leagues you actually follow. IDs are API-Football's
# own league IDs.
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

# Lets you type a league by name (e.g. "Premier League") instead of its
# numeric ID when running predictions.
LEAGUE_NAME_TO_ID = {
    "premier league": 39,
    "la liga": 140,
    "serie a": 135,
    "bundesliga": 78,
    "ligue 1": 61,
    "champions league": 2,
    "europa league": 3,
    "nations league": 5,
    "world cup": 1,
    "euro championship": 4,
    "euros": 4,
}
