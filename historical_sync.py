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
import sys
from datetime import datetime, timezone

import api_football
import config
import storage


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

    current_page = start_p
    while True:
        try:
            page_meta = api_football.get_league_fixtures_page(
                league_id, season, page=current_page, max_budget=historical_budget
            )
            page_fixtures = page_meta.get("fixtures", [])
            page_expected = page_meta.get("expected_pages")

            if expected_pages == 0:
                expected_pages = page_expected
            elif page_expected != expected_pages:
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
            save_result = storage.save_historical_fixtures(page_fixtures, league_id, season)
            total_valid_fixtures += save_result.get("valid", 0)
            total_newly_stored += save_result.get("inserted", 0)
            total_duplicates_skipped += save_result.get("duplicates_skipped", 0)
            total_rejected_count += save_result.get("rejected_count", 0)

            for r_reason, r_cnt in save_result.get("rejection_reasons", {}).items():
                all_rejection_reasons[r_reason] = all_rejection_reasons.get(r_reason, 0) + r_cnt

            pages_completed = current_page
            current_stored_count = storage.get_historical_fixture_count(league_id, season)

            if current_page >= expected_pages:
                acquisition_complete = True

            # Update progress in manifest immediately with cumulative rejected_count and empty_pages_count
            storage.mark_historical_dataset_incomplete(
                league_id,
                season,
                fixture_count=current_stored_count,
                sport="football",
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

        except api_football.APIFootballQuotaExhaustedError as exc:
            quota_budget_stopped = True
            last_error_reason = "quota_budget_exhausted_during_acquisition"
            print(f"API Quota exhausted during fixture fetch on page {current_page}: {exc}", flush=True)
            break
        except Exception as exc:
            acquisition_failed = True
            last_error_reason = "api_error_during_acquisition"
            print(f"API acquisition error during fixture fetch on page {current_page}: {exc}", flush=True)
            break

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
            finished_ids = [
                item.get("fixture", {}).get("id")
                for item in all_stored
                if isinstance(item, dict)
                and item.get("fixture", {}).get("status", {}).get("short") == "FT"
                and item.get("fixture", {}).get("id") is not None
            ]

            stored_enrichment = storage.get_historical_enrichment(finished_ids)
            missing_enrichment_ids = [fid for fid in finished_ids if fid not in stored_enrichment]

            if missing_enrichment_ids:
                try:
                    enriched_batch = api_football.get_enriched_fixtures(
                        missing_enrichment_ids, max_budget=historical_budget
                    )
                    if enriched_batch:
                        enrichment_stored = storage.save_historical_enrichment(enriched_batch)
                except api_football.APIFootballQuotaExhaustedError:
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

    # Update dataset manifest completion status strictly
    # Exact-budget successful completion is permitted as COMPLETE if all pages fetched without quota error
    is_fully_complete = (
        not quota_budget_stopped
        and not acquisition_failed
        and acquisition_complete
        and expected_pages > 0
        and pages_completed == expected_pages
        and rejected_count == 0
        and total_empty_pages_count == 0
        and valid_fixtures_count > 0
        and final_stored_count > 0
    )

    if is_fully_complete:
        storage.mark_historical_dataset_complete(
            league_id,
            season,
            fixture_count=final_stored_count,
            sport="football",
            expected_pages=expected_pages,
            pages_completed=pages_completed,
            acquisition_complete=acquisition_complete,
            enrichment_status=enrichment_status,
            rejected_count=rejected_count,
            empty_pages_count=total_empty_pages_count,
        )
        final_status = "COMPLETE"
    else:
        storage.mark_historical_dataset_incomplete(
            league_id,
            season,
            fixture_count=final_stored_count,
            sport="football",
            expected_pages=expected_pages,
            pages_completed=pages_completed,
            acquisition_complete=acquisition_complete,
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
        target_seasons = [2024]

    seasons = target_seasons

    queue_items = []
    for ssn in seasons:
        for lid in config.ALLOWED_LEAGUE_IDS:
            queue_items.append({"sport": "football", "league_id": lid, "season": ssn})
        for lid in config.ALLOWED_BASKETBALL_LEAGUE_IDS:
            queue_items.append({"sport": "basketball", "league_id": lid, "season": ssn})

    reports = []
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
            print(
                f"Queue: API quota/budget exhausted during {sport} league {league_id} season {season}. Stopping queue execution.",
                flush=True,
            )
            break

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
    parser.add_argument("--season", type=int, default=2024, help="Season year (default: 2024)")
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
        help="Explicit list of seasons for historical queue (e.g. --seasons 2020 2021 2022 2023 2024)",
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
        sync_historical_basketball_games(
            league_id=args.league_id,
            season=args.season,
            refresh=args.refresh,
        )
    else:
        if args.league_id is None:
            parser.error("--league-id is required when --historical-queue is not set")
        sync_historical_fixtures(
            league_id=args.league_id,
            season=args.season,
            with_enrichment=args.with_enrichment,
            refresh=args.refresh,
        )
