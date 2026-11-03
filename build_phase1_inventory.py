#!/usr/bin/env python3
"""
Build PHASE1_INVENTORY.json reproducibly from repository configuration.

The inventory is generated from the current configuration and the versioned
sports knowledge catalogue. It is deliberately NOT a place to record claimed
production numbers: without a readable database every persisted-record field is
``UNKNOWN`` and the generation source says so. When a local database is
available, ``--local-database`` reads the real manifests and record counts into
the inventory instead.

Status vocabulary
-----------------
Lifecycle (does the competition season exist / is it finished?):
    EXPECTED, UPCOMING, ONGOING, COMPLETED, UNKNOWN, NOT_APPLICABLE
Acquisition (how far did persistence get?):
    PENDING, RUNNING, INCOMPLETE, VALIDATED, FAILED

Usage
-----
    python build_phase1_inventory.py                    # configuration only
    python build_phase1_inventory.py --local-database   # + local DB evidence
    python build_phase1_inventory.py --output PHASE1_INVENTORY.json
"""

import argparse
import json
from datetime import datetime, timezone

import config
import sports_knowledge

INVENTORY_VERSION = 1

LIFECYCLE_EXPECTED = "EXPECTED"
LIFECYCLE_UPCOMING = "UPCOMING"
LIFECYCLE_ONGOING = "ONGOING"
LIFECYCLE_COMPLETED = "COMPLETED"
LIFECYCLE_UNKNOWN = "UNKNOWN"
LIFECYCLE_NOT_APPLICABLE = "NOT_APPLICABLE"

ACQUISITION_UNKNOWN = "PENDING"
ACQUISITION_VALIDATED = "VALIDATED"
ACQUISITION_INCOMPLETE = "INCOMPLETE"
ACQUISITION_FAILED = "FAILED"
ACQUISITION_RUNNING = "RUNNING"

UNKNOWN = "UNKNOWN"


def _now_utc():
    return datetime.now(timezone.utc)


def _knowledge_gap_summary():
    """Summarise the full knowledge report without duplicating it in the inventory."""
    rows = sports_knowledge.knowledge_gap_report()
    evidence = {}
    participants = {}
    for row in rows:
        evidence[row["evidence_status"]] = evidence.get(row["evidence_status"], 0) + 1
        participants[row["participant_status"]] = participants.get(row["participant_status"], 0) + 1
    return {
        "horizon": sports_knowledge.KNOWLEDGE_FUTURE_HORIZON,
        "editions_evaluated": len(rows),
        "by_evidence_status": dict(sorted(evidence.items())),
        "by_participant_status": dict(sorted(participants.items())),
        "report_function": "sports_knowledge.knowledge_gap_report()",
    }


def _lifecycle_for_edition(record, now_utc):
    """Return (lifecycle_state, evidence) for a catalogued edition."""
    if record.get("exists") is False:
        return LIFECYCLE_NOT_APPLICABLE, "competition does not stage this edition"
    if record.get("status") != "KNOWN":
        return LIFECYCLE_UNKNOWN, "edition window not documented; completion fails closed"

    from datetime import datetime as _dt

    start = _dt.fromisoformat(record["start"])
    end = _dt.fromisoformat(record["end"])
    if now_utc < start:
        return LIFECYCLE_UPCOMING, f"declared window opens {record['start']}"
    if now_utc < end:
        return LIFECYCLE_ONGOING, f"declared window closes {record['end']}"
    return LIFECYCLE_COMPLETED, f"declared window closed {record['end']}"


def _expected_coverage(sport, league_id, season, competition):
    """
    Return the expected coverage units for a competition season.

    A domestic double-round-robin total is only stated when the team count for
    that season is independently known. Cup, international and tournament
    formats have no fixed pair schedule, so no fixture count is invented.
    """
    expected = {"type": competition.get("coverage_format") or competition.get("format"), "expected_fixtures": UNKNOWN, "expected_teams": UNKNOWN, "basis": None}

    knowledge_units = sports_knowledge.expected_coverage_units(sport, league_id, season)
    if knowledge_units.get("value") is not None:
        expected["expected_fixtures"] = knowledge_units["value"]
        tournament_expectations = getattr(config, "FOOTBALL_TOURNAMENT_COVERAGE_EXPECTATIONS", {}) or {}
        documented = tournament_expectations.get((sport, league_id, season))
        documented_evidence = documented.get("evidence") if isinstance(documented, dict) else None
        expected["basis"] = knowledge_units.get("evidence") or documented_evidence

    if competition.get("coverage_format") == "double_round_robin":
        participants = sports_knowledge.edition_participants(sport, league_id, season)
        if participants:
            team_count = len(participants)
            expected["expected_teams"] = team_count
            expected["expected_fixtures"] = team_count * (team_count - 1)
            expected["basis"] = "knowledge catalogue: verified participants * (participants - 1)"
        else:
            # A generic configured team count is not evidence for a historical
            # season. Promoted/relegated clubs can change the roster, so an
            # unverified season stays UNKNOWN instead of inheriting a current
            # season's arithmetic.
            expected["expected_teams"] = UNKNOWN
            expected["expected_fixtures"] = UNKNOWN
            expected["basis"] = "authoritative participant roster unavailable"
        exceptions = getattr(config, "FOOTBALL_SEASON_COVERAGE_EXCEPTIONS", {}) or {}
        exception = exceptions.get((sport, league_id, season))
        if isinstance(exception, dict) and isinstance(exception.get("expected_fixture_count"), int):
            expected["expected_fixtures"] = exception["expected_fixture_count"]
            expected["basis"] = exception.get("evidence") or "documented season exception"

    return expected


