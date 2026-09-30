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

    if initial_request_count >= historical_budget:
        quota_budget_stopped = True
        print(
            f"Historical daily budget ceiling reached ({initial_request_count}/{historical_budget} requests used today). Skipping API acquisition.",
            flush=True,
        )
        final_count = storage.get_historical_fixture_count(league_id, season)
        storage.mark_historical_dataset_incomplete(league_id, season, fixture_count=final_count)
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
            "api_requests_consumed": 0,
            "quota_budget_stopped": True,
            "final_stored_count": final_count,
            "enrichment_stored": 0,
        }

    fixtures_received = []
    start_p = dataset_info.get("pages_completed", 0) + 1 if dataset_info.get("pages_completed", 0) > 0 else 1
    expected_pages = dataset_info.get("expected_pages", 0)
    pages_completed = dataset_info.get("pages_completed", 0)
    acquisition_complete = False

    try:
        fetch_meta = api_football.get_league_fixtures_with_metadata(
            league_id, season, start_page=start_p, max_budget=historical_budget
        )
        fixtures_received = fetch_meta.get("fixtures", [])
        expected_pages = fetch_meta.get("expected_pages", 0)
        pages_completed = fetch_meta.get("pages_completed", 0)
        acquisition_complete = fetch_meta.get("acquisition_complete", False)
    except api_football.APIFootballQuotaExhaustedError as exc:
        quota_budget_stopped = True
        print(f"API Quota exhausted during fixture fetch: {exc}", flush=True)
    except Exception as exc:
        acquisition_failed = True
        print(f"API acquisition error during fixture fetch: {exc}", flush=True)

    requests_after_fixtures = storage.get_api_request_count("api_football", today_str)
    requests_for_fixtures = requests_after_fixtures - initial_request_count

    if not isinstance(fixtures_received, list):
        fixtures_received = []

    # Save fixtures (storage performs validation and deduplication)
    save_result = storage.save_historical_fixtures(fixtures_received, league_id, season)
    valid_fixtures_count = save_result.get("valid", 0)
    newly_stored = save_result.get("inserted", 0)
    duplicates_skipped = save_result.get("duplicates_skipped", 0)
    rejected_count = save_result.get("rejected_count", 0)
    rejection_reasons = save_result.get("rejection_reasons", {})
    already_existing_skipped = valid_fixtures_count - newly_stored

    enrichment_stored = 0

    # Optional statistical enrichment acquisition
    if with_enrichment and not quota_budget_stopped and not acquisition_failed:
        current_reqs = storage.get_api_request_count("api_football", today_str)
        if current_reqs >= historical_budget:
            quota_budget_stopped = True
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
                except Exception as exc:
                    acquisition_failed = True
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
    print(f"Enriched records stored: {report['enrichment_stored']}")
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
        historical_budget = int(getattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50))

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

    if initial_request_count >= historical_budget:
        quota_budget_stopped = True
        print(
            f"Historical daily budget ceiling reached ({initial_request_count}/{historical_budget} basketball requests used today). Skipping API acquisition.",
            flush=True,
        )
        final_count = storage.get_historical_basketball_game_count(league_id, season)
        storage.mark_historical_dataset_incomplete(league_id, season, fixture_count=final_count, sport="basketball")
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
            "api_requests_consumed": 0,
            "quota_budget_stopped": True,
            "final_stored_count": final_count,
        }

    games_received = []
    try:
        data = basketball_api._get("games", {"league": league_id, "season": season}, max_budget=historical_budget)
        games_received = data.get("response", []) if isinstance(data, dict) else []
    except basketball_api.APIBasketballQuotaExhaustedError as exc:
        quota_budget_stopped = True
        print(f"API Quota exhausted during basketball games fetch: {exc}", flush=True)
    except Exception as exc:
        acquisition_failed = True
        print(f"API acquisition error during basketball games fetch: {exc}", flush=True)

    requests_after = storage.get_api_request_count("api_basketball", today_str)
    total_consumed = requests_after - initial_request_count

    if not isinstance(games_received, list):
        games_received = []

    save_result = storage.save_historical_basketball_games(games_received, league_id, season)
    valid_count = save_result.get("valid", 0)
    newly_stored = save_result.get("inserted", 0)
    duplicates_skipped = save_result.get("duplicates_skipped", 0)
    rejected_count = save_result.get("rejected_count", 0)
    rejection_reasons = save_result.get("rejection_reasons", {})
    already_existing_skipped = valid_count - newly_stored

    final_stored_count = storage.get_historical_basketball_game_count(league_id, season)

    is_fully_complete = (
        not quota_budget_stopped
        and not acquisition_failed
        and rejected_count == 0
        and valid_count > 0
        and final_stored_count > 0
    )

    if is_fully_complete:
        storage.mark_historical_dataset_complete(
            league_id, season, fixture_count=final_stored_count, sport="basketball", expected_pages=1, pages_completed=1, acquisition_complete=True
        )
        final_status = "COMPLETE"
    else:
        storage.mark_historical_dataset_incomplete(
            league_id, season, fixture_count=final_stored_count, sport="basketball", expected_pages=1, pages_completed=1, acquisition_complete=False
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
        "api_requests_consumed": total_consumed,
        "historical_budget": historical_budget,
        "quota_budget_stopped": quota_budget_stopped,
        "final_stored_count": final_stored_count,
    }

    _print_sync_report(report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Historical Data Collector for API-Football/API-Basketball fixtures."
    )
    parser.add_argument("--sport", choices=["football", "basketball"], default="football", help="Sport name")
    parser.add_argument("--league-id", type=int, required=True, help="League ID")
    parser.add_argument("--season", type=int, required=True, help="Season year (e.g. 2024 or 2025)")
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

    args = parser.parse_args()

    storage.init_db()

    if args.sport == "basketball":
        sync_historical_basketball_games(
            league_id=args.league_id,
            season=args.season,
            refresh=args.refresh,
        )
    else:
        sync_historical_fixtures(
            league_id=args.league_id,
            season=args.season,
            with_enrichment=args.with_enrichment,
            refresh=args.refresh,
        )
