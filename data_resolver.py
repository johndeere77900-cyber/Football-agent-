"""
Provider-neutral Data Resolver for Football data.

Implements strict primary -> secondary fallback strategy:
1. Primary: API-Football (api_football.py)
2. Secondary Fallback: football-data.org (football_data_api.py)

Result Status Codes:
- PRIMARY_SUCCESS: Primary provider returned valid, sufficient data.
- PRIMARY_INSUFFICIENT: Primary provider response was missing or malformed.
- PRIMARY_UNAVAILABLE: Primary provider failed (exception / rate limit).
- SECONDARY_SUCCESS: Secondary provider returned valid fallback data.
- SECONDARY_INSUFFICIENT: Secondary provider response was missing or malformed.
- NO_DATA: Neither provider supplied usable data.
- ERROR: Operational error occurred.

Provider Identity & Namespace Safety:
- Every record includes provider provenance metadata.
- Secondary provider IDs (football-data.org) are explicitly namespaced to prevent cross-provider ID leakage into API-Football endpoints.
"""

import logging
from datetime import datetime, timezone
import api_football
import football_data_api
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


def _normalize_api_football_fixture(fixture):
    """Ensure API-Football fixture includes explicit provider identity metadata."""
    if not isinstance(fixture, dict):
        return None

    fix_data = fixture.get("fixture", {})
    league_data = fixture.get("league", {})
    teams_data = fixture.get("teams", {})

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
    Returns (is_sufficient: bool, clean_list: list).
    Valid empty lists (0 scheduled matches on a date) are sufficient.
    """
    if not isinstance(fixtures_list, list):
        return False, []

    clean = []
    for f in fixtures_list:
        if not isinstance(f, dict):
            continue
        fix_obj = f.get("fixture")
        teams_obj = f.get("teams")
        if isinstance(fix_obj, dict) and isinstance(teams_obj, dict):
            clean.append(f)

    return True, clean


def validate_standings_sufficiency(standings_list):
    """
    Validate standings list sufficiency.
    Must contain valid non-empty team entries.
    """
    if not isinstance(standings_list, list) or not standings_list:
        return False, []

    clean = []
    for row in standings_list:
        if not isinstance(row, dict):
            continue
        team_obj = row.get("team")
        if isinstance(team_obj, dict) and team_obj.get("name"):
            clean.append(row)

    return (len(clean) > 0), clean


class DataResolver:
    """
    DataResolver encapsulates API-Football -> football-data.org fallback.
    Judges data sufficiency strictly before considering secondary fallback.
    """

    def __init__(self, force_fallback=False):
        self.force_fallback = force_fallback

    def get_fixtures_for_date(self, date_str, league_id=None):
        """
        Retrieves fixtures for a given date ('YYYY-MM-DD').
        Returns tuple: (fixtures_list, metadata)
        """
        primary_attempted = False
        primary_status = "NOT_ATTEMPTED"
        fallback_reason = None

        if not self.force_fallback:
            primary_attempted = True
            try:
                try:
                    data = api_football.get_fixtures_by_date(date_str, league_id=league_id)
                except TypeError:
                    data = api_football.get_fixtures_by_date(date_str, league_id)

                is_suff, clean_data = validate_fixtures_sufficiency(data)
                if is_suff:
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
                    primary_status = "PRIMARY_INSUFFICIENT"
                    fallback_reason = "Primary provider payload failed sufficiency validation"
            except Exception as exc:
                primary_status = "PRIMARY_UNAVAILABLE"
                fallback_reason = f"Primary provider error: {str(exc)[:100]}"
                logger.warning(f"Primary provider (api_football) failed for date {date_str}: {exc}")
        else:
            fallback_reason = "Forced fallback requested"

        # Secondary fallback strictly conditional
        comp_code = LEAGUE_TO_FD_CODE.get(league_id) if league_id else None
        if league_id and not comp_code:
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
            codes_to_query = [comp_code] if comp_code else list(LEAGUE_TO_FD_CODE.values())
            all_matches = []
            for code in codes_to_query:
                lid = FD_CODE_TO_LEAGUE.get(code)
                matches = football_data_api.get_competition_matches(code)
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
                            all_matches.append(norm_match)

            is_suff, clean_matches = validate_fixtures_sufficiency(all_matches)
            if is_suff:
                meta = {
                    "data_source": "football_data_org",
                    "provider": "football_data_org",
                    "provider_type": "secondary",
                    "primary_attempted": primary_attempted,
                    "fallback_used": True,
                    "fallback_reason": fallback_reason,
                    "resolver_status": "SECONDARY_SUCCESS",
                }
                return clean_matches, meta
            else:
                meta = {
                    "data_source": "none",
                    "provider": "none",
                    "provider_type": "none",
                    "primary_attempted": primary_attempted,
                    "fallback_used": True,
                    "fallback_reason": "Secondary provider payload insufficient",
                    "resolver_status": "SECONDARY_INSUFFICIENT",
                }
                return [], meta

        except Exception as exc:
            logger.error(f"Secondary provider (football_data_api) failed for date {date_str}: {exc}")
            meta = {
                "data_source": "none",
                "provider": "none",
                "provider_type": "none",
                "primary_attempted": primary_attempted,
                "fallback_used": True,
                "fallback_reason": f"Secondary provider error: {str(exc)[:100]}",
                "resolver_status": "NO_DATA",
            }
            return [], meta

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

                is_suff, clean_standings = validate_standings_sufficiency(standings)
                if is_suff:
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
                    fallback_reason = "Primary standings payload insufficient"
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
            is_suff, clean_normalized = validate_standings_sufficiency(normalized)
            if is_suff:
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
                    "fallback_reason": "Secondary standings payload insufficient",
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
