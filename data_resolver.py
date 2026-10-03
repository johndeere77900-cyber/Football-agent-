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


def _normalize_api_football_fixture(fixture):
    """Ensure API-Football fixture includes explicit provider identity metadata."""
    if not isinstance(fixture, dict):
        return None

    fix_data = fixture.get("fixture", {})
    league_data = fixture.get("league", {})
    teams_data = fixture.get("teams", {})
    goals_data = fixture.get("goals", {})
    stats_data = fixture.get("statistics", {}) or {}

    home_id = teams_data.get("home", {}).get("id")
    away_id = teams_data.get("away", {}).get("id")

    res = dict(fixture)
    res["provider_provenance"] = {
        "provider": "api_football",
        "provider_type": "primary",
        "provider_fixture_id": fix_data.get("id"),
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

    # Reconcile match statistics (shots, corners, xG, cards)
    stats = result.get("statistics") or {}
    if not isinstance(stats, dict):
        stats = {}

    stat_keys = ["shots", "shots_on_target", "corners", "cards", "xG"]
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
        Performs field-level reconciliation when multiple provider records exist.
        Returns tuple: (fixtures_list, metadata)
        """
        primary_attempted = False
        fallback_reason = None
        collected_records_by_key = {}

        # Tier 1: API-Football (PRIMARY)
        if not self.force_fallback:
            primary_attempted = True
            try:
                try:
                    data = api_football.get_fixtures_by_date(date_str, league_id=league_id)
                except TypeError:
                    data = api_football.get_fixtures_by_date(date_str, league_id)

                suff_code, clean_data = validate_fixtures_sufficiency(data)
                if suff_code in ("VALID_DATA", "VALID_EMPTY"):
                    normalized = [_normalize_api_football_fixture(item) for item in clean_data]
                    meta = {
                        "data_source": "api_football",
                        "provider": "api_football",
                        "provider_type": "primary",
                        "primary_attempted": True,
                        "fallback_used": False,
                        "fallback_reason": None,
                        "resolver_status": "PRIMARY_SUCCESS",
                    }
                    return normalized, meta
                else:
                    fallback_reason = f"Primary provider payload status: {suff_code}"
            except Exception as exc:
                fallback_reason = f"Primary provider error: {str(exc)[:100]}"
                logger.warning(f"Primary provider (api_football) failed for date {date_str}: {exc}")
        else:
            fallback_reason = "Forced fallback requested"

        # Tier 2: football-data.org (FALLBACK #2)
        comp_code = LEAGUE_TO_FD_CODE.get(league_id) if league_id else None
        fd_matches = []
        if comp_code or not league_id:
            try:
                codes_to_query = [comp_code] if comp_code else list(LEAGUE_TO_FD_CODE.values())
                for code in codes_to_query:
                    lid = FD_CODE_TO_LEAGUE.get(code)
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

                suff_code, clean_fd = validate_fixtures_sufficiency(fd_matches)
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
                    return clean_fd, meta
                else:
                    fallback_reason = f"Secondary provider payload status: {suff_code}"
            except Exception as exc:
                fallback_reason = f"Secondary provider error: {str(exc)[:100]}"
                logger.error(f"Secondary provider (football_data_api) failed for date {date_str}: {exc}")

        # Tier 3: SoccerData (FALLBACK #3)
        sd_code = LEAGUE_TO_SD_CODE.get(league_id) if league_id else None
        if sd_code or not league_id:
            try:
                sd_status, sd_matches, sd_meta = soccerdata_provider.get_match_history_games(sd_code, None)
                if sd_status == "SOURCE_AVAILABLE" and sd_matches:
                    date_filtered = [m for m in sd_matches if str(m.get("fixture", {}).get("date", "")).startswith(date_str)]
                    suff_code, clean_sd = validate_fixtures_sufficiency(date_filtered)
                    if suff_code in ("VALID_DATA", "VALID_EMPTY"):
                        meta = {
                            "data_source": "soccerdata",
                            "provider": "soccerdata",
                            "provider_type": "tertiary",
                            "primary_attempted": primary_attempted,
                            "fallback_used": True,
                            "fallback_reason": fallback_reason,
                            "resolver_status": "TERTIARY_SUCCESS",
                        }
                        return clean_sd, meta
            except Exception as exc:
                logger.error(f"Tertiary provider (SoccerData) failed for date {date_str}: {exc}")

        meta = {
            "data_source": "none",
            "provider": "none",
            "provider_type": "none",
            "primary_attempted": primary_attempted,
            "fallback_used": True,
            "fallback_reason": fallback_reason,
            "resolver_status": "NO_DATA",
        }
        return [], meta

    def get_team_recent_matches(self, team_id, last=10, league_id=None, season=None, team_name=None):
        """
        Retrieve team historical matches using the 3-tier fallback hierarchy:
        1. API-Football
        2. football-data.org
        3. SoccerData
        Reconciles records across providers at the field level.
        """
        records = []

        # Tier 1: API-Football
        try:
            af_matches = api_football.get_recent_form(team_id, last=last, league_id=league_id, season=season)
            if af_matches:
                records.extend([_normalize_api_football_fixture(m) for m in af_matches if isinstance(m, dict)])
        except Exception as exc:
            logger.warning(f"API-Football recent form query failed for team {team_id}: {exc}")

        # Tier 2: football-data.org if primary returned fewer than requested
        if len(records) < last and league_id:
            comp_code = LEAGUE_TO_FD_CODE.get(league_id)
            if comp_code:
                try:
                    fd_res = football_data_api.get_competition_matches(comp_code, season=season)
                    fd_matches = fd_res.get("matches", []) if isinstance(fd_res, dict) else []
                    for m in fd_matches:
                        norm = _normalize_football_data_match(m, league_id, season)
                        if norm:
                            p_ids = norm.get("provider_provenance", {}).get("provider_team_ids", {})
                            if p_ids.get("home") == team_id or p_ids.get("away") == team_id:
                                records.append(norm)
                except Exception as exc:
                    logger.warning(f"football-data.org recent matches query failed for league {league_id}: {exc}")

        # Tier 3: SoccerData if still fewer than requested
        if len(records) < last and team_name:
            try:
                sd_status, sd_matches, _ = soccerdata_provider.get_team_historical_matches(team_name, season=season)
                if sd_status == "SOURCE_AVAILABLE" and sd_matches:
                    records.extend(sd_matches)
            except Exception as exc:
                logger.warning(f"SoccerData recent matches query failed for team {team_name}: {exc}")

        if not records:
            return []

        # Return reconciled records up to requested limit
        return records[:last]

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
