"""
Configuration for the football and basketball prediction agent.
"""

import os

# --- API Keys -------------------------------------------------------------
# Credentials must be supplied through the runtime environment.
# GitHub Actions is responsible for validating required credentials.
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")

# --- API endpoints ----------------------------------------------------------
API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
API_BASKETBALL_BASE_URL = "https://v1.basketball.api-sports.io"
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"

# --- API Quotas & Limits -----------------------------------------------------
API_FOOTBALL_DAILY_CREDIT_LIMIT = 100
API_FOOTBALL_HISTORICAL_DAILY_BUDGET = int(
    os.environ.get("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50)
)
API_BASKETBALL_DAILY_CREDIT_LIMIT = int(
    os.environ.get("API_BASKETBALL_DAILY_CREDIT_LIMIT", 100)
)
API_BASKETBALL_HISTORICAL_DAILY_BUDGET = int(
    os.environ.get("API_BASKETBALL_HISTORICAL_DAILY_BUDGET", 50)
)
ODDS_API_MONTHLY_REQUEST_LIMIT = 500

# --- Cache TTL Settings -----------------------------------------------------
CACHE_TTL_HOURS = 20
ODDS_CACHE_TTL_MINUTES = 15
FIXTURE_RESULT_CACHE_TTL_MINUTES = 6
CACHE_DIR = ".api_cache"
# --- Model settings (football) ----------------------------------------------
RECENT_FORM_MATCHES = 8
HEAD_TO_HEAD_MATCHES = 6

# How much each signal counts toward a team's attack/defense estimate.
# Must sum to 1.0.
SEASON_WEIGHT = 0.40
RECENT_FORM_WEIGHT = 0.45
HEAD_TO_HEAD_WEIGHT = 0.15

# How much Elo's independent view gets blended into the final
# match-result probabilities.
ELO_BLEND_WEIGHT = 0.15

# Dixon-Coles low-score correction factor.
DIXON_COLES_RHO = -0.13

MAX_GOALS_GRID = 10

# Home advantage multipliers. Home/away ratio is about 1.27 and the two
# average to 1.0, so total goals are not inflated.
HOME_ADVANTAGE_MULTIPLIER = 1.12
AWAY_DISADVANTAGE_MULTIPLIER = 0.88

# Fallback per-league averages, used only when live standings data isn't
# available.
LEAGUE_AVG_GOALS = {
    39: 1.40, 140: 1.30, 135: 1.35, 78: 1.55, 61: 1.35,
    2: 1.40, 3: 1.35, 5: 1.30, 1: 1.30, 4: 1.30,
}
LEAGUE_AVG_GOALS_FALLBACK = 1.35

# --- Phase 3 Version Identifiers --------------------------------------------
MODEL_VERSION = "v3.0.0"
FEATURE_VERSION = "v3.0.0"
CALIBRATION_VERSION = "v3.0.0"

# --- Phase 3 Quality Gate & Market Thresholds ------------------------------
MIN_FEATURE_COVERAGE = 0.50
MIN_HISTORICAL_SAMPLE = 5
MIN_EDGE_THRESHOLD = 0.02
MIN_EV_THRESHOLD = 0.00
MAX_ODDS_AGE_HOURS = 24.0

# --- Confidence flag thresholds ----------------------------------------------
CONFIDENCE_HIGH_GAP = 0.20
CONFIDENCE_MODERATE_GAP = 0.08

# --- Storage ------------------------------------------------------------
DB_PATH = os.environ.get("FOOTBALL_AGENT_DB", "predictions.db")
NEON_DATABASE_URL = os.environ.get("NEON_DATABASE_URL")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
REQUIRE_NEON = os.environ.get("REQUIRE_NEON", "false").lower() in ("true", "1", "yes")

# --- League restriction (football) -----------------------------------------
ALLOWED_LEAGUE_IDS = [39, 140, 135, 78, 61, 2, 3, 5, 1, 4]

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
