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
import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import data_resolver
from data_resolver import DataResolver
import config
import storage
from nba_revalidation_evidence import basketball_espn_month_evidence

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
        "knowledge_evidence_status": None,
        "knowledge_evidence_gaps": [],
        "knowledge_participant_status": None,
    }

    try:
        import sports_knowledge

        knowledge_record = sports_knowledge.edition(sport, league_id, season)
        evidence["knowledge_evidence_status"] = knowledge_record.get("evidence_status")
        evidence["knowledge_evidence_gaps"] = knowledge_record.get("evidence_gaps") or []
        evidence["knowledge_participant_status"] = knowledge_record.get("participant_status")
    except Exception:
        knowledge_record = None

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

    # Structural coverage alone is not completion evidence. The edition's
    # participant universe, format expectation and dates must be confirmed by
    # the knowledge catalogue; provisional/unknown evidence fails closed.
    if knowledge_record is not None and knowledge_record.get("evidence_status") != "CONFIRMED":
        knowledge_status = str(knowledge_record.get("evidence_status") or "UNKNOWN").lower()
        return (
            COMPETITION_LIFECYCLE_UNKNOWN,
            f"knowledge_evidence_{knowledge_status}",
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
        "openfootball_json",
        "openfootball_txt",
        "openfootball_internationals",
        "openligadb",
        "football_data_csv",
        "soccerdata",
        "soccerdata_fbref",
        "soccerdata_match_history",
        "mixed",
    }
    if prov not in supported_providers:
        return False

    prov_meta = page_meta.get("provider_metadata") if isinstance(page_meta, dict) else {}
    if not isinstance(prov_meta, dict):
        prov_meta = {}

    # Edition evidence is part of the completion decision. A missing route or an
    # unverified participant/format/window fact must fail closed, not be treated
    # as a successful empty or complete edition.
    route = prov_meta.get("knowledge_provider_route")
    if not isinstance(route, dict) or route.get("completion_allowed") is not True:
        return False

    if prov_meta.get("is_partial") is True or prov_meta.get("is_complete") is False:
        return False

    structural_coverage = prov_meta.get("structural_coverage", {})
    if not isinstance(structural_coverage, dict):
        structural_coverage = {}

    if structural_coverage.get("verified") is not True:
        return False



    return True


def _football_provider_diagnostics(page_meta):
    """
    Build a per-provider evidence record from the resolver's attempt metadata.

    ``provider_outcomes`` classifies each attempt (ACCESS_DENIED, UNSUPPORTED,
    RATE_LIMITED, TIMEOUT, NETWORK_ERROR, PARSER_ERROR, FAILED, VALID_EMPTY, ...),
    but the category alone does not explain a failure. This keeps the provider's
    own status, HTTP status, attempt duration and error text on the manifest so a
    future ``secondary_provider_returned_no_fixtures`` case can be explained
    without the original run logs. It is generated for successful attempts too:
    a verified early-return still records the providers it skipped and the
    duration of the ones it used. Error text is truncated and never contains
    credentials.
    """
    meta = page_meta.get("provider_metadata") if isinstance(page_meta, dict) else None
    if not isinstance(meta, dict):
        return None
    outcomes = meta.get("provider_outcomes") if isinstance(meta.get("provider_outcomes"), dict) else {}
    diagnostics = {}
    for provider in (
        "football_data_csv",
        "soccerdata_fbref",
        "soccerdata_match_history",
        "soccerdata_sofascore",
    ):
        entry = meta.get(provider) if isinstance(meta.get(provider), dict) else {}
        raw = entry.get("raw_metadata") if isinstance(entry.get("raw_metadata"), dict) else {}
        error_text = raw.get("error")
        outcomes_entry = outcomes.get(provider)
        outcome = (
            outcomes_entry.get("outcome")
            if isinstance(outcomes_entry, dict)
            else outcomes_entry
        )
        if outcome is None and not entry and not raw:
            # The provider was never reached in this attempt. Reported explicitly
            # instead of leaving a null that could be mistaken for a failure.
            outcome = "NOT_ATTEMPTED"
        diagnostics[provider] = {
            "outcome": outcome,
            "fixtures": entry.get("count"),
            "duration_seconds": raw.get("duration_seconds"),
            "status": raw.get("status"),
            "error_category": raw.get("error_category"),
            "http_status": raw.get("http_status"),
            "error": str(error_text)[:300] if error_text else None,
        }
    return diagnostics


_FOOTBALL_REQUIRED_STAT_FIELDS = ("shots", "shots_on_target", "corners", "cards")
_FOOTBALL_REPORTED_STAT_FIELDS = (
    "shots", "shots_on_target", "corners", "cards", "possession", "xG",
)


def _fixture_has_stat_field(values, field):
    if field == "cards":
        return all(values.get(column) is not None for column in (
            "home_yellow_cards", "away_yellow_cards",
            "home_red_cards", "away_red_cards",
        ))
    pair = {
        "shots": ("home_shots", "away_shots"),
        "shots_on_target": ("home_shots_on_target", "away_shots_on_target"),
        "corners": ("home_corners", "away_corners"),
        "possession": ("home_possession", "away_possession"),
        "xG": ("home_xg", "away_xg"),
    }.get(field)
    return bool(pair) and values.get(pair[0]) is not None and values.get(pair[1]) is not None


