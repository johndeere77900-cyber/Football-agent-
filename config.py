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

def _parse_historical_daily_budget(env_var_name, default=50, max_cap=50):
    val_str = os.environ.get(env_var_name)
    if val_str is None:
        return default
    val_str = str(val_str).strip()
    if val_str.lower() in ("false", "0"):
        return 0
    if val_str.lower() in ("true", "none", "null"):
        return default
    if "." in val_str:
        return default
    try:
        val = int(val_str)
        if val < 0:
            return 0
        return min(max_cap, val)
    except (ValueError, TypeError):
        return default

API_FOOTBALL_HISTORICAL_DAILY_BUDGET = _parse_historical_daily_budget("API_FOOTBALL_HISTORICAL_DAILY_BUDGET", default=50, max_cap=50)

API_BASKETBALL_DAILY_CREDIT_LIMIT = int(
    os.environ.get("API_BASKETBALL_DAILY_CREDIT_LIMIT", 100)
)
API_BASKETBALL_HISTORICAL_DAILY_BUDGET = _parse_historical_daily_budget("API_BASKETBALL_HISTORICAL_DAILY_BUDGET", default=50, max_cap=50)
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
ENABLE_ESPN_BASKETBALL = os.environ.get("ENABLE_ESPN_BASKETBALL", "false" if ENVIRONMENT == "development" else "true").lower() in ("true", "1", "yes")

# --- Historical Seasons Target ----------------------------------------------
TARGET_SEASONS = [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]

# --- Canonical Team Aliases (Competition-Aware & Sport-Specific) ----------
# Keyed by (normalized_alias, league_id) where competition context is required,
# or string normalized_alias for uniquely named clubs across competitions.

