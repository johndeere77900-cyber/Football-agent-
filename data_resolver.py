"""
Provider-neutral Data Resolver for Football data.

Implements strict 3-tier provider hierarchy with field-level merging and reconciliation:
1. Primary: API-Football (api_football.py)
2. Secondary Fallback: football-data.org (football_data_api.py)
3. Tertiary Fallback: SoccerData (soccerdata_provider.py)
4. Permanent Memory: Neon PostgreSQL / SQLite (storage.py)

Result Status Codes:
- PRIMARY_SUCCESS: Primary provider returned valid, sufficient data (including valid empty fixture lists).
- PRIMARY_INSUFFICIENT: Primary provider response was missing or malformed.
- PRIMARY_UNAVAILABLE: Primary provider failed (exception / rate limit).
- SECONDARY_SUCCESS: Secondary provider returned valid fallback data.
- SECONDARY_INSUFFICIENT: Secondary provider response was missing or malformed.
- TERTIARY_SUCCESS: Tertiary provider returned valid fallback data.
- TERTIARY_INSUFFICIENT: Tertiary provider response was missing or malformed.
- NO_DATA: No provider supplied usable data.
- ERROR: Operational error occurred.

Data Sufficiency States:
- VALID_DATA: Usable records returned.
- VALID_EMPTY: Valid empty payload (0 matches scheduled on date).
- INSUFFICIENT_MALFORMED: Payload present but records failed structural validation.
- PROVIDER_ERROR: Provider returned None or raised error.

Provider Identity & Field-Level Reconciliation:
- Merges data at the FIELD level across providers.
- Preserves explicit provenance for every record and field.
- Disputed critical fields (e.g. score mismatch) are recorded as disputed rather than silently overwritten.
"""

import logging
from datetime import datetime, timezone
import api_football
from api_football import APIFootballError, APIFootballQuotaExhaustedError
import football_data_api
import soccerdata_provider
import time_utils

logger = logging.getLogger(__name__)

# Standard league ID to football-data.org competition code mapping
LEAGUE_TO_FD_CODE = {
    39: "PL",     # Premier League
    140: "PD",    # La Liga
    135: "SA",    # Serie A
    78: "BL1",    # Bundesliga
    61: "FL1",    # Ligue 1
    2: "CL",      # Champions League
    88: "DED",    # Eredivisie
    94: "PPD",    # Primeira Liga
}

FD_CODE_TO_LEAGUE = {v: k for k, v in LEAGUE_TO_FD_CODE.items()}

# Mapping league ID to SoccerData league code strings
LEAGUE_TO_SD_CODE = {
    39: "ENG-Premier League",
    140: "ESP-La Liga",
    135: "ITA-Serie A",
    78: "GER-Bundesliga",
    61: "FRA-Ligue 1",
    88: "NED-Eredivisie",
    94: "POR-Primeira Liga",
}


def generate_synthetic_fixture_id(provider, home_name, away_name, date_str, league_id=None, season=None, home_id=None, away_id=None):
    """
    Generate a deterministic synthetic fixture identity using provider namespace + canonical home ID + canonical away ID + competition + season + FULL event timestamp.
    Returns None if required identity fields (canonical home ID, canonical away ID, full timestamp) are unavailable.
    Does NOT create new team identities (uses resolve_canonical_team_id with auto_register=False).
    """
    if not date_str:
        return None

    ts_str = str(date_str).strip()
    if len(ts_str) < 16:  # Require full ISO timestamp (YYYY-MM-DDTHH:MM...)
        return None

    try:
        import team_identity
        c_home = team_identity.resolve_canonical_team_id(
            raw_name=home_name or (str(home_id) if home_id is not None else ""),
            provider=provider,
            provider_team_id=home_id,
            league_id=league_id,
            sport="football",
            auto_register=False
        )
        c_away = team_identity.resolve_canonical_team_id(
            raw_name=away_name or (str(away_id) if away_id is not None else ""),
            provider=provider,
            provider_team_id=away_id,
            league_id=league_id,
            sport="football",
            auto_register=False
        )
    except Exception:
        c_home, c_away = None, None

    if not c_home or not c_away:
        return None

    lid = str(league_id) if league_id is not None else "noleague"
    ssn = str(season) if season is not None else "noseason"
    raw = f"{provider}_{lid}_{ssn}_{ts_str}_{c_home}_{c_away}"
    return raw.replace(" ", "_").replace("/", "_").replace(":", "_").lower()


def _normalize_api_football_fixture(fixture, league_id=None, season=None):
    """Ensure API-Football fixture includes explicit provider identity metadata and valid synthetic ID if missing."""
    if not isinstance(fixture, dict):
        return None

    res = dict(fixture)
    fix_data = res.get("fixture", {}) if isinstance(res.get("fixture"), dict) else {}
    league_data = res.get("league", {}) if isinstance(res.get("league"), dict) else {}
    teams_data = res.get("teams", {}) if isinstance(res.get("teams"), dict) else {}

    if not league_data:
        league_data = {"id": league_id, "season": season, "name": "League"}
        res["league"] = league_data
    else:
        if not league_data.get("id") and league_id:
            league_data["id"] = league_id
        if not league_data.get("season") and season:
            league_data["season"] = season
        res["league"] = league_data

    home_name = teams_data.get("home", {}).get("name", "") if isinstance(teams_data.get("home"), dict) else ""
    away_name = teams_data.get("away", {}).get("name", "") if isinstance(teams_data.get("away"), dict) else ""
    home_id = teams_data.get("home", {}).get("id") if isinstance(teams_data.get("home"), dict) else None
    away_id = teams_data.get("away", {}).get("id") if isinstance(teams_data.get("away"), dict) else None

    fid = fix_data.get("id")
    raw_date = fix_data.get("date")

    if not fid and home_name and away_name and raw_date:
        fid = generate_synthetic_fixture_id(
            "api_football",
            home_name,
            away_name,
            raw_date,
            league_data.get("id"),
            league_data.get("season"),
            home_id=home_id,
            away_id=away_id
        )
        if fid:
            if "fixture" not in res or not isinstance(res.get("fixture"), dict):
                res["fixture"] = {"id": fid, "date": raw_date}
            else:
                res["fixture"]["id"] = fid

    res["provider_provenance"] = {
        "provider": "api_football",
        "provider_type": "primary",
        "provider_fixture_id": fid or fix_data.get("id"),
        "provider_competition_id": league_data.get("id"),
        "provider_team_ids": {"home": home_id, "away": away_id},
        "retrieved_at": time_utils.format_utc_iso(datetime.now(timezone.utc)),
    }
    return res


def _normalize_football_data_match(match, league_id, season):
    """
    Normalize secondary football-data.org match payload.
    Includes explicit provider identity and provider_team_ids namespace tracking.
    """
    if not isinstance(match, dict):
        return None

    fd_status = match.get("status", "")
    status_map = {
        "FINISHED": "FT",
        "SCHEDULED": "NS",
        "TIMED": "NS",
        "IN_PLAY": "1H",
        "PAUSED": "HT",
        "POSTPONED": "PST",
        "CANCELLED": "CANC",
        "SUSPENDED": "SUSP",
    }
    short_status = status_map.get(fd_status, "NS")

    score = match.get("score", {}) or {}
    ft = score.get("fullTime", {}) or {}

    home_team = match.get("homeTeam", {}) or {}
    away_team = match.get("awayTeam", {}) or {}

    fd_home_id = home_team.get("id")
    fd_away_id = away_team.get("id")

    return {
        "fixture": {
            "id": match.get("id"),
            "date": match.get("utcDate"),
            "status": {"short": short_status, "long": fd_status},
        },
        "league": {
            "id": league_id,
            "season": season,
            "name": match.get("competition", {}).get("name", ""),
        },
        "teams": {
            "home": {
                "id": fd_home_id,
                "name": home_team.get("shortName") or home_team.get("name"),
            },
            "away": {
                "id": fd_away_id,
                "name": away_team.get("shortName") or away_team.get("name"),
            },
        },
        "goals": {
            "home": ft.get("home"),
            "away": ft.get("away"),
        },
        "provider_provenance": {
            "provider": "football_data_org",
            "provider_type": "secondary",
            "provider_fixture_id": match.get("id"),
            "provider_competition_id": LEAGUE_TO_FD_CODE.get(league_id),
            "provider_team_ids": {"home": fd_home_id, "away": fd_away_id},
            "retrieved_at": time_utils.format_utc_iso(datetime.now(timezone.utc)),
        },
    }


