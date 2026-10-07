"""
Historical Data Acquisition Collector for Football Agent.

Acquires historical league fixtures and optional statistics enrichment from API-Football
and stores them permanently in persistent storage (Neon PostgreSQL / SQLite).

Key invariants:
- Backtests read from database, NOT API-Football.
- COMPLETE datasets bypass API acquisition (0 requests made).
- Strict quota safety: enforces API_FOOTBALL_HISTORICAL_DAILY_BUDGET on every HTTP attempt.
- Idempotent and safe to run repeatedly or resume after partial sync.
"""

import argparse
import os
import sys
from datetime import datetime, timezone

from data_resolver import DataResolver, APIFootballQuotaExhaustedError
import config
import storage


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
    if quota_budget_stopped or acquisition_failed:
        return False
    if valid_fixtures_count <= 0 or final_stored_count <= 0:
        return False
    if rejected_count > 0 or empty_pages_count > 0:
        return False

    prov = (page_source or "api_football").lower()

    if prov == "api_football":
        return (
            expected_pages > 0 and
            pages_completed == expected_pages and
            valid_fixtures_count > 0
        )

    prov_meta = page_meta.get("provider_metadata") if isinstance(page_meta, dict) else {}
    if not isinstance(prov_meta, dict):
        prov_meta = {}

    is_partial = prov_meta.get("is_partial", False)
    is_complete = prov_meta.get("is_complete") or prov_meta.get("acquisition_complete")

    if is_partial:
        return False

    if is_complete is True:
        return True

    count = prov_meta.get("count")
    played = prov_meta.get("played")
    first = prov_meta.get("first")
    last = prov_meta.get("last")

    if count is not None and isinstance(count, int) and count > 0:
        if valid_fixtures_count < count:
            return False
        if played is not None and isinstance(played, int):
            if valid_fixtures_count < played or played < count:
                return False
        if first and last and isinstance(first, str) and isinstance(last, str):
            if len(first) >= 10 and len(last) >= 10:
                return True

    return False


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
    # Frozen football historical dataset scope.
    # Only completed historical seasons 2024, 2025, and 2026 are permitted.
    # This guard prevents accidental persistence of out-of-window seasons.
    allowed_seasons = {2024, 2025, 2026}

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
        historical_budget = int(getattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50))

    dataset_info = storage.get_historical_dataset_status(league_id, season)
    current_status = dataset_info["status"]
    existing_before = dataset_info["fixture_count"] or storage.get_historical_fixture_count(league_id, season)

    # Invariant: If COMPLETE and not refresh -> skip acquisition completely with 0 API calls
    if current_status == "COMPLETE" and not refresh:
        print(f"Dataset already COMPLETE for league {league_id} season {season}; API acquisition skipped.", flush=True)
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
            "skipped_reason": "Dataset already COMPLETE",
        }

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    initial_request_count = storage.get_api_request_count("api_football", today_str)

    quota_budget_stopped = False
    acquisition_failed = False
    last_error_reason = None

    if initial_request_count >= historical_budget:
        quota_budget_stopped = True
        print(
            f"Historical daily budget ceiling reached ({initial_request_count}/{historical_budget} requests used today). Skipping API acquisition.",
            flush=True,
        )
        final_count = storage.get_historical_fixture_count(league_id, season)
        storage.mark_historical_dataset_incomplete(
            league_id,
            season,
            fixture_count=final_count,
            sport="football",
            expected_pages=dataset_info.get("expected_pages", 0),
            pages_completed=dataset_info.get("pages_completed", 0),
            acquisition_complete=dataset_info.get("acquisition_complete", False),
            enrichment_status=dataset_info.get("enrichment_status", "NONE"),
            rejected_count=dataset_info.get("rejected_count", 0),
            empty_pages_count=dataset_info.get("empty_pages_count", 0),
            error_reason="quota_budget_exhausted_before_acquisition",
        )
        return {
            "league_id": league_id,
            "season": season,
            "status": "INCOMPLETE",
            "existing_before": existing_before,
            "fixtures_received": 0,
            "valid_fixtures": 0,
            "duplicates_skipped": 0,
            "newly_stored": 0,
            "already_existing_skipped": 0,
            "pages_completed": dataset_info.get("pages_completed", 0),
            "expected_pages": dataset_info.get("expected_pages", 0),
            "rejected_count": dataset_info.get("rejected_count", 0),
            "empty_pages_count": dataset_info.get("empty_pages_count", 0),
            "api_requests_consumed": 0,
            "quota_budget_stopped": True,
            "final_stored_count": final_count,
            "enrichment_stored": 0,
        }

    fixtures_received = []
    start_p = dataset_info.get("pages_completed", 0) + 1 if (dataset_info.get("pages_completed", 0) > 0 and not dataset_info.get("acquisition_complete") and not refresh) else 1
    expected_pages = dataset_info.get("expected_pages", 0) if not refresh else 0
    pages_completed = dataset_info.get("pages_completed", 0) if not refresh else 0
    acquisition_complete = False

    total_valid_fixtures = 0
    total_newly_stored = 0
    total_duplicates_skipped = 0
    total_rejected_count = dataset_info.get("rejected_count", 0) if not refresh else 0
    total_empty_pages_count = dataset_info.get("empty_pages_count", 0) if not refresh else 0
    all_rejection_reasons = {}

    # Preflight coverage check via DataResolver
    resolver = DataResolver()
    cov_status, cov_reason = resolver.check_competition_coverage(league_id, season)
    primary_available = (cov_status != "season_not_available")

    current_page = start_p
    while True:
        try:
            page_meta = resolver.get_league_fixtures_page(
                league_id, season, page=current_page, max_budget=historical_budget
            )
            page_fixtures = page_meta.get("fixtures", [])
            page_expected = page_meta.get("expected_pages", 1)
            page_source = page_meta.get("source", "api_football")

            if page_meta.get("primary_quota_exhausted"):
                quota_budget_stopped = True
                last_error_reason = "quota_budget_exhausted_during_acquisition"

            if page_meta.get("primary_failed") and not page_fixtures:
                if page_meta.get("primary_quota_exhausted"):
                    print(f"API Quota exhausted during fixture fetch on page {current_page}", flush=True)
                else:
                    acquisition_failed = True
                    last_error_reason = "secondary_provider_returned_no_fixtures"
                    print(f"Primary acquisition unavailable; fallback returned no fixtures on page {current_page}", flush=True)
                break

            if expected_pages == 0 or page_source != "api_football":
                expected_pages = page_expected
            elif page_expected != expected_pages and page_fixtures:
                acquisition_failed = True
                last_error_reason = "pagination_metadata_mismatch"
                print(
                    f"Pagination error: Total pages changed during sync ({expected_pages} -> {page_expected}). Failing closed.",
                    flush=True,
                )
                break

            if not page_fixtures:
                total_empty_pages_count += 1

            fixtures_received.extend(page_fixtures)

            # Persist valid fixtures from this page immediately
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

            for r_reason, r_cnt in save_result.get("rejection_reasons", {}).items():
                all_rejection_reasons[r_reason] = all_rejection_reasons.get(r_reason, 0) + r_cnt

            pages_completed = current_page
            current_stored_count = storage.get_historical_fixture_count(league_id, season)

            # Update progress in manifest immediately
            storage.mark_historical_dataset_incomplete(
                league_id,
                season,
                fixture_count=current_stored_count,
                sport="football",
                expected_pages=expected_pages,
                pages_completed=pages_completed,
                acquisition_complete=False,
                rejected_count=total_rejected_count,
                empty_pages_count=total_empty_pages_count,
                error_reason=last_error_reason,
                source=page_source,
            )
            if current_page >= expected_pages:
                break
            current_page += 1

        except APIFootballQuotaExhaustedError as exc:
            quota_budget_stopped = True
            last_error_reason = "quota_budget_exhausted_during_acquisition"
            print(f"API Quota exhausted during fixture fetch on page {current_page}: {exc}", flush=True)
            break
        except Exception as exc:
            acquisition_failed = True
            last_error_reason = f"api_error_during_acquisition: {str(exc)[:100]}"
            print(f"Primary acquisition error during fixture fetch on page {current_page}: {exc}", flush=True)
            break

    if acquisition_failed or total_valid_fixtures == 0 or storage.get_historical_fixture_count(league_id, season) == 0:
        acquisition_failed = True
        if not last_error_reason:
            last_error_reason = "fallback_providers_returned_no_fixtures"
        storage.mark_historical_dataset_incomplete(
            league_id,
            season,
            fixture_count=storage.get_historical_fixture_count(league_id, season),
            sport="football",
            expected_pages=expected_pages,
            pages_completed=pages_completed,
            acquisition_complete=False,
            rejected_count=total_rejected_count,
            empty_pages_count=total_empty_pages_count,
            error_reason=last_error_reason,
            source=page_source if total_valid_fixtures > 0 else "mixed",
        )

    valid_fixtures_count = total_valid_fixtures
    newly_stored = total_newly_stored
    duplicates_skipped = total_duplicates_skipped
    rejected_count = total_rejected_count
    rejection_reasons = all_rejection_reasons
    already_existing_skipped = valid_fixtures_count - newly_stored

    enrichment_stored = 0

    # Optional statistical enrichment acquisition
    if with_enrichment and not quota_budget_stopped and not acquisition_failed:
        current_reqs = storage.get_api_request_count("api_football", today_str)
        if current_reqs >= historical_budget:
            quota_budget_stopped = True
            last_error_reason = "quota_budget_exhausted_during_enrichment"
            print(
                f"Historical budget ceiling reached before enrichment fetch ({current_reqs}/{historical_budget}).",
                flush=True,
            )
        else:
            all_stored = storage.get_historical_fixtures(league_id, season)
            # CRITICAL: Only API-Football fixtures may be sent to api_football.get_enriched_fixtures()
            finished_primary_ids = [
                item.get("fixture", {}).get("id")
                for item in all_stored
                if isinstance(item, dict)
                and item.get("source", item.get("provider_provenance", {}).get("provider", "api_football")) == "api_football"
                and item.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN")
                and item.get("fixture", {}).get("id") is not None
            ]

            stored_enrichment = storage.get_historical_enrichment(finished_primary_ids, source="api_football")
            missing_enrichment_ids = [fid for fid in finished_primary_ids if fid not in stored_enrichment]

            if missing_enrichment_ids:
                try:
                    enriched_batch = resolver.get_enriched_fixtures(
                        missing_enrichment_ids, max_budget=historical_budget
                    )
                    if enriched_batch:
                        enrichment_stored = len(enriched_batch)
                except APIFootballQuotaExhaustedError:
                    quota_budget_stopped = True
                    last_error_reason = "quota_budget_exhausted_during_enrichment"
                except Exception as exc:
                    acquisition_failed = True
                    last_error_reason = "api_error_during_enrichment"
                    print(f"API acquisition error during enrichment fetch: {exc}", flush=True)

    final_reqs = storage.get_api_request_count("api_football", today_str)
    total_consumed = final_reqs - initial_request_count

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

    if not finished_fixture_ids:
        enrichment_status = "NONE"
    elif len(stored_enrichments) >= len(finished_fixture_ids):
        enrichment_status = "COMPLETE"
    elif len(stored_enrichments) > 0:
        enrichment_status = "PARTIAL"
    else:
        enrichment_status = "NONE"

    # Update dataset manifest completion status strictly using provider-neutral helper
    page_source_val = page_source if 'page_source' in locals() else "api_football"
    page_meta_val = page_meta if 'page_meta' in locals() else {}

    is_fully_complete = _is_historical_provider_complete(
        page_source=page_source_val,
        page_meta=page_meta_val,
        valid_fixtures_count=valid_fixtures_count,
        expected_pages=expected_pages,
        pages_completed=pages_completed,
        quota_budget_stopped=quota_budget_stopped,
        acquisition_failed=acquisition_failed,
        rejected_count=rejected_count,
        empty_pages_count=total_empty_pages_count,
        final_stored_count=final_stored_count,
    )
    acquisition_complete = is_fully_complete

    # Determine exact source semantics for dataset manifest
    stored_fixtures_all = storage.get_historical_fixtures(league_id, season)
    sources_found = set()
    for f in stored_fixtures_all:
        if isinstance(f, dict):
            src = f.get("source") or f.get("provider_provenance", {}).get("provider")
            if src:
                sources_found.add(src)

    if len(sources_found) > 1:
        manifest_source = "mixed"
    elif len(sources_found) == 1:
        manifest_source = list(sources_found)[0]
    else:
        manifest_source = page_source_val

    if is_fully_complete:
        storage.mark_historical_dataset_complete(
            league_id,
            season,
            fixture_count=final_stored_count,
            source=manifest_source,
            sport="football",
            expected_pages=expected_pages,
            pages_completed=pages_completed,
            acquisition_complete=True,
            enrichment_status=enrichment_status,
            rejected_count=rejected_count,
            empty_pages_count=total_empty_pages_count,
        )
        final_status = "COMPLETE"
    else:
        if not last_error_reason and page_source_val != "api_football":
            last_error_reason = "provider_coverage_incomplete"
        storage.mark_historical_dataset_incomplete(
            league_id,
            season,
            fixture_count=final_stored_count,
            source=manifest_source,
            sport="football",
            expected_pages=expected_pages,
            pages_completed=pages_completed,
            acquisition_complete=False,
            enrichment_status=enrichment_status,
            rejected_count=rejected_count,
            empty_pages_count=total_empty_pages_count,
            error_reason=last_error_reason,
        )
        final_status = "INCOMPLETE"

    report = {
        "league_id": league_id,
        "season": season,
        "status": final_status,
        "existing_before": existing_before,
        "fixtures_received": len(fixtures_received),
        "valid_fixtures": valid_fixtures_count,
        "duplicates_skipped": duplicates_skipped,
        "rejected_count": rejected_count,
        "rejection_reasons": rejection_reasons,
        "newly_stored": newly_stored,
        "already_existing_skipped": already_existing_skipped,
        "expected_pages": expected_pages,
        "pages_completed": pages_completed,
        "acquisition_complete": acquisition_complete,
        "api_requests_consumed": total_consumed,
        "historical_budget": historical_budget,
        "quota_budget_stopped": quota_budget_stopped,
        "final_stored_count": final_stored_count,
        "enrichment_stored": enrichment_stored,
    }

    _print_sync_report(report)
    return report


