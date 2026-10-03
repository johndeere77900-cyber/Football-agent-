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
    88: 1.50, 94: 1.35,
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

# --- Evaluation Thresholds ----------------------------------------------------
MIN_EVALUATION_SAMPLE_THRESHOLD = 30

# --- Storage ------------------------------------------------------------
DB_PATH = os.environ.get("FOOTBALL_AGENT_DB", "predictions.db")
NEON_DATABASE_URL = os.environ.get("NEON_DATABASE_URL")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
REQUIRE_NEON = os.environ.get("REQUIRE_NEON", "false").lower() in ("true", "1", "yes")

# --- Historical Seasons Target ----------------------------------------------
TARGET_SEASONS = [2020, 2021, 2022, 2023, 2024]

# --- Canonical Team Aliases ------------------------------------------------
CANONICAL_TEAM_ALIASES = {
    # Premier League
    "manchester united fc": "manchester united",
    "manchester united": "manchester united",
    "man united": "manchester united",
    "man utd": "manchester united",
    "manchester city fc": "manchester city",
    "manchester city": "manchester city",
    "man city": "manchester city",
    "tottenham hotspur fc": "tottenham",
    "tottenham hotspur": "tottenham",
    "tottenham": "tottenham",
    "spurs": "tottenham",
    "arsenal fc": "arsenal",
    "arsenal": "arsenal",
    "chelsea fc": "chelsea",
    "chelsea": "chelsea",
    "liverpool fc": "liverpool",
    "liverpool": "liverpool",
    "newcastle united fc": "newcastle",
    "newcastle united": "newcastle",
    "newcastle": "newcastle",
    "aston villa fc": "aston villa",
    "aston villa": "aston villa",
    "west ham united fc": "west ham",
    "west ham united": "west ham",
    "west ham": "west ham",
    "brighton & hove albion fc": "brighton",
    "brighton & hove albion": "brighton",
    "brighton": "brighton",
    "wolverhampton wanderers fc": "wolves",
    "wolverhampton wanderers": "wolves",
    "wolves": "wolves",

    # La Liga
    "fc barcelona": "barcelona",
    "barcelona": "barcelona",
    "real madrid cf": "real madrid",
    "real madrid": "real madrid",
    "atletico de madrid": "atletico madrid",
    "club atletico de madrid": "atletico madrid",
    "atletico madrid": "atletico madrid",
    "athletic club": "athletic bilbao",
    "athletic bilbao": "athletic bilbao",
    "real sociedad de futbol": "real sociedad",
    "real sociedad": "real sociedad",
    "sevilla fc": "sevilla",
    "sevilla": "sevilla",
    "real betis balompie": "real betis",
    "real betis": "real betis",
    "villarreal cf": "villarreal",
    "villarreal": "villarreal",

    # Serie A
    "fc internazionale milano": "inter milan",
    "inter milan": "inter milan",
    "inter": "inter milan",
    "ac milan": "ac milan",
    "milan": "ac milan",
    "juventus fc": "juventus",
    "juventus": "juventus",
    "ss lazio": "lazio",
    "lazio": "lazio",
    "as roma": "roma",
    "roma": "roma",
    "ssc napoli": "napoli",
    "napoli": "napoli",
    "atalaanta bc": "atalanta",
    "atalanta bc": "atalanta",
    "atalanta": "atalanta",
    "acf fiorentina": "fiorentina",
    "fiorentina": "fiorentina",

    # Bundesliga
    "fc bayern munchen": "bayern munich",
    "fc bayern munich": "bayern munich",
    "bayern munich": "bayern munich",
    "bayern munchen": "bayern munich",
    "borussia dortmund": "borussia dortmund",
    "bvb": "borussia dortmund",
    "rb leipzig": "rb leipzig",
    "bayer 04 leverkusen": "bayer leverkusen",
    "bayer leverkusen": "bayer leverkusen",
    "eintracht frankfurt": "eintracht frankfurt",

    # Ligue 1
    "paris saint germain fc": "paris saint germain",
    "paris saint germain": "paris saint germain",
    "psg": "paris saint germain",
    "olympic de marseille": "marseille",
    "olympique de marseille": "marseille",
    "marseille": "marseille",
    "olympique lyonnais": "lyon",
    "lyon": "lyon",
    "as monaco fc": "monaco",
    "as monaco": "monaco",
    "monaco": "monaco",
    "losc lille": "lille",
    "lille osc": "lille",
    "lille": "lille",

    # Eredivisie & Primeira Liga
    "afc ajax": "ajax",
    "ajax": "ajax",
    "psv eindhoven": "psv",
    "psv": "psv",
    "feyenoord rotterdam": "feyenoord",
    "feyenoord": "feyenoord",
    "sl benfica": "benfica",
    "benfica": "benfica",
    "fc porto": "porto",
    "porto": "porto",
    "sporting cp": "sporting cp",
    "sporting lisbon": "sporting cp",
}

# --- League restriction (football) -----------------------------------------
ALLOWED_LEAGUE_IDS = [39, 140, 135, 78, 61, 2, 3, 5, 1, 4, 88, 94]

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
    "eredivisie": 88, "dutch eredivisie": 88,
    "primeira liga": 94, "portuguese primeira liga": 94, "liga portugal": 94,
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
    88: "soccer_netherlands_eredivisie",
    94: "soccer_portugal_primeira_liga",
}

# --- League restriction (basketball) ----------------------------------------
ALLOWED_BASKETBALL_LEAGUE_IDS = [12]
BASKETBALL_HOME_ADVANTAGE_POINTS = 3.0
BASKETBALL_ODDS_SPORT_KEY = "basketball_nba"