def _normalize_football_data_standing(row):
    team_info = row.get("team", {}) or {}
    team_name = team_info.get("shortName") or team_info.get("name")
    return {
        "rank": row.get("position"),
        "team": {
            "id": team_info.get("id"),
            "name": team_name,
        },
        "points": row.get("points", 0),
        "goalsDiff": row.get("goalDifference", 0),
        "all": {
            "played": row.get("playedGames", 0),
            "win": row.get("won", 0),
            "draw": row.get("draw", 0),
            "lose": row.get("lost", 0),
            "goals": {
                "for": row.get("goalsFor", 0),
                "against": row.get("goalsAgainst", 0),
            },
        },
        "provider_provenance": {
            "provider": "football_data_org",
            "provider_type": "secondary",
            "provider_team_ids": {"team": team_info.get("id")},
            "retrieved_at": time_utils.format_utc_iso(datetime.now(timezone.utc)),
        },
    }


def _strict_fixture_match(primary_rec, candidate_rec):
    """
    Strict cross-provider fixture match rule.

    Requires BOTH canonical HOME and canonical AWAY identities
    to resolve and match.

    This function is a reconciliation boundary and MUST NEVER
    create canonical identities.

    Existing canonical IDs are trusted.
    Missing IDs are resolved fail-closed.
    Unresolved identities mean NOT A MATCH.
    """
    import team_identity

    if not isinstance(primary_rec, dict) or not isinstance(candidate_rec, dict):
        return False

    p_prov = primary_rec.get(
        "provider_provenance", {}
    ).get("provider", "api_football")

    c_prov = candidate_rec.get(
        "provider_provenance", {}
    ).get("provider", "fallback")

    p_league = primary_rec.get("league", {})
    c_league = candidate_rec.get("league", {})

    p_lid = p_league.get("id") if isinstance(p_league, dict) else None
    c_lid = c_league.get("id") if isinstance(c_league, dict) else None

    p_teams = primary_rec.get("teams", {})
    c_teams = candidate_rec.get("teams", {})

    if not isinstance(p_teams, dict) or not isinstance(c_teams, dict):
        return False

    p_home = p_teams.get("home", {})
    p_away = p_teams.get("away", {})
    c_home = c_teams.get("home", {})
    c_away = c_teams.get("away", {})

    if not isinstance(p_home, dict):
        p_home = {}

    if not isinstance(p_away, dict):
        p_away = {}

    if not isinstance(c_home, dict):
        c_home = {}

    if not isinstance(c_away, dict):
        c_away = {}

    p_h_id = primary_rec.get("canonical_home_id")
    if not p_h_id:
        p_h_id = team_identity.resolve_canonical_team_id(
            raw_name=p_home.get("name", "") or (
                str(p_home.get("id"))
                if p_home.get("id") is not None
                else ""
            ),
            provider=p_prov,
            provider_team_id=p_home.get("id"),
            league_id=p_lid,
            sport="football",
            auto_register=False,
        )

    p_a_id = primary_rec.get("canonical_away_id")
    if not p_a_id:
        p_a_id = team_identity.resolve_canonical_team_id(
            raw_name=p_away.get("name", "") or (
                str(p_away.get("id"))
                if p_away.get("id") is not None
                else ""
            ),
            provider=p_prov,
            provider_team_id=p_away.get("id"),
            league_id=p_lid,
            sport="football",
            auto_register=False,
        )

    c_h_id = candidate_rec.get("canonical_home_id")
    if not c_h_id:
        c_h_id = team_identity.resolve_canonical_team_id(
            raw_name=c_home.get("name", "") or (
                str(c_home.get("id"))
                if c_home.get("id") is not None
                else ""
            ),
            provider=c_prov,
            provider_team_id=c_home.get("id"),
            league_id=c_lid,
            sport="football",
            auto_register=False,
        )

    c_a_id = candidate_rec.get("canonical_away_id")
    if not c_a_id:
        c_a_id = team_identity.resolve_canonical_team_id(
            raw_name=c_away.get("name", "") or (
                str(c_away.get("id"))
                if c_away.get("id") is not None
                else ""
            ),
            provider=c_prov,
            provider_team_id=c_away.get("id"),
            league_id=c_lid,
            sport="football",
            auto_register=False,
        )

    if not p_h_id or not p_a_id:
        return False

    if not c_h_id or not c_a_id:
        return False

    return p_h_id == c_h_id and p_a_id == c_a_id


def validate_fixtures_sufficiency(fixtures_list):
    """
    Validate fixture list sufficiency.
    Returns tuple: (status_code: str, clean_list: list)
    status_code is one of:
      - VALID_EMPTY: [] (confirmed zero matches)
      - VALID_DATA: non-empty list of valid records
      - INSUFFICIENT_MALFORMED: non-empty list, but records failed validation
      - PROVIDER_ERROR: None or non-list
    """
    if fixtures_list is None or not isinstance(fixtures_list, list):
        return "PROVIDER_ERROR", []

    if len(fixtures_list) == 0:
        return "VALID_EMPTY", []

    clean = []
    for f in fixtures_list:
        if not isinstance(f, dict):
            continue
        fix_obj = f.get("fixture")
        teams_obj = f.get("teams")
        if isinstance(fix_obj, dict) and isinstance(teams_obj, dict):
            clean.append(f)

    if not clean:
        return "INSUFFICIENT_MALFORMED", []

    return "VALID_DATA", clean


def is_valid_stat_value(val):
    """Check if a statistical field value is present, non-None, and non-empty."""
    if val is None:
        return False
    if isinstance(val, (dict, list, set, tuple)):
        if not val:
            return False
        # If dict, ensure it's not full of None values (e.g. {"home": None, "away": None})
        if isinstance(val, dict):
            return any(v is not None for v in val.values())
        return True
    if isinstance(val, str):
        return bool(val.strip())
    return True


def get_missing_fixture_fields(fixture, require_stats=False):
    """
    Calculate the explicit missing field set for a fixture dict.

    Checks required basic fields:
    - fixture_id, kickoff, status, competition, season, home_team, away_team
    - score (home_goals, away_goals for finished matches)

    If require_stats=True, also checks statistical fields:
    - xG, shots, shots_on_target, corners, yellow_cards, red_cards, possession, events

    Returns set of missing field names (e.g. {"xg", "shots", "corners"}).
    """
    if not isinstance(fixture, dict):
        base_missing = {
            "fixture_id", "kickoff", "status", "competition", "season",
            "home_team", "away_team", "score"
        }
        if require_stats:
            base_missing.update({"xg", "shots", "shots_on_target", "corners", "yellow_cards", "red_cards", "possession", "events"})
        return base_missing

    missing = set()

    fix_obj = fixture.get("fixture", {}) if isinstance(fixture.get("fixture"), dict) else {}
    teams_obj = fixture.get("teams", {}) if isinstance(fixture.get("teams"), dict) else {}
    league_obj = fixture.get("league", {}) if isinstance(fixture.get("league"), dict) else {}
    goals_obj = fixture.get("goals", {}) if isinstance(fixture.get("goals"), dict) else {}
    stats_obj = fixture.get("statistics", {}) if isinstance(fixture.get("statistics"), dict) else {}

    if not fix_obj.get("id"):
        missing.add("fixture_id")
    if not fix_obj.get("date"):
        missing.add("kickoff")

    st_short = fix_obj.get("status", {}).get("short") if isinstance(fix_obj.get("status"), dict) else None
    if not st_short:
        missing.add("status")

    if not league_obj.get("id") and not league_obj.get("name"):
        missing.add("competition")
    if not league_obj.get("season"):
        missing.add("season")

    h_team = teams_obj.get("home", {}) if isinstance(teams_obj.get("home"), dict) else {}
    a_team = teams_obj.get("away", {}) if isinstance(teams_obj.get("away"), dict) else {}
    if not h_team.get("name") and not h_team.get("id"):
        missing.add("home_team")
    if not a_team.get("name") and not a_team.get("id"):
        missing.add("away_team")

    # Score checking for finished matches
    is_finished = st_short in ("FT", "AET", "PEN")
    if is_finished:
        if goals_obj.get("home") is None or goals_obj.get("away") is None:
            missing.add("score")

    if require_stats:
        xg_val = stats_obj.get("xG") if stats_obj.get("xG") is not None else stats_obj.get("xg")
        if not is_valid_stat_value(xg_val):
            missing.add("xg")
        if not is_valid_stat_value(stats_obj.get("shots")):
            missing.add("shots")
        if not is_valid_stat_value(stats_obj.get("shots_on_target")):
            missing.add("shots_on_target")
        if not is_valid_stat_value(stats_obj.get("corners")):
            missing.add("corners")

        has_yellow = is_valid_stat_value(stats_obj.get("yellow_cards")) or (
            isinstance(stats_obj.get("cards"), dict) and is_valid_stat_value(stats_obj.get("cards"))
        )
        if not has_yellow:
            missing.add("yellow_cards")

        has_red = is_valid_stat_value(stats_obj.get("red_cards")) or (
            isinstance(stats_obj.get("cards"), dict) and is_valid_stat_value(stats_obj.get("cards"))
        )
        if not has_red:
            missing.add("red_cards")

        if not is_valid_stat_value(stats_obj.get("possession")):
            missing.add("possession")

        events_val = fixture.get("events") if fixture.get("events") is not None else stats_obj.get("events")
        if not is_valid_stat_value(events_val):
            missing.add("events")

    return missing