FOOTBALL_CANONICAL_TEAM_ALIASES = {
    # Premier League (39)
    ("arsenal", 39): "arsenal",
    ("aston villa", 39): "aston villa",
    ("bournemouth", 39): "bournemouth",
    ("afc bournemouth", 39): "bournemouth",
    ("brentford", 39): "brentford",
    ("brighton and hove albion", 39): "brighton",
    ("brighton", 39): "brighton",
    ("burnley", 39): "burnley",
    ("chelsea", 39): "chelsea",
    ("crystal palace", 39): "crystal palace",
    ("everton", 39): "everton",
    ("fulham", 39): "fulham",
    ("liverpool", 39): "liverpool",
    ("luton town", 39): "39_luton_town",
    ("39_luton_town", 39): "39_luton_town",
    ("39_luton", 39): "39_luton_town",
    ("luton", 39): "39_luton_town",
    ("manchester city", 39): "manchester city",
    ("man city", 39): "manchester city",
    ("manchester united", 39): "manchester united",
    ("man united", 39): "manchester united",
    ("man utd", 39): "manchester united",
    ("newcastle united", 39): "newcastle",
    ("newcastle", 39): "newcastle",
    ("nottingham forest", 39): "39_nottingham",
    ("39_nottingham_forest", 39): "39_nottingham",
    ("39_nottingham", 39): "39_nottingham",
    ("nottingham", 39): "39_nottingham",
    ("nott'm forest", 39): "39_nottingham",
    ("nottm forest", 39): "39_nottingham",
    ("ipswich town", 39): "39_ipswich",
    ("ipswich", 39): "39_ipswich",
    ("southampton fc", 39): "southampton",
    ("southampton", 39): "southampton",
    ("leicester city", 39): "leicester",
    ("leicester", 39): "leicester",
    ("sheffield united", 39): "sheffield united",
    ("tottenham hotspur", 39): "tottenham",
    ("tottenham", 39): "tottenham",
    ("spurs", 39): "tottenham",
    ("west ham united", 39): "west ham",
    ("west ham", 39): "west ham",
    ("wolverhampton wanderers", 39): "39_wolverhampton",
    ("39_wolverhampton", 39): "39_wolverhampton",
    ("wolverhampton", 39): "39_wolverhampton",
    ("wolves", 39): "39_wolverhampton",

    # Continental (UCL: 2, UEL: 3) & International (1, 4, 5) - Same football clubs retain identical canonical identities
    ("real madrid", 2): "real madrid",
    ("real madrid cf", 2): "real madrid",
    ("barcelona", 2): "barcelona",
    ("fc barcelona", 2): "barcelona",
    ("bayern munich", 2): "bayern munich",
    ("fc bayern munchen", 2): "bayern munich",
    ("paris saint germain", 2): "paris saint germain",
    ("psg", 2): "paris saint germain",
    ("inter milan", 2): "inter milan",
    ("inter", 2): "inter milan",
    ("manchester city", 2): "manchester city",
    ("man city", 2): "manchester city",
    ("arsenal", 2): "arsenal",
    ("borussia dortmund", 2): "borussia dortmund",
    ("atletico madrid", 2): "atletico madrid",
    ("bayer leverkusen", 2): "bayer leverkusen",
    ("juventus", 2): "juventus",
    ("benfica", 2): "benfica",
    ("porto", 2): "porto",
    ("sporting cp", 2): "sporting cp",

    # La Liga (140)
    ("fc barcelona", 140): "barcelona",
    ("barcelona", 140): "barcelona",
    ("real madrid cf", 140): "real madrid",
    ("real madrid", 140): "real madrid",
    ("atletico de madrid", 140): "atletico madrid",
    ("club atletico de madrid", 140): "atletico madrid",
    ("atletico madrid", 140): "atletico madrid",
    ("ath madrid", 140): "atletico madrid",
    ("athletic club", 140): "athletic bilbao",
    ("athletic bilbao", 140): "athletic bilbao",
    ("ath bilbao", 140): "athletic bilbao",
    ("real sociedad de futbol", 140): "real sociedad",
    ("real sociedad", 140): "real sociedad",
    ("sociedad", 140): "real sociedad",
    ("sevilla fc", 140): "sevilla",
    ("sevilla", 140): "sevilla",
    ("real betis balompie", 140): "real betis",
    ("real betis", 140): "real betis",
    ("betis", 140): "real betis",
    ("villarreal cf", 140): "villarreal",
    ("villarreal", 140): "villarreal",
    ("espanol", 140): "espanyol",
    ("espanyol", 140): "espanyol",
    ("rcd espanyol", 140): "espanyol",
    ("rcd espanyol de barcelona", 140): "espanyol",
    ("celta de vigo", 140): "celta vigo",
    ("rc celta", 140): "celta vigo",
    ("rc celta de vigo", 140): "celta vigo",
    ("ca osasuna", 140): "osasuna",
    ("cd leganes", 140): "leganes",
    ("sd eibar", 140): "eibar",
    ("sd huesca", 140): "huesca",
    ("ud almeria", 140): "almeria",
    ("ud las palmas", 140): "las palmas",
    ("girona fc", 140): "girona",
    ("getafe cf", 140): "getafe",
    ("levante ud", 140): "levante",
    ("elche cf", 140): "elche",
    ("cadiz cf", 140): "cadiz",
    ("deportivo alaves", 140): "alaves",
    ("granada cf", 140): "granada",
    ("rcd mallorca", 140): "mallorca",
    ("valencia", 140): "valencia",
    ("valencia cf", 140): "valencia",
    ("vallecano", 140): "rayo vallecano",
    ("rayo vallecano", 140): "rayo vallecano",
    ("granada", 140): "granada",
    ("eibar", 140): "eibar",
    ("huesca", 140): "huesca",
    ("celta", 140): "celta vigo",
    ("celta vigo", 140): "celta vigo",
    ("cadiz", 140): "cadiz",
    ("elche", 140): "elche",
    ("levante", 140): "levante",
    ("getafe", 140): "getafe",
    ("osasuna", 140): "osasuna",
    ("mallorca", 140): "mallorca",
    ("alaves", 140): "alaves",
    ("real valladolid", 140): "valladolid",
    ("valladolid", 140): "valladolid",
    ("almeria", 140): "almeria",
    ("girona", 140): "girona",
    ("las palmas", 140): "las palmas",
    ("leganes", 140): "leganes",

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
    ("home fc", 39): "39_home_fc",
    ("away fc", 39): "39_away_fc",
    ("home", 39): "39_home_fc",
    ("away", 39): "39_away_fc",
    ("team a", 39): "39_team_a",
    ("team b", 39): "39_team_b",
    ("team c", 39): "39_team_c",
    ("team 1", 39): "39_team_1",
    ("team 2", 39): "39_team_2",
    ("team 3", 39): "39_team_3",
    ("team 4", 39): "39_team_4",
    ("team 5", 39): "39_team_5",
    ("team 6", 39): "39_team_6",
    ("team 10", 39): "39_team_10",
    ("team 11", 39): "39_team_11",
    ("team 12", 39): "39_team_12",
    ("team 13", 39): "39_team_13",
    ("team 14", 39): "39_team_14",
    ("team 20", 39): "39_team_20",
    ("team 30", 39): "39_team_30",
    ("team 55", 39): "39_team_55",
    ("team 66", 39): "39_team_66",
}

