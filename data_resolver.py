"""
Provider-neutral Data Resolver for Football data.

Implements strict 3-tier provider hierarchy with field-level merging and reconciliation:
1. Primary: API-Football (api_football.py)
2. Secondary Fallback: football-data.org (football_data_api.py)
3. Tertiary Fallback: SoccerData MatchHistory & Sofascore (soccerdata_provider.py)
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
import config
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

# Mapping league ID to SoccerData MatchHistory code strings
LEAGUE_TO_SD_MH_CODE = {
    39: "ENG-Premier League",
    140: "ESP-La Liga",
    135: "ITA-Serie A",
    78: "GER-Bundesliga",
    61: "FRA-Ligue 1",
    88: "NED-Eredivisie",
    94: "POR-Primeira Liga",
}

# Mapping league ID to SoccerData Sofascore code strings (all 12 configured football leagues)
LEAGUE_TO_SD_SOFASCORE_CODE = {
    39: "ENG-Premier League",
    140: "ESP-La Liga",
    135: "ITA-Serie A",
    78: "GER-Bundesliga",
    61: "FRA-Ligue 1",
    88: "NED-Eredivisie",
    94: "POR-Primeira Liga",
    2: "INT-Champions League",
    3: "INT-Europa League",
    5: "INT-Nations League",
    1: "INT-World Cup",
    4: "INT-European Championship",
}

# Maintain backward compatibility
LEAGUE_TO_SD_CODE = LEAGUE_TO_SD_MH_CODE

# Configured domestic double-round-robin league competitions
DOUBLE_ROUND_ROBIN_LEAGUE_IDS = {39, 140, 135, 78, 61, 88, 94}

# Exact Source URLs & Reference Constants
FOOTBALL_DATA_CSV_URL_PATTERN = "https://www.football-data.co.uk/mmz4281/{season_code}/{division_code}.csv"
FOOTBALL_DATA_REFERENCE_URL = "https://www.football-data.co.uk/data.php"
SOFASCORE_API_BASE_URL = "https://api.sofascore.com/api/v1/"

# Football-Data.co.uk direct CSV league codes mapping
LEAGUE_TO_FD_CSV_CODE = {
    39: "E0",    # Premier League
    140: "SP1",  # La Liga
    135: "I1",   # Serie A
    78: "D1",    # Bundesliga
    61: "F1",    # Ligue 1
    88: "N1",    # Eredivisie
    94: "P1",    # Primeira Liga
}


def _format_fd_csv_season_code(season: int) -> str:
    """Format season year into Football-Data.co.uk 4-digit season code (e.g. 2024 -> '2425', 2025 -> '2526')."""
    if not isinstance(season, int) or season < 1900 or season > 2100:
        return ""
    y1 = season % 100
    y2 = (season + 1) % 100
    return f"{y1:02d}{y2:02d}"


def _fetch_direct_csv_league_fixtures(league_id: int, season: int, timeout=15) -> list:
    """
    Fetch and validate direct CSV match data from stable Football-Data.co.uk URLs.
    Validates CSV headers, date format, team names, and non-negative scores before returning normalized fixture dicts.
    Uses process-stable deterministic SHA256 fixture ID generation.
    Returns list of normalized fixture dicts.
    """
    import csv
    import hashlib
    import io
    import urllib.request
    import team_identity

    code = LEAGUE_TO_FD_CSV_CODE.get(league_id)
    if not code:
        return []

    s_code = _format_fd_csv_season_code(season)
    if not s_code:
        return []

    url = FOOTBALL_DATA_CSV_URL_PATTERN.format(season_code=s_code, division_code=code)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", errors="ignore")

        reader = csv.DictReader(io.StringIO(content))
        header_map = {}
        for h in (reader.fieldnames or []):
            if h:
                clean_h = str(h).strip().lstrip("\ufeff").strip().lower()
                if clean_h:
                    header_map[clean_h] = h

        required_cols = {"date", "hometeam", "awayteam", "fthg", "ftag"}
        if not required_cols.issubset(set(header_map.keys())):
            logger.warning(f"Direct CSV header validation failed for URL {url}: missing required headers")
            return []

        def get_val(row_dict, col_key):
            actual_key = header_map.get(col_key.lower())
            if actual_key and actual_key in row_dict and row_dict[actual_key] is not None:
                return str(row_dict[actual_key]).strip()
            return ""

        rows = list(reader)
        if not rows:
            return []

        fetched_count = len(rows)
        rejected_count = 0
        unresolved_count = 0
        fixtures = []

        for idx, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                rejected_count += 1
                continue

            raw_date = get_val(row, "date")
            raw_home = get_val(row, "hometeam")
            raw_away = get_val(row, "awayteam")
            raw_fthg = get_val(row, "fthg")
            raw_ftag = get_val(row, "ftag")

            if not raw_date or not raw_home or not raw_away:
                rejected_count += 1
                continue

            # Validate date string (DD/MM/YYYY or DD/MM/YY)
            try:
                date_parts = raw_date.split("/")
                if len(date_parts) == 3:
                    d, m, y = int(date_parts[0]), int(date_parts[1]), int(date_parts[2])
                    if y < 100:
                        y += 2000
                    if not (1 <= m <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100):
                        rejected_count += 1
                        continue
                    iso_date = f"{y:04d}-{m:02d}-{d:02d}T12:00:00Z"
                else:
                    rejected_count += 1
                    continue
            except (ValueError, TypeError):
                rejected_count += 1
                continue

            # Validate scores
            try:
                fthg = int(raw_fthg)
                ftag = int(raw_ftag)
                if fthg < 0 or ftag < 0:
                    rejected_count += 1
                    continue
            except (ValueError, TypeError):
                rejected_count += 1
                continue

            # Generate positive integer provider team IDs deterministically from canonical identity
            c_home = team_identity.bootstrap_historical_team_identity(
                raw_name=raw_home, provider="football_data_csv", provider_team_id=None, league_id=league_id, sport="football"
            )
            c_away = team_identity.bootstrap_historical_team_identity(
                raw_name=raw_away, provider="football_data_csv", provider_team_id=None, league_id=league_id, sport="football"
            )

            if not c_home or not c_away:
                unresolved_count += 1
                continue

            csv_h_id = int(hashlib.sha256(f"fd_csv_team_{c_home}".encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF
            csv_a_id = int(hashlib.sha256(f"fd_csv_team_{c_away}".encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF

            team_identity.bootstrap_historical_team_identity(
                raw_name=raw_home, provider="football_data_csv", provider_team_id=csv_h_id, league_id=league_id, sport="football"
            )
            team_identity.bootstrap_historical_team_identity(
                raw_name=raw_away, provider="football_data_csv", provider_team_id=csv_a_id, league_id=league_id, sport="football"
            )

            # Process-stable deterministic synthetic fixture ID
            syn_id = f"fd_csv_{league_id}_{season}_{iso_date}_{c_home}_{c_away}"
            fixture_id = int(hashlib.sha256(syn_id.encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF

            # Extract optional stats
            shots, shots_ot, corners, cards = None, None, None, None
            hs_val, as_val = get_val(row, "hs"), get_val(row, "as")
            hst_val, ast_val = get_val(row, "hst"), get_val(row, "ast")
            hc_val, ac_val = get_val(row, "hc"), get_val(row, "ac")
            hy_val, ay_val = get_val(row, "hy"), get_val(row, "ay")
            hr_val, ar_val = get_val(row, "hr"), get_val(row, "ar")

            try:
                if hs_val and as_val:
                    shots = {"home": int(hs_val), "away": int(as_val)}
                if hst_val and ast_val:
                    shots_ot = {"home": int(hst_val), "away": int(ast_val)}
                if hc_val and ac_val:
                    corners = {"home": int(hc_val), "away": int(ac_val)}
                if hy_val and ay_val:
                    cards = {
                        "home": {"yellow": int(hy_val), "red": int(hr_val or 0)},
                        "away": {"yellow": int(ay_val), "red": int(ar_val or 0)},
                    }
            except (ValueError, TypeError):
                pass

            fixture_dict = {
                "fixture": {
                    "id": fixture_id,
                    "date": iso_date,
                    "status": {"short": "FT", "long": "Finished"},
                },
                "league": {
                    "id": league_id,
                    "season": season,
                    "name": LEAGUE_TO_FD_CODE.get(league_id, "League"),
                },
                "teams": {
                    "home": {"id": csv_h_id, "name": raw_home},
                    "away": {"id": csv_a_id, "name": raw_away},
                },
                "goals": {
                    "home": fthg,
                    "away": ftag,
                },
                "canonical_home_id": c_home,
                "canonical_away_id": c_away,
                "statistics": {
                    "shots": shots,
                    "shots_on_target": shots_ot,
                    "corners": corners,
                    "cards": cards,
                    "xG": None,
                },
                "provider_provenance": {
                    "provider": "football_data_csv",
                    "provider_type": "direct_url",
                    "provider_fixture_id": fixture_id,
                    "provider_team_ids": {"home": csv_h_id, "away": csv_a_id},
                    "retrieved_at": time_utils.format_utc_iso(datetime.now(timezone.utc)),
                },
            }
            fixtures.append(fixture_dict)

        return fixtures

    except Exception as exc:
        logger.warning(f"Direct CSV acquisition failed for league {league_id} season {season} ({url}): {exc}")
        return []


def _get_authoritative_historical_team_ids(league_id, season, max_budget=None, *args, **kwargs):
    """
    Return the authoritative canonical competition team IDs for structural validation.

    Never fabricate IDs.
    Resolves standings/teams into canonical team IDs using read-only resolution.
    If ANY expected team fails resolution, or if partial results are produced, returns [].

    Priority:
    1. API-Football standings
    2. football-data.org competition teams

    Returns:
        list[str|int] of verified canonical team IDs, or [] when unavailable/incomplete.
    """
    import team_identity

    # Primary authoritative source: API-Football standings
    try:
        standings = api_football.get_league_standings(league_id, season, max_budget=max_budget)

        if isinstance(standings, dict):
            rows = standings.get("response", standings.get("standings", []))
        else:
            rows = standings

        if isinstance(rows, list) and rows:
            expanded = []
            for row in rows:
                if isinstance(row, list):
                    expanded.extend(row)
                elif isinstance(row, dict):
                    expanded.append(row)

            if expanded:
                canonical_ids = set()
                all_resolved = True
                for row in expanded:
                    team = row.get("team", {}) if isinstance(row, dict) else {}
                    team_id = team.get("id") if isinstance(team, dict) else None
                    team_name = team.get("name", "") if isinstance(team, dict) else ""

                    c_id = team_identity.resolve_canonical_team_id(
                        raw_name=team_name or (str(team_id) if team_id is not None else ""),
                        provider="api_football",
                        provider_team_id=team_id,
                        league_id=league_id,
                        sport="football",
                        auto_register=False,
                    )
                    if not c_id:
                        all_resolved = False
                        break
                    canonical_ids.add(c_id)

                if all_resolved and len(canonical_ids) == len(expanded):
                    return sorted(canonical_ids, key=lambda x: str(x))

    except Exception as exc:
        logger.warning(
            "Unable to obtain authoritative API-Football team universe for league %s season %s: %s",
            league_id,
            season,
            exc,
        )

    # Secondary authoritative source: football-data.org
    comp_code = LEAGUE_TO_FD_CODE.get(league_id)

    if comp_code:
        try:
            result = football_data_api.get_competition_teams(
                comp_code,
                season=season,
            )

            if isinstance(result, dict):
                rows = result.get("teams", result.get("response", []))
            else:
                rows = result

            if isinstance(rows, list) and rows:
                canonical_ids = set()
                all_resolved = True
                for row in rows:
                    team_id = row.get("id") if isinstance(row, dict) else None
                    team_name = (row.get("shortName") or row.get("name", "")) if isinstance(row, dict) else ""

                    c_id = team_identity.resolve_canonical_team_id(
                        raw_name=team_name or (str(team_id) if team_id is not None else ""),
                        provider="football_data_org",
                        provider_team_id=team_id,
                        league_id=league_id,
                        sport="football",
                        auto_register=False,
                    )
                    if not c_id:
                        all_resolved = False
                        break
                    canonical_ids.add(c_id)

                if all_resolved and len(canonical_ids) == len(rows):
                    return sorted(canonical_ids, key=lambda x: str(x))

        except Exception as exc:
            logger.warning(
                "Unable to obtain authoritative football-data.org team universe for league %s season %s: %s",
                league_id,
                season,
                exc,
            )

    # Tertiary fallback: stored canonical team identities for double round-robin league
    if league_id and league_id in DOUBLE_ROUND_ROBIN_LEAGUE_IDS:
        try:
            import storage
            stored_c_ids = storage.get_team_identities_by_league("football", league_id)
            min_expected = 18 if league_id in {78, 61, 88, 94} else 20
            if len(stored_c_ids) >= min_expected:
                return stored_c_ids
        except Exception as exc:
            logger.warning(
                "Unable to obtain authoritative DB team universe for league %s: %s",
                league_id,
                exc,
            )

    return []


def _validate_historical_structural_coverage(fixtures, season, provider_metadata=None, league_id=None):
    """
    Independently verify structural completeness of a historical fixture dataset.

    Returns structured evidence:
    {
        "verified": bool,
        "reason": str or None,
        "fixture_count": int,
        "distinct_fixture_count": int,
        "team_count": int,
        "expected_team_count": int,
        "expected_team_ids": list,
        "missing_team_ids": list,
        "unexpected_team_ids": list,
        "team_match_counts": dict,
        "expected_fixture_count": int or None,
    }
    """
    import team_identity

    if not isinstance(fixtures, list):
        fixtures = []

    if provider_metadata is None or not isinstance(provider_metadata, dict):
        provider_metadata = {}

    fixture_count = len(fixtures)

    if league_id is None and fixtures:
        for f in fixtures:
            if isinstance(f, dict):
                lid = f.get("league", {}).get("id") if isinstance(f.get("league"), dict) else None
                if lid is not None:
                    league_id = lid
                    break

    is_double_round_robin = (
        (league_id is not None and league_id in DOUBLE_ROUND_ROBIN_LEAGUE_IDS)
        or provider_metadata.get("competition_format") == "double_round_robin"
    )

    expected_team_ids = provider_metadata.get("expected_team_ids", [])
    if not expected_team_ids and is_double_round_robin:
        expected_team_ids = _get_authoritative_historical_team_ids(
            league_id,
            season,
            max_budget=provider_metadata.get("max_budget"),
        )

    if not expected_team_ids and is_double_round_robin and not fixtures:
        return {
            "verified": False,
            "reason": "authoritative_team_universe_unavailable",
            "fixture_count": fixture_count,
            "distinct_fixture_count": 0,
            "team_count": 0,
            "expected_team_count": 0,
            "expected_team_ids": [],
            "missing_team_ids": [],
            "unexpected_team_ids": [],
            "team_match_counts": {},
            "expected_fixture_count": None,
        }

    import team_identity

    import team_identity

    distinct_pairings = set()
    seen_fixture_ids = {}  # prov_fid_key -> fixture_dict
    participating_teams = set()
    team_match_counts = {}
    valid_fixture_count = 0

    for f in fixtures:
        if not isinstance(f, dict):
            continue
        fix_obj = f.get("fixture")
        if not isinstance(fix_obj, dict):
            continue
        fid = fix_obj.get("id")
        if fid is None or isinstance(fid, bool):
            continue
        if isinstance(fid, str) and fid.isdigit():
            fid = int(fid)
            fix_obj["id"] = fid
        if not isinstance(fid, int) or fid <= 0:
            continue

        f_league = f.get("league")
        if not isinstance(f_league, dict):
            continue
        f_lid = f_league.get("id")
        f_ssn = f_league.get("season")
        if f_lid is None or (league_id is not None and f_lid != league_id):
            continue
        if f_ssn is None or (season is not None and str(f_ssn) != str(season)):
            continue

        p_prov = f.get("provider_provenance", {}).get("provider", "unknown") if isinstance(f.get("provider_provenance"), dict) else "unknown"
        prov_fid_key = (p_prov, fid)
        if prov_fid_key in seen_fixture_ids:
            continue
        seen_fixture_ids[prov_fid_key] = f

        teams_obj = f.get("teams")
        if not isinstance(teams_obj, dict):
            continue
        h_obj = teams_obj.get("home", {}) if isinstance(teams_obj.get("home"), dict) else {}
        a_obj = teams_obj.get("away", {}) if isinstance(teams_obj.get("away"), dict) else {}

        canonical_h = f.get("canonical_home_id") or team_identity.resolve_canonical_team_id(
            raw_name=h_obj.get("name", "") or (str(h_obj.get("id")) if h_obj.get("id") is not None else ""),
            provider=p_prov, provider_team_id=h_obj.get("id"), league_id=f_lid, sport="football", auto_register=False
        )
        canonical_a = f.get("canonical_away_id") or team_identity.resolve_canonical_team_id(
            raw_name=a_obj.get("name", "") or (str(a_obj.get("id")) if a_obj.get("id") is not None else ""),
            provider=p_prov, provider_team_id=a_obj.get("id"), league_id=f_lid, sport="football", auto_register=False
        )

        if not canonical_h or not canonical_a or canonical_h == canonical_a:
            continue

        pairing_key = (canonical_h, canonical_a)
        distinct_pairings.add(pairing_key)
        participating_teams.add(canonical_h)
        participating_teams.add(canonical_a)
        valid_fixture_count += 1

        if canonical_h not in team_match_counts:
            team_match_counts[canonical_h] = {"home": 0, "away": 0, "total": 0}
        team_match_counts[canonical_h]["home"] += 1
        team_match_counts[canonical_h]["total"] += 1

        if canonical_a not in team_match_counts:
            team_match_counts[canonical_a] = {"home": 0, "away": 0, "total": 0}
        team_match_counts[canonical_a]["away"] += 1
        team_match_counts[canonical_a]["total"] += 1

    canonical_expected_set = set()
    for tid in expected_team_ids:
        if isinstance(tid, str) and (tid.startswith("football_team_") or tid.startswith("basketball_team_")):
            canonical_expected_set.add(tid)
        else:
            cid = team_identity.resolve_canonical_team_id(
                raw_name="", provider="api_football", provider_team_id=tid, league_id=league_id, sport="football", auto_register=False
            )
            if cid:
                canonical_expected_set.add(cid)
            else:
                canonical_expected_set.add(str(tid))

    expected_team_set = canonical_expected_set if canonical_expected_set else (set(expected_team_ids) if expected_team_ids else set())

    # Resolve provider team IDs in expected_team_set to canonical team IDs via team_identity
    mapped_expected_set = set()
    for etid in expected_team_set:
        if isinstance(etid, str) and (etid.startswith("football_team_") or etid.startswith("basketball_team_")):
            mapped_expected_set.add(etid)
        else:
            cid = team_identity.resolve_canonical_team_id(
                raw_name="", provider="api_football", provider_team_id=etid, league_id=league_id, sport="football", auto_register=False
            )
            if cid:
                mapped_expected_set.add(cid)
            else:
                mapped_expected_set.add(str(etid))
    expected_team_set = mapped_expected_set

    # Infer expected team universe for double-round-robin leagues if expected_team_ids was unavailable
    if not expected_team_set and is_double_round_robin and participating_teams:
        T = len(participating_teams)
        if T >= 2 and T * (T - 1) == valid_fixture_count:
            expected_team_set = set(participating_teams)

    if not expected_team_set and is_double_round_robin:
        return {
            "verified": False,
            "reason": "authoritative_team_universe_unavailable",
            "fixture_count": fixture_count,
            "distinct_fixture_count": len(distinct_pairings),
            "team_count": len(participating_teams),
            "expected_team_count": 0,
            "expected_team_ids": [],
            "missing_team_ids": [],
            "unexpected_team_ids": [],
            "team_match_counts": team_match_counts,
            "expected_fixture_count": None,
        }

    expected_team_count = len(expected_team_set)

    expected_fixture_count = (
        expected_team_count * (expected_team_count - 1)
        if is_double_round_robin and expected_team_count > 0
        else provider_metadata.get("expected_fixture_count")
    )

    expected_matches_per_team = 2 * (expected_team_count - 1) if is_double_round_robin else None
    expected_home_away_per_team = expected_team_count - 1 if is_double_round_robin else None

    # Re-verify full loop validation against derived expected_team_set
    seen_fixture_ids = {}
    distinct_pairings = set()
    participating_teams = set()
    team_match_counts = {}
    valid_fixture_count = 0

    for f in fixtures:
        if not isinstance(f, dict):
            return {
                "verified": False,
                "reason": "invalid_fixture_metadata",
                "fixture_count": fixture_count,
                "distinct_fixture_count": len(distinct_pairings),
                "team_count": len(participating_teams),
                "expected_team_count": expected_team_count,
                "expected_team_ids": sorted(expected_team_set),
                "missing_team_ids": [],
                "unexpected_team_ids": [],
                "team_match_counts": team_match_counts,
                "expected_fixture_count": expected_fixture_count,
            }

        fix_obj = f.get("fixture")
        if not isinstance(fix_obj, dict):
            return {
                "verified": False,
                "reason": "invalid_fixture_metadata",
                "fixture_count": fixture_count,
                "distinct_fixture_count": len(distinct_pairings),
                "team_count": len(participating_teams),
                "expected_team_count": expected_team_count,
                "expected_team_ids": sorted(expected_team_set),
                "missing_team_ids": [],
                "unexpected_team_ids": [],
                "team_match_counts": team_match_counts,
                "expected_fixture_count": expected_fixture_count,
            }

        fid = fix_obj.get("id")
        if fid is None or isinstance(fid, bool):
            return {
                "verified": False,
                "reason": "invalid_fixture_metadata",
                "fixture_count": fixture_count,
                "distinct_fixture_count": len(distinct_pairings),
                "team_count": len(participating_teams),
                "expected_team_count": expected_team_count,
                "expected_team_ids": sorted(expected_team_set),
                "missing_team_ids": [],
                "unexpected_team_ids": [],
                "team_match_counts": team_match_counts,
                "expected_fixture_count": expected_fixture_count,
            }
        if isinstance(fid, str) and fid.isdigit():
            fid = int(fid)
            fix_obj["id"] = fid
        if not isinstance(fid, int) or fid <= 0:
            return {
                "verified": False,
                "reason": "invalid_fixture_metadata",
                "fixture_count": fixture_count,
                "distinct_fixture_count": len(distinct_pairings),
                "team_count": len(participating_teams),
                "expected_team_count": expected_team_count,
                "expected_team_ids": sorted(expected_team_set),
                "missing_team_ids": [],
                "unexpected_team_ids": [],
                "team_match_counts": team_match_counts,
                "expected_fixture_count": expected_fixture_count,
            }

        # FIX 1 Requirement 1: Explicit league ID & season check on every fixture
        f_league = f.get("league")
        if not isinstance(f_league, dict):
            return {
                "verified": False,
                "reason": "invalid_fixture_metadata",
                "fixture_count": fixture_count,
                "distinct_fixture_count": len(distinct_pairings),
                "team_count": len(participating_teams),
                "expected_team_count": expected_team_count,
                "expected_team_ids": sorted(expected_team_set),
                "missing_team_ids": [],
                "unexpected_team_ids": [],
                "team_match_counts": team_match_counts,
                "expected_fixture_count": expected_fixture_count,
            }

        f_lid = f_league.get("id")
        f_ssn = f_league.get("season")

        if f_lid is None or (league_id is not None and f_lid != league_id):
            return {
                "verified": False,
                "reason": "invalid_fixture_metadata",
                "fixture_count": fixture_count,
                "distinct_fixture_count": len(distinct_pairings),
                "team_count": len(participating_teams),
                "expected_team_count": expected_team_count,
                "expected_team_ids": sorted(expected_team_set),
                "missing_team_ids": [],
                "unexpected_team_ids": [],
                "team_match_counts": team_match_counts,
                "expected_fixture_count": expected_fixture_count,
            }

        if f_ssn is None or (season is not None and str(f_ssn) != str(season)):
            return {
                "verified": False,
                "reason": "invalid_fixture_metadata",
                "fixture_count": fixture_count,
                "distinct_fixture_count": len(distinct_pairings),
                "team_count": len(participating_teams),
                "expected_team_count": expected_team_count,
                "expected_team_ids": sorted(expected_team_set),
                "missing_team_ids": [],
                "unexpected_team_ids": [],
                "team_match_counts": team_match_counts,
                "expected_fixture_count": expected_fixture_count,
            }

        p_prov = f.get("provider_provenance", {}).get("provider", "unknown") if isinstance(f.get("provider_provenance"), dict) else "unknown"

        # Validate unique provider-qualified fixture identity (provider, fid)
        prov_fid_key = (p_prov, fid)
        if prov_fid_key in seen_fixture_ids:
            prev_f = seen_fixture_ids[prov_fid_key]
            # Check if conflicting record
            prev_goals = prev_f.get("goals", {}) if isinstance(prev_f.get("goals"), dict) else {}
            curr_goals = f.get("goals", {}) if isinstance(f.get("goals"), dict) else {}
            prev_teams = prev_f.get("teams", {}) if isinstance(prev_f.get("teams"), dict) else {}
            curr_teams = f.get("teams", {}) if isinstance(f.get("teams"), dict) else {}

            is_conflict = (
                prev_goals != curr_goals or
                prev_teams.get("home", {}).get("id") != curr_teams.get("home", {}).get("id") or
                prev_teams.get("away", {}).get("id") != curr_teams.get("away", {}).get("id")
            )

            if is_conflict:
                return {
                    "verified": False,
                    "reason": "conflicting_fixture_id",
                    "fixture_count": fixture_count,
                    "distinct_fixture_count": len(distinct_pairings),
                    "team_count": len(participating_teams),
                    "expected_team_count": expected_team_count,
                    "expected_team_ids": sorted(expected_team_set),
                    "missing_team_ids": [],
                    "unexpected_team_ids": [],
                    "team_match_counts": team_match_counts,
                    "expected_fixture_count": expected_fixture_count,
                }
            else:
                # Identical duplicate: must not inflate coverage count
                continue

        seen_fixture_ids[prov_fid_key] = f

        teams_obj = f.get("teams")
        if not isinstance(teams_obj, dict):
            return {
                "verified": False,
                "reason": "invalid_fixture_metadata",
                "fixture_count": fixture_count,
                "distinct_fixture_count": len(distinct_pairings),
                "team_count": len(participating_teams),
                "expected_team_count": expected_team_count,
                "expected_team_ids": sorted(expected_team_set),
                "missing_team_ids": [],
                "unexpected_team_ids": [],
                "team_match_counts": team_match_counts,
                "expected_fixture_count": expected_fixture_count,
            }

        h_obj = teams_obj.get("home", {}) if isinstance(teams_obj.get("home"), dict) else {}
        a_obj = teams_obj.get("away", {}) if isinstance(teams_obj.get("away"), dict) else {}

        canonical_h = f.get("canonical_home_id") or team_identity.resolve_canonical_team_id(
            raw_name=h_obj.get("name", "") or (str(h_obj.get("id")) if h_obj.get("id") is not None else ""),
            provider=p_prov,
            provider_team_id=h_obj.get("id"),
            league_id=f_lid,
            sport="football",
            auto_register=False,
        )
        canonical_a = f.get("canonical_away_id") or team_identity.resolve_canonical_team_id(
            raw_name=a_obj.get("name", "") or (str(a_obj.get("id")) if a_obj.get("id") is not None else ""),
            provider=p_prov,
            provider_team_id=a_obj.get("id"),
            league_id=f_lid,
            sport="football",
            auto_register=False,
        )

        h_id = canonical_h
        a_id = canonical_a

        if h_id is None or a_id is None:
            return {
                "verified": False,
                "reason": "unresolved_team_identity",
                "fixture_count": fixture_count,
                "distinct_fixture_count": len(distinct_pairings),
                "team_count": len(participating_teams),
                "expected_team_count": expected_team_count,
                "expected_team_ids": sorted(expected_team_set),
                "missing_team_ids": [],
                "unexpected_team_ids": [],
                "team_match_counts": team_match_counts,
                "expected_fixture_count": expected_fixture_count,
            }

        if h_id == a_id:
            return {
                "verified": False,
                "reason": "same_team_match",
                "fixture_count": fixture_count,
                "distinct_fixture_count": len(distinct_pairings),
                "team_count": len(participating_teams),
                "expected_team_count": expected_team_count,
                "expected_team_ids": sorted(expected_team_set),
                "missing_team_ids": [],
                "unexpected_team_ids": [],
                "team_match_counts": team_match_counts,
                "expected_fixture_count": expected_fixture_count,
            }

        pairing_key = (h_id, a_id)
        distinct_pairings.add(pairing_key)
        participating_teams.add(h_id)
        participating_teams.add(a_id)
        valid_fixture_count += 1

        if h_id not in team_match_counts:
            team_match_counts[h_id] = {"home": 0, "away": 0, "total": 0}
        team_match_counts[h_id]["home"] += 1
        team_match_counts[h_id]["total"] += 1

        if a_id not in team_match_counts:
            team_match_counts[a_id] = {"home": 0, "away": 0, "total": 0}
        team_match_counts[a_id]["away"] += 1
        team_match_counts[a_id]["total"] += 1

    distinct_fixture_count = len(distinct_pairings)

    if not is_double_round_robin:
        # Non-double-round-robin (tournament, cup, international)
        # FIX 1 Requirement 6: Require credible provider-specific completeness evidence.
        # A team list or a nonzero fixture count alone is insufficient.
        has_authoritative_evidence = (
            (expected_fixture_count is not None and expected_fixture_count > 0)
            or (provider_metadata.get("has_authoritative_evidence") is True and expected_fixture_count is not None)
        )
        if not has_authoritative_evidence:
            return {
                "verified": False,
                "reason": "structural_coverage_unverifiable",
                "fixture_count": fixture_count,
                "distinct_fixture_count": distinct_fixture_count,
                "team_count": len(participating_teams),
                "expected_team_count": expected_team_count,
                "expected_team_ids": sorted(expected_team_set),
                "missing_team_ids": sorted(expected_team_set - participating_teams),
                "unexpected_team_ids": sorted(participating_teams - expected_team_set) if expected_team_set else [],
                "team_match_counts": team_match_counts,
                "expected_fixture_count": expected_fixture_count,
            }

        is_verified = (
            valid_fixture_count > 0
            and len(seen_fixture_ids) == valid_fixture_count
            and valid_fixture_count == expected_fixture_count
            and (not expected_team_set or expected_team_set.issubset(participating_teams))
        )
        return {
            "verified": is_verified,
            "reason": None if is_verified else "tournament_coverage_incomplete",
            "fixture_count": valid_fixture_count,
            "distinct_fixture_count": distinct_fixture_count,
            "team_count": len(participating_teams),
            "expected_team_count": expected_team_count,
            "expected_team_ids": sorted(expected_team_set),
            "missing_team_ids": sorted(expected_team_set - participating_teams),
            "unexpected_team_ids": sorted(participating_teams - expected_team_set) if expected_team_set else [],
            "team_match_counts": team_match_counts,
            "expected_fixture_count": expected_fixture_count,
        }

    missing_team_ids = expected_team_set - participating_teams
    unexpected_team_ids = participating_teams - expected_team_set

    res = None
    if missing_team_ids or unexpected_team_ids or len(participating_teams) != expected_team_count:
        res = {
            "verified": False,
            "reason": "team_universe_mismatch",
            "fixture_count": valid_fixture_count,
            "distinct_fixture_count": distinct_fixture_count,
            "team_count": len(participating_teams),
            "expected_team_count": expected_team_count,
            "expected_team_ids": sorted(expected_team_set),
            "missing_team_ids": sorted(missing_team_ids),
            "unexpected_team_ids": sorted(unexpected_team_ids),
            "team_match_counts": team_match_counts,
            "expected_fixture_count": expected_fixture_count,
        }
    elif distinct_fixture_count != expected_fixture_count or valid_fixture_count != expected_fixture_count:
        res = {
            "verified": False,
            "reason": "fixture_count_mismatch",
            "fixture_count": valid_fixture_count,
            "distinct_fixture_count": distinct_fixture_count,
            "team_count": len(participating_teams),
            "expected_team_count": expected_team_count,
            "expected_team_ids": sorted(expected_team_set),
            "missing_team_ids": sorted(missing_team_ids),
            "unexpected_team_ids": sorted(unexpected_team_ids),
            "team_match_counts": team_match_counts,
            "expected_fixture_count": expected_fixture_count,
        }
    else:
        for t_id in expected_team_set:
            counts = team_match_counts.get(t_id, {"home": 0, "away": 0, "total": 0})
            if (
                counts["home"] != expected_home_away_per_team
                or counts["away"] != expected_home_away_per_team
                or counts["total"] != expected_matches_per_team
            ):
                res = {
                    "verified": False,
                    "reason": "team_participation_imbalance",
                    "fixture_count": valid_fixture_count,
                    "distinct_fixture_count": distinct_fixture_count,
                    "team_count": len(participating_teams),
                    "expected_team_count": expected_team_count,
                    "expected_team_ids": sorted(expected_team_set),
                    "missing_team_ids": sorted(missing_team_ids),
                    "unexpected_team_ids": sorted(unexpected_team_ids),
                    "team_match_counts": team_match_counts,
                    "expected_fixture_count": expected_fixture_count,
                }
                break

    if res is None:
        res = {
            "verified": True,
            "reason": None,
            "fixture_count": valid_fixture_count,
            "distinct_fixture_count": distinct_fixture_count,
            "team_count": len(participating_teams),
            "expected_team_count": expected_team_count,
            "expected_team_ids": sorted(expected_team_set),
            "missing_team_ids": sorted(missing_team_ids),
            "unexpected_team_ids": sorted(unexpected_team_ids),
            "team_match_counts": team_match_counts,
            "expected_fixture_count": expected_fixture_count,
        }

    if not res["verified"]:
        logger.warning(
            f"Structural coverage validation REJECTED for league {league_id} season {season}: "
            f"reason={res['reason']}, fixture_count={res['fixture_count']}, "
            f"distinct_fixture_count={res['distinct_fixture_count']}, "
            f"team_count={res['team_count']}, expected_team_count={res['expected_team_count']}, "
            f"missing_team_ids={res['missing_team_ids']}, unexpected_team_ids={res['unexpected_team_ids']}"
        )

    return res


def _validate_and_filter_tertiary_matches(matches, league_id, season, completed_only=True):
    """
    Validate and filter tertiary SoccerData matches.
    Strictly enforces:
    - requested season isolation: fixture.league.season == requested season
    - rejects records where season is missing or does not equal requested season
    - requested league isolation
    - valid score for completed matches (home_goals is not None and away_goals is not None)
    - status filter FT/AET/PEN if completed_only=True
    - real integer fixture ID (positive int, no string prefixes)
    - real integer provider team IDs (rejects matches without real positive provider team IDs)
    - canonical team resolution
    """
    import team_identity

    if not matches or not isinstance(matches, list):
        return []

    valid_matches = []
    for m in matches:
        if not isinstance(m, dict):
            continue

        fix_obj = m.get("fixture", {}) if isinstance(m.get("fixture"), dict) else {}
        fid = fix_obj.get("id")

        if fid is None or isinstance(fid, bool):
            continue

        if isinstance(fid, str) and fid.isdigit():
            fid = int(fid)
            fix_obj["id"] = fid

        if not isinstance(fid, int) or fid <= 0:
            continue

        m_league = m.get("league", {}) if isinstance(m.get("league"), dict) else {}
        m_season = m_league.get("season")
        m_league_id = m_league.get("id")

        # Authoritative season isolation check: reject if season missing or != requested season
        if m_season is None or str(m_season).strip() != str(season).strip():
            continue

        # League isolation check
        if m_league_id is not None and league_id is not None and str(m_league_id) != str(league_id):
            continue

        st_short = fix_obj.get("status", {}).get("short") if isinstance(fix_obj.get("status"), dict) else None

        goals = m.get("goals", {}) if isinstance(m.get("goals"), dict) else {}
        h_g = goals.get("home")
        a_g = goals.get("away")

        is_completed = st_short in ("FT", "AET", "PEN")

        # Reject completed matches missing scores
        if is_completed and (h_g is None or a_g is None):
            continue

        if completed_only and not is_completed:
            continue

        # Ensure canonical team identity resolution before storage
        teams_obj = m.get("teams", {}) if isinstance(m.get("teams"), dict) else {}
        h_obj = teams_obj.get("home", {}) if isinstance(teams_obj.get("home"), dict) else {}
        a_obj = teams_obj.get("away", {}) if isinstance(teams_obj.get("away"), dict) else {}

        h_name = h_obj.get("name")
        a_name = a_obj.get("name")
        if not h_name or not a_name:
            continue

        prov_source = m.get("provider_provenance", {}).get("provider", "soccerdata_sofascore")
        h_prov_id = h_obj.get("id")
        a_prov_id = a_obj.get("id")

        def _is_valid_provider_team_id(val) -> bool:
            if val is None or isinstance(val, (bool, float)):
                return False
            if isinstance(val, int):
                return val > 0
            if isinstance(val, str):
                s = val.strip()
                return s.isdigit() and int(s) > 0
            return False

        if not _is_valid_provider_team_id(h_prov_id) or not _is_valid_provider_team_id(a_prov_id):
            continue

        h_obj["id"] = int(h_prov_id) if isinstance(h_prov_id, str) else h_prov_id
        a_obj["id"] = int(a_prov_id) if isinstance(a_prov_id, str) else a_prov_id

        c_home = team_identity.bootstrap_historical_team_identity(h_name, prov_source, h_prov_id, league_id=league_id)
        c_away = team_identity.bootstrap_historical_team_identity(a_name, prov_source, a_prov_id, league_id=league_id)

        if not c_home or not c_away:
            continue

        m["canonical_home_id"] = c_home
        m["canonical_away_id"] = c_away

        # Ensure league id and season are attached
        if isinstance(m.get("league"), dict):
            m["league"]["id"] = league_id
            m["league"]["season"] = season

        valid_matches.append(m)

    return valid_matches


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
    """Ensure API-Football fixture includes explicit provider identity metadata and internal event key if missing ID."""
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

    synthetic_key = None
    if not fid and home_name and away_name and raw_date:
        synthetic_key = generate_synthetic_fixture_id(
            "api_football",
            home_name,
            away_name,
            raw_date,
            league_data.get("id"),
            league_data.get("season"),
            home_id=home_id,
            away_id=away_id
        )
        if synthetic_key:
            res["event_key"] = synthetic_key

    res["provider_provenance"] = {
        "provider": "api_football",
        "provider_type": "primary",
        "provider_fixture_id": fid,
        "provider_competition_id": league_data.get("id"),
        "provider_team_ids": {"home": home_id, "away": away_id},
        "retrieved_at": time_utils.format_utc_iso(datetime.now(timezone.utc)),
    }

    import team_identity
    c_home = team_identity.bootstrap_historical_team_identity(
        raw_name=home_name, provider="api_football", provider_team_id=home_id, league_id=league_id, sport="football"
    )
    c_away = team_identity.bootstrap_historical_team_identity(
        raw_name=away_name, provider="api_football", provider_team_id=away_id, league_id=league_id, sport="football"
    )
    if c_home:
        res["canonical_home_id"] = c_home
    if c_away:
        res["canonical_away_id"] = c_away

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

    res = {
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
    if match.get("statistics") and isinstance(match.get("statistics"), dict):
        res["statistics"] = match["statistics"]

    import team_identity
    h_name = home_team.get("shortName") or home_team.get("name")
    a_name = away_team.get("shortName") or away_team.get("name")
    c_home = team_identity.bootstrap_historical_team_identity(
        raw_name=h_name, provider="football_data_org", provider_team_id=fd_home_id, league_id=league_id, sport="football"
    )
    c_away = team_identity.bootstrap_historical_team_identity(
        raw_name=a_name, provider="football_data_org", provider_team_id=fd_away_id, league_id=league_id, sport="football"
    )
    if c_home:
        res["canonical_home_id"] = c_home
    if c_away:
        res["canonical_away_id"] = c_away

    return res


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


def get_match_identity_key(fixture_dict, sport="football"):
    """
    Construct a stable match identity key based on sport, competition (league_id),
    season, match date (YYYY-MM-DD), and canonical home/away teams.
    Returns tuple or None if required identity fields are missing.
    """
    if not isinstance(fixture_dict, dict):
        return None

    league_obj = fixture_dict.get("league", {}) if isinstance(fixture_dict.get("league"), dict) else {}
    lid = league_obj.get("id")
    ssn = league_obj.get("season")

    fix_obj = fixture_dict.get("fixture", {}) if isinstance(fixture_dict.get("fixture"), dict) else {}
    raw_date = str(fix_obj.get("date", "") or "").strip()
    date_str = raw_date[:10] if len(raw_date) >= 10 else ""

    c_home = fixture_dict.get("canonical_home_id")
    c_away = fixture_dict.get("canonical_away_id")

    teams_obj = fixture_dict.get("teams", {}) if isinstance(fixture_dict.get("teams"), dict) else {}
    h_obj = teams_obj.get("home", {}) if isinstance(teams_obj.get("home"), dict) else {}
    a_obj = teams_obj.get("away", {}) if isinstance(teams_obj.get("away"), dict) else {}
    p_prov = fixture_dict.get("provider_provenance", {}).get("provider", "unknown") if isinstance(fixture_dict.get("provider_provenance"), dict) else "unknown"

    if not c_home or not c_away:
        import team_identity
        if not c_home:
            c_home = team_identity.resolve_canonical_team_id(
                raw_name=h_obj.get("name", "") or (str(h_obj.get("id")) if h_obj.get("id") is not None else ""),
                provider=p_prov,
                provider_team_id=h_obj.get("id"),
                league_id=lid,
                sport=sport,
                auto_register=False,
            )
            if c_home:
                fixture_dict["canonical_home_id"] = c_home

        if not c_away:
            c_away = team_identity.resolve_canonical_team_id(
                raw_name=a_obj.get("name", "") or (str(a_obj.get("id")) if a_obj.get("id") is not None else ""),
                provider=p_prov,
                provider_team_id=a_obj.get("id"),
                league_id=lid,
                sport=sport,
                auto_register=False,
            )
            if c_away:
                fixture_dict["canonical_away_id"] = c_away

    if not lid or not ssn or not c_home or not c_away:
        return None

    return (sport, int(lid), str(ssn), date_str, c_home, c_away)


def _strict_fixture_match(primary_rec, candidate_rec, match_date_strict=True):
    """
    Strict cross-provider fixture match rule.

    Matches fixtures using sport, league, season, date (YYYY-MM-DD), canonical team identity, and home/away roles.
    Never matches solely by numeric provider fixture ID.
    """
    if not isinstance(primary_rec, dict) or not isinstance(candidate_rec, dict):
        return False

    p_key = get_match_identity_key(primary_rec)
    c_key = get_match_identity_key(candidate_rec)

    if p_key and c_key:
        if match_date_strict:
            return p_key == c_key
        return (p_key[0], p_key[1], p_key[2], p_key[4], p_key[5]) == (c_key[0], c_key[1], c_key[2], c_key[4], c_key[5])

    return False


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


def _provider_result_is_sufficient_for_historical_acquisition(provider, fixtures, provider_metadata=None, season=None, league_id=None):
    """
    Determine whether a provider result is sufficient for historical acquisition of requested league/season.

    Decision order:
    1. Reject explicit provider partial/incomplete metadata (e.g. is_partial=True or is_complete=False).
    2. Validate fixture identity/season/league (empty fixtures list, current/ongoing season).
    3. Run independent structural coverage check against returned fixture set.
    4. Only return True when completeness is independently proven.
    5. A provider's "is_complete=True" must NOT immediately return True.
    """
    if not fixtures or not isinstance(fixtures, list) or len(fixtures) == 0:
        return False

    meta = provider_metadata if isinstance(provider_metadata, dict) else {}

    # 1. Explicit partial/incomplete in metadata -> insufficient
    if meta.get("is_partial") is True or meta.get("is_complete") is False:
        return False

    # 2. Check if requested season is current or ongoing
    current_year = datetime.now(timezone.utc).year
    if season is not None:
        try:
            s_val = int(season)
            if s_val >= current_year:
                return False
        except (ValueError, TypeError):
            pass

    # 3. Independent structural coverage check
    coverage = _validate_historical_structural_coverage(
        fixtures,
        season=season,
        provider_metadata=meta,
        league_id=league_id,
    )

    # 4. Only return True when completeness is independently proven
    return coverage.get("verified") is True


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

            # Tier 3: SoccerData if gaps remain or primary/secondary empty or force_fallback is True
            if self.force_fallback or still_has_gaps or not (primary_fixtures or fd_matches):
                sd_mh_code = LEAGUE_TO_SD_MH_CODE.get(league_id) if league_id else None
                if sd_mh_code or not league_id:
                    try:
                        sd_status, sd_games, sd_meta = soccerdata_provider.get_match_history_games(sd_mh_code, None, league_id=league_id)
                        if sd_status == "SOURCE_AVAILABLE" and sd_games:
                            sd_matches = [m for m in sd_games if str(m.get("fixture", {}).get("date", "")).startswith(date_str)]
                    except Exception as exc:
                        logger.error(f"Tertiary provider (SoccerData MatchHistory) failed for date {date_str}: {exc}")

                if not sd_matches:
                    sd_ss_code = LEAGUE_TO_SD_SOFASCORE_CODE.get(league_id) if league_id else None
                    if sd_ss_code or not league_id:
                        try:
                            sd_status, sd_games, sd_meta = soccerdata_provider.get_sofascore_historical_games(sd_ss_code, None, league_id=league_id)
                            if sd_status == "SOURCE_AVAILABLE" and sd_games:
                                sd_matches = [m for m in sd_games if str(m.get("fixture", {}).get("date", "")).startswith(date_str)]
                        except Exception as exc:
                            logger.error(f"Tertiary provider (SoccerData Sofascore) failed for date {date_str}: {exc}")

        # Field-Level Reconciliation Grouping by Team Identity / Date
        reconciled_fixtures = []

        dt_year = int(date_str[:4])
        dt_month = int(date_str[5:7])
        approx_season = dt_year if dt_month >= 7 else dt_year - 1

        all_candidates = primary_fixtures + fd_matches + sd_matches
        for cand in all_candidates:
            if isinstance(cand, dict) and isinstance(cand.get("league"), dict):
                if not cand["league"].get("season"):
                    cand["league"]["season"] = approx_season

        processed_candidates = set()

        for cand in all_candidates:
            cand_id = id(cand)
            if cand_id in processed_candidates:
                continue

            h_name = cand.get("teams", {}).get("home", {}).get("name", "")
            a_name = cand.get("teams", {}).get("away", {}).get("name", "")
            lid = cand.get("league", {}).get("id") or league_id
            prov = cand.get("provider_provenance", {}).get("provider", "soccerdata")

            c_home = cand.get("canonical_home_id") or team_identity.bootstrap_historical_team_identity(
                h_name, prov, cand.get("teams", {}).get("home", {}).get("id"), league_id=lid
            )
            c_away = cand.get("canonical_away_id") or team_identity.bootstrap_historical_team_identity(
                a_name, prov, cand.get("teams", {}).get("away", {}).get("id"), league_id=lid
            )
            cand["canonical_home_id"] = c_home
            cand["canonical_away_id"] = c_away

            matching_records = [c for c in all_candidates if _strict_fixture_match(cand, c)]
            if matching_records:
                for m in matching_records:
                    processed_candidates.add(id(m))

                # Sort matching records by tier priority: primary -> secondary -> tertiary
                def record_tier_priority(r):
                    p = r.get("provider_provenance", {}).get("provider", "")
                    if p == "api_football":
                        return 0
                    elif p in ("football_data_org", "football_data_csv"):
                        return 1
                    return 2

                matching_records.sort(key=record_tier_priority)
                merged = reconcile_fixture_records(matching_records)
                if merged:
                    if c_home:
                        merged["canonical_home_id"] = c_home
                    if c_away:
                        merged["canonical_away_id"] = c_away
                    reconciled_fixtures.append(merged)
            else:
                # Unresolved candidate (cannot match itself via _strict_fixture_match due to unresolved canonical identity)
                processed_candidates.add(cand_id)

                # Validate whether candidate meets required validity rules to be preserved independently
                fix_obj = cand.get("fixture", {}) if isinstance(cand.get("fixture"), dict) else {}
                fid = fix_obj.get("id")
                fdate = fix_obj.get("date")
                teams_obj = cand.get("teams", {}) if isinstance(cand.get("teams"), dict) else {}
                h_n = teams_obj.get("home", {}).get("name") if isinstance(teams_obj.get("home"), dict) else None
                a_n = teams_obj.get("away", {}).get("name") if isinstance(teams_obj.get("away"), dict) else None
                cand_lid = cand.get("league", {}).get("id") or league_id
                cand_ssn = cand.get("league", {}).get("season")
                prov_id = cand.get("provider_provenance", {}).get("provider") if isinstance(cand.get("provider_provenance"), dict) else "unknown"

                rejection_reason = None
                if fid is None or fid == "" or isinstance(fid, bool):
                    rejection_reason = "missing_or_invalid_fixture_id"
                elif not fdate or not str(fdate).strip():
                    rejection_reason = "missing_fixture_date"
                elif not cand_lid or not cand_ssn:
                    rejection_reason = "missing_league_or_season"
                elif not h_n or not a_n:
                    rejection_reason = "missing_team_names"
                elif not prov_id:
                    rejection_reason = "missing_provider_identifier"

                if not rejection_reason:
                    # Preserve candidate as an independent, unreconciled candidate without merging or marking reconciled across providers
                    unrec = dict(cand)
                    if "provider_provenance" not in unrec or not isinstance(unrec["provider_provenance"], dict):
                        unrec["provider_provenance"] = {"provider": prov_id or "unknown"}
                    reconciled_fixtures.append(unrec)
                else:
                    logger.warning(f"Rejected unresolved candidate fixture (id={fid}) in get_fixtures_for_date: {rejection_reason}")

        if reconciled_fixtures:
            # Persist newly reconciled fixtures permanently into PostgreSQL if season is known
            try:
                for rf in reconciled_fixtures:
                    lid = rf.get("league", {}).get("id") or league_id
                    ssn = rf.get("league", {}).get("season")
                    if lid and ssn:
                        storage.save_historical_fixtures([rf], lid, ssn, source=rf.get("provider_provenance", {}).get("provider", "api_football"), require_completed=True)
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

            st = (
                fix_obj.get("status", {}).get("short")
                if isinstance(fix_obj.get("status"), dict)
                else None
            )

            if st not in ("FT", "AET", "PEN"):
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
                        storage.save_historical_fixtures(valid_to_save, league_id, season, source="api_football", require_completed=True)
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
                        storage.save_historical_fixtures(valid_to_save, league_id, season, source="football_data_org", require_completed=True)
                except Exception as exc:
                    logger.warning(f"football-data.org recent matches query failed for league {league_id}: {exc}")

        # Tier 3: SoccerData if still insufficient
        if len(records) < last and team_name:
            try:
                sd_status, sd_matches, _ = soccerdata_provider.get_team_historical_matches(team_name, season=season, league_id=league_id)
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
                        storage.save_historical_fixtures(valid_to_save, league_id, season, source="soccerdata", require_completed=True)
            except Exception as exc:
                logger.warning(f"SoccerData recent matches query failed for team {team_name}: {exc}")

        sorted_records = sorted(records, key=lambda x: str(x.get("fixture", {}).get("date", "") or ""), reverse=True)
        return sorted_records[:last]

    def get_league_fixtures_page(self, league_id, season, page=1, max_budget=None, completed_only=True):
        """
        DataResolver wrapper for league fixtures page acquisition with multi-provider gap filling and fallback.
        Attempts API-Football first. If API-Football fails, returns incomplete or quota exhausted, continues to
        football-data.org -> SoccerData (MatchHistory -> Sofascore).
        Calculates missing fields for finished matches and fills gaps across secondary and tertiary providers.
        When completed_only=True, returned fixtures are strictly filtered to FT, AET, PEN status.
        """
        expected_team_ids = _get_authoritative_historical_team_ids(league_id, season, max_budget=max_budget)
        competition_format = (
            "double_round_robin"
            if league_id in DOUBLE_ROUND_ROBIN_LEAGUE_IDS
            else "unknown"
        )

        primary_page = None
        primary_failed = False
        primary_quota_exhausted = False
        primary_fixtures_for_fallback = []

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        import storage
        req_cnt = storage.get_api_request_count("api_football", today_str)
        if max_budget is not None and req_cnt >= max_budget:
            primary_failed = True
            primary_quota_exhausted = True
            logger.info(f"API-Football daily request budget ceiling reached ({req_cnt}/{max_budget}). Skipping primary API request.")
        else:
            try:
                primary_page = api_football.get_league_fixtures_page(league_id, season, page=page, max_budget=max_budget)
            except api_football.APIFootballQuotaExhaustedError as exc:
                primary_failed = True
                primary_quota_exhausted = True
                logger.warning(f"DataResolver primary get_league_fixtures_page quota exhausted for league {league_id}: {exc}")
            except Exception as exc:
                primary_failed = True
                logger.warning(f"DataResolver primary get_league_fixtures_page failed for league {league_id}: {exc}")

        # If primary provider succeeded without exception:
        if primary_page is not None and isinstance(primary_page, dict):
            raw_fixtures = primary_page.get("fixtures", [])
            if not isinstance(raw_fixtures, list):
                raw_fixtures = []

            primary_fixtures = [_normalize_api_football_fixture(f, league_id, season) for f in raw_fixtures if isinstance(f, dict)]

            fd_matches = []
            sd_matches = []

            if primary_fixtures:
                needs_gap_filling = any(
                    f.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN") and
                    len(get_missing_fixture_fields(f, require_stats=True)) > 0
                    for f in primary_fixtures
                )

                if needs_gap_filling:
                    comp_code = LEAGUE_TO_FD_CODE.get(league_id)
                    if comp_code and page == 1:
                        try:
                            fd_res = football_data_api.get_competition_matches(comp_code, season=season)
                            matches = fd_res.get("matches", []) if isinstance(fd_res, dict) else (fd_res if isinstance(fd_res, list) else [])
                            fd_matches = [_normalize_football_data_match(m, league_id, season) for m in matches if isinstance(m, dict)]
                        except Exception as fd_exc:
                            logger.warning(f"DataResolver secondary get_league_fixtures_page failed for league {league_id}: {fd_exc}")

                    still_has_gaps = any(
                        f.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN") and
                        len(get_missing_fixture_fields(f, require_stats=True)) > 0
                        for f in primary_fixtures
                    )

                    if page == 1 and still_has_gaps:
                        sd_mh_code = LEAGUE_TO_SD_MH_CODE.get(league_id)
                        if sd_mh_code:
                            try:
                                sd_status, sd_games, _ = soccerdata_provider.get_match_history_games(sd_mh_code, season, league_id=league_id)
                                if sd_status == "SOURCE_AVAILABLE" and sd_games:
                                    sd_matches = sd_games
                            except Exception as sd_exc:
                                logger.warning(f"DataResolver tertiary MatchHistory get_league_fixtures_page failed for league {league_id}: {sd_exc}")

                        if not sd_matches:
                            sd_ss_code = LEAGUE_TO_SD_SOFASCORE_CODE.get(league_id)
                            if sd_ss_code:
                                try:
                                    sd_status, sd_games, _ = soccerdata_provider.get_sofascore_historical_games(sd_ss_code, season, league_id=league_id)
                                    if sd_status == "SOURCE_AVAILABLE" and sd_games:
                                        sd_matches = sd_games
                                except Exception as sd_exc:
                                    logger.warning(f"DataResolver tertiary Sofascore get_league_fixtures_page failed for league {league_id}: {sd_exc}")

                reconciled = []
                for pf in primary_fixtures:
                    matching_fd = [m for m in fd_matches if _strict_fixture_match(pf, m)]
                    matching_sd = [m for m in sd_matches if _strict_fixture_match(pf, m)]
                    candidates = [pf] + matching_fd + matching_sd
                    merged = reconcile_fixture_records(candidates)
                    reconciled.append(merged or pf)

                if completed_only:
                    reconciled = [
                        f
                        for f in reconciled
                        if isinstance(f, dict)
                        and f.get("fixture", {}).get("status", {}).get("short")
                        in ("FT", "AET", "PEN")
                    ]

                import storage
                stored_db_fixtures = storage.get_historical_fixtures(league_id, season) if (league_id and season) else []
                all_primary_fixtures = list(reconciled)
                if stored_db_fixtures:
                    seen_ids = {f.get("fixture", {}).get("id") for f in reconciled if isinstance(f, dict) and f.get("fixture", {}).get("id")}
                    for db_f in stored_db_fixtures:
                        if isinstance(db_f, dict):
                            fid = db_f.get("fixture", {}).get("id")
                            if fid and fid not in seen_ids:
                                seen_ids.add(fid)
                                all_primary_fixtures.append(db_f)

                final_coverage = _validate_historical_structural_coverage(
                    all_primary_fixtures,
                    season=season,
                    provider_metadata={
                        "competition_format": competition_format,
                        "expected_team_ids": expected_team_ids,
                    },
                    league_id=league_id,
                )

                provider_metadata = {
                    "competition_format": competition_format,
                    "expected_team_ids": expected_team_ids,
                    "is_complete": final_coverage["verified"],
                    "is_partial": not final_coverage["verified"],
                    "structural_coverage": final_coverage,
                    "providers_contributed": ["api_football"],
                    "api_football": {
                        "count": len(all_primary_fixtures),
                        "is_complete": final_coverage["verified"],
                        "is_partial": not final_coverage["verified"],
                    },
                }

                if final_coverage["verified"] is True or page < primary_page.get("expected_pages", 1):
                    return {
                        "fixtures": reconciled,
                        "expected_pages": primary_page.get("expected_pages", 1),
                        "current_page": primary_page.get("current_page", page),
                        "source": "api_football",
                        "provider_metadata": provider_metadata,
                    }

                primary_fixtures_for_fallback = list(reconciled)

        # Fallback Hierarchy: football-data.org (API or direct CSV) -> SoccerData MatchHistory -> SoccerData Sofascore
        # Merges candidate matches into combined_matches (retaining primary fixtures if available)
        combined_matches = list(primary_fixtures_for_fallback)
        exp_pages_fallback = primary_page.get("expected_pages", 1) if primary_page else 1

        # Secondary Fallback: football-data.org (API if comp_code exists, otherwise direct CSV)
        fd_matches = []
        fd_meta = {}
        fd_key_missing = False
        comp_code = LEAGUE_TO_FD_CODE.get(league_id)

        if page == 1:
            if comp_code:
                try:
                    fd_res = football_data_api.get_competition_matches(comp_code, season=season)
                    if isinstance(fd_res, dict):
                        matches = fd_res.get("matches", [])
                        fd_meta = fd_res.get("metadata", {}) or {}
                    else:
                        matches = fd_res if isinstance(fd_res, list) else []
                    fd_matches = [_normalize_football_data_match(m, league_id, season) for m in matches if isinstance(m, dict)]
                except Exception as fd_exc:
                    if "FOOTBALL_DATA_API_KEY" in str(fd_exc):
                        fd_key_missing = True
                    else:
                        logger.warning(f"DataResolver secondary fallback get_league_fixtures_page failed for league {league_id}: {fd_exc}")

            if not fd_matches and (fd_key_missing or not comp_code) and (primary_failed or not primary_fixtures_for_fallback or self.force_fallback):
                try:
                    direct_csv_matches = _fetch_direct_csv_league_fixtures(league_id, season)
                    if direct_csv_matches:
                        fd_matches = direct_csv_matches
                except Exception as csv_exc:
                    logger.warning(f"Direct CSV acquisition failed for league {league_id} season {season}: {csv_exc}")

        if completed_only and fd_matches:
            fd_matches = [
                f
                for f in fd_matches
                if isinstance(f, dict)
                and f.get("fixture", {}).get("status", {}).get("short")
                in ("FT", "AET", "PEN")
            ]

        if fd_matches:
            for fm in fd_matches:
                matching = [cm for cm in combined_matches if _strict_fixture_match(cm, fm)]
                if matching:
                    idx = combined_matches.index(matching[0])
                    reconciled_rec = reconcile_fixture_records([matching[0], fm])
                    if reconciled_rec:
                        combined_matches[idx] = reconciled_rec
                else:
                    combined_matches.append(fm)

        fd_coverage = _validate_historical_structural_coverage(
            combined_matches,
            season=season,
            provider_metadata={
                "competition_format": competition_format,
                "expected_team_ids": expected_team_ids,
            },
            league_id=league_id,
        )

        if fd_coverage["verified"] is True:
            contribs = [
                p for p, count in [
                    ("api_football", len(primary_fixtures_for_fallback)),
                    ("football_data_org", len(fd_matches)),
                ] if count > 0
            ]
            source_val = "mixed" if len(contribs) > 1 else ("soccerdata" if (contribs and "soccerdata" in contribs[0]) else (contribs[0] if contribs else "football_data_org"))
            fd_provider_meta = {
                "competition_format": competition_format,
                "expected_team_ids": expected_team_ids,
                "is_complete": True,
                "is_partial": False,
                "structural_coverage": fd_coverage,
                "providers_contributed": contribs,
                "api_football": {
                    "count": len(primary_fixtures_for_fallback),
                    "is_complete": False,
                    "is_partial": True,
                },
                "football_data_org": {
                    "count": len(fd_matches),
                    "is_complete": True,
                    "is_partial": False,
                    "raw_metadata": fd_meta,
                },
            }
            return {
                "fixtures": combined_matches,
                "expected_pages": exp_pages_fallback,
                "current_page": page,
                "source": source_val,
                "provider_metadata": fd_provider_meta,
                "primary_failed": primary_failed,
                "primary_quota_exhausted": primary_quota_exhausted,
            }

        # Tertiary Tier 1: SoccerData MatchHistory
        sd_mh_code = LEAGUE_TO_SD_MH_CODE.get(league_id)
        sd_mh_matches = []
        sd_mh_meta = {}

        if page == 1 and sd_mh_code:
            try:
                sd_status, sd_games, sd_meta_res = soccerdata_provider.get_match_history_games(sd_mh_code, season, league_id=league_id)
                if sd_status == "SOURCE_AVAILABLE" and sd_games:
                    sd_mh_matches = _validate_and_filter_tertiary_matches(sd_games, league_id, season, completed_only=completed_only)
                    sd_mh_meta = sd_meta_res or {}
            except Exception as sd_exc:
                logger.warning(f"DataResolver tertiary MatchHistory fallback failed for league {league_id}: {sd_exc}")

        if sd_mh_matches:
            for sm in sd_mh_matches:
                matching = [cm for cm in combined_matches if _strict_fixture_match(cm, sm)]
                if matching:
                    idx = combined_matches.index(matching[0])
                    reconciled_rec = reconcile_fixture_records([matching[0], sm])
                    if reconciled_rec:
                        combined_matches[idx] = reconciled_rec
                else:
                    combined_matches.append(sm)

        mh_coverage = _validate_historical_structural_coverage(
            combined_matches,
            season=season,
            provider_metadata={
                "competition_format": competition_format,
                "expected_team_ids": expected_team_ids,
            },
            league_id=league_id,
        )

        if mh_coverage["verified"] is True:
            contribs = [
                p for p, count in [
                    ("api_football", len(primary_fixtures_for_fallback)),
                    ("football_data_org", len(fd_matches)),
                    ("soccerdata_match_history", len(sd_mh_matches)),
                ] if count > 0
            ]
            source_val = "mixed" if len(contribs) > 1 else ("soccerdata" if (contribs and "soccerdata" in contribs[0]) else (contribs[0] if contribs else "soccerdata"))
            mh_provider_meta = {
                "competition_format": competition_format,
                "expected_team_ids": expected_team_ids,
                "is_complete": True,
                "is_partial": False,
                "structural_coverage": mh_coverage,
                "providers_contributed": contribs,
                "api_football": {
                    "count": len(primary_fixtures_for_fallback),
                    "is_complete": False,
                    "is_partial": True,
                },
                "football_data_org": {
                    "count": len(fd_matches),
                    "is_complete": False,
                    "is_partial": True,
                    "raw_metadata": fd_meta,
                },
                "soccerdata_match_history": {
                    "count": len(sd_mh_matches),
                    "is_complete": True,
                    "is_partial": False,
                    "raw_metadata": sd_mh_meta,
                },
            }
            return {
                "fixtures": combined_matches,
                "expected_pages": exp_pages_fallback,
                "current_page": page,
                "source": source_val,
                "provider_metadata": mh_provider_meta,
                "primary_failed": primary_failed,
                "primary_quota_exhausted": primary_quota_exhausted,
            }

        # Tertiary Tier 2: SoccerData Sofascore (covers all 12 configured football leagues)
        sd_ss_code = LEAGUE_TO_SD_SOFASCORE_CODE.get(league_id)
        sd_ss_matches = []
        sd_ss_meta = {}

        if page == 1 and sd_ss_code:
            try:
                sd_status, sd_games, sd_meta_res = soccerdata_provider.get_sofascore_historical_games(sd_ss_code, season, league_id=league_id)
                sd_ss_meta = sd_meta_res if isinstance(sd_meta_res, dict) else {}
                if sd_status == "SOURCE_AVAILABLE" and sd_games:
                    sd_ss_matches = _validate_and_filter_tertiary_matches(sd_games, league_id, season, completed_only=completed_only)
                else:
                    if "status" not in sd_ss_meta:
                        sd_ss_meta["status"] = sd_status
            except Exception as sd_exc:
                sd_ss_meta = {"status": "SOURCE_FAILED", "error": str(sd_exc)}
                logger.warning(f"DataResolver tertiary Sofascore fallback failed for league {league_id}: {sd_exc}")

        if sd_ss_matches:
            for sm in sd_ss_matches:
                matching = [cm for cm in combined_matches if _strict_fixture_match(cm, sm)]
                if matching:
                    idx = combined_matches.index(matching[0])
                    reconciled_rec = reconcile_fixture_records([matching[0], sm])
                    if reconciled_rec:
                        combined_matches[idx] = reconciled_rec
                else:
                    combined_matches.append(sm)

        final_coverage = _validate_historical_structural_coverage(
            combined_matches,
            season=season,
            provider_metadata={
                "competition_format": competition_format,
                "expected_team_ids": expected_team_ids,
            },
            league_id=league_id,
        )

        contribs = [
            p for p, count in [
                ("api_football", len(primary_fixtures_for_fallback)),
                ("football_data_org", len(fd_matches)),
                ("soccerdata_match_history", len(sd_mh_matches)),
                ("soccerdata_sofascore", len(sd_ss_matches)),
            ] if count > 0
        ]

        final_source = "mixed" if len(contribs) > 1 else ("soccerdata" if (contribs and "soccerdata" in contribs[0]) else (contribs[0] if contribs else "none"))

        final_provider_metadata = {
            "competition_format": competition_format,
            "expected_team_ids": expected_team_ids,
            "is_complete": final_coverage["verified"],
            "is_partial": not final_coverage["verified"],
            "structural_coverage": final_coverage,
            "providers_contributed": contribs,
            "api_football": {
                "count": len(primary_fixtures_for_fallback),
                "is_complete": False,
                "is_partial": True,
            },
            "football_data_org": {
                "count": len(fd_matches),
                "is_complete": False,
                "is_partial": True,
                "raw_metadata": fd_meta,
            },
            "soccerdata_match_history": {
                "count": len(sd_mh_matches),
                "is_complete": False,
                "is_partial": True,
                "raw_metadata": sd_mh_meta,
            },
            "soccerdata_sofascore": {
                "count": len(sd_ss_matches),
                "is_complete": False,
                "is_partial": True,
                "raw_metadata": sd_ss_meta,
            },
        }

        return {
            "fixtures": combined_matches,
            "expected_pages": exp_pages_fallback,
            "current_page": page,
            "source": final_source,
            "provider_metadata": final_provider_metadata,
            "primary_failed": primary_failed,
            "primary_quota_exhausted": primary_quota_exhausted,
        }

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
            sd_mh_code = LEAGUE_TO_SD_MH_CODE.get(league_id)
            sd_games = []
            if sd_mh_code:
                try:
                    sd_status, sd_games, _ = soccerdata_provider.get_match_history_games(sd_mh_code, season, league_id=league_id)
                except Exception as exc:
                    logger.warning(f"Tertiary MatchHistory provider enrichment failed for league {league_id}: {exc}")

            if not sd_games:
                sd_ss_code = LEAGUE_TO_SD_SOFASCORE_CODE.get(league_id)
                if sd_ss_code:
                    try:
                        sd_status, sd_games, _ = soccerdata_provider.get_sofascore_historical_games(sd_ss_code, season, league_id=league_id)
                    except Exception as exc:
                        logger.warning(f"Tertiary Sofascore provider enrichment failed for league {league_id}: {exc}")

            if sd_games:
                for norm_sd in sd_games:
                    for fid in list(still_missing):
                        ex_rec = existing.get(fid)
                        if ex_rec and _strict_fixture_match(ex_rec, norm_sd):
                            reconciled = reconcile_fixture_records([ex_rec, norm_sd])
                            if reconciled:
                                existing[fid] = reconciled

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

        # Tier 3: SoccerData MatchHistory -> Sofascore
        sd_mh_code = LEAGUE_TO_SD_MH_CODE.get(league_id)
        if sd_mh_code and (team_name or c_id):
            try:
                sd_status, sd_games, _ = soccerdata_provider.get_match_history_games(sd_mh_code, season, league_id=league_id)
                if sd_status == "SOURCE_AVAILABLE" and sd_games:
                    sd_stats = _build_team_stats_from_matches(sd_games, c_id, None, "soccerdata")
                    if validate_team_stats_sufficiency(sd_stats):
                        return sd_stats
            except Exception as exc:
                logger.warning(f"Tertiary MatchHistory provider team statistics failed for team {team_name}: {exc}")

        sd_ss_code = LEAGUE_TO_SD_SOFASCORE_CODE.get(league_id)
        if sd_ss_code and (team_name or c_id):
            try:
                sd_status, sd_games, _ = soccerdata_provider.get_sofascore_historical_games(sd_ss_code, season, league_id=league_id)
                if sd_status == "SOURCE_AVAILABLE" and sd_games:
                    sd_stats = _build_team_stats_from_matches(sd_games, c_id, None, "soccerdata")
                    if validate_team_stats_sufficiency(sd_stats):
                        return sd_stats
            except Exception as exc:
                logger.warning(f"Tertiary Sofascore provider team statistics failed for team {team_name}: {exc}")

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

            st = (
                match.get("fixture", {}).get("status", {}).get("short")
                if isinstance(match.get("fixture", {}).get("status"), dict)
                else None
            )

            if st not in ("FT", "AET", "PEN"):
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
                                        storage.save_historical_fixtures([norm], lid, ssn, source="api_football", require_completed=True)
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
                                    storage.save_historical_fixtures([norm], league_id, ssn, source="football_data_org", require_completed=True)
                except Exception as exc:
                    logger.warning(f"Secondary provider get_head_to_head failed for league {league_id}: {exc}")

        # Tier 3: SoccerData if still insufficient
        if len(h2h_matches) < last:
            sd_mh_code = LEAGUE_TO_SD_MH_CODE.get(league_id) if league_id else None
            sd_games = []
            if sd_mh_code:
                try:
                    sd_status, sd_games, _ = soccerdata_provider.get_match_history_games(sd_mh_code, season=None, league_id=league_id)
                except Exception as exc:
                    logger.warning(f"Tertiary MatchHistory get_head_to_head failed for league {league_id}: {exc}")

            if not sd_games:
                sd_ss_code = LEAGUE_TO_SD_SOFASCORE_CODE.get(league_id) if league_id else None
                if sd_ss_code:
                    try:
                        sd_status, sd_games, _ = soccerdata_provider.get_sofascore_historical_games(sd_ss_code, season=None, league_id=league_id)
                    except Exception as exc:
                        logger.warning(f"Tertiary Sofascore get_head_to_head failed for league {league_id}: {exc}")

            if sd_games:
                for norm in sd_games:
                    if _is_valid_h2h_match(norm):
                        fid = norm.get("fixture", {}).get("id")
                        if fid and fid not in seen_fids:
                            seen_fids.add(fid)
                            h2h_matches.append(norm)
                            ssn = norm.get("league", {}).get("season")
                            if league_id and ssn:
                                storage.save_historical_fixtures([norm], league_id, ssn, source="soccerdata", require_completed=True)

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
