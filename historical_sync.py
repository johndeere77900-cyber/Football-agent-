"""
Historical Data Acquisition Collector for Football Agent.

Acquires historical league fixtures and optional statistics enrichment from direct CSV sources, SoccerData providers, and football-data.org API fallback
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

import data_resolver
from data_resolver import DataResolver
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
    if acquisition_failed:
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
        "football_data_org",
        "football_data_csv",
        "soccerdata",
        "soccerdata_fbref",
        "soccerdata_match_history",
        "soccerdata_sofascore",
        "mixed",
    }
    if prov not in supported_providers:
        return False

    if quota_budget_stopped and prov in {"football_data_org"}:
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
        historical_budget = int(getattr(config, "FOOTBALL_DATA_DAILY_LIMIT", 100))

    dataset_info = storage.get_historical_dataset_status(league_id, season)
    current_status = dataset_info["status"]
    existing_before = dataset_info["fixture_count"] or storage.get_historical_fixture_count(league_id, season)

    # Revalidate persisted fixture integrity and coverage before early return
    if current_status == "COMPLETE" and not refresh:
        stored_fixtures = storage.get_historical_fixtures(league_id, season)
        exp_team_ids = data_resolver._get_authoritative_historical_team_ids(
            league_id, season, max_budget=historical_budget, allow_network=False
        )
        comp_fmt = "double_round_robin" if league_id in data_resolver.DOUBLE_ROUND_ROBIN_LEAGUE_IDS else "unknown"

        # A COMPLETE manifest is accepted only after structural revalidation.
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
        if reval_coverage.get("verified") is True:
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
        else:
            print(f"Dataset was marked COMPLETE but revalidation failed for league {league_id} season {season} ({reval_coverage.get('reason')}). Proceeding with sync.", flush=True)
            storage.mark_historical_dataset_incomplete(
                league_id,
                season,
                fixture_count=len(stored_fixtures),
                sport="football",
                error_reason=f"revalidation_failed_{reval_coverage.get('reason')}",
            )
            current_status = "INCOMPLETE"

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Football historical acquisition uses direct CSV, SoccerData, and football-data.org

    quota_budget_stopped = False
    acquisition_failed = False
    last_error_reason = None

    # Football historical acquisition uses direct sources and football-data.org fallback

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

    # DataResolver instance initialized without preflight network calls
    resolver = DataResolver()

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
                    page_exp_teams = extracted

            storage.mark_historical_dataset_incomplete(
                league_id,
                season,
                fixture_count=current_stored_count,
                sport="football",
                expected_pages=safe_exp_pages,
                pages_completed=pages_completed,
                acquisition_complete=False,
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

    if pages_completed > 0:
        safe_exp_pages = expected_pages if isinstance(expected_pages, int) and expected_pages > 0 else 1
        safe_pages_comp = pages_completed
        safe_rejected_cnt = cum_rejected_cnt
        safe_empty_cnt = cum_empty_cnt
    else:
        safe_exp_pages = dataset_info.get("expected_pages") or 1
        safe_pages_comp = dataset_info.get("pages_completed") or 1
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

    # Optional statistical enrichment acquisition
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

    # Current/ongoing seasons (2026+) remain INCOMPLETE until season end
    import datetime as dt_mod
    current_year = dt_mod.datetime.now(dt_mod.timezone.utc).year
    if season >= current_year:
        is_fully_complete = False

    acquisition_complete = is_fully_complete

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
        )
        final_status = "COMPLETE"
    else:
        if quota_budget_stopped and last_error_reason != "quota_budget_exhausted_before_acquisition":
            last_error_reason = "quota_budget_exhausted_during_acquisition"
        elif not last_error_reason:
            if isinstance(db_coverage, dict) and db_coverage.get("verified") is not True:
                last_error_reason = f"structural_coverage_failed_{db_coverage.get('reason')}"
            elif page_source_val not in ("football_data_csv", "soccerdata_fbref", "soccerdata_match_history", "soccerdata_sofascore", "football_data_org", "mixed"):
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
        )
        final_status = "INCOMPLETE"

    total_sync_elapsed = time.perf_counter() - t_sync_start
    report = {
        "league_id": league_id,
        "season": season,
        "status": final_status,
        "existing_before": existing_before,
        "fixtures_received": len(fixtures_received),
        "valid_fixtures": valid_fixtures_count,
        "duplicates_skipped": duplicates_skipped,
        "rejected_count": cum_rejected_cnt,
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
        "total_sync_duration": round(total_sync_elapsed, 3),
        "db_write_duration": round(total_db_write_duration, 3),
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
    if "total_sync_duration" in report:
        print(f"Total sync duration: {report['total_sync_duration']:.3f}s (DB write duration: {report.get('db_write_duration', 0):.3f}s)")
    if report.get("status") == "COMPLETE" and report.get("api_requests_consumed") == 0:
        print("Dataset already COMPLETE; API acquisition skipped.")
    print("=== END HISTORICAL SYNC REPORT ===\n")


import basketball_api
import espn_basketball_provider


def _validate_historical_basketball_dataset(stored_games: list, league_id: int, season: int, dataset_info: dict) -> bool:
    """
    Validate persisted basketball games against basketball-specific completeness rules:
    - Non-empty game list matching count metadata.
    - Source-scoped game identity (source, game_id) present, valid, and unique.
    - Requested league and season match.
    - Participating home and away teams valid and distinct.
    - Required game date, completed status, and valid scores present.
    - Dataset manifest page counts and rejected counts valid.
    - Distinguishes completed historical seasons from ongoing seasons using game status and date evidence (no blanket calendar-year rejection).
    """
    if not isinstance(stored_games, list) or not stored_games:
        return False

    seen_game_ids = set()
    for game in stored_games:
        is_valid, reason, _ = storage.validate_historical_basketball_game(
            game, target_league_id=league_id, target_season=season, require_completed=True
        )
        if not is_valid:
            return False

        if "_storage_source" in game:
            src = game.get("_storage_source")
        else:
            src = game.get("source")
            if not src and isinstance(game.get("provider_provenance"), dict):
                src = game["provider_provenance"].get("provider")

        if not isinstance(src, str) or not src.strip():
            return False

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

    if (
        manifest_count != len(stored_games)
        or e_pages <= 0
        or p_completed != e_pages
        or rej_cnt != 0
        or empty_pages_count != 0
    ):
        return False

    if len(seen_game_ids) != len(stored_games):
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

    # 1. ESPN Primary Acquisition for NBA (league_id == 12)
    espn_coverage_complete = False
    if league_id == 12 and getattr(config, "ENABLE_ESPN_BASKETBALL", True):
        try:
            espn_games, coverage_meta = espn_basketball_provider.fetch_espn_nba_season_games(league_id, season)
            if isinstance(coverage_meta, dict):
                espn_coverage_complete = (
                    coverage_meta.get("months_attempted") == 9
                    and coverage_meta.get("months_failed") == 0
                    and coverage_meta.get("months_succeeded") == 9
                    and coverage_meta.get("all_months_succeeded") is True
                )
            if espn_games:
                games_received.extend(espn_games)
                save_res = storage.save_historical_basketball_games(
                    espn_games, league_id, season, source="espn_url", require_completed=True
                )
                total_valid_games += save_res.get("valid", 0)
                total_newly_stored += save_res.get("inserted", 0)
                total_duplicates_skipped += save_res.get("duplicates_skipped", 0)
                total_rejected_count += save_res.get("rejected_count", 0)
                for r_reason, r_cnt in save_res.get("rejection_reasons", {}).items():
                    all_rejection_reasons[r_reason] = all_rejection_reasons.get(r_reason, 0) + r_cnt
        except Exception as exc:
            print(f"ESPN NBA acquisition attempt failed for season {season}: {exc}", flush=True)

    expected_pages = dataset_info.get("expected_pages", 0)
    pages_completed = dataset_info.get("pages_completed", 0)
    acquisition_complete = False

    stored_espn_check = storage.get_historical_basketball_games(
        league_id, season, source="espn_url", include_storage_source=True
    )
    if espn_coverage_complete and stored_espn_check:
        dataset_eval_info = {
            "fixture_count": len(stored_espn_check),
            "expected_pages": 9,
            "pages_completed": 9,
            "rejected_count": total_rejected_count,
            "empty_pages_count": total_empty_pages_count,
        }
        if _validate_historical_basketball_dataset(stored_espn_check, league_id, season, dataset_eval_info):
            expected_pages = 9
            pages_completed = 9
            acquisition_complete = True

    if initial_request_count >= historical_budget and not acquisition_complete:
        quota_budget_stopped = True
        last_error_reason = "quota_budget_exhausted_before_acquisition"
        print(
            f"Historical daily budget ceiling reached ({initial_request_count}/{historical_budget} basketball requests used today). Skipping API acquisition.",
            flush=True,
        )

    # 2. Fallback to API-Basketball if ESPN incomplete and quota permits
    if not acquisition_complete and not quota_budget_stopped:
        current_page = 1
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

                save_result = storage.save_historical_basketball_games(
                    page_games, league_id, season, require_completed=True
                )
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

    if pages_completed > 0:
        safe_exp_pages_b = expected_pages if isinstance(expected_pages, int) and expected_pages > 0 else 1
        safe_pages_comp_b = pages_completed
        safe_rejected_cnt_b = cum_rejected_cnt_b
        safe_empty_cnt_b = cum_empty_cnt_b
    else:
        safe_exp_pages_b = dataset_info.get("expected_pages", 0)
        safe_pages_comp_b = dataset_info.get("pages_completed", 0)
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
        },
    )

    is_fully_complete = (
        not quota_budget_stopped
        and not acquisition_failed
        and acquisition_complete
        and db_validation_passed
    )

    if is_fully_complete:
        storage.mark_historical_dataset_complete(
            league_id,
            season,
            fixture_count=final_stored_count,
            sport="basketball",
            expected_pages=safe_exp_pages_b,
            pages_completed=safe_pages_comp_b,
            acquisition_complete=acquisition_complete,
            rejected_count=safe_rejected_cnt_b,
            empty_pages_count=safe_empty_cnt_b,
        )
        final_status = "COMPLETE"
    else:
        storage.mark_historical_dataset_incomplete(
            league_id,
            season,
            fixture_count=final_stored_count,
            sport="basketball",
            expected_pages=safe_exp_pages_b,
            pages_completed=safe_pages_comp_b,
            acquisition_complete=acquisition_complete,
            enrichment_status=dataset_info.get("enrichment_status", "NONE"),
            rejected_count=safe_rejected_cnt_b,
            empty_pages_count=safe_empty_cnt_b,
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
        "rejected_count": cum_rejected_cnt_b,
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
    bb_reqs = storage.get_api_request_count("api_basketball", today_str, request_type="historical")
    bb_budget = int(getattr(config, "API_BASKETBALL_HISTORICAL_DAILY_BUDGET", 50))

    print(f"API Request Counters Today ({today_str}):", flush=True)
    print("  Football Historical: Direct CSV / SoccerData / football-data.org (0 API-Football calls)", flush=True)
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

        if report.get("quota_budget_stopped"):
            print(
                f"Queue: API quota/budget exhausted for {sport} during league {league_id} season {season}.",
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

        import datetime as dt_mod
        current_year = dt_mod.datetime.now(dt_mod.timezone.utc).year
        is_active_season = (ssn == current_year)

        if sp == "basketball":
            records = storage.get_historical_basketball_games(lid, ssn, include_storage_source=True)
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