def _football_statistical_field_coverage(fixtures, enrichments, fixture_ids):
    """
    Measure usable statistics per fixture, not enrichment row count.

    The main fixture payload and its enrichment are both legitimate sources. A
    field counts only when both home and away numeric values are present. xG is
    reported separately and is never required when no verified xG source exists.
    """
    fixture_by_id = {}
    for fixture in fixtures if isinstance(fixtures, list) else []:
        if not isinstance(fixture, dict):
            continue
        fid = (fixture.get("fixture") or {}).get("id")
        try:
            fixture_by_id[int(fid)] = fixture
        except (TypeError, ValueError):
            continue

    counts = {field: 0 for field in _FOOTBALL_REPORTED_STAT_FIELDS}
    missing_by_fixture = {}
    for raw_fid in fixture_ids:
        try:
            fid = int(raw_fid)
        except (TypeError, ValueError):
            continue
        values = {column: None for column in storage._ENRICHMENT_STAT_COLUMNS}
        for source_item in (fixture_by_id.get(fid), (enrichments or {}).get(fid)):
            source_values, _ = storage.extract_historical_enrichment_statistics(source_item)
            for column, value in source_values.items():
                if value is not None:
                    values[column] = value
        missing = [
            field for field in _FOOTBALL_REQUIRED_STAT_FIELDS
            if not _fixture_has_stat_field(values, field)
        ]
        if missing:
            missing_by_fixture[fid] = missing
        for field in _FOOTBALL_REPORTED_STAT_FIELDS:
            if _fixture_has_stat_field(values, field):
                counts[field] += 1

    total = len([fid for fid in fixture_ids if str(fid).isdigit() or isinstance(fid, int)])
    field_coverage = {
        field: {
            "fixtures_with_field": count,
            "fixtures_total": total,
            "ratio": round(count / total, 4) if total else None,
        }
        for field, count in counts.items()
    }
    return {
        "fixtures_total": total,
        "field_coverage": field_coverage,
        "missing_by_fixture": missing_by_fixture,
        "xg_available": counts["xG"] > 0,
        "xg_reason": None if counts["xG"] > 0 else "no_verified_xg_source_available",
    }


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
    db_write_calls = 0
    total_identity_resolution_duration = 0.0
    total_identity_resolution_calls = 0
    total_identity_cache_hits = 0
    total_insert_statements = 0
    total_update_statements = 0
    total_insert_duration = 0.0
    total_update_duration = 0.0
    total_commit_duration = 0.0
    allowed_seasons = set(getattr(config, "TARGET_SEASONS", [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]))

    # A tournament edition whose match total is independently documented (for
    # example the 2018 FIFA World Cup, 64 matches) is acquirable even though its
    # season falls outside the generic target range. Only documented editions of
    # this same competition are admitted, matching the storage-level season
    # scope, so acquisition and persistence can never disagree about a season and
    # an undocumented season is still rejected.
    documented_editions_helper = getattr(config, "documented_football_tournament_editions", None)
    if callable(documented_editions_helper):
        allowed_seasons |= set(documented_editions_helper(league_id) or set())

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
        # Football historical fixture acquisition itself is API-key free
        # (OpenFootball, OpenLigaDB, Football-Data.co.uk CSV and SoccerData). The
        # only API budget this path can consume is the optional API-Football
        # statistical-enrichment backfill, which is bounded by its own budget.
        historical_budget = int(getattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50))

    dataset_info = storage.get_historical_dataset_status(league_id, season)
    current_status = dataset_info["status"]
    approved_sources = tuple(
        getattr(config, "FOOTBALL_APPROVED_HISTORICAL_SOURCES", ())
    )
    existing_before = storage.get_historical_fixture_count(
        league_id, season, sources=approved_sources
    )

    # Revalidate only the approved physical cohort; legacy rows stay present but
    # outside the new acquisition contract.
    stored_fixtures = storage.get_historical_fixtures(
        league_id, season,
        include_reconciliation_candidates=True,
        sources=approved_sources,
    )
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
        route_completion_allowed = False
        try:
            import sports_knowledge

            route_completion_allowed = (
                sports_knowledge.provider_route("football", league_id, season).get("completion_allowed")
                is True
            )
        except Exception as route_exc:
            print(
                f"Knowledge provider route unavailable during revalidation for league {league_id} season {season}: {route_exc}",
                flush=True,
            )

        if (
            reval_coverage.get("verified") is True
            and reval_lifecycle == COMPETITION_LIFECYCLE_COMPLETED
            and route_completion_allowed
        ):
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
                "enriched": 0,
                "already_existing_skipped": 0,
                "already_existing": 0,
                "conflict_only": 0,
                "metadata_conflicts": {},
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

    # Approved football historical providers (in order): OpenFootball JSON,
    # OpenFootball TXT, OpenFootball internationals, OpenLigaDB, Football-Data
    # CSV, SoccerData FBref, SoccerData MatchHistory. Sofascore is not attempted
    # automatically. API-Football and football-data.org are not used on this path.

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
    total_enriched = 0
    total_already_existing = 0
    total_conflict_only = 0
    total_ambiguous_identity = 0
    total_legacy_identity_overlap = 0
    total_duplicates_skipped = 0
    total_rejected_count = 0
    total_empty_pages_count = 0
    all_rejection_reasons = {}
    metadata_conflict_counts = {}
    metadata_conflict_events = 0

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
            # Enrichment, unchanged rows and conflict-only audit evidence are
            # reported separately from new rows, so a re-acquisition's metadata
            # progress is never mistaken for new coverage and never double-counted
            # across providers, pages or retries.
            total_enriched += save_result.get("enriched", 0)
            total_already_existing += save_result.get("already_existing", 0)
            total_conflict_only += save_result.get("conflict_only", 0)
            total_ambiguous_identity += save_result.get("ambiguous_identity", 0)
            total_legacy_identity_overlap += save_result.get("legacy_identity_overlap", 0)
            total_duplicates_skipped += save_result.get("duplicates_skipped", 0)
            total_rejected_count += save_result.get("rejected_count", 0)
            total_db_write_duration += save_result.get("db_write_duration", 0.0)
            db_write_calls += 1
            total_identity_resolution_duration += float(
                save_result.get("identity_resolution_duration") or 0.0
            )
            total_identity_resolution_calls += int(
                save_result.get("identity_resolution_calls") or 0
            )
            total_identity_cache_hits += int(save_result.get("identity_cache_hits") or 0)
            total_insert_statements += int(save_result.get("insert_statements") or 0)
            total_update_statements += int(save_result.get("update_statements") or 0)
            total_insert_duration += float(save_result.get("insert_duration") or 0.0)
            total_update_duration += float(save_result.get("update_duration") or 0.0)
            total_commit_duration += float(save_result.get("commit_duration") or 0.0)

            for conflict_field, conflict_count in (save_result.get("metadata_conflicts") or {}).items():
                metadata_conflict_counts[conflict_field] = (
                    metadata_conflict_counts.get(conflict_field, 0) + conflict_count
                )
            metadata_conflict_events += int(save_result.get("metadata_conflict_events") or 0)

            for r_reason, r_cnt in save_result.get("rejection_reasons", {}).items():
                all_rejection_reasons[r_reason] = all_rejection_reasons.get(r_reason, 0) + r_cnt

            pages_completed = current_page
            current_stored_count = storage.get_historical_fixture_count(
                league_id, season, sources=approved_sources
            )

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

    stored_fixtures_all = storage.get_historical_fixtures(
        league_id, season,
        include_reconciliation_candidates=True,
        sources=approved_sources,
    )
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
    already_existing_skipped = total_already_existing

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
        all_stored = storage.get_historical_fixtures(league_id, season, include_reconciliation_candidates=True)
        finished_ids = [
            item.get("fixture", {}).get("id")
            for item in all_stored
            if isinstance(item, dict)
            and item.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN")
            and item.get("fixture", {}).get("id") is not None
        ]

        stored_enrichment = storage.get_historical_enrichment(finished_ids) if finished_ids else {}
        pre_coverage = _football_statistical_field_coverage(
            all_stored, stored_enrichment, finished_ids
        )
        missing_enrichment_ids = list(pre_coverage["missing_by_fixture"])

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

    final_stored_count = storage.get_historical_fixture_count(
        league_id, season, sources=approved_sources
    )

    # Determine enrichment status
    all_stored_fixtures = storage.get_historical_fixtures(league_id, season, include_reconciliation_candidates=True)
    finished_fixture_ids = [
        item.get("fixture", {}).get("id")
        for item in all_stored_fixtures
        if isinstance(item, dict)
        and item.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN")
        and item.get("fixture", {}).get("id") is not None
    ]
    stored_enrichments = storage.get_historical_enrichment(finished_fixture_ids) if finished_fixture_ids else {}

    enrichment_coverage = _football_statistical_field_coverage(
        all_stored_fixtures, stored_enrichments, finished_fixture_ids
    )
    if finished_fixture_ids:
        total_finished = len(finished_fixture_ids)
        complete_fields = [
            field for field in _FOOTBALL_REQUIRED_STAT_FIELDS
            if enrichment_coverage["field_coverage"][field]["fixtures_with_field"] >= total_finished
        ]
        any_usable = any(
            item["fixtures_with_field"] > 0
            for item in enrichment_coverage["field_coverage"].values()
        )
        if len(complete_fields) == len(_FOOTBALL_REQUIRED_STAT_FIELDS):
            enrichment_status = "COMPLETE"
        elif any_usable:
            enrichment_status = "PARTIAL"
        else:
            enrichment_status = "NONE"
    else:
        enrichment_status = dataset_info.get("enrichment_status") or "NONE"

    # Determine exact source semantics and re-read the approved cohort for final
    # structural validation. Legacy records remain physically present.
    stored_fixtures_all = storage.get_historical_fixtures(
        league_id, season,
        include_reconciliation_candidates=True,
        sources=approved_sources,
    )

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

    # Attribution must describe the persisted dataset, not just this attempt's
    # provider payload: rows acquired by an earlier provider are part of the same
    # dataset, and a manifest contributor list taken only from the current page
    # would misdescribe it. Record-level provenance is the authority; the attempt's
    # own list is used only when the persisted records carry no provenance at all.
    persisted_provider_counts = {}
    for stored_fixture in stored_fixtures_all:
        if not isinstance(stored_fixture, dict):
            continue
        prov_obj = stored_fixture.get("provider_provenance") if isinstance(stored_fixture.get("provider_provenance"), dict) else {}
        provider_name = stored_fixture.get("source") or prov_obj.get("provider")
        if isinstance(provider_name, str) and provider_name.strip():
            key_name = provider_name.strip()
            persisted_provider_counts[key_name] = persisted_provider_counts.get(key_name, 0) + 1

    persisted_contributors = data_resolver._providers_contributed_from_records(stored_fixtures_all)
    attempted_contributors = (
        (page_meta_val.get("provider_metadata", {}) or {}).get("providers_contributed")
        if isinstance(page_meta_val, dict) else None
    )

    provider_coverage_football = {
        "coverage_unit": COVERAGE_UNIT_SINGLE_SHOT,
        "status": acquisition_state,
        "providers_contributed": persisted_contributors or attempted_contributors,
        "persisted_records_by_provider": persisted_provider_counts,
        "provider_outcomes": (page_meta_val.get("provider_metadata", {}) or {}).get("provider_outcomes")
        if isinstance(page_meta_val, dict) else None,
        "provider_diagnostics": _football_provider_diagnostics(page_meta_val),
        # Persistence evidence recorded with the manifest: what this sync actually
        # inserted, enriched, left unchanged, audited as a conflict or rejected.
        "persistence": {
            "valid": total_valid_fixtures,
            "inserted": total_newly_stored,
            "enriched": total_enriched,
            "already_existing": total_already_existing,
            "conflict_only": total_conflict_only,
            "ambiguous_identity": total_ambiguous_identity,
            "legacy_identity_overlap": total_legacy_identity_overlap,
            "duplicates_skipped": total_duplicates_skipped,
            "rejected": total_rejected_count,
            "metadata_conflicts": metadata_conflict_counts,
            "metadata_conflict_events": metadata_conflict_events,
        },
        "coverage_evidence_gaps": db_coverage.get("evidence_gaps") if isinstance(db_coverage, dict) else None,
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
            elif page_source_val not in (
                "openfootball_json", "openfootball_txt",
                "openfootball_internationals", "openligadb",
                "football_data_csv", "soccerdata_fbref",
                "soccerdata_match_history", "mixed",
            ):
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
        "persisted_records_by_provider": persisted_provider_counts,
        "access_denied_providers": sorted(
            provider_name
            for provider_name, outcome in (provider_coverage_football.get("provider_outcomes") or {}).items()
            if outcome == "ACCESS_DENIED"
        ),
        "provider_outcomes": provider_coverage_football.get("provider_outcomes"),
        "provider_diagnostics": provider_coverage_football.get("provider_diagnostics"),
        "provider_coverage": provider_coverage_football,
        "incomplete_reason": None if is_fully_complete else last_error_reason,
        "existing_before": existing_before,
        "fixtures_received": len(fixtures_received),
        "valid_fixtures": valid_fixtures_count,
        "duplicates_skipped": duplicates_skipped,
        "rejected_count": cum_rejected_cnt,
        "rejection_reasons": rejection_reasons,
        "newly_stored": newly_stored,
        "enriched": total_enriched,
        "already_existing": total_already_existing,
        "already_existing_skipped": already_existing_skipped,
        "conflict_only": total_conflict_only,
        "ambiguous_identity": total_ambiguous_identity,
        "legacy_identity_overlap": total_legacy_identity_overlap,
        "metadata_conflicts": metadata_conflict_counts,
        "metadata_conflict_events": metadata_conflict_events,
        "persistence_breakdown": {
            "valid": valid_fixtures_count,
            "inserted": total_newly_stored,
            "enriched": total_enriched,
            "already_existing": total_already_existing,
            "conflict_only": total_conflict_only,
            "ambiguous_identity": total_ambiguous_identity,
            "legacy_identity_overlap": total_legacy_identity_overlap,
            "duplicates_skipped": total_duplicates_skipped,
            "rejected": total_rejected_count,
            "metadata_conflicts": metadata_conflict_counts,
        },
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
        "enrichment_field_coverage": enrichment_coverage["field_coverage"],
        "xg_coverage": {
            "available": enrichment_coverage["xg_available"],
            "reason": enrichment_coverage["xg_reason"],
        },
        "total_sync_duration": round(total_sync_elapsed, 3),
        "db_write_duration": round(total_db_write_duration, 3),
        "db_write_calls": db_write_calls,
        "identity_resolution_duration": round(total_identity_resolution_duration, 3),
        "identity_resolution_calls": total_identity_resolution_calls,
        "identity_cache_hits": total_identity_cache_hits,
        "insert_statements": total_insert_statements,
        "update_statements": total_update_statements,
        "insert_duration": round(total_insert_duration, 3),
        "update_duration": round(total_update_duration, 3),
        "commit_duration": round(total_commit_duration, 3),
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
            if isinstance(prov, dict) and prov.get("coverage_unit"):
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
    print(f"Enriched existing fixtures: {report.get('enriched', 0)}")
    print(f"Already-existing fixtures skipped: {report['already_existing_skipped']}")
    print(f"Conflict-only records audited: {report.get('conflict_only', 0)}")
    if report.get("metadata_conflicts"):
        print(f"Metadata conflicts (existing value kept): {report['metadata_conflicts']}")
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
        if not mapped and " - " in raw:
            # API-Sports basketball reports stage labels in the form
            # "<competition> - <stage>" (for example "NBA - Regular Season").
            # Only the documented stage suffix is recognised; an unknown suffix
            # still returns None instead of being guessed.
            suffix = raw.strip().rsplit(" - ", 1)[-1].strip().lower().replace("-", "_").replace(" ", "_")
            mapped = _BASKETBALL_STAGE_LABELS.get(suffix)
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
            print(
                f"Basketball dataset validation rejected: {counter_name} is not an integer "
                f"({counter_value!r}).",
                flush=True,
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


def _basketball_persisted_revalidation(league_id, season, manifest, promote=False):
    """
    Revalidate a persisted basketball dataset without fetching provider data.

    Completion evidence is rebuilt from the stored rows and the provider's own
    recorded monthly/page units. The old manifest status is never accepted as
    evidence, and a failed fallback provider that persisted no games is reported
    without invalidating a separately verified ESPN dataset.
    """
    stored = storage.get_historical_basketball_games(
        league_id, season, include_storage_source=True
    )
    stored = stored if isinstance(stored, list) else []
    counts_by_source = {}
    seen_ids = set()
    identity_valid = True
    structural_valid = True
    for game in stored:
        source = _basketball_game_source(game) or "unknown_source"
        counts_by_source[source] = counts_by_source.get(source, 0) + 1
        game_id = _basketball_game_event_id(game)
        if game_id is None:
            identity_valid = False
        elif (source, game_id) in seen_ids:
            identity_valid = False
        else:
            seen_ids.add((source, game_id))
        try:
            valid, _, _ = storage.validate_historical_basketball_game(
                game,
                target_league_id=league_id,
                target_season=season,
                require_completed=True,
            )
        except Exception:
            valid = False
        if not valid:
            structural_valid = False

    phase_counts = _basketball_season_type_counts(stored)
    unclassified = phase_counts.get("unclassified", 0)
    regular_games = phase_counts.get("regular_season", 0) + phase_counts.get("regular", 0)
    phase_complete = unclassified == 0 and bool(stored)
    expected_min = _expected_min_season_games(league_id, season)

    raw_coverage = manifest.get("provider_coverage") if isinstance(manifest, dict) else {}
    raw_coverage = raw_coverage if isinstance(raw_coverage, dict) else {}
    provider_evidence = {}
    ignored_providers = {}
    espn_month_evidence = None
    for source_name, record_count in counts_by_source.items():
        entry = raw_coverage.get(source_name)
        entry = entry if isinstance(entry, dict) else {}
        if source_name == "espn_url":
            espn_month_evidence = basketball_espn_month_evidence(season, entry)
            expected_units = espn_month_evidence["expected_units"]
            completed_units = espn_month_evidence["success_units"]
            units_complete = espn_month_evidence["evidence_complete"]
            provider_evidence[source_name] = {
                "coverage_unit": "months",
                "expected": expected_units,
                "completed": completed_units,
                "status": ACQUISITION_STATE_VALIDATED if units_complete else ACQUISITION_STATE_INCOMPLETE,
                "competition_verified": espn_month_evidence["month_units_complete"],
                "valid_empty_units": espn_month_evidence["valid_empty_units"],
                "records_persisted": record_count,
                "contributed": True,
                "attempted": True,
                "monthly_details": entry.get("monthly_details"),
                "evidence_source": "persisted_provider_coverage",
                "expected_month_keys": espn_month_evidence["expected_month_keys"],
                "actual_month_keys": espn_month_evidence["actual_month_keys"],
                "missing_month_keys": espn_month_evidence["missing_month_keys"],
                "unexpected_month_keys": espn_month_evidence["unexpected_month_keys"],
                "duplicate_month_keys": espn_month_evidence["duplicate_month_keys"],
                "month_keys_exact": espn_month_evidence["month_keys_exact"],
                "month_units_complete": espn_month_evidence["month_units_complete"],
                "monthly_rejected_totals": espn_month_evidence["monthly_rejected_totals"],
                "rejection_reasons_by_month": espn_month_evidence["rejection_reasons_by_month"],
                "events_rejected": espn_month_evidence["events_rejected"],
                "events_accepted": espn_month_evidence["events_accepted"],
                "events_non_final": espn_month_evidence["events_non_final"],
                "historical_non_final_rejections": espn_month_evidence["historical_non_final_rejections"],
                "historical_non_final_rejections_unresolved": espn_month_evidence["historical_non_final_rejections_unresolved"],
                "rejection_evidence_consistent": espn_month_evidence["rejection_evidence_consistent"],
            }
        else:
            expected_units = entry.get("expected")
            completed_units = entry.get("completed")
            units_complete = (
                isinstance(expected_units, int) and not isinstance(expected_units, bool)
                and expected_units > 0
                and isinstance(completed_units, int) and not isinstance(completed_units, bool)
                and completed_units == expected_units
                and entry.get("status") == ACQUISITION_STATE_VALIDATED
            )
            provider_evidence[source_name] = {
                **entry,
                "expected": expected_units if isinstance(expected_units, int) else 0,
                "completed": completed_units if isinstance(completed_units, int) else 0,
                "status": ACQUISITION_STATE_VALIDATED if units_complete else (
                    entry.get("status") or ACQUISITION_STATE_INCOMPLETE
                ),
                "records_persisted": record_count,
                "contributed": True,
                "attempted": entry.get("attempted", True),
                "evidence_source": "persisted_manifest_coverage",
            }
    for source_name, entry in raw_coverage.items():
        if source_name in counts_by_source or not isinstance(entry, dict):
            continue
        ignored_providers[source_name] = {
            "status": entry.get("status"),
            "records_persisted": int(entry.get("records_persisted") or 0),
            "reason": "contributed_no_persisted_records",
        }

    expected_units_total = sum(int(entry.get("expected") or 0) for entry in provider_evidence.values())
    completed_units_total = sum(int(entry.get("completed") or 0) for entry in provider_evidence.values())
    valid_empty_total = sum(int(entry.get("valid_empty_units") or 0) for entry in provider_evidence.values())
    units_complete = bool(provider_evidence) and all(
        entry.get("status") == ACQUISITION_STATE_VALIDATED
        and entry.get("expected") == entry.get("completed")
        for entry in provider_evidence.values()
    )
    coverage_units = sorted({
        str(entry.get("coverage_unit"))
        for entry in provider_evidence.values()
        if entry.get("coverage_unit")
    })
    dataset_coverage_unit = "+".join(coverage_units) if coverage_units else COVERAGE_UNIT_ESPN_MONTHS
    manifest_count = manifest.get("fixture_count") if isinstance(manifest, dict) else None
    count_matches = (
        isinstance(manifest_count, int) and not isinstance(manifest_count, bool)
        and manifest_count == len(stored)
    )
    evaluation = {
        "fixture_count": len(stored),
        "expected_pages": expected_units_total,
        "pages_completed": completed_units_total,
        "rejected_count": (
            espn_month_evidence["events_rejected"] if espn_month_evidence else 0
        ),
        "empty_pages_count": valid_empty_total,
        "coverage_unit": dataset_coverage_unit,
        "provider_coverage": provider_evidence,
    }
    dataset_valid = _validate_historical_basketball_dataset(
        stored, league_id, season, evaluation
    )
    season_sufficient = expected_min is not None and regular_games >= expected_min
    coverage_verified = bool(units_complete and structural_valid and identity_valid and phase_complete)
    lifecycle_coverage = {
        "verified": coverage_verified,
        "reason": None,
        "coverage_unit": dataset_coverage_unit,
        "expected_coverage_units": expected_units_total,
        "completed_coverage_units": completed_units_total,
        "provider_coverage": provider_evidence,
    }
    lifecycle_state, lifecycle_reason, _ = get_competition_lifecycle(
        "basketball", league_id, season, stored, coverage=lifecycle_coverage
    )

    if not stored:
        blocker = manifest.get("error_reason") or "no_persisted_records"
    elif not count_matches:
        blocker = f"manifest_count_mismatch:{manifest_count!r}_of_{len(stored)}"
    elif not identity_valid:
        blocker = "invalid_or_duplicate_persisted_game_identities"
    elif not structural_valid:
        blocker = "invalid_persisted_game_record"
    elif not phase_complete:
        blocker = "season_type_metadata_missing"
    elif lifecycle_state != COMPETITION_LIFECYCLE_COMPLETED and (
        str(lifecycle_reason).startswith("active_season")
        or lifecycle_reason == "season_not_started"
    ):
        # An ongoing season is incomplete for its lifecycle, never because a
        # future/current-season total or historical rejection evidence was
        # expected. Active/upcoming lifecycle is checked before provider-event
        # evidence so the 2026/27 season keeps its truthful ongoing blocker.
        blocker = f"lifecycle_{lifecycle_state.lower()}_{lifecycle_reason}"
    elif espn_month_evidence and not espn_month_evidence["month_keys_exact"]:
        parts = []
        if espn_month_evidence["missing_month_keys"]:
            parts.append("missing=" + ",".join(espn_month_evidence["missing_month_keys"]))
        if espn_month_evidence["unexpected_month_keys"]:
            parts.append("unexpected=" + ",".join(espn_month_evidence["unexpected_month_keys"]))
        if espn_month_evidence["duplicate_month_keys"]:
            parts.append("duplicate=" + ",".join(espn_month_evidence["duplicate_month_keys"]))
        if espn_month_evidence["month_key_issues"]:
            parts.append("malformed=" + ",".join(espn_month_evidence["month_key_issues"]))
        if not parts:
            parts.append(
                f"count={len(espn_month_evidence['actual_month_keys'])}"
                f"_of_{espn_month_evidence['expected_units']}"
            )
        blocker = "provider_month_keys_invalid:" + ";".join(parts)
    elif espn_month_evidence and not espn_month_evidence["month_units_complete"]:
        blocker = (
            "provider_month_evidence_incomplete:"
            f"{espn_month_evidence['verified_units']}_of_{espn_month_evidence['expected_units']}"
        )
    elif espn_month_evidence and not espn_month_evidence["rejection_evidence_consistent"]:
        detail = (espn_month_evidence["malformed_details"] or ["unknown"])[0]
        blocker = f"provider_rejection_evidence_inconsistent:{detail}"
    elif espn_month_evidence and (
        espn_month_evidence["events_rejected"] > 0
        or espn_month_evidence["events_non_final"] > 0
    ):
        parts = []
        if espn_month_evidence["events_rejected"]:
            parts.append(f"rejected={espn_month_evidence['events_rejected']}")
        if espn_month_evidence["historical_non_final_rejections"]:
            parts.append(
                f"historical_non_final={espn_month_evidence['historical_non_final_rejections']}"
            )
        if espn_month_evidence["other_rejected_events"]:
            parts.append(f"other_rejected={espn_month_evidence['other_rejected_events']}")
        if espn_month_evidence["events_non_final"]:
            parts.append(f"non_final_events={espn_month_evidence['events_non_final']}")
        blocker = "unresolved_provider_event_evidence:" + ",".join(parts)
    elif lifecycle_state != COMPETITION_LIFECYCLE_COMPLETED:
        blocker = f"lifecycle_{lifecycle_state.lower()}_{lifecycle_reason}"
    elif not units_complete:
        blocker = f"provider_coverage_units_incomplete:{completed_units_total}_of_{expected_units_total}"
    elif expected_min is None:
        blocker = "expected_season_coverage_unavailable"
    elif not season_sufficient:
        blocker = f"regular_season_coverage_insufficient:{regular_games}_of_{expected_min}"
    elif not dataset_valid:
        blocker = "persisted_dataset_invalid"
    else:
        blocker = None

    eligible = blocker is None
    request_necessary = bool(
        blocker and (
            blocker.startswith("manifest_count_mismatch")
            or blocker.startswith("invalid")
            or blocker.startswith("season_type_metadata_missing")
            or blocker.startswith("provider_month_keys_invalid")
            or blocker.startswith("provider_month_evidence_incomplete")
            or blocker.startswith("provider_rejection_evidence_inconsistent")
            or blocker.startswith("unresolved_provider_event_evidence")
            or blocker.startswith("provider_coverage_units_incomplete")
            or blocker.startswith("regular_season_coverage_insufficient")
        )
    )
    result = {
        "persisted_revalidation_eligible": eligible,
        "exact_blocker": blocker,
        "another_provider_request_necessary": request_necessary,
        "stored_count": len(stored),
        "manifest_fixture_count": manifest_count,
        "manifest_count_matches": count_matches,
        "identity_valid": identity_valid,
        "structural_valid": structural_valid,
        "phase_complete": phase_complete,
        "phase_counts": phase_counts,
        "regular_season_games": regular_games,
        "unclassified_games": unclassified,
        "expected_min_regular_season_games": expected_min,
        "season_coverage_sufficient": season_sufficient,
        "provider_coverage_units_complete": units_complete,
        "provider_coverage_unit": dataset_coverage_unit,
        "provider_expected_units": expected_units_total,
        "provider_completed_units": completed_units_total,
        "provider_evidence": provider_evidence,
        "ignored_providers": ignored_providers,
        "espn_month_evidence": espn_month_evidence,
        "dataset_valid": dataset_valid,
        "coverage_verified": coverage_verified,
        "lifecycle_state": lifecycle_state,
        "lifecycle_reason": lifecycle_reason,
        "validation_evidence": {
            "persisted_count_matches_manifest": count_matches,
            "source_scoped_identities_unique": identity_valid,
            "every_game_structurally_valid": structural_valid,
            "every_game_has_provider_phase": phase_complete,
            "provider_units_complete": units_complete,
            "provider_month_keys_exact": (
                espn_month_evidence["month_keys_exact"] if espn_month_evidence else None
            ),
            "provider_month_units_complete": (
                espn_month_evidence["month_units_complete"] if espn_month_evidence else None
            ),
            "rejection_evidence_consistent": (
                espn_month_evidence["rejection_evidence_consistent"]
                if espn_month_evidence else None
            ),
            "regular_season_expectation_met": season_sufficient,
            "competition_lifecycle_completed": lifecycle_state == COMPETITION_LIFECYCLE_COMPLETED,
        },
    }
    if promote and eligible:
        try:
            storage.mark_historical_dataset_complete(
                league_id,
                season,
                fixture_count=len(stored),
                sport="basketball",
                expected_pages=expected_units_total,
                pages_completed=completed_units_total,
                acquisition_complete=True,
                rejected_count=0,
                empty_pages_count=valid_empty_total,
                coverage_unit=dataset_coverage_unit,
                provider_coverage=provider_evidence,
            )
            result["manifest_promoted"] = True
        except Exception as exc:
            result["persisted_revalidation_eligible"] = False
            result["manifest_promoted"] = False
            result["promotion_error"] = f"{type(exc).__name__}: {exc}"[:300]
            result["exact_blocker"] = f"manifest_promotion_failed:{type(exc).__name__}"
            result["another_provider_request_necessary"] = False
    else:
        result["manifest_promoted"] = False
    return result


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

    persisted_revalidation = None
    if not refresh:
        persisted_revalidation = _basketball_persisted_revalidation(
            league_id, season, dataset_info, promote=True
        )
        if persisted_revalidation.get("persisted_revalidation_eligible"):
            print(
                f"Basketball dataset revalidated from persisted evidence for league "
                f"{league_id} season {season}; no provider request required.",
                flush=True,
            )
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
                "enriched": 0,
                "already_existing_skipped": 0,
                "already_existing": 0,
                "conflict_only": 0,
                "metadata_conflicts": {},
                "api_requests_consumed": 0,
                "quota_budget_stopped": False,
                "final_stored_count": existing_before,
                "skipped_reason": "Persisted evidence revalidated; no provider request required",
                "persisted_revalidation": persisted_revalidation,
            }
        if current_status == "COMPLETE":
            reason = persisted_revalidation.get("exact_blocker") or "persisted_revalidation_failed"
            print(
                f"Basketball dataset was marked COMPLETE but persisted revalidation failed "
                f"for league {league_id} season {season} ({reason}). Proceeding with sync.",
                flush=True,
            )
            storage.mark_historical_dataset_incomplete(
                league_id,
                season,
                fixture_count=existing_before,
                sport="basketball",
                error_reason=f"persisted_revalidation_failed:{reason}",
            )
            current_status = "INCOMPLETE"

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    initial_request_count = storage.get_api_request_count("api_basketball", today_str, request_type="historical")

    quota_budget_stopped = False
    acquisition_failed = False
    last_error_reason = None
    # Structured failure diagnostics. The manifest's error_reason is a stable
    # machine-readable class; the exact exception and the stage that raised it are
    # preserved in provider_coverage so a failed request can be explained without
    # a database-side log search.
    last_error_detail = None
    last_error_stage = None

    games_received = []
    total_db_write_duration_b = 0.0
    db_write_calls_b = 0
    total_valid_games = 0
    total_newly_stored = 0
    total_enriched = 0
    total_already_existing = 0
    total_conflict_only = 0
    metadata_conflict_counts = {}
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
    espn_failure_detail = None

    if league_id == NBA_LEAGUE_ID and getattr(config, "ENABLE_ESPN_BASKETBALL", True):
        espn_attempted = True
        try:
            espn_games, coverage_meta = espn_basketball_provider.fetch_espn_nba_season_games(league_id, season)
            if isinstance(coverage_meta, dict):
                espn_coverage = coverage_meta
                espn_completed_units = int(coverage_meta.get("months_succeeded") or 0)
            if espn_games:
                games_received.extend(espn_games)
                t_db_write = time.perf_counter()
                save_res = storage.save_historical_basketball_games(
                    espn_games, league_id, season, source="espn_url", require_completed=True
                )
                total_db_write_duration_b += time.perf_counter() - t_db_write
                db_write_calls_b += 1
                total_valid_games += save_res.get("valid", 0)
                total_newly_stored += save_res.get("inserted", 0)
                # Existing rows whose missing verified metadata was applied. They
                # are not new records, but they are real acquisition progress.
                total_enriched += save_res.get("enriched", 0)
                total_already_existing += save_res.get("already_existing", 0)
                total_conflict_only += save_res.get("conflict_only", 0)
                for conflict_field, conflict_count in (save_res.get("metadata_conflicts") or {}).items():
                    metadata_conflict_counts[conflict_field] = (
                        metadata_conflict_counts.get(conflict_field, 0) + conflict_count
                    )
                total_duplicates_skipped += save_res.get("duplicates_skipped", 0)
                espn_rejected_count = save_res.get("rejected_count", 0)
                total_rejected_count += espn_rejected_count
                for r_reason, r_cnt in save_res.get("rejection_reasons", {}).items():
                    all_rejection_reasons[r_reason] = all_rejection_reasons.get(r_reason, 0) + r_cnt
        except Exception as exc:
            espn_failure_detail = f"{type(exc).__name__}: {exc}"[:300]
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
                    last_error_stage = "pagination_metadata"
                    last_error_detail = f"expected_pages={page_expected!r}"[:300]
                    print(
                        f"Basketball pagination metadata is invalid on page {current_page} "
                        f"(expected_pages={page_expected!r}). Failing closed.",
                        flush=True,
                    )
                    break
                if not isinstance(page_games, list):
                    acquisition_failed = True
                    last_error_reason = "invalid_page_payload"
                    last_error_stage = "page_payload"
                    last_error_detail = f"games type={type(page_games).__name__}"[:300]
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
                    last_error_stage = "pagination_metadata"
                    last_error_detail = f"expected {api_expected_pages}, provider reported {page_expected}"[:300]
                    print(
                        f"Basketball pagination error: Total pages changed during sync ({api_expected_pages} -> {page_expected}). Failing closed.",
                        flush=True,
                    )
                    break

                if not page_games:
                    total_empty_pages_count += 1

                games_received.extend(page_games)

                t_db_write = time.perf_counter()
                save_result = storage.save_historical_basketball_games(
                    page_games, league_id, season, require_completed=True
                )
                total_db_write_duration_b += time.perf_counter() - t_db_write
                db_write_calls_b += 1
                total_valid_games += save_result.get("valid", 0)
                total_newly_stored += save_result.get("inserted", 0)
                total_enriched += save_result.get("enriched", 0)
                total_already_existing += save_result.get("already_existing", 0)
                total_conflict_only += save_result.get("conflict_only", 0)
                for conflict_field, conflict_count in (save_result.get("metadata_conflicts") or {}).items():
                    metadata_conflict_counts[conflict_field] = (
                        metadata_conflict_counts.get(conflict_field, 0) + conflict_count
                    )
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
                last_error_stage = "quota_reservation"
                last_error_detail = f"{type(exc).__name__}: {exc}"[:300]
                print(f"API Quota exhausted during basketball games fetch on page {current_page}: {exc}", flush=True)
                break
            except Exception as exc:
                acquisition_failed = True
                credential_error = (
                    not basketball_api.is_configured()
                    and isinstance(exc, ValueError)
                    and "not configured" in str(exc).lower()
                )
                if credential_error:
                    # The provider client validates credentials before any HTTP
                    # call, so this is a configuration outcome, not an empty or
                    # failed competition. It is reported as UNAVAILABLE.
                    last_error_reason = "api_basketball_unavailable_missing_credential"
                    last_error_stage = "credentials"
                else:
                    last_error_reason = "api_error_during_acquisition"
                    last_error_stage = "provider_request"
                last_error_detail = f"{type(exc).__name__}: {exc}"[:300]
                print(f"API acquisition error during basketball games fetch on page {current_page}: {exc}", flush=True)
                break

    requests_after = storage.get_api_request_count("api_basketball", today_str, request_type="historical")
    total_consumed = requests_after - initial_request_count

    valid_count = total_valid_games
    newly_stored = total_newly_stored
    duplicates_skipped = total_duplicates_skipped
    rejected_count = total_rejected_count
    rejection_reasons = all_rejection_reasons
    already_existing_skipped = total_already_existing

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
            "events_non_final": (espn_coverage or {}).get("events_non_final") if isinstance(espn_coverage, dict) else None,
            "rejection_reasons": (espn_coverage or {}).get("rejection_reasons") if isinstance(espn_coverage, dict) else None,
            "error": espn_failure_detail,
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
            "error": last_error_detail,
            "failure_stage": last_error_stage,
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

    # Completion evidence describes the coverage this acquisition is claiming, not
    # the lifetime history of every earlier attempt. A rejected record or an empty
    # page observed on a previous run proves nothing about the coverage being
    # claimed now, and carrying it forward made a dataset that has since been fully
    # re-acquired permanently unverifiable: the persisted-evidence check below read
    # the accumulated counter, failed, and the dataset could never be promoted even
    # though its records and current provider coverage were complete. The cumulative
    # counters are still recorded in the INCOMPLETE manifest as progress history.
    evidence_rejected_count = total_rejected_count
    # Empty coverage evidence must describe every unit the claimed route actually
    # used. API-Basketball pages have no legitimate-empty notion (a page inside
    # the provider's own page range returning nothing is an acquisition gap), so
    # those empty pages are carried into the completion evidence instead of being
    # dropped: previously an empty middle page was invisible to the guard and the
    # dataset could be promoted to COMPLETE with missing games.
    evidence_empty_units = total_empty_pages_count
    if espn_contributed_validated_coverage:
        evidence_empty_units += espn_valid_empty_units

    stored_games_all = storage.get_historical_basketball_games(league_id, season, include_storage_source=True)
    phase_counts_b = _basketball_season_type_counts(stored_games_all)
    regular_season_games_b = (
        phase_counts_b.get("regular_season", 0) + phase_counts_b.get("regular", 0)
    )
    unclassified_games_b = phase_counts_b.get("unclassified", 0)
    db_validation_passed = _validate_historical_basketball_dataset(
        stored_games_all,
        league_id,
        season,
        {
            "fixture_count": final_stored_count,
            "expected_pages": safe_exp_pages_b,
            "pages_completed": safe_pages_comp_b,
            "rejected_count": evidence_rejected_count,
            "empty_pages_count": evidence_empty_units,
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
        # Name the first fact that prevents validation instead of collapsing
        # every case into one generic string. Missing provider phase is exactly
        # the class of historical gap this acquisition path must be able to
        # explain and repair; it is never assumed to be the regular season.
        if unclassified_games_b > 0:
            coverage_reason = "season_type_metadata_missing"
        elif expected_min_season_games is None:
            coverage_reason = "expected_season_coverage_unavailable"
        elif regular_season_games_b < expected_min_season_games:
            coverage_reason = (
                f"regular_season_coverage_insufficient_"
                f"{regular_season_games_b}_of_{expected_min_season_games}"
            )
        else:
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
        "season_type_counts": phase_counts_b,
        "regular_season_games": regular_season_games_b,
        "unclassified_games": unclassified_games_b,
        "phase_diagnostic": (
            "season_type_metadata_missing" if unclassified_games_b > 0
            else None
        ),
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
            rejected_count=evidence_rejected_count,
            empty_pages_count=evidence_empty_units,
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
        # Rows that already existed and had verified provider metadata applied by
        # the idempotent enrichment upsert. Reported separately from new rows so a
        # re-acquisition's metadata progress is never mistaken for new coverage.
        "enriched": total_enriched,
        "already_existing": total_already_existing,
        "already_existing_skipped": already_existing_skipped,
        "conflict_only": total_conflict_only,
        "metadata_conflicts": metadata_conflict_counts,
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
        "api_basketball_available": basketball_api.is_configured(),
        "quota_budget_stopped": quota_budget_stopped,
        "error_reason": last_error_reason,
        "error_detail": last_error_detail,
        "failure_stage": last_error_stage,
        "final_stored_count": final_stored_count,
        "db_write_duration": round(total_db_write_duration_b, 3),
        "db_write_calls": db_write_calls_b,
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


def _competition_edition_selection(sport, league_id, requested_seasons):
    """
    Restrict one competition's requested seasons to the editions it stages.

    Domestic leagues and the continental club cups are staged every year, but the
    international tournaments are quadrennial and the UEFA Nations League is
    biennial. Applying a blanket annual season list to every configured
    competition would both attempt seasons that never existed and hide real
    coverage gaps behind fake ``NOT_APPLICABLE`` ones.

    Returns a dict with ``supported`` (seasons to acquire), ``not_applicable``
    (requested seasons the competition never staged) and ``unknown`` (editions
    whose lifecycle window is not documented, which are still acquired because a
    season exists and its completion simply fails closed). When the edition
    catalog is unavailable the requested seasons are returned unchanged, so a
    catalog problem can never silently drop acquisitions.
    """
    requested = [s for s in (requested_seasons or []) if isinstance(s, int) and not isinstance(s, bool)]
    try:
        import sports_knowledge

        selection = sports_knowledge.select_seasons(sport, league_id, requested)
    except Exception as exc:  # pragma: no cover - defensive: catalog is repository data
        print(
            f"Competition edition catalog unavailable for {sport} league {league_id} ({exc}); "
            f"using the requested seasons unchanged.",
            flush=True,
        )
        return {
            "supported": requested,
            "not_applicable": [],
            "unknown": [],
            "catalog_available": False,
        }

    return selection


def _write_historical_report(report, prefix):
    """Write a machine-readable report under data/historical_runs; return its path."""
    try:
        report_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "historical_runs"
        )
        os.makedirs(report_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(report_dir, f"{prefix}_{timestamp}.json")
        with open(path, "w", encoding="utf-8") as report_file:
            json.dump(report, report_file, indent=2, sort_keys=True, default=str)
        return path
    except OSError as exc:
        print(f"Report could not be written: {exc}", flush=True)
        return None


def _basketball_game_event_id(game):
    """Return the provider-native positive event id stored on a basketball game."""
    if not isinstance(game, dict):
        return None
    raw = game.get("id")
    if raw is None:
        raw = game.get("game_id")
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def build_basketball_phase_recovery_report(seasons=None, league_id=None, fetch_espn=True) -> dict:
    """
    Compare persisted basketball identities against fresh ESPN identities (read-only).

    The comparison is exact on the persisted ``(source, event_id)`` identity. A
    record is reported as enrichable only when the same ESPN event id is present in
    the fresh acquisition AND that event carries a provider-native phase. A record
    whose identity is absent from the fresh payload requires reacquisition; it is
    never reported as recoverable. No record is written here: enrichment is applied
    by the normal ESPN-first sync, which preserves ids, scores, dates and
    provenance.
    """
    import espn_basketball_provider

    storage.init_db()
    if league_id is None:
        allowed = getattr(config, "ALLOWED_BASKETBALL_LEAGUE_IDS", [])
        league_id = allowed[0] if allowed else 12
    target_league = int(league_id)
    target_seasons = list(seasons) if seasons else list(
        getattr(config, "TARGET_SEASONS", [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026])
    )

    season_reports = []
    totals = {
        key: 0
        for key in (
            "persisted_records", "persisted_identities", "duplicate_identities",
            "espn_records", "espn_classified", "espn_unclassified", "non_espn_records",
            "matched_identities", "enrichable", "matched_without_phase",
            "requires_reacquisition", "conflicting", "fresh_espn_events",
            "fresh_not_persisted", "reacquired", "rejected_by_provider",
            "non_espn_phase_missing", "espn_phase_missing", "exact_identity_matches",
        )
    }

    for season in target_seasons:
        ssn = int(season)
        stored = storage.get_historical_basketball_games(
            target_league, ssn, include_storage_source=True
        )
        stored_list = stored if isinstance(stored, list) else []
        persisted = {}
        duplicate_identities = 0
        for game in stored_list:
            key = (_basketball_game_source(game), _basketball_game_event_id(game))
            if key in persisted:
                duplicate_identities += 1
                continue
            persisted[key] = game

        fresh_games, coverage = [], {}
        espn_attempted = False
        if fetch_espn and target_league == NBA_LEAGUE_ID and getattr(config, "ENABLE_ESPN_BASKETBALL", True):
            espn_attempted = True
            try:
                fresh_games, coverage = espn_basketball_provider.fetch_espn_nba_season_games(
                    target_league, ssn
                )
            except Exception as exc:  # noqa: BLE001 - report must not fail on a provider error
                fresh_games = []
                coverage = {"error": f"{type(exc).__name__}: {exc}"[:300]}

        fresh_by_id = {}
        for game in fresh_games if isinstance(fresh_games, list) else []:
            event_id = _basketball_game_event_id(game)
            if event_id is not None:
                fresh_by_id[event_id] = game

        stats = {
            "persisted_records": len(stored_list),
            "persisted_identities": len(persisted),
            "duplicate_identities": duplicate_identities,
            "espn_records": 0,
            "espn_classified": 0,
            "espn_unclassified": 0,
            "non_espn_records": 0,
            "matched_identities": 0,
            "enrichable": 0,
            "matched_without_phase": 0,
            "requires_reacquisition": 0,
            "conflicting": 0,
            "fresh_espn_events": len(fresh_by_id),
            "fresh_not_persisted": 0,
            "reacquired": 0,
            "rejected_by_provider": int((coverage or {}).get("events_rejected") or 0),
            "non_espn_phase_missing": 0,
            "espn_phase_missing": 0,
        }
        unmatched_examples = []
        conflict_examples = []
        persisted_espn_ids = set()
        source_counts = {}
        phase_counts = {}
        phase_missing_by_source = {}

        for (source, event_id), game in persisted.items():
            source_key = source if isinstance(source, str) and source.strip() else "unknown_source"
            phase = _basketball_game_season_type(game)
            source_counts[source_key] = source_counts.get(source_key, 0) + 1
            phase_key = phase or "unclassified"
            phase_counts[phase_key] = phase_counts.get(phase_key, 0) + 1
            if phase is None:
                phase_missing_by_source[source_key] = phase_missing_by_source.get(source_key, 0) + 1

            if source != "espn_url":
                stats["non_espn_records"] += 1
                if phase is None:
                    # An API-Basketball id is not an ESPN event identity. It must
                    # be refreshed from its own provider (or re-acquired); it is
                    # never counted as recoverable from the ESPN comparison.
                    stats["non_espn_phase_missing"] += 1
                continue
            stats["espn_records"] += 1
            persisted_espn_ids.add(event_id)
            fresh_game = fresh_by_id.get(event_id)
            if phase:
                stats["espn_classified"] += 1
                if fresh_game is not None:
                    stats["exact_identity_matches"] = stats.get("exact_identity_matches", 0) + 1
                    fresh_phase = _basketball_game_season_type(fresh_game)
                    if fresh_phase and fresh_phase != phase:
                        stats["conflicting"] += 1
                        if len(conflict_examples) < 20:
                            conflict_examples.append({
                                "event_id": event_id,
                                "persisted_phase": phase,
                                "fresh_phase": fresh_phase,
                            })
                continue
            stats["espn_unclassified"] += 1
            stats["espn_phase_missing"] += 1
            if fresh_game is None:
                stats["requires_reacquisition"] += 1
                if len(unmatched_examples) < 20:
                    unmatched_examples.append(event_id)
                continue
            stats["exact_identity_matches"] = stats.get("exact_identity_matches", 0) + 1
            stats["matched_identities"] += 1
            if _basketball_game_season_type(fresh_game):
                stats["enrichable"] += 1
            else:
                stats["matched_without_phase"] += 1

        if stats["enrichable"]:
            stats["recommended_recovery_action"] = "run_exact_espn_phase_enrichment"
        elif stats["requires_reacquisition"]:
            stats["recommended_recovery_action"] = "reacquire_unmatched_espn_identities"
        elif stats["non_espn_phase_missing"]:
            stats["recommended_recovery_action"] = "refresh_from_each_records_own_provider"
        elif stats["espn_unclassified"]:
            stats["recommended_recovery_action"] = "provider_phase_evidence_unavailable"
        else:
            stats["recommended_recovery_action"] = "none"

        stats["fresh_not_persisted"] = len(set(fresh_by_id) - persisted_espn_ids)
        stats["source_breakdown"] = dict(sorted(source_counts.items()))
        stats["phase_counts"] = dict(sorted(phase_counts.items()))
        stats["phase_missing_by_source"] = dict(sorted(phase_missing_by_source.items()))
        stats["exact_identity_matches"] = int(stats.get("exact_identity_matches") or 0)
        stats["unmatched_examples"] = unmatched_examples
        stats["conflict_examples"] = conflict_examples
        stats["espn_attempted"] = espn_attempted
        stats["espn_coverage"] = {
            "months_attempted": (coverage or {}).get("months_attempted"),
            "months_succeeded": (coverage or {}).get("months_succeeded"),
            "months_failed": (coverage or {}).get("months_failed"),
            "competition_verified": (coverage or {}).get("competition_verified"),
            "error": (coverage or {}).get("error"),
        }
        season_reports.append({"league_id": target_league, "season": ssn, **stats})
        for key in totals:
            totals[key] += int(stats.get(key) or 0)

    report = {
        "report_type": "basketball_phase_recovery",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "league_id": target_league,
        "mutated": False,
        "method": (
            "Exact persisted (source, event_id) comparison against a fresh ESPN season "
            "acquisition. Phase evidence is the provider-native season_type/season_type_id; "
            "it is never inferred from dates."
        ),
        "seasons": season_reports,
        "totals": totals,
        "limitations": (
            "Read-only. The normal ESPN-first sync applies enrichable metadata. "
            "Records whose identity is absent from the fresh ESPN payload require "
            "reacquisition from their own source and are not counted as recoverable."
        ),
    }
    report["report_path"] = _write_historical_report(report, "nba_phase_recovery")

    print("\n=== NBA PHASE RECOVERY REPORT ===", flush=True)
    for season_report in season_reports:
        print(
            f"  {season_report['league_id']}/{season_report['season']}: "
            f"persisted={season_report['persisted_records']} "
            f"espn={season_report['espn_records']} "
            f"classified={season_report['espn_classified']} "
            f"enrichable={season_report['enrichable']} "
            f"matched_without_phase={season_report['matched_without_phase']} "
            f"requires_reacquisition={season_report['requires_reacquisition']} "
            f"conflicting={season_report['conflicting']}",
            flush=True,
        )
    print(f"  totals: {totals}", flush=True)
    if report.get("report_path"):
        print(f"  report: {report['report_path']}", flush=True)
    return report


def _is_legacy_duplicate_candidate(fixture):
    """True when a persisted football row is an annotated reconciliation candidate."""
    if not isinstance(fixture, dict):
        return False
    metadata = fixture.get("reconciliation_metadata")
    if not isinstance(metadata, dict):
        return False
    value = metadata.get("legacy_duplicate_candidate")
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _football_logical_identity(fixture):
    """Canonical logical identity for duplicate-group reporting, or None."""
    if not isinstance(fixture, dict):
        return None
    home = fixture.get("canonical_home_id")
    away = fixture.get("canonical_away_id")
    fixture_obj = fixture.get("fixture") if isinstance(fixture.get("fixture"), dict) else {}
    kickoff_date = storage._football_fixture_kickoff_date(fixture_obj.get("date"))
    if not home or not away or not kickoff_date:
        return None
    return (str(home), str(away), kickoff_date)


def build_football_manifest_reconciliation(league_ids=None, seasons=None) -> dict:
    """
    Reconcile every persisted football manifest against its actual stored records.

    Reports manifest status/count, stored and valid distinct counts, source
    breakdown, duplicates, expected versus observed canonical teams, date range,
    provider diagnostics and the exact remaining blocker. Read-only: no manifest,
    record or status is changed.
    """
    from collections import Counter

    import sports_knowledge

    storage.init_db()
    manifests = storage.list_historical_datasets("football")
    league_filter = {int(value) for value in league_ids} if league_ids else None
    season_filter = {int(value) for value in seasons} if seasons else None

    results = []
    for manifest in manifests:
        lid = int(manifest["league_id"])
        ssn = int(manifest["season"])
        if league_filter and lid not in league_filter:
            continue
        if season_filter and ssn not in season_filter:
            continue

        fixtures = storage.get_historical_fixtures(
            lid, ssn, include_reconciliation_candidates=True
        )
        physical_fixtures = [
            fixture for fixture in fixtures if isinstance(fixture, dict)
        ] if isinstance(fixtures, list) else []
        fixture_list = [
            fixture for fixture in physical_fixtures
            if not _is_legacy_duplicate_candidate(fixture)
        ]
        physical_stored_count = len(physical_fixtures)
        annotated_candidate_count = physical_stored_count - len(fixture_list)
        source_counts = Counter()
        eligible_source_counts = Counter()
        invalid_reasons = Counter()
        seen = set()
        valid_seen = set()
        valid_count = 0
        physical_valid_count = 0
        eligible_invalid_count = 0
        duplicate_count = 0
        logical_duplicates = 0
        fixture_sources = {}
        observed_teams = set()
        dates = []
        logical_identity_counts = Counter()
        ambiguous_identity_count = 0

        for fixture in physical_fixtures:
            fixture_obj = fixture.get("fixture") if isinstance(fixture.get("fixture"), dict) else {}
            fid_raw = fixture_obj.get("id")
            source = (
                fixture.get("source")
                or (fixture.get("provider_provenance") or {}).get("provider")
                or "unknown"
            )
            is_candidate = _is_legacy_duplicate_candidate(fixture)
            source_counts[source] += 1
            if not is_candidate:
                eligible_source_counts[source] += 1
                identity = _football_logical_identity(fixture)
                if identity is None:
                    ambiguous_identity_count += 1
                else:
                    logical_identity_counts[identity] += 1
            key = (source, fid_raw)
            if key in seen:
                duplicate_count += 1
            seen.add(key)
            fixture_sources.setdefault(fid_raw, set()).add(source)

            kickoff = fixture_obj.get("date")
            if isinstance(kickoff, str) and kickoff:
                dates.append(kickoff[:10])

            if not is_candidate:
                teams = fixture.get("teams") if isinstance(fixture.get("teams"), dict) else {}
                for side in ("home", "away"):
                    team = teams.get(side) if isinstance(teams.get(side), dict) else {}
                    team_id = fixture.get(f"canonical_{side}_id") or team.get("id")
                    if team_id is not None:
                        observed_teams.add(str(team_id))

            try:
                is_valid, reason, _ = storage.validate_historical_fixture(
                    fixture,
                    target_league_id=lid,
                    target_season=ssn,
                    require_completed=True,
                )
            except Exception as exc:  # noqa: BLE001 - report the failure, do not abort the audit
                is_valid, reason = False, f"validation_error:{type(exc).__name__}"
            if not is_valid:
                invalid_reasons[str(reason or "invalid")] += 1
                if not is_candidate:
                    eligible_invalid_count += 1
            else:
                physical_valid_count += 1
                if not is_candidate:
                    valid_count += 1
                    valid_seen.add(key)

        logical_duplicates = sum(
            1 for sources in fixture_sources.values() if len(sources) > 1
        )
        unmarked_duplicate_groups = sum(
            1 for count in logical_identity_counts.values() if count > 1
        )
        unmarked_duplicate_rows = sum(
            count - 1 for count in logical_identity_counts.values() if count > 1
        )

        expected = manifest.get("expected_team_ids")
        expected_source = "manifest"
        if not expected:
            expected_source = "authoritative_lookup"
            try:
                expected = data_resolver._get_authoritative_historical_team_ids(
                    lid, ssn, allow_network=False
                )
            except Exception:  # noqa: BLE001 - absence is reported, not fatal
                expected = []
        if not expected:
            expected_source = "unavailable"
        expected_teams = {str(team_id) for team_id in expected} if expected else set()
        missing_teams = sorted(expected_teams - observed_teams)
        unexpected_teams = sorted(observed_teams - expected_teams) if expected_teams else []

        coverage = data_resolver._validate_historical_structural_coverage(
            fixture_list,
            season=ssn,
            provider_metadata={"allow_network": False},
            league_id=lid,
        )
        lifecycle_state, lifecycle_reason, _ = get_competition_lifecycle(
            "football", lid, ssn, fixture_list, coverage=coverage
        )

        provider_coverage = (
            manifest.get("provider_coverage")
            if isinstance(manifest.get("provider_coverage"), dict)
            else {}
        )
        diagnostics = provider_coverage.get("provider_diagnostics")
        diagnostics_present = isinstance(diagnostics, dict) and any(
            isinstance(value, dict) for value in diagnostics.values()
        )

        try:
            units = sports_knowledge.expected_coverage_units("football", lid, ssn)
        except Exception:  # noqa: BLE001 - an unknown expectation stays unknown
            units = {}
        expected_fixture_total = units.get("value") if isinstance(units, dict) else None

        manifest_status = manifest.get("status") or "INCOMPLETE"
        manifest_fixture_count = manifest.get("fixture_count")
        manifest_count_matches = (
            isinstance(manifest_fixture_count, int)
            and not isinstance(manifest_fixture_count, bool)
            and manifest_fixture_count == physical_stored_count
        )
        invalid_count = sum(invalid_reasons.values())

        # A COMPLETE label is a claim, not evidence. The audit must reconcile the
        # claim against the database on every pass: a stale or manually altered
        # manifest can otherwise hide missing, duplicate or invalid records behind
        # the word COMPLETE. The same checks are applied to INCOMPLETE manifests so
        # the report always names the first fact that blocks promotion.
        if not physical_fixtures:
            blocker = manifest.get("error_reason") or "no_persisted_records"
        elif not fixture_list:
            blocker = "no_eligible_records_after_duplicate_exclusion"
        elif not manifest_count_matches:
            blocker = (
                f"manifest_count_mismatch:{manifest_fixture_count!r}_of_{len(fixture_list)}"
            )
        elif eligible_invalid_count > 0:
            first_invalid = next(iter(invalid_reasons))
            blocker = f"invalid_persisted_records:{first_invalid}"
        elif not expected_teams:
            blocker = "expected_team_universe_unavailable"
        elif coverage.get("verified") is not True:
            blocker = (
                f"structural_coverage_failed:{coverage.get('reason')}"
                if coverage.get("reason") else "structural_coverage_failed"
            )
        elif lifecycle_state != COMPETITION_LIFECYCLE_COMPLETED:
            blocker = f"lifecycle_{lifecycle_state.lower()}_{lifecycle_reason}"
        elif manifest_status != "COMPLETE":
            blocker = manifest.get("error_reason") or "manifest_not_complete_despite_valid_coverage"
        else:
            blocker = None

        manifest_status_consistent = (manifest_status == "COMPLETE" and blocker is None)
        stale_error_reason = (
            manifest_status == "COMPLETE"
            and blocker is None
            and bool(manifest.get("error_reason"))
        )

        if blocker is None:
            recovery_action = "none"
        elif str(blocker).startswith("manifest_count_mismatch"):
            recovery_action = "reconcile_manifest_count_with_persisted_records"
        elif str(blocker).startswith("invalid_persisted_records"):
            recovery_action = "repair_or_reacquire_invalid_records"
        elif blocker == "expected_team_universe_unavailable":
            recovery_action = "resolve_authoritative_expected_team_universe"
        elif blocker == "no_persisted_records":
            recovery_action = "acquire_dataset"
        elif str(blocker).startswith("structural_coverage_failed"):
            recovery_action = "acquire_missing_coverage_or_fix_provider_mapping"
        elif str(blocker).startswith("lifecycle_"):
            recovery_action = "wait_for_season_completion_or_verify_lifecycle"
        else:
            recovery_action = "inspect_manifest_and_provider_diagnostics"

        results.append({
            "league_id": lid,
            "season": ssn,
            "manifest_status": manifest_status,
            "manifest_fixture_count": manifest.get("fixture_count"),
            "stored_count": physical_stored_count,
            "physical_stored_count": physical_stored_count,
            "eligible_record_count": len(fixture_list),
            "eligible_logical_count": len(logical_identity_counts),
            "annotated_duplicate_candidates": annotated_candidate_count,
            "unmarked_duplicate_groups": unmarked_duplicate_groups,
            "unmarked_duplicate_rows": unmarked_duplicate_rows,
            "ambiguous_identity_count": ambiguous_identity_count,
            "physical_valid_count": physical_valid_count,
            "eligible_invalid_count": eligible_invalid_count,
            "manifest_count_matches": manifest_count_matches,
            "manifest_count_delta": (
                physical_stored_count - manifest_fixture_count
                if isinstance(manifest_fixture_count, int)
                and not isinstance(manifest_fixture_count, bool)
                else None
            ),
            "manifest_status_consistent": manifest_status_consistent,
            "stale_error_reason": stale_error_reason,
            "valid_count": valid_count,
            "valid_distinct_count": len(valid_seen),
            "duplicate_count": duplicate_count,
            "logical_duplicate_fixture_ids": logical_duplicates,
            "source_breakdown": dict(source_counts),
            "eligible_source_breakdown": dict(eligible_source_counts),
            "invalid_count": invalid_count,
            "invalid_reasons": dict(invalid_reasons),
            "expected_team_source": expected_source,
            "expected_team_count": len(expected_teams),
            "observed_team_count": len(observed_teams),
            "missing_team_count": len(missing_teams),
            "missing_teams": missing_teams,
            "unexpected_teams": unexpected_teams,
            "date_range": {"start": min(dates), "end": max(dates)} if dates else None,
            "provider_outcomes": provider_coverage.get("provider_outcomes"),
            "provider_diagnostics": diagnostics,
            "provider_diagnostics_present": diagnostics_present,
            "expected_fixture_total": expected_fixture_total,
            "coverage": coverage,
            "coverage_evidence_gaps": coverage.get("evidence_gaps") if isinstance(coverage, dict) else None,
            "lifecycle_state": lifecycle_state,
            "lifecycle_reason": lifecycle_reason,
            "exact_blocker": blocker,
            "recommended_recovery_action": recovery_action,
            "error_reason": manifest.get("error_reason"),
            "updated_at": manifest.get("updated_at"),
        })

    summary = {
        "report_type": "football_manifest_reconciliation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mutated": False,
        "manifest_count": len(results),
        "status_counts": dict(Counter(item["manifest_status"] for item in results)),
        "inconsistent_complete_manifest_count": sum(
            1 for item in results
            if item["manifest_status"] == "COMPLETE" and not item["manifest_status_consistent"]
        ),
        "stale_error_reason_count": sum(1 for item in results if item["stale_error_reason"]),
        "annotated_duplicate_candidate_count": sum(
            item["annotated_duplicate_candidates"] for item in results
        ),
        "unmarked_duplicate_group_count": sum(
            item["unmarked_duplicate_groups"] for item in results
        ),
        "blocker_counts": dict(Counter(
            item["exact_blocker"] or "none" for item in results
        )),
        "targets": results,
    }
    summary["report_path"] = _write_historical_report(
        summary, "football_manifest_reconciliation"
    )

    print("\n=== FOOTBALL MANIFEST RECONCILIATION ===", flush=True)
    for item in results:
        print(
            f"  [{item['manifest_status']}] {item['league_id']}/{item['season']}: "
            f"physical={item['physical_stored_count']} eligible={item['eligible_record_count']} "
            f"candidates={item['annotated_duplicate_candidates']} "
            f"unmarked_dup_groups={item['unmarked_duplicate_groups']} "
            f"ambiguous={item['ambiguous_identity_count']} "
            f"valid_distinct={item['valid_distinct_count']} duplicates={item['duplicate_count']} "
            f"expected_teams={item['expected_team_count']} "
            f"missing_teams={item['missing_team_count']} "
            f"diagnostics={'yes' if item['provider_diagnostics_present'] else 'no'} "
            f"blocker={item['exact_blocker']}",
            flush=True,
        )
    print(f"  manifests={len(results)} statuses={summary['status_counts']}", flush=True)
    if summary.get("report_path"):
        print(f"  report: {summary['report_path']}", flush=True)
    return summary


def build_basketball_manifest_reconciliation(league_ids=None, seasons=None) -> dict:
    """
    Reconcile every persisted basketball manifest against its stored games.

    This is the basketball counterpart to the football manifest audit. It is
    read-only and reports, per NBA season: the manifest's claim versus the actual
    row count, source and phase breakdown, duplicate or invalid identities,
    provider coverage units and status, independently established regular-season
    expectation, lifecycle state and the first exact reason completion cannot be
    claimed. Missing phase metadata is reported as ``season_type_metadata_missing``
    and is never converted into a regular-season count.
    """
    from collections import Counter

    storage.init_db()
    manifests = storage.list_historical_datasets("basketball")
    league_filter = {int(value) for value in league_ids} if league_ids else None
    season_filter = {int(value) for value in seasons} if seasons else None
    results = []

    for manifest in manifests:
        lid = int(manifest["league_id"])
        ssn = int(manifest["season"])
        if league_filter and lid not in league_filter:
            continue
        if season_filter and ssn not in season_filter:
            continue

        games = storage.get_historical_basketball_games(
            lid, ssn, include_storage_source=True
        )
        game_list = games if isinstance(games, list) else []
        source_counts = {}
        phase_counts = _basketball_season_type_counts(game_list)
        phase_missing_by_source = {}
        invalid_reasons = Counter()
        seen = set()
        valid_count = 0
        duplicate_count = 0
        dates = []

        for game in game_list:
            source = _basketball_game_source(game) or "unknown_source"
            source_counts[source] = source_counts.get(source, 0) + 1
            event_id = _basketball_game_event_id(game)
            if event_id is None:
                invalid_reasons["missing_or_invalid_event_id"] += 1
                event_key = None
            else:
                event_key = (source, event_id)
                if event_key in seen:
                    duplicate_count += 1
                seen.add(event_key)

            phase = _basketball_game_season_type(game)
            if phase is None:
                phase_missing_by_source[source] = (
                    phase_missing_by_source.get(source, 0) + 1
                )

            raw_date = game.get("date") if isinstance(game, dict) else None
            if isinstance(raw_date, str) and raw_date:
                dates.append(raw_date[:10])

            try:
                is_valid, reason, _ = storage.validate_historical_basketball_game(
                    game,
                    target_league_id=lid,
                    target_season=ssn,
                    require_completed=True,
                )
            except Exception as exc:  # noqa: BLE001 - report, never abort the audit
                is_valid, reason = False, f"validation_error:{type(exc).__name__}"
            if not is_valid:
                invalid_reasons[str(reason or "invalid")] += 1
            else:
                valid_count += 1

        manifest_status = manifest.get("status") or "INCOMPLETE"
        manifest_count = manifest.get("fixture_count")
        count_matches = (
            isinstance(manifest_count, int)
            and not isinstance(manifest_count, bool)
            and manifest_count == len(game_list)
        )
        expected_min = _expected_min_season_games(lid, ssn)
        regular_count = (
            phase_counts.get("regular_season", 0) + phase_counts.get("regular", 0)
        )
        unclassified_count = phase_counts.get("unclassified", 0)
        provider_coverage = (
            manifest.get("provider_coverage")
            if isinstance(manifest.get("provider_coverage"), dict)
            else {}
        )
        provider_statuses = {
            provider: entry.get("status")
            for provider, entry in provider_coverage.items()
            if isinstance(entry, dict)
        }
        expected_units = manifest.get("expected_pages") or 0
        completed_units = manifest.get("pages_completed") or 0
        dataset_valid = _validate_historical_basketball_dataset(
            game_list, lid, ssn, manifest
        )
        persisted_revalidation = _basketball_persisted_revalidation(
            lid, ssn, manifest, promote=False
        )
        units_complete = expected_units > 0 and completed_units == expected_units
        season_sufficient = (
            expected_min is not None and regular_count >= expected_min
        )
        coverage_verified = bool(
            dataset_valid and units_complete and season_sufficient
            and any(status == ACQUISITION_STATE_VALIDATED for status in provider_statuses.values())
        )
        coverage_reason = None
        if unclassified_count > 0:
            coverage_reason = "season_type_metadata_missing"
        elif not units_complete:
            coverage_reason = "provider_coverage_units_incomplete"
        elif expected_min is None:
            coverage_reason = "expected_season_coverage_unavailable"
        elif regular_count < expected_min:
            coverage_reason = "regular_season_coverage_insufficient"
        elif not dataset_valid:
            coverage_reason = "persisted_dataset_invalid"
        lifecycle_coverage = {
            "verified": coverage_verified,
            "reason": coverage_reason,
            "coverage_unit": manifest.get("coverage_unit"),
            "expected_coverage_units": expected_units,
            "completed_coverage_units": completed_units,
            "provider_coverage": provider_coverage,
        }
        lifecycle_state, lifecycle_reason, _ = get_competition_lifecycle(
            "basketball", lid, ssn, game_list, coverage=lifecycle_coverage
        )

        # First fact that prevents a COMPLETE claim. Missing provider phase is
        # reported before the lifecycle it invalidates, otherwise "coverage
        # unverified" would hide the actual repairable gap. An unfinished season
        # is still the blocker when no phase/data defect exists.
        if not game_list:
            blocker = manifest.get("error_reason") or "no_persisted_records"
        elif not count_matches:
            blocker = f"manifest_count_mismatch:{manifest_count!r}_of_{len(game_list)}"
        elif sum(invalid_reasons.values()) > 0:
            blocker = f"invalid_persisted_records:{next(iter(invalid_reasons))}"
        elif duplicate_count > 0:
            blocker = "duplicate_persisted_game_identities"
        elif unclassified_count > 0:
            blocker = "season_type_metadata_missing"
        elif expected_min is None and lifecycle_state != COMPETITION_LIFECYCLE_COMPLETED:
            blocker = f"lifecycle_{lifecycle_state.lower()}_{lifecycle_reason}"
        elif expected_min is None:
            blocker = "expected_season_coverage_unavailable"
        elif regular_count < expected_min:
            blocker = (
                f"regular_season_coverage_insufficient:"
                f"{regular_count}_of_{expected_min}"
            )
        elif not units_complete:
            blocker = (
                f"provider_coverage_units_incomplete:"
                f"{completed_units}_of_{expected_units}"
            )
        elif not any(status == ACQUISITION_STATE_VALIDATED for status in provider_statuses.values()):
            blocker = "no_validated_provider_coverage"
        elif not dataset_valid:
            blocker = "persisted_dataset_invalid"
        elif lifecycle_state != COMPETITION_LIFECYCLE_COMPLETED:
            blocker = f"lifecycle_{lifecycle_state.lower()}_{lifecycle_reason}"
        elif manifest_status != "COMPLETE":
            blocker = manifest.get("error_reason") or "manifest_not_complete_despite_valid_game_data"
        else:
            blocker = None

        if (
            manifest_status != "COMPLETE"
            and persisted_revalidation.get("persisted_revalidation_eligible")
        ):
            blocker = "manifest_not_complete_despite_valid_persisted_evidence"
        if blocker is None and not persisted_revalidation.get(
            "persisted_revalidation_eligible"
        ):
            blocker = (
                persisted_revalidation.get("exact_blocker")
                or "persisted_revalidation_failed"
            )

        manifest_status_consistent = (manifest_status == "COMPLETE" and blocker is None)
        stale_error_reason = (
            manifest_status == "COMPLETE"
            and blocker is None
            and bool(manifest.get("error_reason"))
        )

        if blocker is None:
            action = "none"
        elif blocker == "season_type_metadata_missing":
            espn_candidates = phase_missing_by_source.get("espn_url", 0)
            action = (
                "run_exact_espn_phase_enrichment"
                if espn_candidates
                else "refresh_missing_phases_from_each_source"
            )
        elif str(blocker).startswith("regular_season_coverage_insufficient"):
            action = "reacquire_missing_regular_season_games"
        elif (
            str(blocker).startswith("provider_month_keys_invalid")
            or str(blocker).startswith("provider_month_evidence_incomplete")
        ):
            action = "refresh_espn_monthly_coverage"
        elif (
            str(blocker).startswith("provider_rejection_evidence_inconsistent")
            or str(blocker).startswith("unresolved_provider_event_evidence")
        ):
            action = "refresh_espn_rejected_events"
        elif str(blocker).startswith("provider_coverage_units_incomplete"):
            action = "complete_provider_coverage_units"
        elif blocker == "no_validated_provider_coverage":
            action = "repair_provider_coverage_evidence"
        elif str(blocker).startswith("manifest_count_mismatch"):
            action = "reconcile_manifest_count_with_persisted_records"
        elif str(blocker).startswith("invalid_persisted_records"):
            action = "repair_or_reacquire_invalid_records"
        elif blocker == "duplicate_persisted_game_identities":
            action = "reconcile_duplicate_identities"
        elif str(blocker).startswith("lifecycle_"):
            action = "wait_for_season_completion_or_verify_lifecycle"
        elif blocker == "expected_season_coverage_unavailable":
            action = "wait_for_documented_season_expectation"
        elif blocker == "persisted_dataset_invalid":
            action = "inspect_persisted_dataset_validation"
        else:
            action = "inspect_manifest_and_provider_evidence"

        results.append({
            "league_id": lid,
            "season": ssn,
            "manifest_status": manifest_status,
            "manifest_fixture_count": manifest_count,
            "stored_count": len(game_list),
            "manifest_count_matches": count_matches,
            "manifest_count_delta": (
                len(game_list) - manifest_count
                if isinstance(manifest_count, int)
                and not isinstance(manifest_count, bool)
                else None
            ),
            "manifest_status_consistent": manifest_status_consistent,
            "stale_error_reason": stale_error_reason,
            "valid_count": valid_count,
            "invalid_count": sum(invalid_reasons.values()),
            "invalid_reasons": dict(invalid_reasons),
            "duplicate_count": duplicate_count,
            "source_breakdown": dict(sorted(source_counts.items())),
            "phase_counts": dict(sorted(phase_counts.items())),
            "phase_missing_by_source": dict(sorted(phase_missing_by_source.items())),
            "regular_season_games": regular_count,
            "unclassified_games": unclassified_count,
            "expected_min_regular_season_games": expected_min,
            "persisted_revalidation_eligible": persisted_revalidation.get(
                "persisted_revalidation_eligible"
            ),
            "persisted_revalidation_blocker": persisted_revalidation.get(
                "exact_blocker"
            ),
            "another_provider_request_necessary": persisted_revalidation.get(
                "another_provider_request_necessary"
            ),
            "persisted_revalidation_evidence": persisted_revalidation.get(
                "validation_evidence"
            ),
            "provider_expected_units": persisted_revalidation.get(
                "provider_expected_units"
            ),
            "provider_completed_units": persisted_revalidation.get(
                "provider_completed_units"
            ),
            "provider_specific_evidence": persisted_revalidation.get(
                "provider_evidence"
            ),
            "ignored_provider_coverage": persisted_revalidation.get(
                "ignored_providers"
            ),
            "espn_month_evidence": persisted_revalidation.get(
                "espn_month_evidence"
            ),
            "expected_espn_month_keys": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("expected_month_keys"),
            "actual_espn_month_keys": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("actual_month_keys"),
            "missing_espn_month_keys": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("missing_month_keys"),
            "unexpected_espn_month_keys": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("unexpected_month_keys"),
            "duplicate_espn_month_keys": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("duplicate_month_keys"),
            "monthly_rejected_event_totals": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("monthly_rejected_totals"),
            "rejection_reasons_by_month": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("rejection_reasons_by_month"),
            "espn_events_rejected_total": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("events_rejected"),
            "espn_events_non_final_total": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("events_non_final"),
            "historical_non_final_rejections_unresolved": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("historical_non_final_rejections_unresolved"),
            "non_final_events_unresolved": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("non_final_events_unresolved"),
            "other_rejected_events_total": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("other_rejected_events"),
            "rejection_evidence_consistent": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("rejection_evidence_consistent"),
            "espn_month_keys_exact": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("month_keys_exact"),
            "espn_month_units_complete": (
                persisted_revalidation.get("espn_month_evidence") or {}
            ).get("month_units_complete"),
            "coverage_unit": manifest.get("coverage_unit"),
            "expected_coverage_units": expected_units,
            "completed_coverage_units": completed_units,
            "provider_statuses": provider_statuses,
            "provider_coverage": provider_coverage,
            "coverage_verified": coverage_verified,
            "coverage_reason": coverage_reason,
            "lifecycle_state": lifecycle_state,
            "lifecycle_reason": lifecycle_reason,
            "dataset_valid": dataset_valid,
            "date_range": {"start": min(dates), "end": max(dates)} if dates else None,
            "exact_blocker": blocker,
            "recommended_recovery_action": action,
            "error_reason": manifest.get("error_reason"),
            "updated_at": manifest.get("updated_at"),
        })

    summary = {
        "report_type": "basketball_manifest_reconciliation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mutated": False,
        "manifest_count": len(results),
        "status_counts": dict(Counter(item["manifest_status"] for item in results)),
        "inconsistent_complete_manifest_count": sum(
            1 for item in results
            if item["manifest_status"] == "COMPLETE" and not item["manifest_status_consistent"]
        ),
        "stale_error_reason_count": sum(1 for item in results if item["stale_error_reason"]),
        "phase_missing_source_counts": dict(Counter(
            source
            for item in results
            for source, count in item["phase_missing_by_source"].items()
            for _ in range(count)
        )),
        "phase_missing_count": sum(item["unclassified_games"] for item in results),
        "blocker_counts": dict(Counter(
            item["exact_blocker"] or "none" for item in results
        )),
        "targets": results,
    }
    summary["report_path"] = _write_historical_report(
        summary, "basketball_manifest_reconciliation"
    )

    print("\n=== BASKETBALL MANIFEST RECONCILIATION ===", flush=True)
    for item in results:
        print(
            f"  [{item['manifest_status']}] {item['league_id']}/{item['season']}: "
            f"stored={item['stored_count']} valid={item['valid_count']} "
            f"duplicates={item['duplicate_count']} "
            f"phases={item['phase_counts']} "
            f"regular={item['regular_season_games']}/{item['expected_min_regular_season_games']} "
            f"coverage={item['completed_coverage_units']}/{item['expected_coverage_units']} "
            f"rejected={item['espn_events_rejected_total']} "
            f"non_final={item['espn_events_non_final_total']} "
            f"blocker={item['exact_blocker']}",
            flush=True,
        )
    print(
        f"  manifests={len(results)} statuses={summary['status_counts']} "
        f"phase_missing={summary['phase_missing_count']}",
        flush=True,
    )
    if summary.get("report_path"):
        print(f"  report: {summary['report_path']}", flush=True)
    return summary


def _write_queue_checkpoint(acquisition_run_id, payload):
    """Persist a resumable checkpoint after every completed target (best effort)."""
    try:
        report_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "historical_runs"
        )
        os.makedirs(report_dir, exist_ok=True)
        path = os.path.join(report_dir, f"{acquisition_run_id}.checkpoint.json")
        with open(path, "w", encoding="utf-8") as checkpoint_file:
            json.dump(payload, checkpoint_file, indent=2, sort_keys=True, default=str)
        return path
    except OSError as exc:
        print(f"Queue checkpoint could not be written: {exc}", flush=True)
        return None


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
    # A unique acquisition run identifier. GitHub Actions run numbers, provider
    # request ids, season labels and acquisition runs are separate namespaces, so
    # every future run is traceable by this id in its own logs/report without
    # claiming an identity for a historical run whose id was never recorded.
    acquisition_run_id = (
        f"acq-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    )

    # SIGINT/SIGTERM are deferred until the current target's transaction has
    # committed. No new target starts after the signal, and the run remains an
    # interrupted (nonzero) outcome rather than being reported as complete.
    interrupted = {"requested": False, "signal": None, "current_target": None}
    in_progress_target = None
    last_completed_target = None

    def _queue_signal_handler(signum, _frame):
        interrupted["requested"] = True
        interrupted["signal"] = signal.Signals(signum).name
        interrupted["current_target"] = in_progress_target
        print(
            f"Queue: {interrupted['signal']} received; finishing the current target "
            f"transaction and starting no new work.",
            flush=True,
        )

    previous_signal_handlers = {}
    for _signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_signal_handlers[_signal_name] = signal.signal(
                _signal_name, _queue_signal_handler
            )
        except (ValueError, OSError):
            # Signal handlers can only be installed in the main thread.
            pass

    if seasons is not None and isinstance(seasons, (list, tuple)) and seasons:
        target_seasons = list(seasons)
    elif season is not None:
        target_seasons = [season]
    else:
        target_seasons = list(getattr(config, "TARGET_SEASONS", [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]))

    seasons = target_seasons

    # Competition-specific season/edition selection. A season a competition never
    # staged is reported as skipped (NOT_APPLICABLE), never attempted and never
    # silently dropped from the run.
    supported_seasons_by_competition = {"football": {}, "basketball": {}}
    edition_selection = {"skipped": [], "unknown_windows": []}
    for sport_name, league_ids in (
        ("football", config.ALLOWED_LEAGUE_IDS),
        ("basketball", config.ALLOWED_BASKETBALL_LEAGUE_IDS),
    ):
        for lid in league_ids:
            selection = _competition_edition_selection(sport_name, lid, seasons)
            supported_seasons_by_competition[sport_name][lid] = set(selection.get("supported") or [])
            for skipped_season in selection.get("not_applicable") or []:
                edition_selection["skipped"].append(
                    {
                        "sport": sport_name,
                        "league_id": lid,
                        "season": skipped_season,
                        "reason": "unsupported_season_edition",
                    }
                )
            for unknown_season in selection.get("unknown") or []:
                edition_selection["unknown_windows"].append(
                    {"sport": sport_name, "league_id": lid, "season": unknown_season}
                )

    all_football_items = [
        {"sport": "football", "league_id": lid, "season": ssn}
        for ssn in seasons
        for lid in config.ALLOWED_LEAGUE_IDS
        if ssn in supported_seasons_by_competition["football"].get(lid, set())
    ]
    all_basketball_items = [
        {"sport": "basketball", "league_id": lid, "season": ssn}
        for ssn in seasons
        for lid in config.ALLOWED_BASKETBALL_LEAGUE_IDS
        if ssn in supported_seasons_by_competition["basketball"].get(lid, set())
    ]

    if edition_selection["skipped"]:
        skipped_descriptions = ", ".join(
            f"{item['sport']}:{item['league_id']}/{item['season']}"
            for item in edition_selection["skipped"]
        )
        print(
            f"Queue: {len(edition_selection['skipped'])} competition-season target(s) are "
            f"NOT_APPLICABLE (the competition does not stage that edition): {skipped_descriptions}",
            flush=True,
        )

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
        f"Starting target historical acquisition queue ({len(queue_items)} datasets across seasons {seasons}) "
        f"[acquisition run {acquisition_run_id}]...",
        flush=True,
    )

    # Per-target timing evidence. Without it a multi-hour run cannot be explained:
    # the queue reports how much time each dataset spent in the coverage-check
    # (database) phase versus actual acquisition, plus the slowest targets.
    target_timings = []
    queue_started_at = datetime.now(timezone.utc).isoformat()
    queue_started_perf = time.perf_counter()

    for item in queue_items:
        if interrupted["requested"]:
            break
        sport = item["sport"]
        league_id = item["league_id"]
        season = item["season"]
        t_target_start = time.perf_counter()

        # Check coverage before making requests
        cov = check_acquisition_coverage(league_id, season, sport=sport)
        coverage_check_seconds = time.perf_counter() - t_target_start
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
            target_timings.append({
                "sport": sport,
                "league_id": league_id,
                "season": season,
                "status": "COMPLETE",
                "skipped_reason": "Dataset already COMPLETE",
                "duration_seconds": round(time.perf_counter() - t_target_start, 3),
                "coverage_check_seconds": round(coverage_check_seconds, 3),
                "acquisition_seconds": 0.0,
                "persistence_seconds": 0.0,
                "db_write_calls": 0,
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
            target_timings.append({
                "sport": sport,
                "league_id": league_id,
                "season": season,
                "status": "INCOMPLETE",
                "skipped_reason": "sport_acquisition_budget_exhausted",
                "duration_seconds": round(time.perf_counter() - t_target_start, 3),
                "coverage_check_seconds": round(coverage_check_seconds, 3),
                "acquisition_seconds": 0.0,
                "persistence_seconds": 0.0,
                "db_write_calls": 0,
                "api_requests_consumed": 0,
            })
            continue

        in_progress_target = {
            "sport": sport, "league_id": league_id, "season": season,
        }
        _write_queue_checkpoint(acquisition_run_id, {
            "acquisition_run_id": acquisition_run_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "interrupted": interrupted["requested"],
            "last_completed_target": last_completed_target,
            "in_progress_target": in_progress_target,
            "completed_target_count": len(target_timings),
            "total_targets": len(queue_items),
            "target_timings": target_timings,
        })
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

        target_duration = time.perf_counter() - t_target_start
        report["duration_seconds"] = round(target_duration, 3)
        reports.append(report)
        target_timings.append({
            "sport": sport,
            "league_id": league_id,
            "season": season,
            "status": report.get("status"),
            "duration_seconds": round(target_duration, 3),
            "coverage_check_seconds": round(coverage_check_seconds, 3),
            "acquisition_seconds": round(max(0.0, target_duration - coverage_check_seconds), 3),
            "persistence_seconds": round(float(report.get("db_write_duration") or 0.0), 3),
            "db_write_calls": int(report.get("db_write_calls") or 0),
            "api_requests_consumed": report.get("api_requests_consumed", 0),
            "final_stored_count": report.get("final_stored_count", 0),
            "skipped_reason": report.get("skipped_reason"),
        "identity_resolution_seconds": round(
            float(report.get("identity_resolution_duration") or 0.0), 3
        ),
        })

        last_completed_target = {
            "sport": sport, "league_id": league_id, "season": season,
            "status": report.get("status"),
        }
        in_progress_target = None
        _write_queue_checkpoint(acquisition_run_id, {
            "acquisition_run_id": acquisition_run_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "interrupted": interrupted["requested"],
            "last_completed_target": last_completed_target,
            "in_progress_target": None,
            "completed_target_count": len(target_timings),
            "total_targets": len(queue_items),
            "target_timings": target_timings,
        })
        if interrupted["requested"]:
            print(
                f"Queue: stopping after safely completing {sport} league {league_id} "
                f"season {season}; the run is interrupted.",
                flush=True,
            )
            break

    # Attach the run id to every target record, not only the run summary, so a
    # single target's evidence can be traced to the run that produced it.
    for timing_record in target_timings:
        timing_record["acquisition_run_id"] = acquisition_run_id
    for report_record in reports:
        if isinstance(report_record, dict):
            report_record["acquisition_run_id"] = acquisition_run_id

    total_duration_seconds = time.perf_counter() - queue_started_perf
    slowest_targets = sorted(
        target_timings, key=lambda timing: timing.get("duration_seconds", 0.0), reverse=True
    )[:10]
    queue_finished_at = datetime.now(timezone.utc).isoformat()

    print(f"Historical queue run complete. Processed {len(reports)} dataset(s).", flush=True)
    print(
        f"Historical queue timing: total {total_duration_seconds:.2f}s "
        f"({total_duration_seconds / 60.0:.2f} min) for {len(target_timings)} target(s).",
        flush=True,
    )
    for timing in slowest_targets:
        print(
            f"  slowest target: {timing['sport']} league {timing['league_id']} season {timing['season']} "
            f"took {timing['duration_seconds']:.2f}s (coverage-check {timing['coverage_check_seconds']:.2f}s, "
            f"acquisition {timing['acquisition_seconds']:.2f}s, status={timing['status']}, "
            f"stored={timing.get('final_stored_count', 0)})",
            flush=True,
        )

    total_api_requests_consumed = sum(
        int(timing_record.get("api_requests_consumed") or 0)
        for timing_record in target_timings
    )
    quota_consumed_by_sport = {}
    for timing_record in target_timings:
        sport_name = timing_record.get("sport")
        quota_consumed_by_sport[sport_name] = (
            quota_consumed_by_sport.get(sport_name, 0)
            + int(timing_record.get("api_requests_consumed") or 0)
        )
    targets_skipped = [
        timing_record for timing_record in target_timings
        if timing_record.get("skipped_reason")
    ]
    slowest_operations = sorted(
        [
            {
                "target": f"{timing_record.get('sport')}:{timing_record.get('league_id')}/{timing_record.get('season')}",
                "operation": operation,
                "seconds": round(float(timing_record.get(operation_key) or 0.0), 3),
            }
            for timing_record in target_timings
            for operation, operation_key in (
                ("coverage_check", "coverage_check_seconds"),
                ("acquisition", "acquisition_seconds"),
                ("persistence", "persistence_seconds"),
                ("identity_resolution", "identity_resolution_seconds"),
            )
        ],
        key=lambda operation: operation["seconds"],
        reverse=True,
    )[:10]

    for _signal_name, _previous_handler in previous_signal_handlers.items():
        try:
            signal.signal(_signal_name, _previous_handler)
        except (ValueError, OSError):
            pass

    run_report = {
        "report_type": "historical_acquisition_run",
        "interrupted": interrupted["requested"],
        "interrupt_signal": interrupted["signal"],
        "last_completed_target": last_completed_target,
        "in_progress_target": (
            interrupted["current_target"] if interrupted["requested"] else None
        ),
        "acquisition_run_id": acquisition_run_id,
        "target_seasons": seasons,
        "processed_count": len(reports),
        "edition_selection": edition_selection,
        "reports": reports,
        "started_at": queue_started_at,
        "finished_at": queue_finished_at,
        "total_duration_seconds": round(total_duration_seconds, 3),
        "target_timings": target_timings,
        "slowest_targets": slowest_targets,
        "slowest_operations": slowest_operations,
        "total_api_requests_consumed": total_api_requests_consumed,
        "quota_consumed_by_sport": quota_consumed_by_sport,
        "targets_skipped": targets_skipped,
        "targets_skipped_count": len(targets_skipped),
    }
    report_path = None
    try:
        run_report_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "historical_runs"
        )
        os.makedirs(run_report_dir, exist_ok=True)
        report_path = os.path.join(run_report_dir, f"{acquisition_run_id}.json")
        with open(report_path, "w", encoding="utf-8") as report_file:
            json.dump(run_report, report_file, indent=2, sort_keys=True, default=str)
        print(f"Historical queue run report written: {report_path}", flush=True)
    except OSError as exc:
        print(f"Historical queue run report could not be written: {exc}", flush=True)
    run_report["report_path"] = report_path

    return run_report


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
        # The verification audit deliberately covers the full configured
        # competition x requested-season matrix rather than only the editions a
        # competition stages: it reports every requested dataset that has no
        # COMPLETE manifest, including editions that never existed, so a missing
        # dataset can never be hidden by an edition filter. Acquisition itself is
        # edition-scoped (see run_historical_queue), and this audit is what makes
        # the difference visible.
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
            records = storage.get_historical_fixtures(lid, ssn, include_reconciliation_candidates=True)
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