def _provider_support(competition):
    coverage = competition.get("provider_coverage") if isinstance(competition.get("provider_coverage"), dict) else {}
    support = {}
    for provider_name, declaration in coverage.items():
        if isinstance(declaration, dict):
            support[provider_name] = {
                "supported": declaration.get("supported") is True,
                "code": declaration.get("code"),
                "reason": declaration.get("reason"),
            }
    return support


def _provider_sources(sport, competition):
    """Return the concrete source/endpoint for every provider that covers a competition."""
    registry = sports_knowledge.providers(sport)
    coverage = competition.get("provider_coverage") if isinstance(competition.get("provider_coverage"), dict) else {}
    sources = {}
    for provider_name in sports_knowledge.provider_order(sport):
        declaration = coverage.get(provider_name)
        if not isinstance(declaration, dict) or declaration.get("supported") is not True:
            continue
        info = registry.get(provider_name) if isinstance(registry.get(provider_name), dict) else {}
        sources[provider_name] = {
            "name": info.get("name"),
            "kind": info.get("kind"),
            "competition_code": declaration.get("code"),
            "url": (
                info.get("url_pattern")
                or info.get("documentation_url")
                or info.get("library_url")
                or info.get("upstream_url")
            ),
            "coverage": info.get("coverage"),
        }
    return sources


def _evidence_gaps(sport, league_id, season, competition, record):
    """List the evidence this repository does not hold for a competition season."""
    gaps = []
    if record.get("status") != "KNOWN":
        gaps.append("edition_window")

    # A double-round-robin total is derivable from the season's authoritative team
    # count, so it is not a missing-evidence gap. Every other format needs an
    # independently documented total; when the catalogue has none (or records an
    # explicit null for this edition) coverage stays unverifiable.
    if competition.get("coverage_format") != "double_round_robin":
        expected_value = sports_knowledge.expected_coverage_units(sport, league_id, season).get("value")
        if expected_value is None:
            gaps.append("expected_fixture_total")

    if not sports_knowledge.edition_participants(sport, league_id, season):
        # Completion cannot be established from a match total or a configured
        # team count alone. The catalogue must carry a verified roster for every
        # format; absence is reported, never replaced by acquired fixtures.
        gaps.append("authoritative_participant_universe")
    if record.get("is_future"):
        gaps.append("future_edition_details_not_refreshed")

    return gaps


def _local_database_evidence(sport, league_id, season):
    """Read a local database manifest/record count, or report it unavailable."""
    try:
        import storage

        manifest = storage.get_historical_dataset_status(league_id, season, sport=sport)
        if sport == "basketball":
            records = storage.get_historical_basketball_games(league_id, season, include_storage_source=True)
        else:
            records = storage.get_historical_fixtures(
                league_id, season, include_reconciliation_candidates=True
            )
        return {
            "source": "local_database",
            "manifest_status": manifest.get("status") or UNKNOWN,
            "persisted_records": len(records) if isinstance(records, list) else UNKNOWN,
            "expected_coverage_units": manifest.get("expected_pages", UNKNOWN),
            "completed_coverage_units": manifest.get("pages_completed", UNKNOWN),
            "coverage_unit": manifest.get("coverage_unit"),
            "error_reason": manifest.get("error_reason"),
            "provider_coverage": manifest.get("provider_coverage"),
        }
    except Exception as exc:  # noqa: BLE001 - evidence is optional and must not fail generation
        return {
            "source": "local_database",
            "manifest_status": UNKNOWN,
            "persisted_records": UNKNOWN,
            "error": f"database evidence unavailable: {exc}",
        }