def _print_sync_report(report: dict) -> None:
    print("\n=== HISTORICAL SYNC REPORT ===")
    print(f"League ID: {report['league_id']}")
    print(f"Season: {report['season']}")
    print(f"Dataset Status: {report['status']}")
    print(f"Existing fixtures before sync: {report['existing_before']}")
    print(f"Fixtures received from API: {report['fixtures_received']}")
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
    if report.get("status") == "COMPLETE" and report.get("api_requests_consumed") == 0:
        print("Dataset already COMPLETE; API acquisition skipped.")
    print("=== END HISTORICAL SYNC REPORT ===\n")


import basketball_api


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
        print(f"Basketball dataset already COMPLETE for league {league_id} season {season}; API acquisition skipped.", flush=True)
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
            "skipped_reason": "Dataset already COMPLETE",
        }

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    initial_request_count = storage.get_api_request_count("api_basketball", today_str)

    quota_budget_stopped = False
    acquisition_failed = False
    last_error_reason = None

    if initial_request_count >= historical_budget:
        quota_budget_stopped = True
        print(
            f"Historical daily budget ceiling reached ({initial_request_count}/{historical_budget} basketball requests used today). Skipping API acquisition.",
            flush=True,
        )
        final_count = storage.get_historical_basketball_game_count(league_id, season)
        storage.mark_historical_dataset_incomplete(
            league_id,
            season,
            fixture_count=final_count,
            sport="basketball",
            expected_pages=dataset_info.get("expected_pages", 0),
            pages_completed=dataset_info.get("pages_completed", 0),
            acquisition_complete=dataset_info.get("acquisition_complete", False),
            enrichment_status=dataset_info.get("enrichment_status", "NONE"),
            rejected_count=dataset_info.get("rejected_count", 0),
            empty_pages_count=dataset_info.get("empty_pages_count", 0),
            error_reason="quota_budget_exhausted_before_acquisition",
        )
        return {
            "sport": "basketball",
            "league_id": league_id,
            "season": season,
            "status": "INCOMPLETE",
            "existing_before": existing_before,
            "fixtures_received": 0,
            "valid_fixtures": 0,
            "duplicates_skipped": 0,
            "newly_stored": 0,
            "already_existing_skipped": 0,
            "pages_completed": dataset_info.get("pages_completed", 0),
            "expected_pages": dataset_info.get("expected_pages", 0),
            "rejected_count": dataset_info.get("rejected_count", 0),
            "empty_pages_count": dataset_info.get("empty_pages_count", 0),
            "api_requests_consumed": 0,
            "quota_budget_stopped": True,
            "final_stored_count": final_count,
        }

    games_received = []
    start_p = dataset_info.get("pages_completed", 0) + 1 if (dataset_info.get("pages_completed", 0) > 0 and not dataset_info.get("acquisition_complete") and not refresh) else 1
    expected_pages = dataset_info.get("expected_pages", 0) if not refresh else 0
    pages_completed = dataset_info.get("pages_completed", 0) if not refresh else 0
    acquisition_complete = False

    total_valid_games = 0
    total_newly_stored = 0
    total_duplicates_skipped = 0
    total_rejected_count = dataset_info.get("rejected_count", 0) if not refresh else 0
    total_empty_pages_count = dataset_info.get("empty_pages_count", 0) if not refresh else 0
    all_rejection_reasons = {}

    current_page = start_p
    while True:
        try:
            page_meta = basketball_api.get_league_games_page(
                league_id, season, page=current_page, max_budget=historical_budget
            )
            page_games = page_meta.get("games", [])
            page_expected = page_meta.get("expected_pages")

            if expected_pages == 0:
                expected_pages = page_expected
            elif page_expected != expected_pages:
                acquisition_failed = True
                last_error_reason = "pagination_metadata_mismatch"
                print(
                    f"Basketball pagination error: Total pages changed during sync ({expected_pages} -> {page_expected}). Failing closed.",
                    flush=True,
                )
                break

            if not page_games:
                total_empty_pages_count += 1

            games_received.extend(page_games)

            save_result = storage.save_historical_basketball_games(page_games, league_id, season)
            total_valid_games += save_result.get("valid", 0)
            total_newly_stored += save_result.get("inserted", 0)
            total_duplicates_skipped += save_result.get("duplicates_skipped", 0)
            total_rejected_count += save_result.get("rejected_count", 0)

            for r_reason, r_cnt in save_result.get("rejection_reasons", {}).items():
                all_rejection_reasons[r_reason] = all_rejection_reasons.get(r_reason, 0) + r_cnt

            pages_completed = current_page
            current_stored_count = storage.get_historical_basketball_game_count(league_id, season)

            if current_page >= expected_pages:
                acquisition_complete = True

            storage.mark_historical_dataset_incomplete(
                league_id,
                season,
                fixture_count=current_stored_count,
                sport="basketball",
                expected_pages=expected_pages,
                pages_completed=pages_completed,
                acquisition_complete=acquisition_complete,
                rejected_count=total_rejected_count,
                empty_pages_count=total_empty_pages_count,
                error_reason=last_error_reason,
            )
            if acquisition_complete or current_page >= expected_pages:
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

    requests_after = storage.get_api_request_count("api_basketball", today_str)
    total_consumed = requests_after - initial_request_count

    valid_count = total_valid_games
    newly_stored = total_newly_stored
    duplicates_skipped = total_duplicates_skipped
    rejected_count = total_rejected_count
    rejection_reasons = all_rejection_reasons
    already_existing_skipped = valid_count - newly_stored

    final_stored_count = storage.get_historical_basketball_game_count(league_id, season)

    is_fully_complete = (
        not quota_budget_stopped
        and not acquisition_failed
        and acquisition_complete
        and expected_pages > 0
        and pages_completed == expected_pages
        and rejected_count == 0
        and total_empty_pages_count == 0
        and valid_count > 0
        and final_stored_count > 0
    )

    if is_fully_complete:
        storage.mark_historical_dataset_complete(
            league_id,
            season,
            fixture_count=final_stored_count,
            sport="basketball",
            expected_pages=expected_pages,
            pages_completed=pages_completed,
            acquisition_complete=acquisition_complete,
            rejected_count=rejected_count,
            empty_pages_count=total_empty_pages_count,
        )
        final_status = "COMPLETE"
    else:
        storage.mark_historical_dataset_incomplete(
            league_id,
            season,
            fixture_count=final_stored_count,
            sport="basketball",
            expected_pages=expected_pages,
            pages_completed=pages_completed,
            acquisition_complete=acquisition_complete,
            rejected_count=rejected_count,
            empty_pages_count=total_empty_pages_count,
            error_reason=last_error_reason,
        )
        final_status = "INCOMPLETE"

    report = {
        "sport": "basketball",
        "league_id": league_id,
        "season": season,
        "status": final_status,
        "existing_before": existing_before,
        "fixtures_received": len(games_received),
        "valid_fixtures": valid_count,
        "duplicates_skipped": duplicates_skipped,
        "rejected_count": rejected_count,
        "rejection_reasons": rejection_reasons,
        "newly_stored": newly_stored,
        "already_existing_skipped": already_existing_skipped,
        "expected_pages": expected_pages,
        "pages_completed": pages_completed,
        "acquisition_complete": acquisition_complete,
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
    fb_reqs = storage.get_api_request_count("api_football", today_str)
    fb_budget = int(getattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50))
    bb_reqs = storage.get_api_request_count("api_basketball", today_str)
    bb_budget = int(getattr(config, "API_BASKETBALL_HISTORICAL_DAILY_BUDGET", 50))

    print(f"API Request Counters Today ({today_str}):", flush=True)
    print(f"  API-Football: {fb_reqs}/{fb_budget} requests used today", flush=True)
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
        target_seasons = list(getattr(config, "TARGET_SEASONS", [2022, 2023, 2024]))

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
    quota_stopped_sports = set()

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

        if sport in quota_stopped_sports:
            print(
                f"Queue: Skipping {sport} league {league_id} season {season} because {sport} historical daily budget ceiling was reached.",
                flush=True,
            )
            dataset_info = storage.get_historical_dataset_status(league_id, season, sport=sport)
            final_count = dataset_info["fixture_count"] or (
                storage.get_historical_basketball_game_count(league_id, season) if sport == "basketball"
                else storage.get_historical_fixture_count(league_id, season)
            )
            reports.append({
                "sport": sport,
                "league_id": league_id,
                "season": season,
                "status": "INCOMPLETE",
                "existing_before": final_count,
                "fixtures_received": 0,
                "valid_fixtures": 0,
                "duplicates_skipped": 0,
                "newly_stored": 0,
                "already_existing_skipped": 0,
                "pages_completed": dataset_info.get("pages_completed", 0),
                "expected_pages": dataset_info.get("expected_pages", 0),
                "rejected_count": dataset_info.get("rejected_count", 0),
                "empty_pages_count": dataset_info.get("empty_pages_count", 0),
                "api_requests_consumed": 0,
                "quota_budget_stopped": True,
                "final_stored_count": final_count,
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

        reports.append(report)

        if report.get("quota_budget_stopped"):
            quota_stopped_sports.add(sport)
            print(
                f"Queue: API quota/budget exhausted for {sport} during league {league_id} season {season}. {sport.capitalize()} acquisitions paused for this run.",
                flush=True,
            )

    print(f"Historical queue run complete. Processed {len(reports)} dataset(s).", flush=True)
    return {
        "target_seasons": seasons,
        "processed_count": len(reports),
        "reports": reports,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Historical Data Collector for API-Football/API-Basketball fixtures."
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
        help="Explicit list of seasons for historical queue (e.g. --seasons 2022 2023 2024)",
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
