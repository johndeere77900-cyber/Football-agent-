"""
Provider-neutral Data Resolver for Football data.

Implements primary -> secondary fallback strategy:
1. Primary: API-Football (api_football.py)
2. Secondary Fallback: football-data.org (football_data_api.py)

Includes feature provenance and fallback telemetry metadata in all returned records.
"""

import logging
import api_football
import football_data_api

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


def _normalize_football_data_match(match, league_id, season):
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
                "id": home_team.get("id"),
                "name": home_team.get("shortName") or home_team.get("name"),
            },
            "away": {
                "id": away_team.get("id"),
                "name": away_team.get("shortName") or away_team.get("name"),
            },
        },
        "goals": {
            "home": ft.get("home"),
            "away": ft.get("away"),
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
    }


class DataResolver:
    """
    DataResolver encapsulates API-Football -> football-data.org failover.
    """

    def __init__(self, force_fallback=False):
        self.force_fallback = force_fallback

    def get_fixtures_for_date(self, date_str, league_id=None):
        """
        Retrieves fixtures for a given date ('YYYY-MM-DD').
        Returns tuple: (fixtures_list, metadata)
        metadata contains:
          - data_source: 'api_football' | 'football_data_org'
          - primary_attempted: bool
          - fallback_used: bool
          - resolver_status: 'PRIMARY_SUCCESS' | 'FALLBACK_SUCCESS' | 'UNAVAILABLE'
        """
        primary_attempted = False
        fallback_used = False

        if not self.force_fallback:
            primary_attempted = True
            try:
                try:
                    data = api_football.get_fixtures_by_date(date_str, league_id=league_id)
                except TypeError:
                    data = api_football.get_fixtures_by_date(date_str, league_id)
                if data is not None:
                    meta = {
                        "data_source": "api_football",
                        "primary_attempted": True,
                        "fallback_used": False,
                        "resolver_status": "PRIMARY_SUCCESS",
                    }
                    return data, meta
            except Exception as exc:
                logger.warning(f"Primary provider (api_football) failed for date {date_str}: {exc}")

        # Secondary fallback
        comp_code = LEAGUE_TO_FD_CODE.get(league_id) if league_id else None
        if league_id and not comp_code:
            meta = {
                "data_source": "none",
                "primary_attempted": primary_attempted,
                "fallback_used": False,
                "resolver_status": "UNAVAILABLE",
            }
            return [], meta

        fallback_used = True
        try:
            # football-data.org doesn't have a single multi-competition date endpoint in free tier without comp code,
            # so if comp_code is provided, query that comp's matches.
            codes_to_query = [comp_code] if comp_code else list(LEAGUE_TO_FD_CODE.values())
            all_matches = []
            for code in codes_to_query:
                lid = FD_CODE_TO_LEAGUE.get(code)
                matches = football_data_api.get_competition_matches(code)
                # Filter by date_str (match['utcDate'] starts with date_str)
                for m in matches:
                    utc_date = m.get("utcDate", "")
                    if utc_date.startswith(date_str):
                        season = m.get("season", {}).get("startDate", "")[:4] or None
                        if season:
                            season = int(season)
                        all_matches.append(_normalize_football_data_match(m, lid, season))

            meta = {
                "data_source": "football_data_org",
                "primary_attempted": primary_attempted,
                "fallback_used": True,
                "resolver_status": "FALLBACK_SUCCESS",
            }
            return all_matches, meta
        except Exception as exc:
            logger.error(f"Secondary provider (football_data_api) failed for date {date_str}: {exc}")
            meta = {
                "data_source": "none",
                "primary_attempted": primary_attempted,
                "fallback_used": True,
                "resolver_status": "UNAVAILABLE",
            }
            return [], meta

    def get_standings(self, league_id, season=None):
        """
        Retrieves league standings.
        Returns tuple: (standings_list, metadata)
        """
        primary_attempted = False

        if not self.force_fallback:
            primary_attempted = True
            try:
                try:
                    standings = api_football.get_league_standings(league_id, season)
                except TypeError:
                    standings = api_football.get_league_standings(league_id)
                if standings is not None:
                    meta = {
                        "data_source": "api_football",
                        "primary_attempted": True,
                        "fallback_used": False,
                        "resolver_status": "PRIMARY_SUCCESS",
                    }
                    return standings, meta
            except Exception as exc:
                logger.warning(f"Primary standings query failed for league {league_id}: {exc}")

        comp_code = LEAGUE_TO_FD_CODE.get(league_id)
        if not comp_code:
            meta = {
                "data_source": "none",
                "primary_attempted": primary_attempted,
                "fallback_used": False,
                "resolver_status": "UNAVAILABLE",
            }
            return [], meta

        try:
            raw_table = football_data_api.get_competition_standings(comp_code, season)
            normalized = [_normalize_football_data_standing(row) for row in raw_table]
            meta = {
                "data_source": "football_data_org",
                "primary_attempted": primary_attempted,
                "fallback_used": True,
                "resolver_status": "FALLBACK_SUCCESS",
            }
            return normalized, meta
        except Exception as exc:
            logger.error(f"Secondary standings query failed for league {league_id}: {exc}")
            meta = {
                "data_source": "none",
                "primary_attempted": primary_attempted,
                "fallback_used": True,
                "resolver_status": "UNAVAILABLE",
            }
            return [], meta
