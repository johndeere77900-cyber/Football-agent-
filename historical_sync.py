"""
Historical Data Acquisition Collector for Football Agent.

Acquires historical league fixtures and optional statistics enrichment from API-Football
and stores them permanently in persistent storage (Neon PostgreSQL / SQLite).

Key invariants:
- Backtests read from database, NOT API-Football.
- This module is the sole controlled mechanism for acquiring historical fixture data.
- Strict quota safety: enforces API_FOOTBALL_HISTORICAL_DAILY_BUDGET.
- Idempotent and safe to run repeatedly.
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
    historical_budget: int = None,
) -> dict:
    """
    Acquire historical fixtures (and optional enrichment) for a league/season
    and save them into persistent storage.
    """
    if historical_budget is None:
        historical_budget = int(getattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50))

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    initial_request_count = storage.get_api_request_count("api_football", today_str)

    existing_before = storage.get_historical_fixture_count(league_id, season)
    existing_fixtures = storage.get_historical_fixtures(league_id, season)
    existing_ids = {
        item.get("fixture", {}).get("id")
        for item in existing_fixtures
        if isinstance(item, dict) and item.get("fixture", {}).get("id") is not None
    }

    quota_budget_stopped = False

    if initial_request_count >= historical_budget:
        quota_budget_stopped = True
        print(
            f"Historical daily budget ceiling reached ({initial_request_count}/{historical_budget} requests used today). Skipping API acquisition.",
            flush=True,
        )
        final_count = storage.get_historical_fixture_count(league_id, season)
        report = {
            "league_id": league_id,
            "season": season,
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
        return report

    fixtures_received = []
    try:
        fixtures_received = api_football.get_league_fixtures(league_id, season)
    except api_football.APIFootballQuotaExhaustedError as exc:
        quota_budget_stopped = True
        print(f"API Quota exhausted during fixture fetch: {exc}", flush=True)

    requests_after_fixtures = storage.get_api_request_count("api_football", today_str)
    requests_for_fixtures = requests_after_fixtures - initial_request_count

    if not isinstance(fixtures_received, list):
        fixtures_received = []

    # Filter & deduplicate received fixtures
    valid_fixtures = []
    seen_ids = set()
    duplicates_skipped = 0

    for item in fixtures_received:
        if not isinstance(item, dict):
            continue

        fid = item.get("fixture", {}).get("id")
        if fid is None:
            continue

        try:
            fid = int(fid)
        except (TypeError, ValueError):
            continue

        if fid in seen_ids:
            duplicates_skipped += 1
            continue

        seen_ids.add(fid)
        valid_fixtures.append(item)

    # Save fixtures
    save_result = storage.save_historical_fixtures(valid_fixtures, league_id, season)
    newly_stored = save_result.get("inserted", 0)
    already_existing_skipped = save_result.get("valid", 0) - newly_stored

    enrichment_stored = 0

    # Optional statistical enrichment acquisition
    if with_enrichment and not quota_budget_stopped:
        current_reqs = storage.get_api_request_count("api_football", today_str)
        if current_reqs >= historical_budget:
            quota_budget_stopped = True
            print(
                f"Historical budget ceiling reached before enrichment fetch ({current_reqs}/{historical_budget}).",
                flush=True,
            )
        else:
            # Get stored historical fixtures and check which are missing from enrichment
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
                    enriched_batch = api_football.get_enriched_fixtures(missing_enrichment_ids)
                    if enriched_batch:
                        enrichment_stored = storage.save_historical_enrichment(enriched_batch)
                except api_football.APIFootballQuotaExhaustedError:
                    quota_budget_stopped = True

    final_reqs = storage.get_api_request_count("api_football", today_str)
    total_consumed = final_reqs - initial_request_count

    if final_reqs >= historical_budget:
        quota_budget_stopped = True

    final_stored_count = storage.get_historical_fixture_count(league_id, season)

    report = {
        "league_id": league_id,
        "season": season,
        "existing_before": existing_before,
        "fixtures_received": len(fixtures_received),
        "valid_fixtures": len(valid_fixtures),
        "duplicates_skipped": duplicates_skipped,
        "newly_stored": newly_stored,
        "already_existing_skipped": already_existing_skipped,
        "api_requests_consumed": total_consumed,
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
    print(f"Existing fixtures before sync: {report['existing_before']}")
    print(f"Fixtures received from API: {report['fixtures_received']}")
    print(f"Valid fixtures: {report['valid_fixtures']}")
    print(f"Duplicates skipped: {report['duplicates_skipped']}")
    print(f"Newly stored fixtures: {report['newly_stored']}")
    print(f"Already-existing fixtures skipped: {report['already_existing_skipped']}")
    print(f"API requests consumed: {report['api_requests_consumed']}")
    print(f"Quota budget stopped job: {report['quota_budget_stopped']}")
    print(f"Enriched records stored: {report['enrichment_stored']}")
    print(f"Final stored fixture count: {report['final_stored_count']}")
    print("=== END HISTORICAL SYNC REPORT ===\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Historical Data Collector for API-Football fixtures."
    )
    parser.add_argument("--league-id", type=int, required=True, help="API-Football League ID")
    parser.add_argument("--season", type=int, required=True, help="Season year (e.g. 2024 or 2025)")
    parser.add_argument(
        "--with-enrichment",
        action="store_true",
        help="Also fetch and store statistical enrichment (corners/cards)",
    )

    args = parser.parse_args()

    # Initialize DB schema if needed
    storage.init_db()

    sync_historical_fixtures(
        league_id=args.league_id,
        season=args.season,
        with_enrichment=args.with_enrichment,
    )
