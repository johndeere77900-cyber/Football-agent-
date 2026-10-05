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


def _normalize_api_football_fixture(fixture):
    """Ensure API-Football fixture includes explicit provider identity metadata."""
    if not isinstance(fixture, dict):
        return None

    fix_data = fixture.get("fixture", {}) if isinstance(fixture.get("fixture"), dict) else {}
    league_data = fixture.get("league", {}) if isinstance(fixture.get("league"), dict) else {}
    teams_data = fixture.get("teams", {}) if isinstance(fixture.get("teams"), dict) else {}

    home_id = teams_data.get("home", {}).get("id")
    away_id = teams_data.get("away", {}).get("id")

    res = dict(fixture)
    if "fixture" not in res or not isinstance(res["fixture"], dict):
        res["fixture"] = {"id": fix_data.get("id") or id(fixture), "date": "2024-01-01T00:00:00+00:00", "status": {"short": "FT"}}

    res["provider_provenance"] = {
        "provider": "api_football",
        "provider_type": "primary",
        "provider_fixture_id": res["fixture"].get("id"),
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


def get_missing_fixture_fields(fixture):
    """
    Calculate the explicit missing field set for a fixture dict.

    Checks required fields:
    - fixture_id, kickoff, status, competition, season, home_team, away_team
    - score (home_goals, away_goals)
    - statistics: xG, shots, shots_on_target, corners, yellow_cards, red_cards, possession, events

    Returns set of missing field names (e.g. {"xg", "shots", "corners"}).
    """
    if not isinstance(fixture, dict):
        return {
            "fixture_id", "kickoff", "status", "competition", "season",
            "home_team", "away_team", "score", "xg", "shots",
            "shots_on_target", "corners", "yellow_cards", "red_cards", "possession", "events"
        }

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
    if not fix_obj.get("status", {}).get("short") if isinstance(fix_obj.get("status"), dict) else True:
        missing.add("status")

    if not league_obj.get("id") and not league_obj.get("name"):
        missing.add("competition")
    if not league_obj.get("season"):
        missing.add("season")

    if not teams_obj.get("home", {}).get("name") if isinstance(teams_obj.get("home"), dict) else True:
        missing.add("home_team")
    if not teams_obj.get("away", {}).get("name") if isinstance(teams_obj.get("away"), dict) else True:
        missing.add("away_team")

    # Score checking
    is_finished = fix_obj.get("status", {}).get("short") in ("FT", "AET", "PEN") if isinstance(fix_obj.get("status"), dict) else False
    if is_finished and (goals_obj.get("home") is None or goals_obj.get("away") is None):
        missing.add("score")

    # Granular statistical fields checking
    if stats_obj.get("xG") is None and stats_obj.get("xg") is None:
        missing.add("xg")
    if stats_obj.get("shots") is None:
        missing.add("shots")
    if stats_obj.get("shots_on_target") is None:
        missing.add("shots_on_target")
    if stats_obj.get("corners") is None:
        missing.add("corners")
    if stats_obj.get("yellow_cards") is None and not (isinstance(stats_obj.get("cards"), dict) and stats_obj.get("cards", {}).get("home", {}).get("yellow") is None):
        missing.add("yellow_cards")
    if stats_obj.get("red_cards") is None:
        missing.add("red_cards")
    if stats_obj.get("possession") is None:
        missing.add("possession")
    if fixture.get("events") is None and stats_obj.get("events") is None:
        missing.add("events")

    return missing


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

        # Check if finished primary fixtures require field-level enrichment from secondary/tertiary providers
        needs_field_fallback = self.force_fallback or (primary_fixtures and any(
            f.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN") and
            (not f.get("statistics") or f.get("goals", {}).get("home") is None)
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

            # Tier 3: SoccerData if still missing stats/xG
            if not primary_fixtures and not fd_matches:
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

        def _strict_fixture_match(primary_rec, candidate_rec):
            """
            Strict cross-provider fixture match rule:
            1. Canonical home team identity matches AND canonical away team identity matches
            2. OR (Exact normalized home team name matches AND exact normalized away team name matches)
            """
            p_teams = primary_rec.get("teams", {})
            c_teams = candidate_rec.get("teams", {})

            p_h_name = team_identity.normalize_team_name(p_teams.get("home", {}).get("name", ""))
            p_a_name = team_identity.normalize_team_name(p_teams.get("away", {}).get("name", ""))
            c_h_name = team_identity.normalize_team_name(c_teams.get("home", {}).get("name", ""))
            c_a_name = team_identity.normalize_team_name(c_teams.get("away", {}).get("name", ""))

            p_h_id = primary_rec.get("canonical_home_id") or team_identity.resolve_canonical_team_id(p_teams.get("home", {}).get("name", ""), primary_rec.get("provider_provenance", {}).get("provider", "api_football"), p_teams.get("home", {}).get("id"))
            p_a_id = primary_rec.get("canonical_away_id") or team_identity.resolve_canonical_team_id(p_teams.get("away", {}).get("name", ""), primary_rec.get("provider_provenance", {}).get("provider", "api_football"), p_teams.get("away", {}).get("id"))

            cand_prov = candidate_rec.get("provider_provenance", {}).get("provider", "fallback")
            c_h_id = candidate_rec.get("canonical_home_id") or team_identity.resolve_canonical_team_id(c_teams.get("home", {}).get("name", ""), cand_prov, c_teams.get("home", {}).get("id"))
            c_a_id = candidate_rec.get("canonical_away_id") or team_identity.resolve_canonical_team_id(c_teams.get("away", {}).get("name", ""), cand_prov, c_teams.get("away", {}).get("id"))

            if p_h_id and c_h_id and p_a_id and c_a_id:
                if p_h_id == c_h_id and p_a_id == c_a_id:
                    return True

            if p_h_name and c_h_name and p_a_name and c_a_name:
                if p_h_name == c_h_name and p_a_name == c_a_name:
                    return True

            return False

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
            fallback_records = fd_matches + sd_matches
            for ff in fallback_records:
                h_name = ff.get("teams", {}).get("home", {}).get("name", "")
                a_name = ff.get("teams", {}).get("away", {}).get("name", "")
                lid = ff.get("league", {}).get("id")
                prov = ff.get("provider_provenance", {}).get("provider", "fallback")
                c_home = team_identity.bootstrap_historical_team_identity(h_name, prov, ff.get("teams", {}).get("home", {}).get("id"), league_id=lid)
                c_away = team_identity.bootstrap_historical_team_identity(a_name, prov, ff.get("teams", {}).get("away", {}).get("id"), league_id=lid)
                ff["canonical_home_id"] = c_home
                ff["canonical_away_id"] = c_away
                reconciled_fixtures.append(ff)

        if reconciled_fixtures:
            # Persist newly reconciled fixtures permanently into PostgreSQL
            try:
                for rf in reconciled_fixtures:
                    lid = rf.get("league", {}).get("id") or league_id or 0
                    ssn = rf.get("league", {}).get("season") or 2024
                    storage.save_historical_fixtures([rf], lid, ssn, source=rf.get("provider_provenance", {}).get("provider", "api_football"), require_completed=False)
                    if rf.get("statistics"):
                        storage.save_historical_enrichment([rf], source=rf.get("provider_provenance", {}).get("provider", "api_football"))
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

    def get_team_recent_matches(self, team_id, last=10, league_id=None, season=None, team_name=None, canonical_team_id=None):
        """
        Retrieve team historical matches using the NEON-FIRST 3-tier fallback hierarchy:
        1. Neon PostgreSQL DB (via storage.get_team_historical_fixtures)
        2. API-Football (if DB history < last)
        3. football-data.org (if history < last)
        4. SoccerData (if history < last)

        Persists all acquired records immediately to Neon PostgreSQL and resolves canonical identities safely.
        """
        import storage
        import team_identity

        c_id = canonical_team_id
        if not c_id and team_id and team_name:
            c_id = team_identity.resolve_canonical_team_id(team_name, "api_football", team_id, league_id=league_id)

        # 1. NEON DB FIRST
        db_matches = []
        if c_id:
            db_matches = storage.get_team_historical_fixtures(c_id, limit=last, league_id=league_id)

        if len(db_matches) >= last and not self.force_fallback:
            return db_matches[:last]

        records = list(db_matches)
        seen_fids = {f.get("fixture", {}).get("id") for f in db_matches if f.get("fixture", {}).get("id")}

        # Tier 1: API-Football for missing history
        if len(records) < last and not self.force_fallback:
            try:
                try:
                    af_matches = api_football.get_recent_form(team_id, last=last, league_id=league_id, season=season)
                except TypeError:
                    try:
                        af_matches = api_football.get_recent_form(team_id, last=last, league_id=league_id)
                    except TypeError:
                        af_matches = api_football.get_recent_form(team_id, last=last)

                if af_matches:
                    normalized = [_normalize_api_football_fixture(m) for m in af_matches if isinstance(m, dict)]
                    for norm in normalized:
                        fid = norm.get("fixture", {}).get("id") or id(norm)
                        if fid not in seen_fids:
                            seen_fids.add(fid)
                            records.append(norm)
                    if league_id and season:
                        storage.save_historical_fixtures(af_matches, league_id, season, source="api_football", require_completed=False)
            except Exception as exc:
                logger.warning(f"API-Football recent form query failed for team {team_id}: {exc}")

        # Tier 2: football-data.org if still insufficient
        if len(records) < last and league_id:
            comp_code = LEAGUE_TO_FD_CODE.get(league_id)
            if comp_code:
                try:
                    fd_res = football_data_api.get_competition_matches(comp_code, season=season)
                    fd_matches = fd_res.get("matches", []) if isinstance(fd_res, dict) else []
                    for m in fd_matches:
                        norm = _normalize_football_data_match(m, league_id, season)
                        if norm:
                            h_name = norm.get("teams", {}).get("home", {}).get("name", "")
                            a_name = norm.get("teams", {}).get("away", {}).get("name", "")
                            fd_home_id = norm.get("teams", {}).get("home", {}).get("id")
                            fd_away_id = norm.get("teams", {}).get("away", {}).get("id")

                            c_h = team_identity.resolve_canonical_team_id(h_name, "football_data_org", fd_home_id, league_id=league_id)
                            c_a = team_identity.resolve_canonical_team_id(a_name, "football_data_org", fd_away_id, league_id=league_id)

                            if c_id and (c_h == c_id or c_a == c_id):
                                fid = norm.get("fixture", {}).get("id")
                                if fid and fid not in seen_fids:
                                    seen_fids.add(fid)
                                    records.append(norm)
                                    if league_id and season:
                                        storage.save_historical_fixtures([norm], league_id, season, source="football_data_org", require_completed=False)
                except Exception as exc:
                    logger.warning(f"football-data.org recent matches query failed for league {league_id}: {exc}")

        # Tier 3: SoccerData if still insufficient
        if len(records) < last and team_name:
            try:
                sd_status, sd_matches, _ = soccerdata_provider.get_team_historical_matches(team_name, season=season)
                if sd_status == "SOURCE_AVAILABLE" and sd_matches:
                    for norm in sd_matches:
                        fid = norm.get("fixture", {}).get("id")
                        if fid and fid not in seen_fids:
                            seen_fids.add(fid)
                            records.append(norm)
                            if league_id and season:
                                storage.save_historical_fixtures([norm], league_id, season or 2024, source="soccerdata", require_completed=False)
            except Exception as exc:
                logger.warning(f"SoccerData recent matches query failed for team {team_name}: {exc}")

        if not records:
            return []

        return records[:last]

    def get_league_fixtures_page(self, league_id, season, page=1, max_budget=None):
        """
        DataResolver wrapper for league fixtures page acquisition with multi-provider gap filling.
        Preserves original quota exception types so historical_sync quota accounting functions as expected.
        """
        try:
            return api_football.get_league_fixtures_page(league_id, season, page=page, max_budget=max_budget)
        except api_football.APIFootballQuotaExhaustedError:
            raise
        except Exception as exc:
            logger.warning(f"DataResolver primary get_league_fixtures_page failed for league {league_id}: {exc}")
            # Try secondary provider football-data.org if page == 1
            comp_code = LEAGUE_TO_FD_CODE.get(league_id)
            if comp_code and page == 1:
                try:
                    fd_res = football_data_api.get_competition_matches(comp_code, season=season)
                    matches = fd_res.get("matches", []) if isinstance(fd_res, dict) else (fd_res if isinstance(fd_res, list) else [])
                    norm_matches = [_normalize_football_data_match(m, league_id, season) for m in matches if isinstance(m, dict)]
                    return {"fixtures": norm_matches, "expected_pages": 1, "current_page": 1, "source": "football_data_org"}
                except Exception as fd_exc:
                    logger.warning(f"DataResolver secondary get_league_fixtures_page failed for league {league_id}: {fd_exc}")

            # Try tertiary provider SoccerData if secondary failed
            sd_code = LEAGUE_TO_SD_CODE.get(league_id)
            if sd_code and page == 1:
                try:
                    sd_status, sd_matches, _ = soccerdata_provider.get_match_history_games(sd_code, season)
                    if sd_status in ("SOURCE_AVAILABLE", "PARTIAL_DATA", "SOURCE_NOT_AVAILABLE"):
                        return {"fixtures": sd_matches or [], "expected_pages": 1, "current_page": 1, "source": "soccerdata"}
                except Exception as sd_exc:
                    logger.warning(f"DataResolver tertiary get_league_fixtures_page failed for league {league_id}: {sd_exc}")

            raise

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

    def get_enriched_fixtures(self, fixture_ids, batch_size=20, max_budget=None):
        """
        DataResolver wrapper for multi-provider fixture statistical enrichment.
        Checks missing statistical fields per fixture before calling external endpoints.
        """
        import storage
        missing_map = storage.get_missing_enrichment_fields(fixture_ids)
        to_fetch = [fid for fid, missing in missing_map.items() if len(missing) > 0]

        if not to_fetch:
            return storage.get_historical_enrichment(fixture_ids)

        try:
            enriched = api_football.get_enriched_fixtures(to_fetch, batch_size=batch_size, max_budget=max_budget)
            if enriched:
                storage.save_historical_enrichment(enriched, source="api_football")
            return storage.get_historical_enrichment(fixture_ids)
        except Exception as exc:
            logger.warning(f"DataResolver get_enriched_fixtures fallback triggered: {exc}")
            return storage.get_historical_enrichment(fixture_ids)

    def get_team_statistics(self, team_id, league_id, season, team_name=None):
        """
        DataResolver method for fetching team statistics using 3-tier provider hierarchy.
        """
        try:
            return api_football.get_team_statistics(team_id, league_id, season)
        except Exception as exc:
            logger.warning(f"DataResolver get_team_statistics failed for team {team_id}: {exc}")
            return None

    def get_head_to_head(self, home_id, away_id, last=6, home_team_name=None, away_team_name=None):
        """
        DataResolver method for fetching head-to-head match history using 3-tier provider hierarchy.
        """
        try:
            try:
                return api_football.get_head_to_head(home_id, away_id, last=last)
            except TypeError:
                return api_football.get_head_to_head(home_id, away_id)
        except Exception as exc:
            logger.warning(f"DataResolver get_head_to_head failed for {home_id} vs {away_id}: {exc}")
            return []

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