def validate_team_stats_sufficiency(stats, min_matches=None):
    """Check if team statistics dict contains valid fixtures played (>= min_matches) and goal averages."""
    if not isinstance(stats, dict):
        return False
    try:
        if min_matches is None:
            import config
            min_matches = getattr(config, "MIN_HISTORICAL_SAMPLE", 5)

        played = stats.get("fixtures", {}).get("played", {}).get("total")
        if played is None or int(played) < min_matches:
            return False

        gf_avg = stats.get("goals", {}).get("for", {}).get("average", {}).get("total")
        ga_avg = stats.get("goals", {}).get("against", {}).get("average", {}).get("total")

        if gf_avg is None:
            gf_tot = stats.get("goals", {}).get("for", {}).get("total", {}).get("total")
            if gf_tot is not None and int(played) > 0:
                gf_avg = float(gf_tot) / float(played)

        if ga_avg is None:
            ga_tot = stats.get("goals", {}).get("against", {}).get("total", {}).get("total")
            if ga_tot is not None and int(played) > 0:
                ga_avg = float(ga_tot) / float(played)

        return gf_avg is not None and ga_avg is not None
    except Exception:
        return False


def _build_team_stats_from_matches(matches, canonical_team_id, team_id=None, provider_name="internal_db"):
    """Derive normalized team_stats from match records using canonical_team_id strictly."""
    if not canonical_team_id:
        return None

    played = 0
    goals_for_total = 0
    goals_against_total = 0

    for m in matches:
        if not isinstance(m, dict):
            continue
        st = m.get("fixture", {}).get("status", {}).get("short") if isinstance(m.get("fixture"), dict) else None
        if st is not None and st not in ("FT", "AET", "PEN"):
            continue

        h_goals = m.get("goals", {}).get("home") if isinstance(m.get("goals"), dict) else None
        a_goals = m.get("goals", {}).get("away") if isinstance(m.get("goals"), dict) else None
        if h_goals is None or a_goals is None:
            continue

        m_c_home = m.get("canonical_home_id")
        m_c_away = m.get("canonical_away_id")

        if not m_c_home or not m_c_away:
            continue

        is_home = (m_c_home == canonical_team_id)
        is_away = (m_c_away == canonical_team_id)

        if is_home:
            played += 1
            goals_for_total += int(h_goals)
            goals_against_total += int(a_goals)
        elif is_away:
            played += 1
            goals_for_total += int(a_goals)
            goals_against_total += int(h_goals)

    if played == 0:
        return None

    gf_avg = round(goals_for_total / played, 2)
    ga_avg = round(goals_against_total / played, 2)

    return {
        "fixtures": {
            "played": {"home": None, "away": None, "total": played}
        },
        "goals": {
            "for": {
                "total": {"home": None, "away": None, "total": goals_for_total},
                "average": {"home": None, "away": None, "total": str(gf_avg)}
            },
            "against": {
                "total": {"home": None, "away": None, "total": goals_against_total},
                "average": {"home": None, "away": None, "total": str(ga_avg)}
            }
        },
        "provider_provenance": {
            "provider": provider_name,
            "provider_type": "derived",
            "retrieved_at": time_utils.format_utc_iso(datetime.now(timezone.utc)),
        }
    }


def _build_team_stats_from_fd_standing(standing_row):
    """Derive normalized team_stats from a football-data.org standings row."""
    if not isinstance(standing_row, dict):
        return None

    all_obj = standing_row.get("all", {}) if isinstance(standing_row.get("all"), dict) else {}
    played = all_obj.get("played") or standing_row.get("playedGames") or 0
    if not played or played <= 0:
        return None

    gf = all_obj.get("goals", {}).get("for") if isinstance(all_obj.get("goals"), dict) else None
    if gf is None:
        gf = standing_row.get("goalsFor", 0)

    ga = all_obj.get("goals", {}).get("against") if isinstance(all_obj.get("goals"), dict) else None
    if ga is None:
        ga = standing_row.get("goalsAgainst", 0)

    gf_avg = round(gf / played, 2)
    ga_avg = round(ga / played, 2)

    return {
        "fixtures": {
            "played": {"home": None, "away": None, "total": played}
        },
        "goals": {
            "for": {
                "total": {"home": None, "away": None, "total": gf},
                "average": {"home": None, "away": None, "total": str(gf_avg)}
            },
            "against": {
                "total": {"home": None, "away": None, "total": ga},
                "average": {"home": None, "away": None, "total": str(ga_avg)}
            }
        },
        "provider_provenance": {
            "provider": "football_data_org",
            "provider_type": "secondary",
            "retrieved_at": time_utils.format_utc_iso(datetime.now(timezone.utc)),
        }
    }


def validate_standings_sufficiency(standings_list):
    """
    Validate standings list sufficiency.
    Returns tuple: (status_code: str, clean_list: list)
    status_code is one of:
      - VALID_EMPTY: [] (confirmed empty table)
      - VALID_DATA: non-empty list of valid records
      - INSUFFICIENT_MALFORMED: records failed validation
      - PROVIDER_ERROR: None or non-list
    """
    if standings_list is None or not isinstance(standings_list, list):
        return "PROVIDER_ERROR", []

    if len(standings_list) == 0:
        return "VALID_EMPTY", []

    clean = []
    for row in standings_list:
        if not isinstance(row, dict):
            continue
        team_obj = row.get("team")
        if isinstance(team_obj, dict) and team_obj.get("name"):
            clean.append(row)

    if not clean:
        return "INSUFFICIENT_MALFORMED", []

    return "VALID_DATA", clean


def reconcile_fixture_records(records):
    """
    Field-level reconciliation across multiple provider records for a fixture.

    `records` is a list of normalized fixture dicts from providers, ordered by priority
    (e.g., [api_football_record, football_data_record, soccerdata_record]).

    Rule:
    - Base structure (fixture, league, teams) comes from primary available record.
    - Fields missing in primary are populated from secondary/tertiary records.
    - Disputed fields (e.g. score mismatch) are recorded in `data_conflicts` without silently choosing one.
    - Explicit `field_provenance` tracks the source provider for each field.
    """
    if not records or not isinstance(records, list):
        return None

    valid_records = [r for r in records if isinstance(r, dict)]
    if not valid_records:
        return None

    providers_used = {
        r.get("provider_provenance", {}).get("provider")
        for r in valid_records
        if isinstance(r.get("provider_provenance"), dict)
        and r.get("provider_provenance", {}).get("provider")
    }

    primary = valid_records[0]
    result = dict(primary)

    # Initialize field provenance
    primary_prov = primary.get("provider_provenance", {}).get("provider", "unknown")
    field_provenance = {
        "fixture": primary_prov,
        "league": primary_prov,
        "teams": primary_prov,
        "goals": primary_prov,
    }
    data_conflicts = []

    # Check goal/score reconciliation across providers
    primary_goals = primary.get("goals", {})
    p_home_goals = primary_goals.get("home") if isinstance(primary_goals, dict) else None
    p_away_goals = primary_goals.get("away") if isinstance(primary_goals, dict) else None

    # Reconcile missing goals or detect conflict
    for rec in valid_records[1:]:
        rec_prov = rec.get("provider_provenance", {}).get("provider", "unknown")
        rec_goals = rec.get("goals", {})
        if not isinstance(rec_goals, dict):
            continue

        r_home = rec_goals.get("home")
        r_away = rec_goals.get("away")

        # Fill missing goals
        if p_home_goals is None and r_home is not None:
            if "goals" not in result or not isinstance(result["goals"], dict):
                result["goals"] = {}
            result["goals"]["home"] = r_home
            p_home_goals = r_home
            field_provenance["goals_home"] = rec_prov

        if p_away_goals is None and r_away is not None:
            if "goals" not in result or not isinstance(result["goals"], dict):
                result["goals"] = {}
            result["goals"]["away"] = r_away
            p_away_goals = r_away
            field_provenance["goals_away"] = rec_prov

        # Conflict check for final goals
        if (p_home_goals is not None and r_home is not None and p_home_goals != r_home) or \
           (p_away_goals is not None and r_away is not None and p_away_goals != r_away):
            data_conflicts.append({
                "field": "goals",
                "primary_value": {"home": p_home_goals, "away": p_away_goals, "provider": primary_prov},
                "conflicting_value": {"home": r_home, "away": r_away, "provider": rec_prov},
                "disputed": True,
            })
            result["disputed_score"] = True

    # Reconcile match statistics (shots, shots_on_target, corners, yellow_cards, red_cards, possession, xG)
    stats = result.get("statistics") or {}
    if not isinstance(stats, dict):
        stats = {}

    stat_keys = ["shots", "shots_on_target", "corners", "yellow_cards", "red_cards", "cards", "possession", "xG"]
    for key in stat_keys:
        if stats.get(key) is not None:
            field_provenance[f"stats_{key}"] = primary_prov
        else:
            # Look for value in secondary/tertiary providers
            for rec in valid_records[1:]:
                rec_prov = rec.get("provider_provenance", {}).get("provider", "unknown")
                rec_stats = rec.get("statistics") or {}
                if isinstance(rec_stats, dict) and rec_stats.get(key) is not None:
                    stats[key] = rec_stats[key]
                    field_provenance[f"stats_{key}"] = rec_prov
                    break

    result["statistics"] = stats
    result["field_provenance"] = field_provenance
    result["reconciliation_metadata"] = {
        "providers_used": sorted(providers_used),
        "provider_count": len(providers_used),
        "is_reconciled": len(providers_used) > 1,
    }
    if data_conflicts:
        result["data_conflicts"] = data_conflicts

    return result


