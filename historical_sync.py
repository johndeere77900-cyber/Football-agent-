"""
Historical Data Acquisition Collector for Football Agent.

Acquires historical league fixtures/games and optional statistics enrichment and
stores them permanently in persistent storage (Neon PostgreSQL / SQLite).

Approved historical football acquisition route (no secondary API providers):
    direct Football-Data.co.uk CSV -> SoccerData FBref -> SoccerData MatchHistory
    -> SoccerData Sofascore

Approved historical basketball acquisition route:
    ESPN URL scoreboard (primary) -> API-Basketball (fallback)

API-Football and football-data.org are NOT historical football fixture
acquisition providers. Their clients and normalizers are retained because live
prediction, live fixtures, team form/statistics and standings workflows still
use them; those call sites are intentionally untouched.

Key invariants:
- Backtests read from the database, not from external APIs.
- COMPLETE datasets bypass acquisition (0 requests) only once the persisted data
  independently passes coverage validation on a revalidation run.
- Competition lifecycle and provider acquisition state are distinct concepts:
  lifecycle is UPCOMING/ONGOING/COMPLETED/UNKNOWN, acquisition state is
  PENDING/RUNNING/INCOMPLETE/VALIDATED/FAILED.
- A dataset becomes COMPLETE only when acquisition coverage is complete AND the
  competition lifecycle is independently established as COMPLETED. Calendar
  dates plus non-empty records can never establish completion.
- Strict quota safety: the configured historical daily budget is enforced on
  every HTTP attempt.
- Idempotent and safe to run repeatedly or resume after a partial sync.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import data_resolver
from data_resolver import DataResolver
import config
import storage

# --- Competition lifecycle states (independent of acquisition state) -------
COMPETITION_LIFECYCLE_UPCOMING = "UPCOMING"
COMPETITION_LIFECYCLE_ONGOING = "ONGOING"
COMPETITION_LIFECYCLE_COMPLETED = "COMPLETED"
COMPETITION_LIFECYCLE_UNKNOWN = "UNKNOWN"

# --- Provider acquisition states --------------------------------------------
ACQUISITION_STATE_PENDING = "PENDING"
ACQUISITION_STATE_RUNNING = "RUNNING"
ACQUISITION_STATE_INCOMPLETE = "INCOMPLETE"
ACQUISITION_STATE_VALIDATED = "VALIDATED"
ACQUISITION_STATE_FAILED = "FAILED"

# --- Manifest coverage unit labels ------------------------------------------
# `expected_pages`/`pages_completed` are generic coverage units; these labels
# record what was actually counted so provider progress is never misrepresented.
COVERAGE_UNIT_API_PAGES = "api_pages"
COVERAGE_UNIT_ESPN_MONTHS = "espn_months"
COVERAGE_UNIT_SINGLE_SHOT = "single_shot"

NBA_LEAGUE_ID = 12


def _parse_iso_utc(raw) -> datetime | None:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC datetime."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _max_record_date_utc(records, sport) -> datetime | None:
    """Return the latest observed match date across persisted records, or None."""
    latest = None
    sport_key = str(sport).strip().lower() if sport else "football"
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        if sport_key == "basketball":
            raw = record.get("date")
        else:
            fix_obj = record.get("fixture") if isinstance(record.get("fixture"), dict) else {}
            raw = fix_obj.get("date") or record.get("date")
        parsed = _parse_iso_utc(raw)
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    return latest


def parse_season_label(season):
    """
    Return a competition season's start year as an int, or None when the label is
    not a recognized season label.

    Handles every label form used across providers, configuration and CLI input:
    a single year ("2019"), split-year labels with a slash or dash ("2019/2020",
    "2019/20", "2019-20") and en/em dash forms ("2019\u201320", "2020\u201321"). A
    two-digit end year is completed against the start year ("1999/00" -> 2000).
    Only consecutive season labels are accepted; anything else fails closed with
    None rather than being guessed.
    """
    if isinstance(season, bool):
        return None
    if isinstance(season, int):
        return season if season > 0 else None
    if not isinstance(season, str):
        return None

    text = season.strip()
    if not text:
        return None
    for separator in ("\u2013", "\u2014", "\\", "-", "/"):
        text = text.replace(separator, "/")

    parts = [part.strip() for part in text.split("/") if part.strip()]
    if not parts or len(parts) > 2:
        return None

    start_text = parts[0]
    if len(start_text) != 4 or not start_text.isdigit():
        return None
    start_year = int(start_text)

    if len(parts) == 2:
        end_text = parts[1]
        if not end_text.isdigit() or len(end_text) not in (2, 4):
            return None
        if len(end_text) == 4:
            end_year = int(end_text)
        else:
            end_year = (start_year // 100) * 100 + int(end_text)
            if end_year <= start_year:
                end_year += 100
        if end_year != start_year + 1:
            return None

    return start_year if start_year > 0 else None


def _competition_season_window(sport, league_id, season):
    """
    Return (start_utc, end_utc) for a competition's declared season window.

    Windows come from config.COMPETITION_SEASON_WINDOWS, which describes each
    supported competition explicitly instead of assuming every competition is a
    European domestic league. config.COMPETITION_SEASON_WINDOW_OVERRIDES takes
    precedence for seasons with authoritative, externally documented boundaries
    (for example the COVID-extended 2019-20 NBA season).

    Returns None when no authoritative metadata exists for the competition or
    season, so callers fail closed instead of assuming a calendar boundary.
    """
    try:
        lid = int(league_id)
    except (TypeError, ValueError):
        return None

    ssn = parse_season_label(season)
    if ssn is None:
        return None

    sport_key = str(sport).strip().lower() if sport else "football"

    overrides = getattr(config, "COMPETITION_SEASON_WINDOW_OVERRIDES", {}) or {}
    window = overrides.get((sport_key, lid, ssn))
    if not isinstance(window, dict):
        windows = getattr(config, "COMPETITION_SEASON_WINDOWS", {}) or {}
        window = windows.get((sport_key, lid))
    if not isinstance(window, dict):
        return None

    start_spec = window.get("start")
    end_spec = window.get("end")
    if not (
        isinstance(start_spec, (tuple, list)) and len(start_spec) == 3
        and isinstance(end_spec, (tuple, list)) and len(end_spec) == 3
    ):
        return None

    try:
        start_utc = datetime(ssn + int(start_spec[0]), int(start_spec[1]), int(start_spec[2]), tzinfo=timezone.utc)
        end_utc = datetime(ssn + int(end_spec[0]), int(end_spec[1]), int(end_spec[2]), tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None

    if end_utc <= start_utc:
        return None

    return start_utc, end_utc


def get_competition_lifecycle(
    sport: str,
    league_id: int,
    season: int,
    stored_records: list,
    coverage: dict = None,
    now_utc: datetime = None,
) -> tuple[str, str, dict]:
    """
    Decide the competition lifecycle state independently of acquisition state.

    Returns (state, reason, evidence) where state is one of UPCOMING, ONGOING,
    COMPLETED or UNKNOWN.

    Completion requires ALL of:
      - authoritative competition/season metadata (declared season window),
      - independently verified coverage of the persisted dataset,
      - a declared window that has already closed, and
      - an observed schedule that stays inside the declared window (plus a
        bounded grace period for postponed/rescheduled fixtures).

    Anything else fails closed: UNKNOWN (metadata/coverage unavailable) or
    ONGOING (window still open). A date boundary combined with non-empty records
    is never sufficient.
    """
    evidence = {
        "sport": str(sport).strip().lower() if sport else None,
        "league_id": league_id,
        "season": season,
        "record_count": len(stored_records) if isinstance(stored_records, list) else 0,
        "coverage_verified": bool(isinstance(coverage, dict) and coverage.get("verified") is True),
        "coverage_reason": coverage.get("reason") if isinstance(coverage, dict) else None,
        "season_window": None,
        "observed_last_match_date": None,
    }

    window = _competition_season_window(sport, league_id, season)
    if window is None:
        return COMPETITION_LIFECYCLE_UNKNOWN, "competition_metadata_unavailable", evidence

    start_utc, end_utc = window
    evidence["season_window"] = {"start": start_utc.isoformat(), "end": end_utc.isoformat()}

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    records = stored_records if isinstance(stored_records, list) else []
    if not records:
        if now_utc < start_utc:
            return COMPETITION_LIFECYCLE_UPCOMING, "season_not_started", evidence
        if now_utc < end_utc:
            return COMPETITION_LIFECYCLE_ONGOING, "no_records_available", evidence
        # The declared window has closed but nothing is persisted, so neither an
        # active season nor a concluded one can be evidenced from stored data.
        # This is UNKNOWN (fail closed), not ONGOING: reporting a season that
        # ended years ago as still in progress misrepresents historical
        # competitions and their acquisition state.
        return COMPETITION_LIFECYCLE_UNKNOWN, "no_records_after_window_closed", evidence

    if not (isinstance(coverage, dict) and coverage.get("verified") is True):
        if now_utc < end_utc:
            return COMPETITION_LIFECYCLE_ONGOING, "active_season_in_progress", evidence
        reason = coverage.get("reason") if isinstance(coverage, dict) and coverage.get("reason") else "unverified"
        return COMPETITION_LIFECYCLE_UNKNOWN, f"coverage_unverified_{reason}", evidence

    if now_utc < start_utc:
        return COMPETITION_LIFECYCLE_UPCOMING, "season_not_started", evidence

    if now_utc < end_utc:
        return (
            COMPETITION_LIFECYCLE_ONGOING,
            f"active_season_in_progress_until_{end_utc.strftime('%Y-%m-%d')}",
            evidence,
        )

    max_date = _max_record_date_utc(records, sport)
    evidence["observed_last_match_date"] = max_date.isoformat() if max_date is not None else None

    grace_days = int(getattr(config, "COMPETITION_SEASON_COMPLETION_GRACE_DAYS", 0) or 0)
    if max_date is not None and max_date > end_utc + timedelta(days=grace_days):
        # Postponed/rescheduled fixtures extending well past the declared window
        # mean the lifecycle cannot be established authoritatively.
        return COMPETITION_LIFECYCLE_UNKNOWN, "schedule_extends_beyond_declared_window", evidence

    return COMPETITION_LIFECYCLE_COMPLETED, "season_concluded_with_verified_coverage", evidence


def _is_season_lifecycle_completed(sport: str, league_id: int, season: int, stored_records: list, structural_coverage: dict = None) -> tuple[bool, str]:
    """
    Compatibility wrapper: True only when the competition lifecycle is COMPLETED.

    Returns (is_completed: bool, reason: str). The reason is prefixed with the
    lifecycle state so logs distinguish ONGOING from UNKNOWN.
    """
    state, reason, _ = get_competition_lifecycle(
        sport, league_id, season, stored_records, coverage=structural_coverage
    )
    return (state == COMPETITION_LIFECYCLE_COMPLETED), f"{state.lower()}_{reason}"


def _is_historical_provider_complete(
    page_source: str,
    page_meta: dict,
    valid_fixtures_count: int,
    expected_pages: int,
    pages_completed: int,
    quota_budget_stopped: bool,
    acquisition_failed: bool,
    rejected_count: int,
    empty_pages_count: int,
    final_stored_count: int,
) -> bool:
    """
    Provider-neutral historical dataset completeness decision.
    Validates provider metadata, acquisition unit execution, fixture count, and error states.
    Fails closed if completeness cannot be established.
    """
    if acquisition_failed:
        return False

    if quota_budget_stopped:
        # An acquisition-stage quota stop leaves acquisition unproven, even when
        # the rows already persisted happen to satisfy the structural checks. The
        # caller passes False when the stop occurred only during optional
        # enrichment, so that case keeps its existing semantics.
        return False

    if final_stored_count <= 0:
        return False

    prov_meta_check = page_meta.get("provider_metadata", {}) if isinstance(page_meta, dict) else {}
    struct_cov = prov_meta_check.get("structural_coverage", {}) if isinstance(prov_meta_check, dict) else {}

    if valid_fixtures_count <= 0 and struct_cov.get("verified") is not True:
        return False

    if not page_source or not isinstance(page_source, str):
        return False

    prov = page_source.strip().lower()
    supported_providers = {
        "football_data_csv",
        "soccerdata",
        "soccerdata_fbref",
        "soccerdata_match_history",
        "soccerdata_sofascore",
        "mixed",
    }
    if prov not in supported_providers:
        return False

    prov_meta = page_meta.get("provider_metadata") if isinstance(page_meta, dict) else {}
    if not isinstance(prov_meta, dict):
        prov_meta = {}

    if prov_meta.get("is_partial") is True or prov_meta.get("is_complete") is False:
        return False

    structural_coverage = prov_meta.get("structural_coverage", {})
    if not isinstance(structural_coverage, dict):
        structural_coverage = {}

    if structural_coverage.get("verified") is not True:
        return False



    return True


def sync_historical_fixtures(
    league_id: int,
    season: int,
    with_enrichment: bool = False,
    refresh: bool = False,
    historical_budget: int = None,
) -> dict:
    """
    Acquire historical fixtures (and optional enrichment) for a league/season
    and save them into persistent storage.
    """
    import time
    t_sync_start = time.perf_counter()
    total_db_write_duration = 0.0
    allowed_seasons = set(getattr(config, "TARGET_SEASONS", [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]))

    if season not in allowed_seasons:
        raise ValueError(
            f"Football historical acquisition only supports seasons "
            f"{sorted(allowed_seasons)}; received season={season}."
        )

    allowed_leagues = set(getattr(config, "ALLOWED_LEAGUE_IDS", []))

    if league_id not in allowed_leagues:
        raise ValueError(
            f"Football historical acquisition only supports configured leagues "
            f"{sorted(allowed_leagues)}; received league_id={league_id}."
        )

    if historical_budget is None:
        # Football historical fixture acquisition itself is API-key free (direct
        # Football-Data.co.uk CSV and SoccerData). The only API budget this path
        # can consume is the optional API-Football statistical-enrichment
        # backfill, which is bounded by the API-Football historical daily budget.
        historical_budget = int(getattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50))

    dataset_info = storage.get_historical_dataset_status(league_id, season)
    current_status = dataset_info["status"]
    existing_before = dataset_info["fixture_count"] or storage.get_historical_fixture_count(league_id, season)

    # Revalidate persisted fixture integrity and coverage before early return
    stored_fixtures = storage.get_historical_fixtures(league_id, season)
    if stored_fixtures and not refresh:
        exp_team_ids = data_resolver._get_authoritative_historical_team_ids(
            league_id, season, max_budget=historical_budget, allow_network=False
        )
        comp_fmt = "double_round_robin" if league_id in data_resolver.DOUBLE_ROUND_ROBIN_LEAGUE_IDS else "unknown"

        reval_meta = {
            "competition_format": comp_fmt,
            "max_budget": historical_budget,
            "allow_network": False,
        }
        if exp_team_ids:
            reval_meta["expected_team_ids"] = exp_team_ids

        reval_coverage = data_resolver._validate_historical_structural_coverage(
            stored_fixtures,
            season=season,
            provider_metadata=reval_meta,
            league_id=league_id,
        )
        reval_lifecycle, reval_lifecycle_reason, _ = get_competition_lifecycle(
            "football", league_id, season, stored_fixtures, coverage=reval_coverage
        )
        if reval_coverage.get("verified") is True and reval_lifecycle == COMPETITION_LIFECYCLE_COMPLETED:
            storage.mark_historical_dataset_complete(
                league_id,
                season,
                fixture_count=len(stored_fixtures),
                sport="football",
                acquisition_complete=True,
                rejected_count=0,
                empty_pages_count=0,
                expected_team_ids=reval_coverage.get("expected_team_ids"),
                coverage_unit=COVERAGE_UNIT_SINGLE_SHOT,
                provider_coverage={
                    "read_only_revalidation": {
                        "coverage_unit": "dataset",
                        "status": ACQUISITION_STATE_VALIDATED,
                    }
                },
            )
            print(f"Dataset already COMPLETE and revalidated for league {league_id} season {season}; API acquisition skipped.", flush=True)
            return {
                "league_id": league_id,
                "season": season,
                "status": "COMPLETE",
                "existing_before": existing_before,
                "fixtures_received": 0,
                "valid_fixtures": 0,
                "duplicates_skipped": 0,
                "newly_stored": 0,
                "already_existing_skipped": 0,
                "api_requests_consumed": 0,
                "quota_budget_stopped": False,
                "final_stored_count": existing_before,
                "enrichment_stored": 0,
                "skipped_reason": "Dataset already COMPLETE and revalidated",
            }
        elif current_status == "COMPLETE":
            reval_failure_reason = (
                f"lifecycle_{reval_lifecycle.lower()}_{reval_lifecycle_reason}"
                if reval_coverage.get("verified") is True
                else f"revalidation_failed_{reval_coverage.get('reason')}"
            )
            print(f"Dataset was marked COMPLETE but revalidation failed for league {league_id} season {season} ({reval_failure_reason}). Proceeding with sync.", flush=True)
            storage.mark_historical_dataset_incomplete(
                league_id,
                season,
                fixture_count=len(stored_fixtures),
                sport="football",
                error_reason=reval_failure_reason,
            )
            current_status = "INCOMPLETE"

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Approved football historical providers (in order): direct CSV, SoccerData
    # FBref, SoccerData MatchHistory, SoccerData Sofascore. API-Football and
    # football-data.org are not used on this path.

    quota_budget_stopped = False
    acquisition_failed = False
    last_error_reason = None

    fixtures_received = []
    start_p = 1
    expected_pages = 0
    pages_completed = 0
    acquisition_complete = False

    total_valid_fixtures = 0
    total_newly_stored = 0
    total_duplicates_skipped = 0
    total_rejected_count = 0
    total_empty_pages_count = 0
    all_rejection_reasons = {}

    # DataResolver instance initialized without preflight network calls.
    # A refresh must bypass the read-first DB shortcut so acquisition really
    # re-fetches instead of replaying already-persisted records.
    resolver = DataResolver(force_fallback=bool(refresh))

    page_exp_teams = None
    current_page = start_p
    while True:
        try:
            page_meta = resolver.get_league_fixtures_page(
                league_id, season, page=current_page, max_budget=historical_budget
            )
            page_fixtures = page_meta.get("fixtures", [])
            page_expected = page_meta.get("expected_pages", 1)
            page_source = str(page_meta.get("source") or "unknown")

            if page_meta.get("secondary_quota_exhausted"):
                quota_budget_stopped = True
                if not last_error_reason:
                    last_error_reason = "quota_budget_exhausted_during_acquisition"

            if page_meta.get("secondary_failed") and not page_fixtures:
                if page_meta.get("secondary_quota_exhausted"):
                    print(f"API Quota exhausted during fixture fetch on page {current_page}", flush=True)
                    break
                else:
                    acquisition_failed = True
                    last_error_reason = "secondary_provider_returned_no_fixtures"
                    print(f"Primary acquisition unavailable; fallback returned no fixtures on page {current_page}", flush=True)
                    break

            if isinstance(page_expected, int) and page_expected > 0:
                expected_pages = page_expected
            else:
                expected_pages = expected_pages or 1

            if not page_fixtures:
                total_empty_pages_count += 1

            fixtures_received.extend(page_fixtures)

            # Persist valid fixtures from this page immediately.
            # Records served from the internal DB are already persisted facts and
            # must never be re-inserted under a new source (that would duplicate
            # logical games and corrupt coverage counting).
            if page_source == "internal_db":
                completed_page_fixtures = []
            else:
                completed_page_fixtures = [
                    fixture
                    for fixture in page_fixtures
                    if (
                        isinstance(fixture, dict)
                        and fixture.get("fixture", {}).get("status", {}).get("short")
                        in ("FT", "AET", "PEN")
                    )
                ]

            save_result = storage.save_historical_fixtures(
                completed_page_fixtures,
                league_id,
                season,
                source=page_source,
                require_completed=True,
            )
            total_valid_fixtures += save_result.get("valid", 0)
            total_newly_stored += save_result.get("inserted", 0)
            total_duplicates_skipped += save_result.get("duplicates_skipped", 0)
            total_rejected_count += save_result.get("rejected_count", 0)
            total_db_write_duration += save_result.get("db_write_duration", 0.0)

            for r_reason, r_cnt in save_result.get("rejection_reasons", {}).items():
                all_rejection_reasons[r_reason] = all_rejection_reasons.get(r_reason, 0) + r_cnt

            pages_completed = current_page
            current_stored_count = storage.get_historical_fixture_count(league_id, season)

            # Update progress in manifest immediately
            safe_exp_pages = expected_pages if isinstance(expected_pages, int) and expected_pages > 0 else 1
            if isinstance(page_meta, dict):
                extracted = None
                if isinstance(page_meta.get("expected_team_ids"), (list, set)):
                    extracted = list(page_meta["expected_team_ids"])
                else:
                    pm = page_meta.get("provider_metadata")
                    if isinstance(pm, dict):
                        if isinstance(pm.get("expected_team_ids"), (list, set)):
                            extracted = list(pm["expected_team_ids"])
                        else:
                            sc = pm.get("structural_coverage")
                            if isinstance(sc, dict) and isinstance(sc.get("expected_team_ids"), (list, set)):
                                extracted = list(sc["expected_team_ids"])
                if extracted:
                    val_teams = data_resolver._validate_and_canonicalize_team_manifest(extracted, league_id, sport="football")
                    if val_teams:
                        page_exp_teams = val_teams

            storage.mark_historical_dataset_incomplete(
                league_id,
                season,
                fixture_count=current_stored_count,
                sport="football",
                expected_pages=safe_exp_pages,
                pages_completed=pages_completed,
                acquisition_complete=False,
                enrichment_status=dataset_info.get("enrichment_status", "NONE"),
                rejected_count=total_rejected_count,
                empty_pages_count=total_empty_pages_count,
                error_reason=last_error_reason,
                source=page_source,
                expected_team_ids=page_exp_teams,
            )
            if current_page >= safe_exp_pages:
                break
            current_page += 1

        except Exception as exc:
            acquisition_failed = True
            last_error_reason = "api_error_during_acquisition"
            print(f"API acquisition error on page {current_page}: {exc}", flush=True)
            break

    cum_rejected_cnt = (dataset_info.get("rejected_count", 0) if not refresh else 0) + total_rejected_count
    cum_empty_cnt = (dataset_info.get("empty_pages_count", 0) if not refresh else 0) + total_empty_pages_count

    if total_valid_fixtures > 0:
        # This run acquired valid fixtures of its own; report its own coverage units.
        safe_exp_pages = expected_pages if isinstance(expected_pages, int) and expected_pages > 0 else 1
        safe_pages_comp = pages_completed
        safe_rejected_cnt = cum_rejected_cnt
        safe_empty_cnt = cum_empty_cnt
    elif (dataset_info.get("expected_pages") or 0) > 0:
        # This run acquired nothing (provider failure, valid empty result, or an
        # unverified dataset). The coverage progress already recorded for the
        # persisted dataset is preserved instead of being reset to this attempt's
        # zero-yield units, so a later resume continues from recorded coverage.
        safe_exp_pages = dataset_info.get("expected_pages")
        safe_pages_comp = dataset_info.get("pages_completed") or 0
        safe_rejected_cnt = cum_rejected_cnt
        safe_empty_cnt = cum_empty_cnt
    else:
        safe_exp_pages = expected_pages if isinstance(expected_pages, int) and expected_pages > 0 else 1
        safe_pages_comp = dataset_info.get("pages_completed") or pages_completed
        safe_rejected_cnt = cum_rejected_cnt
        safe_empty_cnt = cum_empty_cnt

    stored_fixtures_all = storage.get_historical_fixtures(league_id, season)
    db_coverage_pre = data_resolver._validate_historical_structural_coverage(
        stored_fixtures_all,
        season=season,
        provider_metadata={"allow_network": False},
        league_id=league_id,
    )
    db_is_verified_pre = (db_coverage_pre.get("verified") is True and len(stored_fixtures_all) > 0)

    if (acquisition_failed or total_valid_fixtures == 0 or len(stored_fixtures_all) == 0) and not db_is_verified_pre:
        acquisition_failed = True
        if not last_error_reason:
            last_error_reason = "secondary_provider_returned_no_fixtures"
        storage.mark_historical_dataset_incomplete(
            league_id,
            season,
            fixture_count=len(stored_fixtures_all),
            sport="football",
            expected_pages=safe_exp_pages,
            pages_completed=safe_pages_comp,
            acquisition_complete=False,
            enrichment_status=dataset_info.get("enrichment_status", "NONE"),
            rejected_count=safe_rejected_cnt,
            empty_pages_count=safe_empty_cnt,
            error_reason=last_error_reason,
            source=page_source if total_valid_fixtures > 0 else "mixed",
        )
    elif db_is_verified_pre:
        acquisition_failed = False
        last_error_reason = None

    valid_fixtures_count = total_valid_fixtures
    newly_stored = total_newly_stored
    duplicates_skipped = total_duplicates_skipped
    rejected_count = total_rejected_count
    rejection_reasons = all_rejection_reasons
    already_existing_skipped = valid_fixtures_count - newly_stored

    enrichment_stored = 0

    # Optional statistical enrichment acquisition. It has its own API-Football
    # quota ceiling: when that ceiling is already reached, enrichment is skipped
    # and the stop is reported truthfully, without invalidating fixtures that were
    # acquired and validated without enrichment.
    enrichment_requests_used = storage.get_api_request_count(
        "api_football", datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    if (
        with_enrichment
        and not quota_budget_stopped
        and not acquisition_failed
        and enrichment_requests_used >= historical_budget
    ):
        quota_budget_stopped = True
        last_error_reason = "quota_budget_exhausted_during_enrichment"
        print(
            f"API-Football enrichment budget ceiling reached "
            f"({enrichment_requests_used}/{historical_budget} requests used today). Skipping enrichment.",
            flush=True,
        )

    if with_enrichment and not quota_budget_stopped and not acquisition_failed:
        all_stored = storage.get_historical_fixtures(league_id, season)
        finished_ids = [
            item.get("fixture", {}).get("id")
            for item in all_stored
            if isinstance(item, dict)
            and item.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN")
            and item.get("fixture", {}).get("id") is not None
        ]

        stored_enrichment = storage.get_historical_enrichment(finished_ids) if finished_ids else {}
        missing_enrichment_ids = [fid for fid in finished_ids if fid not in stored_enrichment]

        if missing_enrichment_ids:
            try:
                enriched_batch = resolver.get_enriched_fixtures(
                    missing_enrichment_ids, league_id=league_id, season=season, max_budget=historical_budget
                )
                if enriched_batch:
                    enrichment_stored = len(enriched_batch)
            except Exception as exc:
                acquisition_failed = True
                last_error_reason = "api_error_during_enrichment"
                print(f"API acquisition error during enrichment fetch: {exc}", flush=True)

    total_consumed = 0

    final_stored_count = storage.get_historical_fixture_count(league_id, season)

    # Determine enrichment status
    all_stored_fixtures = storage.get_historical_fixtures(league_id, season)
    finished_fixture_ids = [
        item.get("fixture", {}).get("id")
        for item in all_stored_fixtures
        if isinstance(item, dict)
        and item.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN")
        and item.get("fixture", {}).get("id") is not None
    ]
    stored_enrichments = storage.get_historical_enrichment(finished_fixture_ids) if finished_fixture_ids else {}

    if finished_fixture_ids:
        if len(stored_enrichments) >= len(finished_fixture_ids):
            enrichment_status = "COMPLETE"
        elif len(stored_enrichments) > 0:
            enrichment_status = "PARTIAL"
        else:
            enrichment_status = "NONE"
    else:
        enrichment_status = dataset_info.get("enrichment_status") or "NONE"

    # Determine exact source semantics and re-read persisted fixtures for final structural validation
    stored_fixtures_all = storage.get_historical_fixtures(league_id, season)

    sources_found = set()
    for f in stored_fixtures_all:
        if isinstance(f, dict):
            src = f.get("source") or f.get("provider_provenance", {}).get("provider")
            if src:
                sources_found.add(src)

    raw_page_source = str(page_source) if ('page_source' in locals() and page_source is not None) else "unknown"
    if len(sources_found) > 1:
        manifest_source = "mixed"
    elif len(sources_found) == 1:
        manifest_source = list(sources_found)[0]
    else:
        manifest_source = raw_page_source

    page_source_val = manifest_source
    page_meta_val = page_meta if 'page_meta' in locals() else {}

    db_coverage = data_resolver._validate_historical_structural_coverage(
        stored_fixtures_all,
        season=season,
        provider_metadata=page_meta_val.get("provider_metadata", {}) if isinstance(page_meta_val, dict) else {},
        league_id=league_id,
    )

    # Re-verify page metadata with DB structural verification result
    if isinstance(page_meta_val, dict):
        if "provider_metadata" not in page_meta_val or not isinstance(page_meta_val["provider_metadata"], dict):
            page_meta_val["provider_metadata"] = {}
        page_meta_val["provider_metadata"]["structural_coverage"] = db_coverage
        if db_coverage.get("verified") is True:
            page_meta_val["provider_metadata"]["is_complete"] = True
            page_meta_val["provider_metadata"]["is_partial"] = False

    is_fully_complete = _is_historical_provider_complete(
        page_source=page_source_val,
        page_meta=page_meta_val,
        valid_fixtures_count=valid_fixtures_count,
        expected_pages=expected_pages,
        pages_completed=pages_completed,
        quota_budget_stopped=quota_budget_stopped and last_error_reason != "quota_budget_exhausted_during_enrichment",
        acquisition_failed=acquisition_failed,
        rejected_count=rejected_count,
        empty_pages_count=total_empty_pages_count,
        final_stored_count=len(stored_fixtures_all),
    )

    # Competition lifecycle is decided independently of provider acquisition state.
    lifecycle_state, lifecycle_reason, lifecycle_evidence = get_competition_lifecycle(
        "football", league_id, season, stored_fixtures_all, coverage=db_coverage
    )
    lifecycle_completed = lifecycle_state == COMPETITION_LIFECYCLE_COMPLETED
    lifecycle_reason = f"{lifecycle_state.lower()}_{lifecycle_reason}"
    if not lifecycle_completed:
        is_fully_complete = False
        # Preserve a more specific acquisition failure reason if one exists; the
        # lifecycle state is reported separately in the log and manifest metadata.
        if not last_error_reason:
            last_error_reason = f"season_lifecycle_{lifecycle_reason}"

    acquisition_state = (
        ACQUISITION_STATE_VALIDATED if is_fully_complete
        else (ACQUISITION_STATE_FAILED if acquisition_failed else ACQUISITION_STATE_INCOMPLETE)
    )
    acquisition_complete = is_fully_complete

    provider_coverage_football = {
        "coverage_unit": COVERAGE_UNIT_SINGLE_SHOT,
        "status": acquisition_state,
        "providers_contributed": (page_meta_val.get("provider_metadata", {}) or {}).get("providers_contributed")
        if isinstance(page_meta_val, dict) else None,
        "provider_outcomes": (page_meta_val.get("provider_metadata", {}) or {}).get("provider_outcomes")
        if isinstance(page_meta_val, dict) else None,
    }

    print(
        f"[FOOTBALL] league {league_id} season {season}: provider={manifest_source}, "
        f"lifecycle={lifecycle_state} ({lifecycle_reason}), acquisition_state={acquisition_state}, "
        f"stored={final_stored_count}, coverage={db_coverage.get('reason') if isinstance(db_coverage, dict) else None}",
        flush=True,
    )

    exp_team_ids_to_save = db_coverage.get("expected_team_ids") if isinstance(db_coverage, dict) else None

    if is_fully_complete:
        storage.mark_historical_dataset_complete(
            league_id,
            season,
            fixture_count=final_stored_count,
            source=manifest_source,
            sport="football",
            expected_pages=safe_exp_pages,
            pages_completed=safe_pages_comp,
            acquisition_complete=True,
            enrichment_status=enrichment_status,
            rejected_count=0,
            empty_pages_count=0,
            expected_team_ids=exp_team_ids_to_save,
            coverage_unit=COVERAGE_UNIT_SINGLE_SHOT,
            provider_coverage=provider_coverage_football,
        )
        final_status = "COMPLETE"
    else:
        if quota_budget_stopped and last_error_reason != "quota_budget_exhausted_before_acquisition":
            last_error_reason = "quota_budget_exhausted_during_acquisition"
        elif not last_error_reason:
            if isinstance(db_coverage, dict) and db_coverage.get("verified") is not True:
                last_error_reason = f"structural_coverage_failed_{db_coverage.get('reason')}"
            elif page_source_val not in ("football_data_csv", "soccerdata_fbref", "soccerdata_match_history", "soccerdata_sofascore", "mixed"):
                last_error_reason = "provider_coverage_incomplete"
            else:
                last_error_reason = "acquisition_incomplete"

        storage.mark_historical_dataset_incomplete(
            league_id,
            season,
            fixture_count=final_stored_count,
            source=manifest_source,
            sport="football",
            expected_pages=safe_exp_pages,
            pages_completed=safe_pages_comp,
            acquisition_complete=False,
            enrichment_status=enrichment_status if pages_completed > 0 else dataset_info.get("enrichment_status", "NONE"),
            rejected_count=safe_rejected_cnt,
            empty_pages_count=safe_empty_cnt,
            error_reason=last_error_reason,
            expected_team_ids=exp_team_ids_to_save,
            coverage_unit=COVERAGE_UNIT_SINGLE_SHOT,
            provider_coverage=provider_coverage_football,
        )
        final_status = "INCOMPLETE"

    total_sync_elapsed = time.perf_counter() - t_sync_start
    report = {
        "sport": "football",
        "league_id": league_id,
        "season": season,
        "status": final_status,
        "source": manifest_source,
        "providers_contributed": provider_coverage_football.get("providers_contributed"),
        "provider_outcomes": provider_coverage_football.get("provider_outcomes"),
        "provider_coverage": provider_coverage_football,
        "incomplete_reason": None if is_fully_complete else last_error_reason,
        "existing_before": existing_before,
        "fixtures_received": len(fixtures_received),
        "valid_fixtures": valid_fixtures_count,
        "duplicates_skipped": duplicates_skipped,
        "rejected_count": cum_rejected_cnt,
        "rejection_reasons": rejection_reasons,
        "newly_stored": newly_stored,
        "already_existing_skipped": already_existing_skipped,
        "expected_pages": safe_exp_pages,
        "pages_completed": safe_pages_comp,
        "coverage_unit": COVERAGE_UNIT_SINGLE_SHOT,
        "acquisition_complete": acquisition_complete,
        "acquisition_state": acquisition_state,
        "lifecycle_state": lifecycle_state,
        "lifecycle_reason": lifecycle_reason,
        "api_requests_consumed": total_consumed,
        "historical_budget": historical_budget,
        "quota_budget_stopped": quota_budget_stopped,
        "final_stored_count": final_stored_count,
        "enrichment_stored": enrichment_stored,
        "total_sync_duration": round(total_sync_elapsed, 3),
        "db_write_duration": round(total_db_write_duration, 3),
    }

    _print_sync_report(report)
    return report


def _print_sync_report(report: dict) -> None:
    print("\n=== HISTORICAL SYNC REPORT ===")
    print(f"Sport: {report.get('sport', 'football')}")
    print(f"League ID: {report['league_id']}")
    print(f"Season: {report['season']}")
    print(f"Dataset Status: {report['status']}")
    if report.get("acquisition_state") is not None:
        print(f"Acquisition state: {report.get('acquisition_state')}")
    if report.get("lifecycle_state") is not None:
        print(f"Competition lifecycle: {report.get('lifecycle_state')} ({report.get('lifecycle_reason')})")
    if report.get("source") is not None:
        print(f"Selected provider/source: {report.get('source')}")
    if report.get("providers_contributed"):
        print(f"Providers contributing persisted records: {', '.join(report.get('providers_contributed') or [])}")
    if report.get("provider_outcomes"):
        for outcome_provider, outcome_value in report["provider_outcomes"].items():
            if isinstance(outcome_value, dict):
                print(
                    f"  provider outcome {outcome_provider}: "
                    f"{outcome_value.get('outcome')} ({outcome_value.get('reason')})"
                )
            else:
                print(f"  provider outcome {outcome_provider}: {outcome_value}")
    if report.get("coverage_unit") is not None:
        print(
            f"Coverage unit: {report.get('coverage_unit')} "
            f"({report.get('completed_coverage_units', report.get('pages_completed'))}/"
            f"{report.get('expected_coverage_units', report.get('expected_pages'))})"
        )
    if report.get("provider_coverage"):
        for provider_name, prov in report["provider_coverage"].items():
            if isinstance(prov, dict):
                print(
                    f"  provider {provider_name}: unit={prov.get('coverage_unit')} "
                    f"{prov.get('completed')}/{prov.get('expected')} status={prov.get('status')}"
                )
    print(f"Existing fixtures before sync: {report['existing_before']}")
    print(f"Fixtures received from provider(s): {report['fixtures_received']}")
    print(f"Valid fixtures: {report['valid_fixtures']}")
    print(f"Duplicates skipped: {report['duplicates_skipped']}")
    print(f"Rejected fixtures: {report.get('rejected_count', 0)}")
    if report.get("rejection_reasons"):
        print(f"Rejection reasons: {report['rejection_reasons']}")
    print(f"Newly stored fixtures: {report['newly_stored']}")
    print(f"Already-existing fixtures skipped: {report['already_existing_skipped']}")
    print(f"API requests consumed: {report['api_requests_consumed']}")
    print(f"Historical Daily Budget: {report.get('historical_budget')}")
    print(f"Quota budget stopped job: {report['quota_budget_stopped']}")
    print(f"Enriched records stored: {report.get('enrichment_stored', 0)}")
    print(f"Final stored fixture count: {report['final_stored_count']}")
    if "total_sync_duration" in report:
        print(f"Total sync duration: {report['total_sync_duration']:.3f}s (DB write duration: {report.get('db_write_duration', 0):.3f}s)")
    if report.get("status") != "COMPLETE":
        print(f"Reason dataset remains incomplete: {report.get('incomplete_reason') or report.get('error_reason') or 'unspecified'}")
    if report.get("status") == "COMPLETE" and report.get("api_requests_consumed") == 0:
        print("Dataset already COMPLETE; acquisition skipped.")
    print("=== END HISTORICAL SYNC REPORT ===\n")


import basketball_api
import espn_basketball_provider


def _expected_min_season_games(league_id, season):
    """
    Independently established expected game coverage for a basketball season.

    The NBA's published regular-season game totals are the authority for the
    season (see config.NBA_EXPECTED_MIN_REGULAR_SEASON_GAMES). Returns None when
    no authoritative expectation exists for the competition or season, so callers
    fail closed instead of treating the stored record count as its own evidence of
    completeness.
    """
    label = parse_season_label(season)
    if label is None:
        return None
    try:
        lid = int(league_id)
    except (TypeError, ValueError):
        return None
    if lid != NBA_LEAGUE_ID:
        return None

    expected = (getattr(config, "NBA_EXPECTED_MIN_REGULAR_SEASON_GAMES", {}) or {}).get(label)
    if not isinstance(expected, int) or isinstance(expected, bool) or expected <= 0:
        return None
    return expected


def _basketball_game_source(game):
    """Return the provider that produced a persisted basketball game, or None."""
    if not isinstance(game, dict):
        return None
    if game.get("_storage_source"):
        src = game.get("_storage_source")
    else:
        src = game.get("source")
        if not src and isinstance(game.get("provider_provenance"), dict):
            src = game["provider_provenance"].get("provider")
    return src.strip() if isinstance(src, str) and src.strip() else None


def _basketball_game_season_type(game):
    """
    Return the recorded competition phase of a persisted basketball game.

    ESPN records the provider-native season type (preseason, regular_season,
    postseason, all_star) on each record, and API-Basketball reports the stage of
    the game it returned. Both are provider-native evidence recorded on the record
    itself; neither is inferred from dates, team names or scores. A record whose
    provider supplied no recognisable phase returns None, which callers treat as
    unclassified rather than as a regular-season claim.
    """
    if not isinstance(game, dict):
        return None
    provenance = game.get("provider_provenance") if isinstance(game.get("provider_provenance"), dict) else {}
    candidates = (
        provenance.get("season_type"),
        game.get("season_type"),
        game.get("game_type"),
        # API-Basketball reports a free-text stage alongside each game. Only the
        # documented, unambiguous labels are recognised; anything else stays
        # unclassified instead of being guessed.
        provenance.get("stage"),
        game.get("stage"),
        game.get("season_stage"),
    )
    for raw in candidates:
        if not isinstance(raw, str) or not raw.strip():
            continue
        normalized = raw.strip().lower().replace("-", "_").replace(" ", "_")
        mapped = _BASKETBALL_STAGE_LABELS.get(normalized)
        if mapped:
            return mapped
        if normalized in ("regular_season", "regular", "preseason", "postseason", "play_in", "all_star"):
            return normalized
    return None


# Provider-native stage labels that unambiguously name a competition phase.
# ``regular`` is the api-sports label for the regular season; playoff and
# exhibition stages are kept distinct so they can never be counted as regular
# season coverage. Unlisted labels stay unclassified.
_BASKETBALL_STAGE_LABELS = {
    "regular_season": "regular_season",
    "regular": "regular_season",
    "regularseason": "regular_season",
    "preseason": "preseason",
    "pre_season": "preseason",
    "playoffs": "postseason",
    "postseason": "postseason",
    "post_season": "postseason",
    "play_in": "play_in",
    "playin": "play_in",
    "all_star": "all_star",
    "allstar": "all_star",
}


def _basketball_season_type_counts(stored_games) -> dict:
    """
    Count persisted basketball games by recorded competition phase.

    Reported with every basketball acquisition and verification run so that
    regular-season, preseason, play-in, postseason and All-Star coverage are
    visibly distinct, and records persisted before the provider recorded a phase
    are visible as ``unclassified`` instead of being silently folded into the
    regular-season total.
    """
    counts = {}
    for game in stored_games if isinstance(stored_games, list) else []:
        phase = _basketball_game_season_type(game) or "unclassified"
        counts[phase] = counts.get(phase, 0) + 1
    return counts


def _validate_historical_basketball_dataset(stored_games: list, league_id: int, season: int, dataset_info: dict) -> bool:
    """
    Validate persisted basketball games against basketball-specific completeness rules:
    - Non-empty game list matching count metadata.
    - Source-scoped game identity (source, game_id) present, valid, and unique.
    - Requested league and season match.
    - Participating home and away teams valid and distinct.
    - Required game date, completed status, and valid scores present.
    - Positive, fully completed provider coverage units (ESPN months or API pages)
      and zero rejected/empty counters. A zero/zero coverage count is never proof
      of completed acquisition and fails closed.
    - Distinguishes completed historical seasons from ongoing seasons using game status and date evidence (no blanket calendar-year rejection).
    """
    if not isinstance(stored_games, list) or not stored_games:
        return False

    seen_game_ids = set()
    counts_by_source = {}
    for game in stored_games:
        is_valid, reason, _ = storage.validate_historical_basketball_game(
            game, target_league_id=league_id, target_season=season, require_completed=True
        )
        if not is_valid:
            return False

        src = _basketball_game_source(game)

        if not src:
            return False
        counts_by_source[src] = counts_by_source.get(src, 0) + 1

        gid = game.get("id") or game.get("game_id")
        if gid is None or isinstance(gid, bool):
            return False
        try:
            gid_int = int(gid)
            if gid_int <= 0:
                return False
        except (ValueError, TypeError):
            return False

        key = (src.strip(), gid_int)
        if key in seen_game_ids:
            return False
        seen_game_ids.add(key)

    manifest_count = dataset_info.get("fixture_count", 0) if isinstance(dataset_info, dict) else 0
    e_pages = dataset_info.get("expected_pages", 0) if isinstance(dataset_info, dict) else 0
    p_completed = dataset_info.get("pages_completed", 0) if isinstance(dataset_info, dict) else 0
    rej_cnt = dataset_info.get("rejected_count", 0) if isinstance(dataset_info, dict) else 0
    empty_pages_count = dataset_info.get("empty_pages_count", 0) if isinstance(dataset_info, dict) else 0

    # Counters are evidence, so each must be a real integer before it participates in
    # any completeness decision. A non-numeric value (a missing field filled with a
    # sentinel, or a caller error) is not a zero and cannot be compared with record
    # counts, so the dataset fails closed here instead of raising mid-validation.
    for counter_name, counter_value in (
        ("fixture_count", manifest_count),
        ("expected_pages", e_pages),
        ("pages_completed", p_completed),
        ("rejected_count", rej_cnt),
        ("empty_pages_count", empty_pages_count),
    ):
        if isinstance(counter_value, bool) or not isinstance(counter_value, int):
            logger.warning(
                "Basketball dataset validation rejected: %s is not an integer (%r).",
                counter_name,
                counter_value,
            )
            return False

    provider_coverage = dataset_info.get("provider_coverage") if isinstance(dataset_info, dict) else None
    if not isinstance(provider_coverage, dict) or not provider_coverage:
        return False

    # Empty coverage units are legitimate only where a provider recorded the
    # period as successfully acquired with no games (for example an NBA month that
    # genuinely contains no fixtures). A failed, missing or unverified unit is
    # never treated as a legitimate empty period, and empty units never stand in
    # for acquired games. This mirrors the storage-level completion rule.
    verified_empty_units = 0
    for provider_entry in provider_coverage.values():
        if not isinstance(provider_entry, dict):
            return False
        entry_empty = provider_entry.get("valid_empty_units")
        if isinstance(entry_empty, int) and not isinstance(entry_empty, bool) and entry_empty > 0:
            verified_empty_units += entry_empty

    if manifest_count != len(stored_games) or e_pages <= 0 or p_completed != e_pages or rej_cnt != 0:
        return False

    if empty_pages_count > verified_empty_units:
        return False

    if len(seen_game_ids) != len(stored_games):
        return False

    # Provider-specific coverage units and their recorded outcomes must be present
    # and internally consistent. ESPN months and API pages are separate
    # namespaces, each checked against its own recorded expectation. At least one
    # provider must have completed with a VALIDATED outcome; a provider that was
    # attempted and failed (for example ESPN before the API-Basketball fallback)
    # is reported truthfully without invalidating the validated route.
    validated_provider_found = False
    for provider_entry in provider_coverage.values():
        if not isinstance(provider_entry, dict):
            return False
        expected_units = provider_entry.get("expected")
        completed_units = provider_entry.get("completed")
        if not isinstance(expected_units, int) or isinstance(expected_units, bool) or expected_units < 0:
            return False
        if not isinstance(completed_units, int) or isinstance(completed_units, bool) or completed_units < 0:
            return False
        if completed_units > expected_units:
            return False
        if provider_entry.get("status") == ACQUISITION_STATE_VALIDATED:
            if expected_units <= 0:
                return False
            validated_provider_found = True

    if not validated_provider_found:
        return False

    # Per-source coverage reconciliation: every persisted record is attributed to
    # the provider that actually produced it, and that provider's own coverage
    # units must be complete and validated. ESPN months and API-Basketball pages
    # never substitute for each other, so a dataset holding ESPN rows can never be
    # described (or completed) by an API-Basketball coverage record alone.
    for source_name, source_count in counts_by_source.items():
        source_entry = provider_coverage.get(source_name)
        if not isinstance(source_entry, dict):
            return False
        if source_entry.get("status") != ACQUISITION_STATE_VALIDATED:
            return False
        entry_expected = source_entry.get("expected")
        entry_completed = source_entry.get("completed")
        if not isinstance(entry_expected, int) or isinstance(entry_expected, bool) or entry_expected <= 0:
            return False
        if entry_completed != entry_expected:
            return False
        if source_count <= 0:
            return False
        if source_name == "espn_url":
            # ESPN coverage is measured in the season's own monthly scoreboards
            # (13 for the COVID-extended 2019-20 season), never in API pages.
            if entry_expected != espn_basketball_provider.get_season_month_count(season):
                return False
            # A missing or None value is not proof of NBA membership either: the
            # persisted validator must enforce the same positive-evidence rule as
            # the acquisition gate in sync_historical_basketball_games().
            if source_entry.get("competition_verified") is not True:
                return False

    # A manifest that agrees with its own records is not proof that the season is
    # complete. The independently established NBA regular-season total is the
    # authority, and a season without such an expectation fails closed.
    expected_min_games = _expected_min_season_games(league_id, season)
    if expected_min_games is None:
        return False

    # Event classification separates regular-season coverage from preseason,
    # postseason and All-Star events, so a playoff or exhibition schedule can never
    # substitute for missing regular-season games. Records persisted before the
    # provider recorded a phase are counted only when they are not known to belong
    # to another phase; anything classified otherwise is excluded from the
    # regular-season total.
    regular_season_games = 0
    non_regular_season_games = 0
    unclassified_games = 0
    for game in stored_games:
        phase = _basketball_game_season_type(game)
        if phase in ("regular_season", "regular"):
            regular_season_games += 1
        elif phase in (None, "unclassified"):
            # A record with no recorded phase could be a regular-season game, a
            # preseason, postseason, play-in, All-Star or exhibition game. Missing
            # classification is never proof of the regular season, so it cannot
            # satisfy the regular-season minimum; it is kept and reported as
            # unclassified instead of being folded into either count.
            unclassified_games += 1
        else:
            non_regular_season_games += 1

    if regular_season_games < expected_min_games:
        return False

    return True


def sync_historical_basketball_games(
    league_id: int,
    season: int,
    refresh: bool = False,
    historical_budget: int = None,
) -> dict:
    """
    Acquire historical basketball games for a league/season and save them into persistent storage.
    """
    if historical_budget is None:
        historical_budget = int(getattr(config, "API_BASKETBALL_HISTORICAL_DAILY_BUDGET", 50))

    dataset_info = storage.get_historical_dataset_status(league_id, season, sport="basketball")
    current_status = dataset_info["status"]
    existing_before = dataset_info["fixture_count"] or storage.get_historical_basketball_game_count(league_id, season)

    if current_status == "COMPLETE" and not refresh:
        stored_games = storage.get_historical_basketball_games(league_id, season, include_storage_source=True)
        if _validate_historical_basketball_dataset(stored_games, league_id, season, dataset_info):
            # The persisted records and the recorded provider coverage agree, but
            # the competition lifecycle is recalculated independently before the
            # COMPLETE manifest is retained: an ongoing or unverifiable season can
            # never be kept complete on the strength of its stored records alone.
            retention_coverage = {
                "verified": True,
                "reason": None,
                "coverage_unit": dataset_info.get("coverage_unit"),
                "expected_coverage_units": dataset_info.get("expected_pages"),
                "completed_coverage_units": dataset_info.get("pages_completed"),
                "provider_coverage": dataset_info.get("provider_coverage"),
            }
            retained_lifecycle, retained_lifecycle_reason, _ = get_competition_lifecycle(
                "basketball", league_id, season, stored_games, coverage=retention_coverage
            )
            if retained_lifecycle != COMPETITION_LIFECYCLE_COMPLETED:
                print(
                    f"Basketball dataset was marked COMPLETE but its competition lifecycle is "
                    f"{retained_lifecycle} ({retained_lifecycle_reason}) for league {league_id} "
                    f"season {season}. Proceeding with sync.",
                    flush=True,
                )
                storage.mark_historical_dataset_incomplete(
                    league_id,
                    season,
                    fixture_count=len(stored_games),
                    sport="basketball",
                    error_reason=f"season_lifecycle_{retained_lifecycle.lower()}_{retained_lifecycle_reason}",
                )
                current_status = "INCOMPLETE"
            else:
                print(f"Basketball dataset already COMPLETE and revalidated for league {league_id} season {season}; API acquisition skipped.", flush=True)
                return {
                "sport": "basketball",
                "league_id": league_id,
                "season": season,
                "status": "COMPLETE",
                "existing_before": existing_before,
                "fixtures_received": 0,
                "valid_fixtures": 0,
                "duplicates_skipped": 0,
                "newly_stored": 0,
                "already_existing_skipped": 0,
                "api_requests_consumed": 0,
                "quota_budget_stopped": False,
                "final_stored_count": existing_before,
                "skipped_reason": "Dataset already COMPLETE and revalidated",
            }
        else:
            print(f"Basketball dataset was marked COMPLETE but revalidation failed for league {league_id} season {season}. Proceeding with sync.", flush=True)
            storage.mark_historical_dataset_incomplete(
                league_id,
                season,
                fixture_count=len(stored_games),
                sport="basketball",
                error_reason="revalidation_failed",
            )
            current_status = "INCOMPLETE"

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    initial_request_count = storage.get_api_request_count("api_basketball", today_str, request_type="historical")

    quota_budget_stopped = False
    acquisition_failed = False
    last_error_reason = None

    games_received = []
    total_valid_games = 0
    total_newly_stored = 0
    total_duplicates_skipped = 0
    total_rejected_count = 0
    total_empty_pages_count = 0
    all_rejection_reasons = {}

    # 1. ESPN URL-based primary acquisition for the NBA (league_id == 12).
    # Provider selection is explicit and deterministic: ESPN runs first for the
    # NBA when enabled; API-Basketball is only attempted when ESPN did not
    # produce independently validated coverage. No test/mock inspection decides
    # which production provider runs.
    # ESPN coverage is measured in the season's monthly scoreboards: nine for a
    # standard NBA season, and more for the COVID-extended 2019-20 season whose
    # resumption months must also be acquired.
    espn_expected_units = espn_basketball_provider.get_season_month_count(season)
    espn_attempted = False
    espn_coverage = None
    espn_completed_units = 0
    espn_rejected_count = 0
    espn_valid_empty_units = 0
    espn_dataset_validated = False

    if league_id == NBA_LEAGUE_ID and getattr(config, "ENABLE_ESPN_BASKETBALL", True):
        espn_attempted = True
        try:
            espn_games, coverage_meta = espn_basketball_provider.fetch_espn_nba_season_games(league_id, season)
            if isinstance(coverage_meta, dict):
                espn_coverage = coverage_meta
                espn_completed_units = int(coverage_meta.get("months_succeeded") or 0)
            if espn_games:
                games_received.extend(espn_games)
                save_res = storage.save_historical_basketball_games(
                    espn_games, league_id, season, source="espn_url", require_completed=True
                )
                total_valid_games += save_res.get("valid", 0)
                total_newly_stored += save_res.get("inserted", 0)
                total_duplicates_skipped += save_res.get("duplicates_skipped", 0)
                espn_rejected_count = save_res.get("rejected_count", 0)
                total_rejected_count += espn_rejected_count
                for r_reason, r_cnt in save_res.get("rejection_reasons", {}).items():
                    all_rejection_reasons[r_reason] = all_rejection_reasons.get(r_reason, 0) + r_cnt
        except Exception as exc:
            print(f"ESPN NBA acquisition attempt failed for season {season}: {exc}", flush=True)

    # ESPN coverage is measured in the season's own monthly scoreboards (nine for a
    # standard NBA season, thirteen for the COVID-extended 2019-20 season, eight for
    # the late-starting 2020-21 season). A month is considered covered when its
    # request succeeded and the payload proved NBA competition membership; a
    # successful request that returns zero events is a valid no-games result (for
    # example early October), not a failure. Monthly units are never described as a
    # single API page, and a payload whose competition metadata contradicts the NBA
    # is never accepted as covered.
    espn_coverage_complete = bool(
        espn_attempted
        and isinstance(espn_coverage, dict)
        and espn_coverage.get("months_attempted") == espn_expected_units
        and espn_coverage.get("months_failed") == 0
        and espn_coverage.get("months_succeeded") == espn_expected_units
        and espn_coverage.get("all_months_succeeded") is True
        # Competition membership must be positively proven. A missing or None
        # value is not proof, so only an explicit True can validate ESPN coverage.
        and espn_coverage.get("competition_verified") is True
    )

    stored_espn_check = storage.get_historical_basketball_games(
        league_id, season, source="espn_url", include_storage_source=True
    )
    # A month whose request succeeded but which legitimately contains no games
    # (for example early October) is recorded as a verified empty coverage unit. It
    # is never reported as a failure, and never as acquired games.
    if isinstance(espn_coverage, dict):
        for month_detail in espn_coverage.get("monthly_details") or []:
            if (
                isinstance(month_detail, dict)
                and month_detail.get("status") == "SUCCESS"
                and int(month_detail.get("event_count") or 0) == 0
            ):
                espn_valid_empty_units += 1

    if espn_coverage_complete and stored_espn_check:
        espn_eval_info = {
            "fixture_count": len(stored_espn_check),
            "expected_pages": espn_expected_units,
            "pages_completed": espn_completed_units,
            "rejected_count": espn_rejected_count,
            # A month that succeeded with no events is a verified empty unit, not a
            # missing acquisition unit, so it is reported as such here instead of
            # being hidden behind a zero counter.
            "empty_pages_count": espn_valid_empty_units,
            "coverage_unit": COVERAGE_UNIT_ESPN_MONTHS,
            "provider_coverage": {
                "espn_url": {
                    "coverage_unit": "months",
                    "expected": espn_expected_units,
                    "completed": espn_completed_units,
                    "valid_empty_units": espn_valid_empty_units,
                    # The acquisition gate above requires positively proven NBA
                    # membership, so the proven value is carried into the coverage
                    # evidence that persisted validation reads. Without it an
                    # ESPN-covered season failed its own persisted revalidation
                    # and could never be marked COMPLETE.
                    "competition_verified": espn_coverage.get("competition_verified"),
                    "status": ACQUISITION_STATE_VALIDATED,
                }
            },
        }
        espn_dataset_validated = _validate_historical_basketball_dataset(
            stored_espn_check, league_id, season, espn_eval_info
        )

    # API-Basketball pagination is tracked in its own units, independently of
    # ESPN months. These variables never describe ESPN acquisition.
    api_attempted = False
    api_expected_pages = 0
    api_pages_completed = 0

    acquisition_complete = espn_dataset_validated

    if initial_request_count >= historical_budget and not acquisition_complete:
        quota_budget_stopped = True
        last_error_reason = "quota_budget_exhausted_before_acquisition"
        print(
            f"Historical daily budget ceiling reached ({initial_request_count}/{historical_budget} basketball requests used today). Skipping API acquisition.",
            flush=True,
        )

    # 2. Fallback to API-Basketball when ESPN coverage is absent or unvalidated.
    # Pagination coverage is tracked in API pages using the provider's own
    # paging metadata; none of these counters describe ESPN months.
    if not acquisition_complete and not quota_budget_stopped:
        api_attempted = True
        current_page = 1
        while True:
            try:
                page_meta = basketball_api.get_league_games_page(
                    league_id, season, page=current_page, max_budget=historical_budget
                )
                # Pagination metadata decides how many coverage units the dataset
                # requires, so it is validated at the boundary where the provider's
                # response first enters the acquisition loop. A missing, non-numeric
                # or non-positive page total is a failed acquisition unit, never a
                # completed page, and a non-list game payload is never extended into
                # the received set.
                page_games = page_meta.get("games") if isinstance(page_meta, dict) else None
                page_expected = page_meta.get("expected_pages") if isinstance(page_meta, dict) else None
                if isinstance(page_expected, bool) or not isinstance(page_expected, int) or page_expected < 1:
                    acquisition_failed = True
                    last_error_reason = "invalid_pagination_metadata"
                    print(
                        f"Basketball pagination metadata is invalid on page {current_page} "
                        f"(expected_pages={page_expected!r}). Failing closed.",
                        flush=True,
                    )
                    break
                if not isinstance(page_games, list):
                    acquisition_failed = True
                    last_error_reason = "invalid_page_payload"
                    print(
                        f"Basketball page {current_page} did not return a game list. Failing closed.",
                        flush=True,
                    )
                    break

                if api_expected_pages == 0:
                    api_expected_pages = page_expected
                elif page_expected != api_expected_pages:
                    acquisition_failed = True
                    last_error_reason = "pagination_metadata_mismatch"
                    print(
                        f"Basketball pagination error: Total pages changed during sync ({api_expected_pages} -> {page_expected}). Failing closed.",
                        flush=True,
                    )
                    break

                if not page_games:
                    total_empty_pages_count += 1

                games_received.extend(page_games)

                save_result = storage.save_historical_basketball_games(
                    page_games, league_id, season, require_completed=True
                )
                total_valid_games += save_result.get("valid", 0)
                total_newly_stored += save_result.get("inserted", 0)
                total_duplicates_skipped += save_result.get("duplicates_skipped", 0)
                total_rejected_count += save_result.get("rejected_count", 0)

                for r_reason, r_cnt in save_result.get("rejection_reasons", {}).items():
                    all_rejection_reasons[r_reason] = all_rejection_reasons.get(r_reason, 0) + r_cnt

                api_pages_completed = current_page
                current_stored_count = storage.get_historical_basketball_game_count(league_id, season)

                if current_page >= api_expected_pages:
                    acquisition_complete = True

                storage.mark_historical_dataset_incomplete(
                    league_id,
                    season,
                    fixture_count=current_stored_count,
                    sport="basketball",
                    expected_pages=api_expected_pages,
                    pages_completed=api_pages_completed,
                    acquisition_complete=acquisition_complete,
                    rejected_count=total_rejected_count,
                    empty_pages_count=total_empty_pages_count,
                    error_reason=last_error_reason,
                    coverage_unit=COVERAGE_UNIT_API_PAGES,
                )
                if acquisition_complete or current_page >= api_expected_pages:
                    break
                current_page += 1

            except basketball_api.APIBasketballQuotaExhaustedError as exc:
                quota_budget_stopped = True
                last_error_reason = "quota_budget_exhausted_during_acquisition"
                print(f"API Quota exhausted during basketball games fetch on page {current_page}: {exc}", flush=True)
                break
            except Exception as exc:
                acquisition_failed = True
                last_error_reason = "api_error_during_acquisition"
                print(f"API acquisition error during basketball games fetch on page {current_page}: {exc}", flush=True)
                break

    requests_after = storage.get_api_request_count("api_basketball", today_str, request_type="historical")
    total_consumed = requests_after - initial_request_count

    valid_count = total_valid_games
    newly_stored = total_newly_stored
    duplicates_skipped = total_duplicates_skipped
    rejected_count = total_rejected_count
    rejection_reasons = all_rejection_reasons
    already_existing_skipped = valid_count - newly_stored

    final_stored_count = storage.get_historical_basketball_game_count(league_id, season)

    cum_rejected_cnt_b = (dataset_info.get("rejected_count", 0) if not refresh else 0) + total_rejected_count
    cum_empty_cnt_b = (dataset_info.get("empty_pages_count", 0) if not refresh else 0) + total_empty_pages_count

    # Which providers actually produced the persisted records, as distinct from
    # providers that were merely attempted. The manifest source and every provider
    # entry report this distinction, so persisted ESPN rows can never be described
    # by an API-Basketball manifest (or the reverse).
    persisted_by_source = {}
    for persisted_game in storage.get_historical_basketball_games(
        league_id, season, include_storage_source=True
    ):
        persisted_source = _basketball_game_source(persisted_game)
        if persisted_source:
            persisted_by_source[persisted_source] = persisted_by_source.get(persisted_source, 0) + 1

    if len(persisted_by_source) == 1:
        manifest_source_b = next(iter(persisted_by_source))
    elif persisted_by_source:
        manifest_source_b = "mixed"
    else:
        manifest_source_b = "unknown"

    # Provider-specific coverage. ESPN months and API pages are kept in separate
    # namespaces and only combined when both providers actually contributed; the
    # manifest records which unit was counted.
    provider_coverage = {}
    if espn_attempted:
        provider_coverage["espn_url"] = {
            "coverage_unit": "months",
            "expected": espn_expected_units,
            "completed": espn_completed_units,
            "valid_empty_units": espn_valid_empty_units,
            "months_attempted": (espn_coverage or {}).get("months_attempted") if isinstance(espn_coverage, dict) else None,
            "months_failed": (espn_coverage or {}).get("months_failed") if isinstance(espn_coverage, dict) else None,
            "competition_verified": (espn_coverage or {}).get("competition_verified") if isinstance(espn_coverage, dict) else None,
            "events_accepted": (espn_coverage or {}).get("events_accepted") if isinstance(espn_coverage, dict) else None,
            "events_rejected": (espn_coverage or {}).get("events_rejected") if isinstance(espn_coverage, dict) else None,
            "rejection_reasons": (espn_coverage or {}).get("rejection_reasons") if isinstance(espn_coverage, dict) else None,
            "attempted": True,
            "contributed": bool(persisted_by_source.get("espn_url")),
            "records_persisted": persisted_by_source.get("espn_url", 0),
            "status": (
                ACQUISITION_STATE_VALIDATED if espn_dataset_validated
                else (ACQUISITION_STATE_FAILED if espn_coverage is None else ACQUISITION_STATE_INCOMPLETE)
            ),
            "monthly_details": (espn_coverage or {}).get("monthly_details") if isinstance(espn_coverage, dict) else None,
        }
    if api_attempted:
        provider_coverage["api_basketball"] = {
            "coverage_unit": "pages",
            "expected": api_expected_pages,
            "completed": api_pages_completed,
            "attempted": True,
            "contributed": bool(persisted_by_source.get("api_basketball")),
            "records_persisted": persisted_by_source.get("api_basketball", 0),
            "status": (
                ACQUISITION_STATE_VALIDATED
                if (acquisition_complete and api_expected_pages > 0 and api_pages_completed >= api_expected_pages)
                else (ACQUISITION_STATE_FAILED if acquisition_failed else ACQUISITION_STATE_INCOMPLETE)
            ),
        }

    # The dataset's coverage unit describes the acquisition route that is actually
    # being counted for completeness. When ESPN delivers validated coverage its nine
    # monthly scoreboards are the units; when ESPN did not deliver validated coverage
    # and the documented API-Basketball fallback did, the units are API pages; the
    # two namespaces are combined only when both routes produced validated coverage.
    # A failed ESPN attempt therefore never inflates the expected unit total of a
    # fallback-completed dataset, and ESPN months are never reported as API pages
    # (or the reverse).
    espn_contributed_validated_coverage = bool(espn_dataset_validated)
    api_contributed_validated_coverage = bool(
        acquisition_complete and api_expected_pages > 0 and api_pages_completed >= api_expected_pages
    )

    if espn_contributed_validated_coverage and api_contributed_validated_coverage:
        dataset_coverage_unit = f"{COVERAGE_UNIT_ESPN_MONTHS}+{COVERAGE_UNIT_API_PAGES}"
        dataset_expected_units = espn_expected_units + api_expected_pages
        dataset_completed_units = espn_completed_units + api_pages_completed
    elif espn_contributed_validated_coverage:
        dataset_coverage_unit = COVERAGE_UNIT_ESPN_MONTHS
        dataset_expected_units = espn_expected_units
        dataset_completed_units = espn_completed_units
    elif api_attempted:
        dataset_coverage_unit = COVERAGE_UNIT_API_PAGES
        dataset_expected_units = api_expected_pages
        dataset_completed_units = api_pages_completed
    elif espn_attempted:
        dataset_coverage_unit = COVERAGE_UNIT_ESPN_MONTHS
        dataset_expected_units = espn_expected_units
        dataset_completed_units = espn_completed_units
    else:
        dataset_coverage_unit = dataset_info.get("coverage_unit") or COVERAGE_UNIT_SINGLE_SHOT
        dataset_expected_units = dataset_info.get("expected_pages", 0) or 0
        dataset_completed_units = dataset_info.get("pages_completed", 0) or 0

    if dataset_completed_units <= 0 and (dataset_info.get("expected_pages") or 0) > 0:
        # This run acquired no completed coverage unit (for example a
        # pre-acquisition quota stop, or an ESPN attempt that produced nothing).
        # The previously recorded coverage progress describes the persisted data
        # and is preserved so a later run resumes from recorded coverage instead
        # of resetting the manifest to zero. The current attempt is still reported
        # truthfully in provider_coverage.
        dataset_coverage_unit = dataset_info.get("coverage_unit") or dataset_coverage_unit
        dataset_expected_units = dataset_info.get("expected_pages") or 0
        dataset_completed_units = dataset_info.get("pages_completed") or 0

    safe_exp_pages_b = dataset_expected_units
    safe_pages_comp_b = dataset_completed_units
    safe_rejected_cnt_b = cum_rejected_cnt_b
    safe_empty_cnt_b = cum_empty_cnt_b

    stored_games_all = storage.get_historical_basketball_games(league_id, season, include_storage_source=True)
    db_validation_passed = _validate_historical_basketball_dataset(
        stored_games_all,
        league_id,
        season,
        {
            "fixture_count": final_stored_count,
            "expected_pages": safe_exp_pages_b,
            "pages_completed": safe_pages_comp_b,
            "rejected_count": safe_rejected_cnt_b,
            "empty_pages_count": safe_empty_cnt_b,
            "coverage_unit": dataset_coverage_unit,
            "provider_coverage": provider_coverage,
        },
    )

    # Independently established expected season coverage. The NBA's published
    # regular-season game total is the authority for the season; the number of
    # persisted records is never used as its own evidence, and a season without an
    # established expectation (for example the ongoing season) fails closed.
    expected_min_season_games = None
    season_label_bb = parse_season_label(season)
    if league_id == NBA_LEAGUE_ID and season_label_bb is not None:
        expected_min_season_games = (
            getattr(config, "NBA_EXPECTED_MIN_REGULAR_SEASON_GAMES", {}) or {}
        ).get(season_label_bb)

    if expected_min_season_games is None:
        season_coverage_sufficient = False
        season_coverage_reason = "expected_season_coverage_unavailable"
    elif final_stored_count < expected_min_season_games:
        season_coverage_sufficient = False
        season_coverage_reason = (
            f"persisted_games_below_expected_season_coverage_{final_stored_count}_of_{expected_min_season_games}"
        )
    else:
        season_coverage_sufficient = True
        season_coverage_reason = None

    coverage_units_complete = safe_exp_pages_b > 0 and safe_pages_comp_b == safe_exp_pages_b
    if not coverage_units_complete:
        coverage_reason = "coverage_units_incomplete"
    elif not db_validation_passed:
        coverage_reason = "persisted_dataset_invalid"
    elif not season_coverage_sufficient:
        coverage_reason = season_coverage_reason
    else:
        coverage_reason = None

    basketball_coverage = {
        "verified": bool(
            db_validation_passed and coverage_units_complete and season_coverage_sufficient
        ),
        "reason": coverage_reason,
        "coverage_unit": dataset_coverage_unit,
        "expected_coverage_units": safe_exp_pages_b,
        "completed_coverage_units": safe_pages_comp_b,
        "expected_min_season_games": expected_min_season_games,
        "season_coverage_verified": bool(season_coverage_sufficient),
        "season_type_counts": _basketball_season_type_counts(stored_games_all),
        "provider_coverage": provider_coverage,
    }

    lifecycle_state_b, lifecycle_reason_raw_b, _ = get_competition_lifecycle(
        "basketball", league_id, season, stored_games_all, coverage=basketball_coverage
    )
    lifecycle_completed_b = lifecycle_state_b == COMPETITION_LIFECYCLE_COMPLETED
    lifecycle_reason_b = f"{lifecycle_state_b.lower()}_{lifecycle_reason_raw_b}"

    acquisition_state_b = (
        ACQUISITION_STATE_VALIDATED if (acquisition_complete and basketball_coverage["verified"])
        else (ACQUISITION_STATE_FAILED if acquisition_failed else ACQUISITION_STATE_INCOMPLETE)
    )

    is_fully_complete = (
        not quota_budget_stopped
        and not acquisition_failed
        and acquisition_complete
        and basketball_coverage["verified"]
        and lifecycle_completed_b
    )

    if not lifecycle_completed_b and not last_error_reason:
        last_error_reason = f"season_lifecycle_{lifecycle_reason_b}"
    if not basketball_coverage["verified"] and not last_error_reason:
        last_error_reason = f"coverage_failed_{coverage_reason}"

    print(
        f"[BASKETBALL] league {league_id} season {season}: coverage_unit={dataset_coverage_unit}, "
        f"coverage={safe_pages_comp_b}/{safe_exp_pages_b}, lifecycle={lifecycle_state_b} ({lifecycle_reason_b}), "
        f"acquisition_state={acquisition_state_b}, stored={final_stored_count}, "
        f"season_types={basketball_coverage['season_type_counts']}",
        flush=True,
    )

    if is_fully_complete:
        storage.mark_historical_dataset_complete(
            league_id,
            season,
            fixture_count=final_stored_count,
            sport="basketball",
            source=manifest_source_b,
            expected_pages=safe_exp_pages_b,
            pages_completed=safe_pages_comp_b,
            acquisition_complete=acquisition_complete,
            rejected_count=safe_rejected_cnt_b,
            empty_pages_count=safe_empty_cnt_b,
            coverage_unit=dataset_coverage_unit,
            provider_coverage=provider_coverage,
        )
        final_status = "COMPLETE"
    else:
        storage.mark_historical_dataset_incomplete(
            league_id,
            season,
            fixture_count=final_stored_count,
            sport="basketball",
            source=manifest_source_b,
            expected_pages=safe_exp_pages_b,
            pages_completed=safe_pages_comp_b,
            acquisition_complete=acquisition_complete,
            enrichment_status=dataset_info.get("enrichment_status", "NONE"),
            rejected_count=safe_rejected_cnt_b,
            empty_pages_count=safe_empty_cnt_b,
            error_reason=last_error_reason,
            coverage_unit=dataset_coverage_unit,
            provider_coverage=provider_coverage,
        )
        final_status = "INCOMPLETE"

    report = {
        "sport": "basketball",
        "league_id": league_id,
        "season": season,
        "status": final_status,
        "source": manifest_source_b,
        "existing_before": existing_before,
        "fixtures_received": len(games_received),
        "valid_fixtures": valid_count,
        "duplicates_skipped": duplicates_skipped,
        "rejected_count": cum_rejected_cnt_b,
        "rejection_reasons": rejection_reasons,
        "newly_stored": newly_stored,
        "already_existing_skipped": already_existing_skipped,
        "coverage_unit": dataset_coverage_unit,
        "expected_coverage_units": safe_exp_pages_b,
        "completed_coverage_units": safe_pages_comp_b,
        "provider_coverage": provider_coverage,
        "acquisition_complete": acquisition_complete,
        "acquisition_state": acquisition_state_b,
        "lifecycle_state": lifecycle_state_b,
        "lifecycle_reason": lifecycle_reason_b,
        "api_requests_consumed": total_consumed,
        "historical_budget": historical_budget,
        "quota_budget_stopped": quota_budget_stopped,
        "final_stored_count": final_stored_count,
    }

    _print_sync_report(report)
    return report


def run_safe_preflight(seasons: list, queue_items: list) -> None:
    """
    Execute a SAFE PREFLIGHT check before consuming historical API quota.
    Confirms database connectivity, active backend, configured leagues,
    requested target seasons, dataset statuses, and API request counters.
    """
    print("\n==================================================", flush=True)
    print("SAFE PREFLIGHT CHECK", flush=True)
    print("==================================================", flush=True)

    # 1. Confirm database connectivity and active storage backend
    is_neon_active = storage.is_neon()
    neon_url = getattr(config, "NEON_DATABASE_URL", None) or os.environ.get("NEON_DATABASE_URL")
    print(f"Database Storage Backend: {'PostgreSQL (Neon)' if is_neon_active else 'SQLite Fallback'}", flush=True)
    print(f"  NEON_DATABASE_URL configured: {bool(neon_url and neon_url.strip())}", flush=True)

    try:
        storage.init_db()
        print("  Database connectivity & schema initialized successfully.", flush=True)
    except Exception as exc:
        print(f"  Database initialization ERROR: {exc}", flush=True)
        raise

    # 2. Confirm configured football league IDs
    fb_leagues = getattr(config, "ALLOWED_LEAGUE_IDS", [])
    print(f"Configured Football League IDs ({len(fb_leagues)}): {fb_leagues}", flush=True)

    # 3. Confirm basketball league ID
    bb_leagues = getattr(config, "ALLOWED_BASKETBALL_LEAGUE_IDS", [])
    print(f"Configured Basketball League IDs ({len(bb_leagues)}): {bb_leagues}", flush=True)

    # 4. Confirm requested seasons
    print(f"Requested Target Seasons: {seasons}", flush=True)

    # 5. Confirm current API request counters
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bb_reqs = storage.get_api_request_count("api_basketball", today_str, request_type="historical")
    bb_budget = int(getattr(config, "API_BASKETBALL_HISTORICAL_DAILY_BUDGET", 50))

    print(f"API Request Counters Today ({today_str}):", flush=True)
    print("  Football Historical: Direct CSV -> SoccerData FBref -> SoccerData MatchHistory -> SoccerData Sofascore (0 API-Football calls)", flush=True)
    print(f"  API-Basketball: {bb_reqs}/{bb_budget} requests used today", flush=True)

    # 6. Confirm dataset statuses for target datasets
    complete_count = 0
    incomplete_count = 0
    print("\nTarget Dataset Manifest Statuses:", flush=True)
    for item in queue_items:
        sport = item["sport"]
        league_id = item["league_id"]
        season = item["season"]
        st = storage.get_historical_dataset_status(league_id, season, sport=sport)
        status_val = st.get("status", "INCOMPLETE")
        f_count = st.get("fixture_count", 0)
        p_completed = st.get("pages_completed", 0)
        e_pages = st.get("expected_pages", 0)
        if status_val == "COMPLETE":
            complete_count += 1
        else:
            incomplete_count += 1
        print(
            f"  [{sport.upper()}] League {league_id} Season {season}: Status={status_val}, Fixtures/Games={f_count}, Pages={p_completed}/{e_pages}",
            flush=True,
        )

    print(
        f"\nPreflight Summary: {len(queue_items)} total datasets ({complete_count} COMPLETE [0 API calls], {incomplete_count} INCOMPLETE).",
        flush=True,
    )
    print("Verification: No dataset marked COMPLETE will be re-fetched.", flush=True)
    print("==================================================\n", flush=True)


def check_acquisition_coverage(league_id: int, season: int, sport: str = "football") -> dict:
    """
    Inspect database dataset status and persistent API cache to estimate required acquisition requests.
    Clearly distinguishes database storage manifest state from API cache inspection.
    """
    dataset_info = storage.get_historical_dataset_status(league_id, season, sport=sport)
    db_status = dataset_info.get("status", "INCOMPLETE")
    db_fixture_count = dataset_info.get("fixture_count", 0)
    expected_pages = dataset_info.get("expected_pages", 0)
    pages_completed = dataset_info.get("pages_completed", 0)

    # Inspect persistent cache key for page 1
    cache_key = f"fixtures_{league_id}_{season}_page_1" if sport == "football" else f"games_{league_id}_{season}_page_1"
    cached_p1 = storage.get_api_cache(cache_key)
    has_cached_page_1 = cached_p1 is not None

    if db_status == "COMPLETE":
        estimated_requests = 0
    elif expected_pages > 0:
        estimated_requests = max(0, expected_pages - pages_completed)
    else:
        estimated_requests = 1 if not has_cached_page_1 else 0

    return {
        "sport": sport,
        "league_id": league_id,
        "season": season,
        "database_status": db_status,
        "database_fixture_count": db_fixture_count,
        "expected_pages": expected_pages,
        "pages_completed": pages_completed,
        "persistent_cache_inspected": True,
        "has_cached_page_1": has_cached_page_1,
        "estimated_required_requests": estimated_requests,
        "already_complete": (db_status == "COMPLETE"),
    }


def run_historical_queue(
    seasons: list = None,
    season: int = None,
    with_enrichment: bool = False,
    refresh: bool = False,
) -> dict:
    """
    Run historical data acquisition queue sequentially across target 5 seasons
    and configured leagues. Checks coverage before acquiring missing data.
    """
    if seasons is not None and isinstance(seasons, (list, tuple)) and seasons:
        target_seasons = list(seasons)
    elif season is not None:
        target_seasons = [season]
    else:
        target_seasons = list(getattr(config, "TARGET_SEASONS", [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]))

    seasons = target_seasons

    all_football_items = [
        {"sport": "football", "league_id": lid, "season": ssn}
        for ssn in seasons
        for lid in config.ALLOWED_LEAGUE_IDS
    ]
    all_basketball_items = [
        {"sport": "basketball", "league_id": lid, "season": ssn}
        for ssn in seasons
        for lid in config.ALLOWED_BASKETBALL_LEAGUE_IDS
    ]

    queue_items = []
    i, j = 0, 0
    while i < len(all_football_items) or j < len(all_basketball_items):
        if i < len(all_football_items):
            queue_items.append(all_football_items[i])
            i += 1
        if j < len(all_basketball_items):
            queue_items.append(all_basketball_items[j])
            j += 1

    # Execute Safe Preflight Check
    run_safe_preflight(seasons, queue_items)

    reports = []

    # Per-sport quota/budget state. When a sport's acquisition budget is
    # exhausted the remaining datasets for that sport are skipped for the rest of
    # the run (failing safely instead of repeatedly hammering the provider), while
    # the other sport's independent budget keeps being processed.
    sport_quota_stopped = {"football": False, "basketball": False}

    print(
        f"Starting target historical acquisition queue ({len(queue_items)} datasets across seasons {seasons})...",
        flush=True,
    )

    for item in queue_items:
        sport = item["sport"]
        league_id = item["league_id"]
        season = item["season"]

        # Check coverage before making requests
        cov = check_acquisition_coverage(league_id, season, sport=sport)
        if cov["already_complete"] and not refresh:
            print(
                f"Queue: Skipping COMPLETE {sport} dataset for league {league_id} season {season} (0 API requests required).",
                flush=True,
            )
            reports.append({
                "sport": sport,
                "league_id": league_id,
                "season": season,
                "status": "COMPLETE",
                "skipped_reason": "Dataset already COMPLETE",
                "api_requests_consumed": 0,
            })
            continue

        if sport_quota_stopped.get(sport):
            print(
                f"Queue: Skipping {sport} dataset for league {league_id} season {season}: "
                f"the {sport} acquisition budget is exhausted for this run.",
                flush=True,
            )
            reports.append({
                "sport": sport,
                "league_id": league_id,
                "season": season,
                "status": "INCOMPLETE",
                "skipped_reason": "sport_acquisition_budget_exhausted",
                "quota_budget_stopped": True,
                "api_requests_consumed": 0,
            })
            continue

        print(
            f"Queue: Syncing missing data for {sport} league {league_id} season {season} (est. required requests: {cov['estimated_required_requests']})...",
            flush=True,
        )

        if sport == "basketball":
            report = sync_historical_basketball_games(
                league_id=league_id,
                season=season,
                refresh=refresh,
            )
        else:
            report = sync_historical_fixtures(
                league_id=league_id,
                season=season,
                with_enrichment=with_enrichment,
                refresh=refresh,
            )

        if report.get("quota_budget_stopped"):
            sport_quota_stopped[sport] = True
            print(
                f"Queue: API quota/budget exhausted for {sport} during league {league_id} season {season}. "
                f"Remaining {sport} datasets are skipped for this run; the other sport continues.",
                flush=True,
            )

        reports.append(report)

    print(f"Historical queue run complete. Processed {len(reports)} dataset(s).", flush=True)
    return {
        "target_seasons": seasons,
        "processed_count": len(reports),
        "reports": reports,
    }


def verify_persisted_historical_datasets(
    sport: str = None,
    league_id: int = None,
    seasons: list = None,
    season: int = None,
    is_queue: bool = False,
) -> dict:
    """
    Independently verify persisted historical records and manifest statuses against actual DB state.
    Reconciles stored counts, distinct record IDs, date coverage, and structural integrity for football and basketball.
    Returns summary dict and prints a formatted report.
    """
    storage.init_db()

    if is_queue or (league_id is None and season is None and not seasons):
        target_seasons = seasons if seasons else list(getattr(config, "TARGET_SEASONS", [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]))
        football_targets = [
            {"sport": "football", "league_id": lid, "season": ssn}
            for ssn in target_seasons
            for lid in getattr(config, "ALLOWED_LEAGUE_IDS", [])
        ]
        basketball_targets = [
            {"sport": "basketball", "league_id": lid, "season": ssn}
            for ssn in target_seasons
            for lid in getattr(config, "ALLOWED_BASKETBALL_LEAGUE_IDS", [])
        ]
        targets = football_targets + basketball_targets
    else:
        sp = sport or "football"
        target_seasons = seasons if seasons else ([season] if season is not None else [2024])
        targets = [
            {"sport": sp, "league_id": league_id, "season": ssn}
            for ssn in target_seasons
        ]

    results = []
    total_requested = len(targets)
    verified_count = 0
    failed_count = 0

    print("\n==================================================", flush=True)
    print("PERSISTED HISTORICAL DATASET VERIFICATION AUDIT", flush=True)
    print("==================================================", flush=True)
    print(f"Total requested datasets: {total_requested}\n", flush=True)

    for target in targets:
        sp = target["sport"]
        lid = target["league_id"]
        ssn = target["season"]

        status_info = storage.get_historical_dataset_status(lid, ssn, sport=sp)
        manifest_status = status_info.get("status", "INCOMPLETE")
        manifest_count = status_info.get("fixture_count", 0)

        issues = []

        if sp == "basketball":
            records = storage.get_historical_basketball_games(lid, ssn, include_storage_source=True)
            # Whether an incomplete manifest describes a season still in progress is
            # decided by the competition's own declared season window, never by
            # comparing the season label with the current calendar year: a label that
            # happens to equal this year proves nothing about the competition.
            lifecycle_state, _, _ = get_competition_lifecycle(sp, lid, ssn, records)
            is_active_season = lifecycle_state == COMPETITION_LIFECYCLE_ONGOING
            actual_count = len(records)
            seen_ids = set()
            dates = []
            for r in records:
                if "_storage_source" in r:
                    src = r.get("_storage_source")
                else:
                    src = r.get("source")
                    if not src and isinstance(r.get("provider_provenance"), dict):
                        src = r["provider_provenance"].get("provider")

                if not isinstance(src, str) or not src.strip():
                    issues.append("invalid_or_missing_source")
                    src = "invalid"

                gid_raw = r.get("id") or r.get("game_id")
                gid_int = None
                if gid_raw is not None and not isinstance(gid_raw, bool):
                    try:
                        parsed_gid = int(gid_raw)
                        if parsed_gid > 0:
                            gid_int = parsed_gid
                    except (ValueError, TypeError):
                        pass

                if gid_int is None:
                    issues.append(f"invalid_or_missing_game_id_{gid_raw}")
                    gid_val = f"invalid_{gid_raw}"
                else:
                    gid_val = gid_int

                key = (src.strip() if isinstance(src, str) else str(src), gid_val)
                if key in seen_ids:
                    issues.append(f"duplicate_game_id_{gid_val}")
                seen_ids.add(key)

                d = r.get("date")
                if d:
                    dates.append(str(d)[:10])

            distinct_count = len(seen_ids)

            if manifest_status != "COMPLETE" and not is_active_season:
                issues.append(f"manifest_not_complete(status={manifest_status})")

            if manifest_count != actual_count:
                issues.append(f"count_mismatch(manifest={manifest_count},actual={actual_count})")

            if manifest_status == "COMPLETE":
                if not _validate_historical_basketball_dataset(records, lid, ssn, status_info):
                    issues.append("failed_basketball_structural_validation")

        else:
            records = storage.get_historical_fixtures(lid, ssn)
            # Same rule for football: the declared competition window establishes
            # whether the season is still running. A season whose window has not
            # opened is not "in progress", and a season with no verifiable window is
            # reported as a failure rather than excused by its year label.
            lifecycle_state, _, _ = get_competition_lifecycle(sp, lid, ssn, records)
            is_active_season = lifecycle_state == COMPETITION_LIFECYCLE_ONGOING
            actual_count = len(records)
            seen_ids = set()
            dates = []
            for r in records:
                fid = r.get("fixture", {}).get("id") if isinstance(r.get("fixture"), dict) else None
                if fid:
                    if fid in seen_ids:
                        issues.append(f"duplicate_fixture_id_{fid}")
                    seen_ids.add(fid)
                d = r.get("fixture", {}).get("date") if isinstance(r.get("fixture"), dict) else None
                if d:
                    dates.append(str(d)[:10])

            distinct_count = len(seen_ids)

            if manifest_status != "COMPLETE" and not is_active_season:
                issues.append(f"manifest_not_complete(status={manifest_status})")

            if manifest_count != actual_count:
                issues.append(f"count_mismatch(manifest={manifest_count},actual={actual_count})")

            if manifest_status == "COMPLETE":
                cov = data_resolver._validate_historical_structural_coverage(
                    records,
                    season=ssn,
                    league_id=lid,
                    provider_metadata={"allow_network": False},
                )
                if not cov.get("verified"):
                    issues.append(f"failed_football_structural_coverage({cov.get('reason')})")

        is_active_in_progress = (
            is_active_season
            and manifest_status != "COMPLETE"
            and len(issues) == 0
            and manifest_count == actual_count
            and distinct_count == actual_count
        )

        is_passed = (
            len(issues) == 0
            and manifest_count == actual_count
            and distinct_count == actual_count
            and manifest_status == "COMPLETE"
        )

        if is_passed:
            verified_count += 1
        elif is_active_in_progress:
            active_in_progress_count = locals().get("active_in_progress_count", 0) + 1
        else:
            failed_count += 1

        min_date = min(dates) if dates else "N/A"
        max_date = max(dates) if dates else "N/A"

        res_item = {
            "sport": sp,
            "league_id": lid,
            "season": ssn,
            "manifest_status": manifest_status,
            "manifest_count": manifest_count,
            "actual_stored_count": actual_count,
            "distinct_records": distinct_count,
            "duplicate_count": actual_count - distinct_count,
            "date_range": f"{min_date} to {max_date}",
            "verified": is_passed,
            "active_in_progress": is_active_in_progress,
            "issues": issues,
        }
        results.append(res_item)

        status_flag = "PASS (COMPLETE)" if is_passed else ("ACTIVE (IN_PROGRESS)" if is_active_in_progress else "FAIL")
        print(
            f"[{status_flag}] [{sp.upper()}] League {lid} Season {ssn}: Status={manifest_status}, ManifestCount={manifest_count}, Actual={actual_count}, Distinct={distinct_count}, DateRange={min_date}..{max_date}"
            + (f", Issues={issues}" if issues else ""),
            flush=True,
        )

    act_in_prog_cnt = sum(1 for r in results if r.get("active_in_progress"))
    print("\n==================================================", flush=True)
    print(f"VERIFICATION AUDIT SUMMARY: Requested={total_requested}, Verified Complete={verified_count}, Active In-Progress={act_in_prog_cnt}, Failed={failed_count}", flush=True)
    print("==================================================\n", flush=True)

    summary = {
        "total_requested": total_requested,
        "verified_count": verified_count,
        "active_in_progress_count": act_in_prog_cnt,
        "failed_count": failed_count,
        "success": failed_count == 0,
        "results": results,
    }
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Historical Data Collector (football: direct CSV/SoccerData; basketball: ESPN then API-Basketball)."
    )
    parser.add_argument("--sport", choices=["football", "basketball"], default="football", help="Sport name")
    parser.add_argument("--league-id", type=int, help="League ID")
    parser.add_argument("--season", type=int, default=None, help="Season year (default: TARGET_SEASONS for queue, 2024 for single dataset)")
    parser.add_argument(
        "--with-enrichment",
        action="store_true",
        help="Also fetch and store statistical enrichment (corners/cards for football)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Explicitly re-fetch and refresh dataset even if status is COMPLETE",
    )
    parser.add_argument(
        "--historical-queue",
        action="store_true",
        help="Run historical acquisition queue across all configured leagues",
    )
    parser.add_argument(
        "--seasons",
        nargs="+",
        type=int,
        help="Explicit list of seasons for historical queue (e.g. --seasons 2019 2020 2021 2022 2023 2024 2025 2026)",
    )

    args = parser.parse_args()

    storage.init_db()

    if args.historical_queue:
        run_historical_queue(
            seasons=args.seasons,
            season=args.season,
            with_enrichment=args.with_enrichment,
            refresh=args.refresh,
        )
    elif args.sport == "basketball":
        if args.league_id is None:
            parser.error("--league-id is required when --historical-queue is not set")
        season_val = args.season if args.season is not None else 2024
        sync_historical_basketball_games(
            league_id=args.league_id,
            season=season_val,
            refresh=args.refresh,
        )
    else:
        if args.league_id is None:
            parser.error("--league-id is required when --historical-queue is not set")
        season_val = args.season if args.season is not None else 2024
        sync_historical_fixtures(
            league_id=args.league_id,
            season=season_val,
            with_enrichment=args.with_enrichment,
            refresh=args.refresh,
        )
