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
# How many recent matches count as "current form"
RECENT_FORM_MATCHES = 8

# How many seasons back to pull for head-to-head context (1-3 recommended)
HEAD_TO_HEAD_SEASONS_BACK = 3

# Weight given to recent form vs full-season stats when estimating expected
# goals. 0.0 = ignore recent form entirely, 1.0 = ignore season stats entirely.
RECENT_FORM_WEIGHT = 0.55

# Max goals to consider per team when building the scoreline probability
# grid.
MAX_GOALS_GRID = 10

# --- Confidence flag thresholds ----------------------------------------------
# Based on the gap between the top outcome's probability and the next one.
CONFIDENCE_HIGH_GAP = 0.20     # e.g. 55% vs 35% or wider -> High
CONFIDENCE_MODERATE_GAP = 0.08  # smaller gap than this -> Toss-up

# --- Storage ------------------------------------------------------------
DB_PATH = os.environ.get("FOOTBALL_AGENT_DB", "predictions.db")

# --- Caching --------------------------------------------------------------
# How long (in hours) fetched team stats stay valid before being re-fetched.
# This is what keeps you under the free API-Football daily request cap.
CACHE_TTL_HOURS = 20
CACHE_DIR = ".api_cache"