class DataResolver:
    """
    DataResolver encapsulates API-Football -> football-data.org -> SoccerData 3-tier fallback hierarchy.
    Judges data sufficiency at each tier and performs field-level data reconciliation.
    """

    def __init__(self, force_fallback=False):
        self.force_fallback = force_fallback

    def get_fixtures_for_date(self, date_str, league_id=None):
        """
        Retrieves fixtures for a given date ('YYYY-MM-DD').
        NEON FIRST: Queries local database storage first. If stored records are complete
        (0 missing fields), returns stored records immediately without calling external APIs.
        Otherwise executes 3-tier gap filling (API-Football -> football-data.org -> SoccerData)
        strictly for missing fields.
        Returns tuple: (reconciled_fixtures_list, metadata)
        """
        import storage
        import team_identity

        primary_attempted = False
        fallback_used = False
        fallback_reason = None

        # 0. NEON FIRST: Query local DB for existing fixtures on date_str
        db_stored = []
        if league_id and not self.force_fallback:
            # Reconstruct season approximation from date_str
            dt_year = int(date_str[:4])
            dt_month = int(date_str[5:7])
            ssn = dt_year if dt_month >= 7 else dt_year - 1
            stored_all = storage.get_historical_fixtures(league_id, ssn)
            db_stored = [f for f in stored_all if isinstance(f, dict) and str(f.get("fixture", {}).get("date", "")).startswith(date_str)]

        if db_stored and not self.force_fallback:
            # Check if all stored fixtures have 0 missing fields
            all_complete = True
            for f in db_stored:
                gaps = get_missing_fixture_fields(f)
                if len(gaps) > 0:
                    all_complete = False
                    break

            if all_complete:
                meta = {
                    "data_source": "internal_db",
                    "provider": "internal_db",
                    "provider_type": "primary",
                    "primary_attempted": False,
                    "fallback_used": False,
                    "fallback_reason": None,
                    "resolver_status": "PRIMARY_SUCCESS",
                }
                return db_stored, meta

        primary_fixtures = []

        # Tier 1: API-Football (PRIMARY)
        if not self.force_fallback:
            primary_attempted = True
            try:
                try:
                    data = api_football.get_fixtures_by_date(date_str, league_id=league_id)
                except TypeError:
                    data = api_football.get_fixtures_by_date(date_str, league_id)

                suff_code, clean_data = validate_fixtures_sufficiency(data)
                if suff_code == "VALID_EMPTY":
                    meta = {
                        "data_source": "api_football",
                        "provider": "api_football",
                        "provider_type": "primary",
                        "primary_attempted": True,
                        "fallback_used": False,
                        "fallback_reason": None,
                        "resolver_status": "PRIMARY_SUCCESS",
                    }
                    return [], meta
                elif suff_code == "VALID_DATA":
                    primary_fixtures = [_normalize_api_football_fixture(item) for item in clean_data]
                else:
                    fallback_reason = f"Primary provider payload status: {suff_code}"
            except Exception as exc:
                fallback_reason = f"Primary provider error: {str(exc)[:100]}"
                logger.warning(f"Primary provider (api_football) failed for date {date_str}: {exc}")
        else:
            fallback_reason = "Forced fallback requested"

        # Check if primary fixtures require field-level gap filling from secondary/tertiary providers
        needs_field_fallback = self.force_fallback or (primary_fixtures and any(
            len(get_missing_fixture_fields(f, require_stats=(f.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN")))) > 0
            for f in primary_fixtures
        ))

        fd_matches = []
        sd_matches = []

        if not primary_fixtures or needs_field_fallback:
            fallback_used = True
            # Tier 2: football-data.org
            comp_code = LEAGUE_TO_FD_CODE.get(league_id) if league_id else None
            if comp_code or not league_id:
                try:
                    codes_to_query = [comp_code] if comp_code else list(LEAGUE_TO_FD_CODE.values())
                    for code in codes_to_query:
                        lid = FD_CODE_TO_LEAGUE.get(code) or league_id
                        fd_res = football_data_api.get_competition_matches(code)
                        matches = fd_res.get("matches", []) if isinstance(fd_res, dict) else (fd_res if isinstance(fd_res, list) else [])
                        for m in matches:
                            utc_date = m.get("utcDate", "")
                            if utc_date.startswith(date_str):
                                season = m.get("season", {}).get("startDate", "")[:4] or None
                                if season:
                                    try:
                                        season = int(season)
                                    except (TypeError, ValueError):
                                        season = None
                                norm_match = _normalize_football_data_match(m, lid, season)
                                if norm_match:
                                    fd_matches.append(norm_match)
                except Exception as exc:
                    logger.error(f"Secondary provider (football_data_api) failed for date {date_str}: {exc}")

            # Check remaining gaps after secondary provider
            still_has_gaps = True
            if primary_fixtures or fd_matches:
                partially_reconciled = []
                for pf in (primary_fixtures or fd_matches):
                    matching_fd = [m for m in fd_matches if _strict_fixture_match(pf, m)]
                    merged = reconcile_fixture_records([pf] + matching_fd)
                    partially_reconciled.append(merged or pf)

                still_has_gaps = any(
                    len(get_missing_fixture_fields(f, require_stats=(f.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN")))) > 0
                    for f in partially_reconciled
                )

            # Tier 3: SoccerData if gaps remain or primary/secondary empty
            if still_has_gaps or not (primary_fixtures or fd_matches):
                sd_code = LEAGUE_TO_SD_CODE.get(league_id) if league_id else None
                if sd_code or not league_id:
                    try:
                        sd_status, sd_games, sd_meta = soccerdata_provider.get_match_history_games(sd_code, None)
                        if sd_status == "SOURCE_AVAILABLE" and sd_games:
                            sd_matches = [m for m in sd_games if str(m.get("fixture", {}).get("date", "")).startswith(date_str)]
                    except Exception as exc:
                        logger.error(f"Tertiary provider (SoccerData) failed for date {date_str}: {exc}")

        # Field-Level Reconciliation Grouping by Team Identity / Date
        reconciled_fixtures = []

        if primary_fixtures:
            for pf in primary_fixtures:
                h_name = pf.get("teams", {}).get("home", {}).get("name", "")
                a_name = pf.get("teams", {}).get("away", {}).get("name", "")
                lid = pf.get("league", {}).get("id")

                c_home = team_identity.bootstrap_historical_team_identity(h_name, "api_football", pf.get("teams", {}).get("home", {}).get("id"), league_id=lid)
                c_away = team_identity.bootstrap_historical_team_identity(a_name, "api_football", pf.get("teams", {}).get("away", {}).get("id"), league_id=lid)
                pf["canonical_home_id"] = c_home
                pf["canonical_away_id"] = c_away

                # Match corresponding secondary and tertiary records for this fixture deterministically
                matching_fd = [m for m in fd_matches if _strict_fixture_match(pf, m)]
                matching_sd = [m for m in sd_matches if _strict_fixture_match(pf, m)]

                candidate_records = [pf] + matching_fd + matching_sd
                merged = reconcile_fixture_records(candidate_records)
                if merged:
                    merged["canonical_home_id"] = c_home
                    merged["canonical_away_id"] = c_away
                    reconciled_fixtures.append(merged)
        elif fd_matches or sd_matches:
            if fd_matches:
                for ff in fd_matches:
                    h_name = ff.get("teams", {}).get("home", {}).get("name", "")
                    a_name = ff.get("teams", {}).get("away", {}).get("name", "")
                    lid = ff.get("league", {}).get("id") or league_id
                    c_home = team_identity.bootstrap_historical_team_identity(h_name, "football_data_org", ff.get("teams", {}).get("home", {}).get("id"), league_id=lid)
                    c_away = team_identity.bootstrap_historical_team_identity(a_name, "football_data_org", ff.get("teams", {}).get("away", {}).get("id"), league_id=lid)
                    ff["canonical_home_id"] = c_home
                    ff["canonical_away_id"] = c_away

                    matching_sd = [m for m in sd_matches if _strict_fixture_match(ff, m)]
                    merged = reconcile_fixture_records([ff] + matching_sd)
                    if merged:
                        merged["canonical_home_id"] = c_home
                        merged["canonical_away_id"] = c_away
                        reconciled_fixtures.append(merged)
            else:
                for sf in sd_matches:
                    h_name = sf.get("teams", {}).get("home", {}).get("name", "")
                    a_name = sf.get("teams", {}).get("away", {}).get("name", "")
                    lid = sf.get("league", {}).get("id") or league_id
                    c_home = team_identity.bootstrap_historical_team_identity(h_name, "soccerdata", sf.get("teams", {}).get("home", {}).get("id"), league_id=lid)
                    c_away = team_identity.bootstrap_historical_team_identity(a_name, "soccerdata", sf.get("teams", {}).get("away", {}).get("id"), league_id=lid)
                    sf["canonical_home_id"] = c_home
                    sf["canonical_away_id"] = c_away
                    reconciled_fixtures.append(sf)

        if reconciled_fixtures:
            # Persist newly reconciled fixtures permanently into PostgreSQL if season is known
            try:
                for rf in reconciled_fixtures:
                    lid = rf.get("league", {}).get("id") or league_id
                    ssn = rf.get("league", {}).get("season")
                    if lid and ssn:
                        storage.save_historical_fixtures([rf], lid, ssn, source=rf.get("provider_provenance", {}).get("provider", "api_football"), require_completed=False)
                        if rf.get("statistics"):
                            enrichment_record = dict(rf)
                            reconciliation_metadata = enrichment_record.get("reconciliation_metadata", {})
                            has_reconciliation_provenance = (
                                isinstance(reconciliation_metadata, dict)
                                and reconciliation_metadata.get("is_reconciled") is True
                            )

                            if has_reconciliation_provenance:
                                field_provenance = enrichment_record.get("field_provenance", {})
                                enrichment_record["enrichment_provenance"] = {
                                    "record_type": "reconciled",
                                    "fields": {
                                        key: value
                                        for key, value in field_provenance.items()
                                        if key.startswith("stats_")
                                    },
                                }
                                storage.save_historical_enrichment([enrichment_record], source="reconciled")
                            else:
                                prov_source = enrichment_record.get("provider_provenance", {}).get("provider", "api_football")
                                storage.save_historical_enrichment([enrichment_record], source=prov_source)
            except Exception as exc:
                logger.warning(f"Error persisting reconciled fixtures to DB: {exc}")

            primary_prov = reconciled_fixtures[0].get("provider_provenance", {}).get("provider", "api_football")
            if "soccerdata" in primary_prov:
                norm_provider_name = "soccerdata"
            else:
                norm_provider_name = primary_prov

            if primary_fixtures and not fallback_used:
                resolver_status = "PRIMARY_SUCCESS"
            elif fd_matches and not primary_fixtures:
                resolver_status = "SECONDARY_SUCCESS"
            elif sd_matches and not primary_fixtures and not fd_matches:
                resolver_status = "TERTIARY_SUCCESS"
            else:
                resolver_status = "PRIMARY_SUCCESS" if primary_fixtures else "SECONDARY_SUCCESS"

            meta = {
                "data_source": norm_provider_name,
                "provider": norm_provider_name,
                "provider_type": "primary" if norm_provider_name == "api_football" else "fallback",
                "primary_attempted": primary_attempted,
                "fallback_used": fallback_used,
                "fallback_reason": fallback_reason,
                "resolver_status": resolver_status,
            }
            return reconciled_fixtures, meta

        meta = {
            "data_source": "none",
            "provider": "none",
            "provider_type": "none",
            "primary_attempted": primary_attempted,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
            "resolver_status": "NO_DATA",
        }
        return [], meta

    def get_team_recent_matches(self, team_id, last=10, league_id=None, season=None, team_name=None, canonical_team_id=None, cutoff_date=None):
        """
        Retrieve team historical matches using NEON-FIRST 3-tier fallback hierarchy:
        1. Neon PostgreSQL DB (via storage.get_team_historical_fixtures)
        2. API-Football (if DB history < last)
        3. football-data.org (if history < last)
        4. SoccerData (if history < last)

        Strictly enforces provider-neutral canonical identity resolution for both teams and
        strict date cutoff (`match_date < cutoff_date`).
        """
        import storage
        import team_identity

        c_id = canonical_team_id

        if not c_id and (team_name or team_id is not None):
            c_id = team_identity.resolve_canonical_team_id(
                raw_name=team_name or (
                    str(team_id) if team_id is not None else ""
                ),
                provider="api_football",
                provider_team_id=team_id,
                league_id=league_id,
                sport="football",
                auto_register=False,
            )

        if not c_id:
            return []

        def _is_valid_recent_match(match):
            if not isinstance(match, dict):
                return False

            fix_obj = match.get("fixture", {}) if isinstance(match.get("fixture"), dict) else {}
            m_date = str(fix_obj.get("date", "") or "")
            if not m_date:
                return False

            st = fix_obj.get("status", {}).get("short") if isinstance(fix_obj.get("status"), dict) else None
            if st is not None and st not in ("FT", "AET", "PEN"):
                return False

            goals = match.get("goals", {}) if isinstance(match.get("goals"), dict) else {}
            if goals.get("home") is None or goals.get("away") is None:
                return False

            if cutoff_date:
                if not time_utils.is_strictly_before(m_date, cutoff_date):
                    return False
            else:
                now_iso = time_utils.format_utc_iso(datetime.now(timezone.utc))
                if not time_utils.is_strictly_before(m_date, now_iso):
                    return False

            m_prov = match.get("provider_provenance", {}).get("provider", "api_football")
            m_c_home = match.get("canonical_home_id")
            m_c_away = match.get("canonical_away_id")

            teams_obj = match.get("teams", {}) if isinstance(match.get("teams"), dict) else {}
            h_id = teams_obj.get("home", {}).get("id") if isinstance(teams_obj.get("home"), dict) else None
            a_id = teams_obj.get("away", {}).get("id") if isinstance(teams_obj.get("away"), dict) else None

            if not m_c_home or not m_c_away:
                h_n = teams_obj.get("home", {}).get("name", "") if isinstance(teams_obj.get("home"), dict) else ""
                a_n = teams_obj.get("away", {}).get("name", "") if isinstance(teams_obj.get("away"), dict) else ""

                m_c_home = team_identity.resolve_canonical_team_id(
                    raw_name=h_n or (
                        str(h_id) if h_id is not None else ""
                    ),
                    provider=m_prov,
                    provider_team_id=h_id,
                    league_id=league_id,
                    sport="football",
                    auto_register=False,
                )

                m_c_away = team_identity.resolve_canonical_team_id(
                    raw_name=a_n or (
                        str(a_id) if a_id is not None else ""
                    ),
                    provider=m_prov,
                    provider_team_id=a_id,
                    league_id=league_id,
                    sport="football",
                    auto_register=False,
                )

            return c_id is not None and (m_c_home == c_id or m_c_away == c_id)

        # 1. NEON Persistent DB First
        db_matches = []
        if c_id and not self.force_fallback:
            try:
                raw_db = storage.get_team_historical_fixtures(c_id, limit=last * 2, league_id=league_id)
                db_matches = [m for m in raw_db if _is_valid_recent_match(m)]
            except Exception as exc:
                logger.warning(f"Error fetching recent DB matches for team {c_id}: {exc}")

        if len(db_matches) >= last and not self.force_fallback:
            sorted_db = sorted(db_matches, key=lambda x: str(x.get("fixture", {}).get("date", "") or ""), reverse=True)
            return sorted_db[:last]

        records = list(db_matches)
        seen_fids = {f.get("fixture", {}).get("id") for f in db_matches if f.get("fixture", {}).get("id")}

        # Tier 1: API-Football for missing history
        if len(records) < last and team_id and not self.force_fallback:
            try:
                try:
                    af_matches = api_football.get_recent_form(team_id, last=last * 2, league_id=league_id, season=season)
                except TypeError:
                    try:
                        af_matches = api_football.get_recent_form(team_id, last=last * 2, league_id=league_id)
                    except TypeError:
                        af_matches = api_football.get_recent_form(team_id, last=last * 2)

                if af_matches:
                    normalized = [_normalize_api_football_fixture(m) for m in af_matches if isinstance(m, dict)]
                    valid_to_save = []
                    for norm in normalized:
                        if norm and _is_valid_recent_match(norm):
                            fid = norm.get("fixture", {}).get("id")
                            if fid and fid not in seen_fids:
                                seen_fids.add(fid)
                                records.append(norm)
                                valid_to_save.append(norm)
                    if valid_to_save and league_id and season:
                        storage.save_historical_fixtures(valid_to_save, league_id, season, source="api_football", require_completed=False)
            except Exception as exc:
                logger.warning(f"API-Football recent form query failed for team {team_id}: {exc}")

        # Tier 2: football-data.org if still insufficient
        if len(records) < last and league_id:
            comp_code = LEAGUE_TO_FD_CODE.get(league_id)
            if comp_code:
                try:
                    fd_res = football_data_api.get_competition_matches(comp_code, season=season)
                    fd_matches = fd_res.get("matches", []) if isinstance(fd_res, dict) else []
                    valid_to_save = []
                    for m in fd_matches:
                        norm = _normalize_football_data_match(m, league_id, season)
                        if norm and _is_valid_recent_match(norm):
                            fid = norm.get("fixture", {}).get("id")
                            if fid and fid not in seen_fids:
                                seen_fids.add(fid)
                                records.append(norm)
                                valid_to_save.append(norm)
                    if valid_to_save and league_id and season:
                        storage.save_historical_fixtures(valid_to_save, league_id, season, source="football_data_org", require_completed=False)
                except Exception as exc:
                    logger.warning(f"football-data.org recent matches query failed for league {league_id}: {exc}")

        # Tier 3: SoccerData if still insufficient
        if len(records) < last and team_name:
            try:
                sd_status, sd_matches, _ = soccerdata_provider.get_team_historical_matches(team_name, season=season)
                if sd_status == "SOURCE_AVAILABLE" and sd_matches:
                    valid_to_save = []
                    for norm in sd_matches:
                        if norm and _is_valid_recent_match(norm):
                            fid = norm.get("fixture", {}).get("id")
                            if fid and fid not in seen_fids:
                                seen_fids.add(fid)
                                records.append(norm)
                                valid_to_save.append(norm)
                    if valid_to_save and league_id and season:
                        storage.save_historical_fixtures(valid_to_save, league_id, season, source="soccerdata", require_completed=False)
            except Exception as exc:
                logger.warning(f"SoccerData recent matches query failed for team {team_name}: {exc}")

        sorted_records = sorted(records, key=lambda x: str(x.get("fixture", {}).get("date", "") or ""), reverse=True)
        return sorted_records[:last]

    def get_league_fixtures_page(self, league_id, season, page=1, max_budget=None):
        """
        DataResolver wrapper for league fixtures page acquisition with multi-provider gap filling.
        Preserves original quota exception types so historical_sync quota accounting functions as expected.
        Calculates missing fields for finished matches and fills gaps across secondary and tertiary providers.
        """
        primary_page = None
        try:
            primary_page = api_football.get_league_fixtures_page(league_id, season, page=page, max_budget=max_budget)
        except api_football.APIFootballQuotaExhaustedError:
            raise
        except Exception as exc:
            logger.warning(f"DataResolver primary get_league_fixtures_page failed for league {league_id}: {exc}")

        primary_fixtures = []
        if primary_page and isinstance(primary_page.get("fixtures"), list):
            primary_fixtures = [_normalize_api_football_fixture(f, league_id, season) for f in primary_page["fixtures"] if isinstance(f, dict)]

        needs_gap_filling = not primary_fixtures or any(
            f.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN") and
            len(get_missing_fixture_fields(f, require_stats=True)) > 0
            for f in primary_fixtures
        )

        fd_matches = []
        sd_matches = []

        if needs_gap_filling:
            comp_code = LEAGUE_TO_FD_CODE.get(league_id)
            if comp_code and page == 1:
                try:
                    fd_res = football_data_api.get_competition_matches(comp_code, season=season)
                    matches = fd_res.get("matches", []) if isinstance(fd_res, dict) else (fd_res if isinstance(fd_res, list) else [])
                    fd_matches = [_normalize_football_data_match(m, league_id, season) for m in matches if isinstance(m, dict)]
                except Exception as fd_exc:
                    logger.warning(f"DataResolver secondary get_league_fixtures_page failed for league {league_id}: {fd_exc}")

            # Recalculate gaps after merging football-data.org matches
            still_has_gaps = True
            if primary_fixtures or fd_matches:
                partially_reconciled = []
                for pf in (primary_fixtures or fd_matches):
                    matching_fd = [m for m in fd_matches if _strict_fixture_match(pf, m)]
                    merged = reconcile_fixture_records([pf] + matching_fd)
                    partially_reconciled.append(merged or pf)

                still_has_gaps = any(
                    f.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN") and
                    len(get_missing_fixture_fields(f, require_stats=True)) > 0
                    for f in partially_reconciled
                )

            sd_code = LEAGUE_TO_SD_CODE.get(league_id)
            if sd_code and page == 1 and (still_has_gaps or not (primary_fixtures or fd_matches)):
                try:
                    sd_status, sd_games, _ = soccerdata_provider.get_match_history_games(sd_code, season)
                    if sd_status == "SOURCE_AVAILABLE" and sd_games:
                        sd_matches = sd_games
                except Exception as sd_exc:
                    logger.warning(f"DataResolver tertiary get_league_fixtures_page failed for league {league_id}: {sd_exc}")

        if primary_fixtures:
            reconciled = []
            for pf in primary_fixtures:
                matching_fd = [m for m in fd_matches if _strict_fixture_match(pf, m)]
                matching_sd = [m for m in sd_matches if _strict_fixture_match(pf, m)]
                candidates = [pf] + matching_fd + matching_sd
                merged = reconcile_fixture_records(candidates)
                reconciled.append(merged or pf)

            return {
                "fixtures": reconciled,
                "expected_pages": primary_page.get("expected_pages", 1) if primary_page else 1,
                "current_page": primary_page.get("current_page", page) if primary_page else page,
                "source": "api_football",
            }

        if fd_matches:
            return {"fixtures": fd_matches, "expected_pages": 1, "current_page": 1, "source": "football_data_org"}

        if sd_matches:
            return {"fixtures": sd_matches, "expected_pages": 1, "current_page": 1, "source": "soccerdata"}

        return {"fixtures": [], "expected_pages": 1, "current_page": page, "source": "none"}

    def check_competition_coverage(self, league_id, season):
        """DataResolver owner for checking provider coverage for league + season."""
        import main as main_mod
        return main_mod.check_competition_coverage(league_id, season)

    def search_leagues(self, name):
        """DataResolver owner for searching leagues by name."""
        return api_football.search_leagues(name)

    def get_league_coverage(self, league_id):
        """DataResolver owner for getting league coverage metadata."""
        return api_football.get_league_coverage(league_id)

    def raw_debug_call(self, endpoint, params):
        """DataResolver owner for raw debug calls."""
        return api_football.raw_debug_call(endpoint, params)

    def get_enriched_fixtures(self, fixture_ids, batch_size=20, max_budget=None, league_id=None, season=None):
        """
        DataResolver wrapper for multi-provider fixture statistical enrichment.
        Checks missing statistical fields per fixture before calling external endpoints.
        Reconciles new fields while preserving existing values, recording provenance, and saving to Neon.
        """
        import storage
        existing = storage.get_historical_enrichment(fixture_ids) or {}
        missing_fids = [fid for fid in fixture_ids if len(get_missing_fixture_fields(existing.get(fid), require_stats=True)) > 0]

        if not missing_fids:
            return existing

        # Tier 1: API-Football
        af_enriched = {}
        if not self.force_fallback:
            try:
                af_enriched = api_football.get_enriched_fixtures(missing_fids, batch_size=batch_size, max_budget=max_budget) or {}
            except Exception as exc:
                logger.warning(f"API-Football enrichment failed for missing fixtures: {exc}")

        for fid in missing_fids:
            af_rec = af_enriched.get(fid)
            ex_rec = existing.get(fid)
            if af_rec:
                norm_af = _normalize_api_football_fixture(af_rec)
                if ex_rec:
                    reconciled = reconcile_fixture_records([ex_rec, norm_af or af_rec])
                    if reconciled:
                        existing[fid] = reconciled
                else:
                    existing[fid] = norm_af or af_rec

        # Recalculate remaining missing fields
        still_missing = [fid for fid in missing_fids if len(get_missing_fixture_fields(existing.get(fid), require_stats=True)) > 0]

        # Tier 2: football-data.org if league_id is available and gaps remain
        if still_missing and league_id:
            comp_code = LEAGUE_TO_FD_CODE.get(league_id)
            if comp_code:
                try:
                    fd_res = football_data_api.get_competition_matches(comp_code, season=season)
                    fd_matches = fd_res.get("matches", []) if isinstance(fd_res, dict) else []
                    for m in fd_matches:
                        norm_fd = _normalize_football_data_match(m, league_id, season)
                        if norm_fd:
                            for fid in list(still_missing):
                                ex_rec = existing.get(fid)
                                if ex_rec and _strict_fixture_match(ex_rec, norm_fd):
                                    reconciled = reconcile_fixture_records([ex_rec, norm_fd])
                                    if reconciled:
                                        existing[fid] = reconciled
                except Exception as exc:
                    logger.warning(f"Secondary provider enrichment failed for league {league_id}: {exc}")

        # Tier 3: SoccerData if gaps still remain
        still_missing = [fid for fid in missing_fids if len(get_missing_fixture_fields(existing.get(fid), require_stats=True)) > 0]
        if still_missing and league_id:
            sd_code = LEAGUE_TO_SD_CODE.get(league_id)
            if sd_code:
                try:
                    sd_status, sd_games, _ = soccerdata_provider.get_match_history_games(sd_code, season)
                    if sd_status == "SOURCE_AVAILABLE" and sd_games:
                        for norm_sd in sd_games:
                            for fid in list(still_missing):
                                ex_rec = existing.get(fid)
                                if ex_rec and _strict_fixture_match(ex_rec, norm_sd):
                                    reconciled = reconcile_fixture_records([ex_rec, norm_sd])
                                    if reconciled:
                                        existing[fid] = reconciled
                except Exception as exc:
                    logger.warning(f"Tertiary provider enrichment failed for league {league_id}: {exc}")

        # Save merged results
        to_save = [v for k, v in existing.items() if k in missing_fids and v]
        if to_save:
            try:
                for record in to_save:
                    if not isinstance(record, dict):
                        continue

                    record_copy = dict(record)
                    reconciliation_metadata = record_copy.get("reconciliation_metadata", {})
                    has_reconciliation_provenance = (
                        isinstance(reconciliation_metadata, dict)
                        and reconciliation_metadata.get("is_reconciled") is True
                    )

                    if has_reconciliation_provenance:
                        field_provenance = record_copy.get("field_provenance", {})
                        record_copy["enrichment_provenance"] = {
                            "record_type": "reconciled",
                            "fields": {
                                key: value
                                for key, value in field_provenance.items()
                                if key.startswith("stats_")
                            },
                        }
                        storage.save_historical_enrichment([record_copy], source="reconciled")
                    else:
                        prov_source = record_copy.get("provider_provenance", {}).get("provider", "api_football")
                        storage.save_historical_enrichment([record_copy], source=prov_source)
            except Exception as exc:
                logger.warning(f"Error saving enriched fixtures to DB: {exc}")

        return storage.get_historical_enrichment(fixture_ids)

    def get_team_statistics(self, team_id, league_id, season, team_name=None):
        """
        DataResolver method for fetching team statistics using 3-tier provider hierarchy.
        1. Check persistent historical/derived data first if sufficient.
        2. API-Football.
        3. football-data.org.
        4. SoccerData.
        5. Recalculate sufficiency after every provider.
        6. Return best validated result.
        7. Never crash because one provider failed.
        """
        import storage
        import team_identity

        c_id = None
        if team_name or team_id is not None:
            c_id = team_identity.resolve_canonical_team_id(
                raw_name=team_name or (str(team_id) if team_id is not None else ""),
                provider="api_football",
                provider_team_id=team_id,
                league_id=league_id,
                sport="football",
                auto_register=False
            )

        # 1. Neon Persistent DB History First
        if not self.force_fallback and c_id:
            try:
                db_fixtures = storage.get_historical_fixtures(league_id, season)
                if db_fixtures:
                    db_stats = _build_team_stats_from_matches(db_fixtures, c_id, team_id, "internal_db")
                    if validate_team_stats_sufficiency(db_stats):
                        return db_stats
            except Exception as exc:
                logger.warning(f"DB team statistics calculation failed for team {team_id}: {exc}")

        # Tier 1: API-Football
        if team_id and not self.force_fallback:
            try:
                af_stats = api_football.get_team_statistics(team_id, league_id, season)
                if validate_team_stats_sufficiency(af_stats):
                    return af_stats
            except Exception as exc:
                logger.warning(f"Primary provider (api_football) team statistics failed for team {team_id}: {exc}")

        # Tier 2: football-data.org
        comp_code = LEAGUE_TO_FD_CODE.get(league_id)
        if comp_code:
            try:
                fd_standings = football_data_api.get_competition_standings(comp_code, season)
                if fd_standings:
                    for row in fd_standings:
                        fd_team = row.get("team", {}) or {}
                        fd_tname = fd_team.get("shortName") or fd_team.get("name")
                        fd_tid = fd_team.get("id")
                        fd_c_id = team_identity.resolve_canonical_team_id(
                            raw_name=fd_tname or (
                                str(fd_tid) if fd_tid is not None else ""
                            ),
                            provider="football_data_org",
                            provider_team_id=fd_tid,
                            league_id=league_id,
                            sport="football",
                            auto_register=False,
                        )
                        if c_id and fd_c_id == c_id:
                            fd_stats = _build_team_stats_from_fd_standing(row)
                            if validate_team_stats_sufficiency(fd_stats):
                                return fd_stats
            except Exception as exc:
                logger.warning(f"Secondary provider (football_data_api) team statistics failed for league {league_id}: {exc}")

        # Tier 3: SoccerData
        sd_code = LEAGUE_TO_SD_CODE.get(league_id)
        if sd_code and (team_name or c_id):
            try:
                sd_status, sd_games, _ = soccerdata_provider.get_match_history_games(sd_code, season)
                if sd_status == "SOURCE_AVAILABLE" and sd_games:
                    sd_stats = _build_team_stats_from_matches(sd_games, c_id, None, "soccerdata")
                    if validate_team_stats_sufficiency(sd_stats):
                        return sd_stats
            except Exception as exc:
                logger.warning(f"Tertiary provider (SoccerData) team statistics failed for team {team_name}: {exc}")

        return None

    def get_head_to_head(self, home_id, away_id, last=6, home_team_name=None, away_team_name=None, fixture_date=None, league_id=None):
        """
        Retrieves head-to-head match history using 3-tier provider hierarchy with strict canonical identity matching
        and strict cutoff filtering (match_date < fixture_date).

        Requires BOTH home and away canonical identities to match expected identities.
        Does NOT rely on fuzzy/substring/home-only/away-only matching.
        """
        import storage
        import team_identity

        c_home_id = team_identity.resolve_canonical_team_id(
            raw_name=home_team_name or (
                str(home_id) if home_id is not None else ""
            ),
            provider="api_football",
            provider_team_id=home_id,
            league_id=league_id,
            sport="football",
            auto_register=False,
        )

        c_away_id = team_identity.resolve_canonical_team_id(
            raw_name=away_team_name or (
                str(away_id) if away_id is not None else ""
            ),
            provider="api_football",
            provider_team_id=away_id,
            league_id=league_id,
            sport="football",
            auto_register=False,
        )

        if not c_home_id or not c_away_id or c_home_id == c_away_id:
            logger.warning(f"get_head_to_head called with invalid/ambiguous canonical team identities: {c_home_id} vs {c_away_id}")
            return []

        def _is_valid_h2h_match(match):
            """Verify match contains BOTH canonical home and away identities and respects strict date cutoff."""
            if not isinstance(match, dict):
                return False

            m_date = str(match.get("fixture", {}).get("date", "") or "")
            if not m_date:
                return False

            st = match.get("fixture", {}).get("status", {}).get("short") if isinstance(match.get("fixture"), dict) else None
            if st is not None and st not in ("FT", "AET", "PEN"):
                return False

            goals = match.get("goals", {}) or {}
            if goals.get("home") is None or goals.get("away") is None:
                return False

            # Strict cutoff check
            if fixture_date:
                if not time_utils.is_strictly_before(m_date, fixture_date):
                    return False
            else:
                now_iso = time_utils.format_utc_iso(datetime.now(timezone.utc))
                if not time_utils.is_strictly_before(m_date, now_iso):
                    return False

            m_prov = match.get("provider_provenance", {}).get("provider", "api_football")
            m_c_home = match.get("canonical_home_id")
            m_c_away = match.get("canonical_away_id")

            if not m_c_home or not m_c_away:
                h_id = match.get("teams", {}).get("home", {}).get("id")
                a_id = match.get("teams", {}).get("away", {}).get("id")
                h_n = match.get("teams", {}).get("home", {}).get("name", "") if isinstance(match.get("teams"), dict) else ""
                a_n = match.get("teams", {}).get("away", {}).get("name", "") if isinstance(match.get("teams"), dict) else ""
                m_c_home = team_identity.resolve_canonical_team_id(
                    raw_name=h_n or (
                        str(h_id) if h_id is not None else ""
                    ),
                    provider=m_prov,
                    provider_team_id=h_id,
                    league_id=league_id,
                    sport="football",
                    auto_register=False,
                )

                m_c_away = team_identity.resolve_canonical_team_id(
                    raw_name=a_n or (
                        str(a_id) if a_id is not None else ""
                    ),
                    provider=m_prov,
                    provider_team_id=a_id,
                    league_id=league_id,
                    sport="football",
                    auto_register=False,
                )

            if not m_c_home or not m_c_away:
                return False

            is_match = (
                (m_c_home == c_home_id and m_c_away == c_away_id) or
                (m_c_home == c_away_id and m_c_away == c_home_id)
            )
            return is_match

        h2h_matches = []
        seen_fids = set()

        # 1. NEON Persistent DB first
        if not self.force_fallback:
            try:
                db_home_matches = storage.get_team_historical_fixtures(c_home_id, limit=100)
                for m in db_home_matches:
                    if _is_valid_h2h_match(m):
                        fid = m.get("fixture", {}).get("id")
                        if fid and fid not in seen_fids:
                            seen_fids.add(fid)
                            h2h_matches.append(m)
            except Exception as exc:
                logger.warning(f"Error querying DB for H2H history: {exc}")

        if len(h2h_matches) >= last and not self.force_fallback:
            sorted_h2h = sorted(h2h_matches, key=lambda x: str(x.get("fixture", {}).get("date", "") or ""), reverse=True)
            return sorted_h2h[:last]

        # Tier 1: API-Football
        if home_id and away_id and not self.force_fallback:
            try:
                af_raw = api_football.get_head_to_head(home_id, away_id, last=last * 2)
                if af_raw:
                    for m in af_raw:
                        norm = _normalize_api_football_fixture(m) if isinstance(m, dict) else None
                        if norm and _is_valid_h2h_match(norm):
                            fid = norm.get("fixture", {}).get("id")
                            if fid and fid not in seen_fids:
                                seen_fids.add(fid)
                                h2h_matches.append(norm)
                                lid = norm.get("league", {}).get("id") or league_id
                                ssn = norm.get("league", {}).get("season")
                                if lid and ssn and isinstance(lid, int) and lid > 0:
                                    try:
                                        storage.save_historical_fixtures([norm], lid, ssn, source="api_football", require_completed=False)
                                    except Exception:
                                        pass
            except Exception as exc:
                logger.warning(f"Primary provider get_head_to_head failed for {home_id} vs {away_id}: {exc}")

        # Tier 2: football-data.org if still insufficient
        if len(h2h_matches) < last and league_id:
            comp_code = LEAGUE_TO_FD_CODE.get(league_id)
            if comp_code:
                try:
                    fd_res = football_data_api.get_competition_matches(comp_code)
                    fd_matches = fd_res.get("matches", []) if isinstance(fd_res, dict) else []
                    for m in fd_matches:
                        norm = _normalize_football_data_match(m, league_id, season=None)
                        if norm and _is_valid_h2h_match(norm):
                            fid = norm.get("fixture", {}).get("id")
                            if fid and fid not in seen_fids:
                                seen_fids.add(fid)
                                h2h_matches.append(norm)
                                ssn = norm.get("league", {}).get("season")
                                if league_id and ssn:
                                    storage.save_historical_fixtures([norm], league_id, ssn, source="football_data_org", require_completed=False)
                except Exception as exc:
                    logger.warning(f"Secondary provider get_head_to_head failed for league {league_id}: {exc}")

        # Tier 3: SoccerData if still insufficient
        if len(h2h_matches) < last:
            sd_code = LEAGUE_TO_SD_CODE.get(league_id) if league_id else None
            if sd_code:
                try:
                    sd_status, sd_games, _ = soccerdata_provider.get_match_history_games(sd_code, season=None)
                    if sd_status == "SOURCE_AVAILABLE" and sd_games:
                        for norm in sd_games:
                            if _is_valid_h2h_match(norm):
                                fid = norm.get("fixture", {}).get("id")
                                if fid and fid not in seen_fids:
                                    seen_fids.add(fid)
                                    h2h_matches.append(norm)
                                    ssn = norm.get("league", {}).get("season")
                                    if league_id and ssn:
                                        storage.save_historical_fixtures([norm], league_id, ssn, source="soccerdata", require_completed=False)
                except Exception as exc:
                    logger.warning(f"Tertiary provider get_head_to_head failed for league {league_id}: {exc}")

        sorted_h2h = sorted(h2h_matches, key=lambda x: str(x.get("fixture", {}).get("date", "") or ""), reverse=True)
        return sorted_h2h[:last]

    def get_standings(self, league_id, season=None):
        """
        Retrieves league standings.
        Returns tuple: (standings_list, metadata)
        """
        primary_attempted = False
        fallback_reason = None

        if not self.force_fallback:
            primary_attempted = True
            try:
                try:
                    standings = api_football.get_league_standings(league_id, season)
                except TypeError:
                    standings = api_football.get_league_standings(league_id)

                suff_code, clean_standings = validate_standings_sufficiency(standings)
                if suff_code in ("VALID_DATA", "VALID_EMPTY"):
                    meta = {
                        "data_source": "api_football",
                        "provider": "api_football",
                        "provider_type": "primary",
                        "primary_attempted": True,
                        "fallback_used": False,
                        "fallback_reason": None,
                        "resolver_status": "PRIMARY_SUCCESS",
                    }
                    return clean_standings, meta
                else:
                    fallback_reason = f"Primary standings payload status: {suff_code}"
            except Exception as exc:
                fallback_reason = f"Primary standings error: {str(exc)[:100]}"
                logger.warning(f"Primary standings query failed for league {league_id}: {exc}")

        comp_code = LEAGUE_TO_FD_CODE.get(league_id)
        if not comp_code:
            meta = {
                "data_source": "none",
                "provider": "none",
                "provider_type": "none",
                "primary_attempted": primary_attempted,
                "fallback_used": False,
                "fallback_reason": f"League {league_id} not supported by secondary provider",
                "resolver_status": "NO_DATA",
            }
            return [], meta

        try:
            raw_table = football_data_api.get_competition_standings(comp_code, season)
            normalized = [_normalize_football_data_standing(row) for row in raw_table]
            suff_code, clean_normalized = validate_standings_sufficiency(normalized)
            if suff_code in ("VALID_DATA", "VALID_EMPTY"):
                meta = {
                    "data_source": "football_data_org",
                    "provider": "football_data_org",
                    "provider_type": "secondary",
                    "primary_attempted": primary_attempted,
                    "fallback_used": True,
                    "fallback_reason": fallback_reason,
                    "resolver_status": "SECONDARY_SUCCESS",
                }
                return clean_normalized, meta
            else:
                meta = {
                    "data_source": "none",
                    "provider": "none",
                    "provider_type": "none",
                    "primary_attempted": primary_attempted,
                    "fallback_used": True,
                    "fallback_reason": f"Secondary standings payload status: {suff_code}",
                    "resolver_status": "SECONDARY_INSUFFICIENT",
                }
                return [], meta
        except Exception as exc:
            logger.error(f"Secondary standings query failed for league {league_id}: {exc}")
            meta = {
                "data_source": "none",
                "provider": "none",
                "provider_type": "none",
                "primary_attempted": primary_attempted,
                "fallback_used": True,
                "fallback_reason": f"Secondary standings error: {str(exc)[:100]}",
                "resolver_status": "NO_DATA",
            }
            return [], meta
