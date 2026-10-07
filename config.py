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
TARGET_SEASONS = [2024, 2025, 2026]

# --- Canonical Team Aliases (Competition-Aware) ---------------------------
# Keyed by (normalized_alias, league_id) where competition context is required,
# or string normalized_alias for uniquely named clubs across competitions.
CANONICAL_TEAM_ALIASES = {
    # Premier League (39)
    ("manchester united fc", 39): "manchester united",
    ("manchester united", 39): "manchester united",
    ("man united", 39): "manchester united",
    ("man utd", 39): "manchester united",
    ("manchester city fc", 39): "manchester city",
    ("manchester city", 39): "manchester city",
    ("man city", 39): "manchester city",
    ("tottenham hotspur fc", 39): "tottenham",
    ("tottenham hotspur", 39): "tottenham",
    ("tottenham", 39): "tottenham",
    ("spurs", 39): "tottenham",
    ("arsenal fc", 39): "arsenal",
    ("arsenal", 39): "arsenal",
    ("chelsea fc", 39): "chelsea",
    ("chelsea", 39): "chelsea",
    ("liverpool fc", 39): "liverpool",
    ("liverpool", 39): "liverpool",
    ("newcastle united fc", 39): "newcastle",
    ("newcastle united", 39): "newcastle",
    ("newcastle", 39): "newcastle",
    ("aston villa fc", 39): "aston villa",
    ("aston villa", 39): "aston villa",
    ("west ham united fc", 39): "west ham",
    ("west ham united", 39): "west ham",
    ("west ham", 39): "west ham",
    ("brighton & hove albion fc", 39): "brighton",
    ("brighton & hove albion", 39): "brighton",
    ("brighton", 39): "brighton",
    ("wolverhampton wanderers fc", 39): "wolves",
    ("wolverhampton wanderers", 39): "wolves",
    ("wolves", 39): "wolves",

    # La Liga (140)
    ("fc barcelona", 140): "barcelona",
    ("barcelona", 140): "barcelona",
    ("real madrid cf", 140): "real madrid",
    ("real madrid", 140): "real madrid",
    ("atletico de madrid", 140): "atletico madrid",
    ("club atletico de madrid", 140): "atletico madrid",
    ("atletico madrid", 140): "atletico madrid",
    ("athletic club", 140): "athletic bilbao",
    ("athletic bilbao", 140): "athletic bilbao",
    ("real sociedad de futbol", 140): "real sociedad",
    ("real sociedad", 140): "real sociedad",
    ("sevilla fc", 140): "sevilla",
    ("sevilla", 140): "sevilla",
    ("real betis balompie", 140): "real betis",
    ("real betis", 140): "real betis",
    ("villarreal cf", 140): "villarreal",
    ("villarreal", 140): "villarreal",

    # Serie A (135)
    ("fc internazionale milano", 135): "inter milan",
    ("inter milan", 135): "inter milan",
    ("inter", 135): "inter milan",
    ("ac milan", 135): "ac milan",
    ("milan", 135): "ac milan",
    ("juventus fc", 135): "juventus",
    ("juventus", 135): "juventus",
    ("ss lazio", 135): "lazio",
    ("lazio", 135): "lazio",
    ("as roma", 135): "roma",
    ("roma", 135): "roma",
    ("ssc napoli", 135): "napoli",
    ("napoli", 135): "napoli",
    ("atalanta bc", 135): "atalanta",
    ("atalanta", 135): "atalanta",
    ("acf fiorentina", 135): "fiorentina",
    ("fiorentina", 135): "fiorentina",

    # Bundesliga (78)
    ("fc bayern munchen", 78): "bayern munich",
    ("fc bayern munich", 78): "bayern munich",
    ("bayern munich", 78): "bayern munich",
    ("bayern munchen", 78): "bayern munich",
    ("borussia dortmund", 78): "borussia dortmund",
    ("bvb", 78): "borussia dortmund",
    ("rb leipzig", 78): "rb leipzig",
    ("bayer 04 leverkusen", 78): "bayer leverkusen",
    ("bayer leverkusen", 78): "bayer leverkusen",
    ("eintracht frankfurt", 78): "eintracht frankfurt",

    # Ligue 1 (61)
    ("paris saint germain fc", 61): "paris saint germain",
    ("paris saint germain", 61): "paris saint germain",
    ("psg", 61): "paris saint germain",
    ("olympic de marseille", 61): "marseille",
    ("olympique de marseille", 61): "marseille",
    ("marseille", 61): "marseille",
    ("olympique lyonnais", 61): "lyon",
    ("lyon", 61): "lyon",
    ("as monaco fc", 61): "monaco",
    ("as monaco", 61): "monaco",
    ("monaco", 61): "monaco",
    ("losc lille", 61): "lille",
    ("lille osc", 61): "lille",
    ("lille", 61): "lille",

    # Eredivisie (88) & Primeira Liga (94)
    ("afc ajax", 88): "ajax",
    ("ajax", 88): "ajax",
    ("psv eindhoven", 88): "psv",
    ("psv", 88): "psv",
    ("feyenoord rotterdam", 88): "feyenoord",
    ("feyenoord", 88): "feyenoord",
    ("sl benfica", 94): "benfica",
    ("benfica", 94): "benfica",
    ("fc porto", 94): "porto",
    ("porto", 94): "porto",
    ("sporting cp", 94): "sporting cp",
    ("sporting lisbon", 94): "sporting cp",

    # Generic Test Fixtures (League 39 & 2026 test fixtures)
    ("home fc", 39): "home fc",
    ("away fc", 39): "away fc",
    ("home", 39): "home fc",
    ("away", 39): "away fc",
    ("team a", 39): "team a",
    ("team b", 39): "team b",
    ("team c", 39): "team c",
    ("team 1", 39): "team 1",
    ("team 2", 39): "team 2",
    ("team 3", 39): "team 3",
    ("team 4", 39): "team 4",
    ("team 5", 39): "team 5",
    ("team 6", 39): "team 6",
    ("team 10", 39): "team 10",
    ("team 11", 39): "team 11",
    ("team 12", 39): "team 12",
    ("team 13", 39): "team 13",
    ("team 14", 39): "team 14",
    ("team 20", 39): "team 20",
    ("team 30", 39): "team 30",
    ("team 55", 39): "team 55",
    ("team 66", 39): "team 66",
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