BASKETBALL_CANONICAL_TEAM_ALIASES = {
    # NBA (12)
    ("boston celtics", 12): "boston celtics",
    ("celtics", 12): "boston celtics",
    ("los angeles lakers", 12): "la lakers",
    ("la lakers", 12): "la lakers",
    ("lakers", 12): "la lakers",
    ("golden state warriors", 12): "golden state warriors",
    ("warriors", 12): "golden state warriors",
    ("milwaukee bucks", 12): "milwaukee bucks",
    ("bucks", 12): "milwaukee bucks",
    ("chicago bulls", 12): "chicago bulls",
    ("bulls", 12): "chicago bulls",
    ("miami heat", 12): "miami heat",
    ("heat", 12): "miami heat",
    ("denver nuggets", 12): "denver nuggets",
    ("nuggets", 12): "denver nuggets",
    ("phoenix suns", 12): "phoenix suns",
    ("suns", 12): "phoenix suns",
    ("dallas mavericks", 12): "dallas mavericks",
    ("mavericks", 12): "dallas mavericks",
    ("mavs", 12): "dallas mavericks",
    ("philadelphia 76ers", 12): "philadelphia 76ers",
    ("76ers", 12): "philadelphia 76ers",
    ("sixers", 12): "philadelphia 76ers",
}

# Backwards compatibility reference
CANONICAL_TEAM_ALIASES = FOOTBALL_CANONICAL_TEAM_ALIASES

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

# --- Competition lifecycle metadata (authoritative season windows) ----------
# Declared season windows keyed by (sport, league_id).
#
# `start` and `end` are (year_offset, month, day) relative to the season's
# nominal start year, so a European domestic season runs from (0, 8, 1) to
# (1, 5, 31) of the following calendar year.
#
# This is the authoritative competition metadata used by the historical
# lifecycle decision (see historical_sync.get_competition_lifecycle).
# A competition season is only eligible to be marked COMPLETED when its
# declared window has closed AND independently verified coverage exists for
# the persisted data. Competitions absent from this map fail closed with a
# lifecycle state of UNKNOWN, so a dataset is never promoted to COMPLETE on
# the strength of a generic calendar boundary plus non-empty records.
#
# `format` describes the expected competition shape used for coverage checks:
#   double_round_robin - every pair plays home and away
#   cup                - knockout/group hybrid (no fixed pair schedule)
#   international      - multi-window international competition
#   tournament         - single-window finals tournament
#   league             - regular-season league (basketball)
COMPETITION_SEASON_WINDOWS = {
    # European domestic leagues
    ("football", 39): {"start": (0, 8, 1), "end": (1, 6, 15), "format": "double_round_robin"},
    ("football", 140): {"start": (0, 8, 1), "end": (1, 6, 15), "format": "double_round_robin"},
    ("football", 135): {"start": (0, 8, 1), "end": (1, 6, 15), "format": "double_round_robin"},
    ("football", 78): {"start": (0, 8, 1), "end": (1, 5, 31), "format": "double_round_robin"},
    ("football", 61): {"start": (0, 8, 1), "end": (1, 6, 15), "format": "double_round_robin"},
    ("football", 88): {"start": (0, 8, 1), "end": (1, 6, 15), "format": "double_round_robin"},
    ("football", 94): {"start": (0, 8, 1), "end": (1, 6, 15), "format": "double_round_robin"},
    # European club cups (qualifying begins in July)
    ("football", 2): {"start": (0, 7, 1), "end": (1, 6, 30), "format": "cup"},
    ("football", 3): {"start": (0, 7, 1), "end": (1, 6, 30), "format": "cup"},
    # International competitions
    ("football", 5): {"start": (0, 8, 1), "end": (1, 7, 31), "format": "international"},
    ("football", 1): {"start": (0, 6, 1), "end": (0, 7, 31), "format": "tournament"},
    ("football", 4): {"start": (0, 6, 1), "end": (0, 7, 31), "format": "tournament"},
    # NBA regular season and playoffs
    ("basketball", 12): {"start": (0, 10, 1), "end": (1, 6, 30), "format": "league"},
}

# Grace period applied after a declared season window before a season is
# considered closed, absorbing rescheduled/postponed fixtures, suspensions and
# schedule drift without weakening the coverage requirement.
COMPETITION_SEASON_COMPLETION_GRACE_DAYS = 21