def build_inventory(include_local_database=False, now_utc=None):
    """Build the inventory dictionary from the current repository configuration."""
    now_utc = now_utc or _now_utc()
    knowledge_problems = sports_knowledge.validate()

    targets = []
    for sport in sports_knowledge.SPORT_FILES:
        if sport == "football":
            league_ids = list(getattr(config, "ALLOWED_LEAGUE_IDS", []))
        else:
            league_ids = list(getattr(config, "ALLOWED_BASKETBALL_LEAGUE_IDS", []))

        requested = list(getattr(config, "TARGET_SEASONS", []))

        for league_id in league_ids:
            competition = sports_knowledge.get_competition(sport, league_id)
            selection = sports_knowledge.select_seasons(sport, league_id, requested)
            # The inventory must list every configured competition season, not
            # only the seasons inside the generic target range: a documented
            # edition such as the 2018 FIFA World Cup is acquirable and storable,
            # and an edition whose window is undocumented must be visible as an
            # explicit gap instead of being omitted.
            documented_editions = set(
                sports_knowledge.edition_years(
                    sport,
                    league_id,
                    include_future=True,
                    future_to=sports_knowledge.KNOWLEDGE_FUTURE_HORIZON,
                )
                or []
            )
            season_candidates = sorted(
                set(selection.get("supported") or [])
                | set(selection.get("not_applicable") or [])
                | documented_editions
            )

            for season in season_candidates:
                record = sports_knowledge.edition(sport, league_id, season) if competition else {
                    "exists": None,
                    "status": LIFECYCLE_UNKNOWN,
                }
                if competition:
                    lifecycle, lifecycle_evidence = _lifecycle_for_edition(record, now_utc)
                else:
                    lifecycle, lifecycle_evidence = (
                        LIFECYCLE_UNKNOWN,
                        "competition is not catalogued; no lifecycle can be established",
                    )

                target = {
                    "sport": sport,
                    "competition": (competition or {}).get("name") or UNKNOWN,
                    "league_id": league_id,
                    "aliases": (competition or {}).get("aliases") or [],
                    "governing_body": (competition or {}).get("governing_body") or UNKNOWN,
                    "country": ((competition or {}).get("country") if competition else None) or "INTERNATIONAL_OR_UNKNOWN",
                    "season": season,
                    "edition_label": record.get("label") if competition else UNKNOWN,
                    "format": (competition or {}).get("format") or UNKNOWN,
                    "coverage_format": (competition or {}).get("coverage_format") or UNKNOWN,
                    "format_details": record.get("format_details") or UNKNOWN,
                    "stages": record.get("stages") or [],
                    "fixture_total": record.get("fixture_total") if record.get("fixture_total") is not None else UNKNOWN,
                    "is_future": bool(record.get("is_future")),
                    "evidence_status": record.get("evidence_status") or sports_knowledge.EVIDENCE_UNKNOWN,
                    "knowledge_evidence_gaps": record.get("evidence_gaps") or [],
                    "participant_status": record.get("participant_status") or UNKNOWN,
                    "participants": [
                        {"name": p.get("name"), "canonical_id": p.get("canonical_id")}
                        for p in (record.get("participants") or [])
                        if isinstance(p, dict)
                    ],
                    "expected_participant_count": (
                        len(record.get("participants") or [])
                        if record.get("participant_status") == sports_knowledge.PARTICIPANT_STATUS_VERIFIED
                        else UNKNOWN
                    ),
                    "participant_evidence": record.get("participant_evidence") or UNKNOWN,
                    "promoted": record.get("promoted") or [],
                    "relegated": record.get("relegated") or [],
                    "renames": record.get("renames") or [],
                    "coverage_requirements": record.get("coverage_requirements") or {},
                    "window_start": record.get("start") if record.get("status") == "KNOWN" else UNKNOWN,
                    "window_end": record.get("end") if record.get("status") == "KNOWN" else UNKNOWN,
                    "window_evidence": record.get("evidence") if record.get("status") == "KNOWN" else "edition window not documented",
                    "lifecycle_state": lifecycle,
                    "lifecycle_evidence": lifecycle_evidence,
                    "expected_coverage": (
                        _expected_coverage(sport, league_id, season, competition)
                        if competition
                        else {"type": UNKNOWN, "expected_fixtures": UNKNOWN, "expected_teams": UNKNOWN, "basis": None}
                    ),
                    "approved_provider_order": sports_knowledge.provider_order(sport),
                    "provider_support": _provider_support(competition or {}),
                    "provider_sources": _provider_sources(sport, competition) if competition else {},
                    "evidence_gaps": _evidence_gaps(sport, league_id, season, competition, record) if competition else ["competition_not_catalogued"],
                    "acquisition_state": ACQUISITION_UNKNOWN,
                    "persisted_records": UNKNOWN,
                    "attempted_coverage_units": UNKNOWN,
                    "parsed_records": UNKNOWN,
                    "duplicates_conflicts": UNKNOWN,
                    "unresolved_team_ids": UNKNOWN,
                    "missing_coverage_units": UNKNOWN,
                    "provenance_evidence": UNKNOWN,
                    "known_blockers": (competition or {}).get("known_blockers") or [],
                    "next_safe_action": None,
                }

                if lifecycle == LIFECYCLE_NOT_APPLICABLE:
                    target["acquisition_state"] = LIFECYCLE_NOT_APPLICABLE
                    target["next_safe_action"] = "none; the competition does not stage this edition"
                elif competition is None:
                    target["next_safe_action"] = "add a catalogued competition entry before acquiring"
                elif record.get("is_future"):
                    target["next_safe_action"] = "refresh future edition facts before acquisition; provisional evidence never completes a season"
                elif record.get("status") != "KNOWN":
                    target["next_safe_action"] = "document the edition window from an authoritative source; acquisition stays incomplete until then"
                elif "authoritative_participant_universe" in target["evidence_gaps"]:
                    target["next_safe_action"] = (
                        "record a verified edition participant roster and aliases before treating acquisition as complete"
                    )
                elif "expected_fixture_total" in target["evidence_gaps"]:
                    target["next_safe_action"] = (
                        "record a documented format-specific coverage total before treating acquisition as complete"
                    )
                else:
                    target["next_safe_action"] = "acquire through the approved provider order, then validate persisted coverage"

                if include_local_database and competition is not None:
                    evidence = _local_database_evidence(sport, league_id, season)
                    target["acquisition_state"] = evidence.get("manifest_status") or UNKNOWN
                    target["persisted_records"] = evidence.get("persisted_records", UNKNOWN)
                    target["attempted_coverage_units"] = evidence.get("completed_coverage_units", UNKNOWN)
                    target["expected_coverage_units"] = evidence.get("expected_coverage_units", UNKNOWN)
                    target["provenance_evidence"] = evidence

                targets.append(target)

    database_inspected = bool(include_local_database)
    return {
        "inventory_version": INVENTORY_VERSION,
        "generated_by": "build_phase1_inventory.py",
        "generation_source": "repository_configuration_and_sports_knowledge",
        "database_inspected": database_inspected,
        "knowledge_schema_version": sports_knowledge.KNOWLEDGE_SCHEMA_VERSION,
        "knowledge_validation_problems": knowledge_problems,
        "excluded_from_historical_fixture_acquisition": sports_knowledge.load_sport("football").get(
            "excluded_from_historical_fixture_acquisition"
        ),
        "status_conventions": {
            "lifecycle": [LIFECYCLE_EXPECTED, LIFECYCLE_UPCOMING, LIFECYCLE_ONGOING, LIFECYCLE_COMPLETED, LIFECYCLE_UNKNOWN, LIFECYCLE_NOT_APPLICABLE],
            "acquisition": [ACQUISITION_UNKNOWN, ACQUISITION_RUNNING, ACQUISITION_INCOMPLETE, ACQUISITION_VALIDATED, ACQUISITION_FAILED],
            "unknown_policy": (
                "UNKNOWN means repository evidence is unavailable. It is never a zero, never a success and "
                "never a substitute for a database readback. Persisted counts are only recorded when a "
                "database was actually read."
            ),
        },
        "targets": targets,
        "knowledge_future_horizon": sports_knowledge.KNOWLEDGE_FUTURE_HORIZON,
        "knowledge_gap_summary": _knowledge_gap_summary(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the reproducible Phase 1 inventory from repository configuration.")
    parser.add_argument("--output", default="PHASE1_INVENTORY.json", help="Output path (default: PHASE1_INVENTORY.json)")
    parser.add_argument(
        "--local-database",
        action="store_true",
        help="Also read a local SQLite/PostgreSQL database for persisted-record evidence (never claims a production readback).",
    )
    args = parser.parse_args(argv)

    inventory = build_inventory(include_local_database=args.local_database)

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(inventory, handle, indent=2, sort_keys=False)
        handle.write("\n")

    not_applicable = sum(1 for t in inventory["targets"] if t["lifecycle_state"] == LIFECYCLE_NOT_APPLICABLE)
    print(
        f"Wrote {args.output}: {len(inventory['targets'])} competition-season targets "
        f"({not_applicable} NOT_APPLICABLE), {len(inventory['knowledge_validation_problems'])} knowledge validation problem(s), "
        f"database_inspected={inventory['database_inspected']}."
    )
    return 0 if not inventory["knowledge_validation_problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