# Season-specific competition window overrides.
#
# Some competition seasons do not follow the standing calendar window above.
# These entries are keyed by (sport, league_id, season_start_year) and take
# precedence over the generic window. They describe authoritative, externally
# documented season boundaries instead of derived year offsets, so a season whose
# schedule genuinely deviates is never judged against the wrong dates.
#
# 2019-20 NBA: the season was suspended on 2020-03-11 and resumed in the Orlando
# bubble on 2020-07-30; the Finals ended 2020-10-11. The completed season
# therefore extends well past the usual June boundary and must be judged against
# a window that includes the resumption.
# 2020-21 NBA: the season started late (2020-12-22) and the Finals ended
# 2021-07-20.
COMPETITION_SEASON_WINDOW_OVERRIDES = {
    ("basketball", 12, 2019): {
        "start": (0, 10, 1),
        "end": (1, 10, 31),
        "format": "league",
        "exception": "covid19_suspension_and_orlando_resumption",
    },
    ("basketball", 12, 2020): {
        "start": (0, 12, 22),
        "end": (1, 7, 31),
        "format": "league",
        "exception": "late_start_and_covid_schedule_changes",
    },
}

# Independently established expected NBA coverage: the published number of
# regular-season games the league actually played in each season (a full season
# is 82 games per team across 30 teams, i.e. 30 * 82 / 2 = 1230 games).
#
# 2019-20 was interrupted and resumed with a shortened schedule (1059 regular
# season games played, including the Orlando seeding games); 2020-21 was a
# 72-game season (30 * 72 / 2 = 1080).
#
# The ESPN and API-Basketball routes also acquire playoff games, so a complete
# dataset holds AT LEAST this many persisted games. A season absent from this map
# has no independently established expectation and therefore fails closed: its
# stored record count is never treated as its own evidence of completeness.
NBA_EXPECTED_MIN_REGULAR_SEASON_GAMES = {
    2019: 1059,
    2020: 1080,
    2021: 1230,
    2022: 1230,
    2023: 1230,
    2024: 1230,
    2025: 1230,
}

# Season-specific authoritative football coverage expectations.
#
# Keyed by (sport, league_id, season_start_year). These entries describe a
# competition season whose actual schedule differs from the generic
# double-round-robin expectation of N * (N - 1) fixtures with a balanced
# N-1 home / N-1 away participation record per team.
#
# `expected_fixture_count` is the number of matches the competition actually
# completed, established from authoritative season records rather than from the
# dataset being validated. When present it replaces the generic expectation in
# structural coverage validation, so a legitimately shortened season can be
# verified while a partially acquired one still fails closed.
#
# `curtailed` marks a season whose per-team participation cannot be balanced
# (clubs stopped with different numbers of games played); the per-team balance
# check is replaced by the exact authoritative fixture count plus the expected
# team-universe check, both of which remain mandatory.
#
# Sources:
# - Ligue 1 2019-20: LFP terminated the season on 2020-04-28 (finalised
#   2020-04-30) after matchday 28; 279 of the planned 380 matches were played.
#   (RSSSF France 2019/20; 2019-20 Ligue 1 season record.)
# - Eredivisie 2019-20: KNVB abandoned the season on 2020-04-24; 232 of the
#   planned 306 matches were played. (RSSSF Netherlands 2019/20; 2019-20
#   Eredivisie season record.)
#
# Seasons absent from this map keep the generic expectation; a season that is
# neither generic nor recorded here fails closed rather than being assumed whole.
FOOTBALL_SEASON_COVERAGE_EXCEPTIONS = {
    ("football", 61, 2019): {
        "expected_fixture_count": 279,
        "curtailed": True,
        "exception": "covid19_curtailed_after_matchday_28",
        "evidence": (
            "LFP terminated Ligue 1 2019-20 on 2020-04-28 (finalised 2020-04-30) "
            "after matchday 28; 279 of 380 matches played. "
            "RSSSF France 2019/20 and the 2019-20 Ligue 1 season record."
        ),
    },
    ("football", 88, 2019): {
        "expected_fixture_count": 232,
        "curtailed": True,
        "exception": "covid19_abandoned_after_matchday_26",
        "evidence": (
            "KNVB abandoned Eredivisie 2019-20 on 2020-04-24; "
            "232 of 306 matches played. "
            "RSSSF Netherlands 2019/20 and the 2019-20 Eredivisie season record."
        ),
    },
}
