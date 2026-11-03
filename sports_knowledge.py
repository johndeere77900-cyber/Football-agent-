"""
Versioned sports knowledge layer.

This module is the repository's structured, machine-readable catalogue of sports
facts used by historical acquisition: competition identities, aliases, formats,
season/edition calendars, expected coverage units, provider mappings and source
provenance. It is deliberately NOT an LLM prompt, a free-text memory or a vector
store: every entry is curated data that can be validated, diffed and reviewed.

Design rules
------------
- Facts and hypotheses are separated. A ``fact`` must resolve to a declared
  source URL with a retrieval timestamp and a confidence label. A ``hypothesis``
  is a research note and is never used to decide acquisition or completion.
- Unknown stays unknown. An edition whose date window is not documented is
  reported with lifecycle status ``UNKNOWN`` and fails closed downstream; no
  window is invented.
- Football and basketball are complete; another sport is added by dropping a new
  catalogue file in ``sports_knowledge/`` and registering it in ``SPORT_FILES``.
- No secrets, credentials or personal data belong in this layer.

The layer is additive: ``config`` remains the runtime authority for the declared
season windows and expected NBA totals that the acquisition code already reads,
and :func:`validate` fails when this catalogue and that configuration disagree.
"""

import json
import os
import re
import unicodedata

KNOWLEDGE_SCHEMA_VERSION = 2

SPORT_FILES = {
    "football": "football_competitions.json",
    "basketball": "basketball_competitions.json",
}

# Edition-level facts live in separate files so the competition catalogue remains
# reviewable and a missing edition file cannot silently erase the competition
# catalogue. Absence of an edition file means "no independently verified edition
# facts yet", not "no participants".
SPORT_EDITION_FILES = {
    "football": "football_editions.json",
    "basketball": "basketball_editions.json",
}

KNOWLEDGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sports_knowledge")

# Acquisition/verification status vocabulary shared with the Phase 1 inventory.
STATUS_EXPECTED = "EXPECTED"
STATUS_UPCOMING = "UPCOMING"
STATUS_ONGOING = "ONGOING"
STATUS_COMPLETED = "COMPLETED"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

PARTICIPANT_STATUS_VERIFIED = "VERIFIED"
PARTICIPANT_STATUS_UNKNOWN = "UNKNOWN"
PARTICIPANT_STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

# A refreshable knowledge architecture needs a bounded horizon for future
# editions. Beyond this point the catalogue reports "not yet enumerated" instead
# of inventing an unbounded stream of seasons.
KNOWLEDGE_FUTURE_HORIZON = 2030
HISTORICAL_KNOWLEDGE_START = 2019

EVIDENCE_CONFIRMED = "CONFIRMED"
EVIDENCE_PROVISIONAL = "PROVISIONAL"
EVIDENCE_UNKNOWN = "UNKNOWN"
EVIDENCE_NOT_APPLICABLE = "NOT_APPLICABLE"

_REQUIRED_COMPETITION_FIELDS = (
    "league_id",
    "name",
    "aliases",
    "governing_body",
    "format",
    "edition_rule",
    "evidence",
)

_CACHE = {}


def _normalize_sport(sport):
    key = str(sport or "football").strip().lower()
    if key not in SPORT_FILES:
        raise KeyError(f"Unknown sport '{sport}'. Known sports: {sorted(SPORT_FILES)}")
    return key


def _canonical_slug(name):
    """Return the repository's deterministic canonical slug for a team name."""
    value = str(name or "")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    return re.sub(r"\s+", "_", value) or "unknown"


def _normalise_participant(sport, raw):
    """Normalise one participant declaration without inventing missing evidence."""
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    canonical_id = str(raw.get("canonical_id") or "").strip()
    if not canonical_id:
        canonical_id = f"{sport}_team_{_canonical_slug(name)}"
    aliases = []
    for alias in raw.get("aliases") or []:
        if isinstance(alias, str) and alias.strip() and alias.strip() != name:
            aliases.append(alias.strip())
    return {
        "name": name,
        "canonical_id": canonical_id,
        "aliases": sorted(set(aliases), key=str.casefold),
    }


def _resolve_source_reference(fact, sources):
    """Resolve a source id/reference against the catalogue source registry."""
    if not isinstance(fact, dict):
        return None
    resolved = dict(fact)
    source_id = resolved.get("source")
    source = sources.get(source_id) if isinstance(source_id, str) else None
    if isinstance(source, dict):
        resolved.setdefault("source_url", source.get("url"))
        resolved.setdefault("source_name", source.get("name"))
        resolved.setdefault("retrieved_at", source.get("retrieved_at"))
        resolved.setdefault("confidence", source.get("confidence"))
    return resolved


def _resolve_edition_records(sport, raw_editions, sources):
    """Resolve edition participant/evidence declarations into one mapping."""
    result = {}
    if not isinstance(raw_editions, dict):
        return result
    for season_key, raw in raw_editions.items():
        if not isinstance(raw, dict):
            continue
        participants = []
        for item in raw.get("participants") or []:
            participant = _normalise_participant(sport, item)
            if participant:
                participants.append(participant)
        record = dict(raw)
        record["participants"] = participants
        record["canonical_participant_ids"] = [p["canonical_id"] for p in participants]
        record["participant_status"] = str(raw.get("participant_status") or (
            PARTICIPANT_STATUS_VERIFIED if participants else PARTICIPANT_STATUS_UNKNOWN
        )).upper()
        record["participant_evidence"] = _resolve_source_reference(raw.get("participant_evidence"), sources)
        stages = raw.get("stages") if isinstance(raw.get("stages"), list) else []
        record["stages"] = [dict(stage) for stage in stages if isinstance(stage, dict)]
        result[str(season_key)] = record
    return result


def load_sport(sport):
    """
    Return the parsed, source-resolved catalogue for one sport.

    Evidence entries reference a shared ``sources`` registry in the same file and
    are returned with the resolved ``source_url``/``retrieved_at``/``confidence``
    so callers never need to resolve two layers.
    """
    key = _normalize_sport(sport)
    if key in _CACHE:
        return _CACHE[key]

    path = os.path.join(KNOWLEDGE_DIR, SPORT_FILES[key])
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)

    edition_raw = {}
    edition_path = os.path.join(KNOWLEDGE_DIR, SPORT_EDITION_FILES[key])
    if os.path.exists(edition_path):
        with open(edition_path, "r", encoding="utf-8") as handle:
            edition_raw = json.load(handle)

    sources = dict(raw.get("sources") or {})
    edition_sources = edition_raw.get("sources") if isinstance(edition_raw.get("sources"), dict) else {}
    sources.update(edition_sources)
    edition_data = edition_raw.get("editions") if isinstance(edition_raw.get("editions"), dict) else {}

    competitions = []
    for entry in raw.get("competitions") or []:
        if not isinstance(entry, dict):
            continue
        resolved = dict(entry)
        league_key = str(entry.get("league_id"))
        resolved["editions"] = _resolve_edition_records(key, edition_data.get(league_key) or {}, sources)
        resolved_evidence = []
        for fact in entry.get("evidence") or []:
            if not isinstance(fact, dict):
                continue
            resolved_fact = dict(fact)
            source = sources.get(fact.get("source")) if isinstance(fact.get("source"), str) else None
            if isinstance(source, dict):
                resolved_fact["source_url"] = source.get("url")
                resolved_fact["source_name"] = source.get("name")
                resolved_fact["retrieved_at"] = source.get("retrieved_at")
                resolved_fact["confidence"] = source.get("confidence")
            resolved_evidence.append(resolved_fact)
        resolved["evidence"] = resolved_evidence
        competitions.append(resolved)

    raw["sources"] = sources
    raw["competitions"] = competitions
    raw["edition_schema_version"] = edition_raw.get("schema_version") if edition_raw else None
    raw["edition_sport"] = edition_raw.get("sport") if edition_raw else None
    _CACHE[key] = raw
    return raw


def list_competitions(sport=None):
    """Return every competition entry, optionally for a single sport."""
    if sport is not None:
        return list(load_sport(sport).get("competitions") or [])
    entries = []
    for sport_key in SPORT_FILES:
        entries.extend(list_competitions(sport_key))
    return entries


def get_competition(sport, league_id):
    """Return one competition's catalogue entry, or None when it is not catalogued."""
    try:
        lid = int(league_id)
    except (TypeError, ValueError):
        return None
    for entry in list_competitions(sport):
        if int(entry.get("league_id", -1)) == lid:
            return entry
    return None


def providers(sport):
    """Return the provider registry for a sport (empty dict when absent)."""
    registry = load_sport(sport).get("providers")
    return registry if isinstance(registry, dict) else {}


def provider_order(sport):
    """
    Return the competition-acquisition provider order for a sport.

    This is the approved order the acquisition code must keep using; the
    catalogue records it so the inventory and tests can assert it without
    duplicating the list in prose.
    """
    key = "approved_historical_acquisition_order" if _normalize_sport(sport) == "football" else "approved_acquisition_order"
    order = load_sport(sport).get(key)
    return [p for p in order or [] if isinstance(p, str) and p.strip()]


def _edition_facts(competition, season):
    """Return one resolved edition record, or an explicit empty evidence record."""
    empty = {
        "format_details": None,
        "stages": [],
        "fixture_total": None,
        "participants": [],
        "canonical_participant_ids": [],
        "participant_status": PARTICIPANT_STATUS_UNKNOWN,
        "participant_evidence": None,
        "promoted": [],
        "relegated": [],
        "renames": [],
        "coverage_requirements": {},
        "edition_notes": None,
    }
    editions = competition.get("editions") if isinstance(competition, dict) else None
    if not isinstance(editions, dict):
        return empty
    raw = editions.get(str(season))
    if not isinstance(raw, dict):
        return empty
    result = dict(empty)
    result.update(raw)
    return result


def edition_participants(sport, league_id, season):
    """Return verified participants for one edition (empty when unverified)."""
    record = edition(sport, league_id, season)
    if record.get("participant_status") != PARTICIPANT_STATUS_VERIFIED:
        return []
    participants = record.get("participants")
    return list(participants) if isinstance(participants, list) else []


def edition_stages(sport, league_id, season):
    """Return the format stages recorded for an edition, if any."""
    record = edition(sport, league_id, season)
    stages = record.get("stages")
    return list(stages) if isinstance(stages, list) else []


def participant_alias_entries(sport):
    """
    Return ``{(ready_alias, league_id): canonical_slug}`` for catalogue participants.

    Competition context is part of every key. A club name that occurs in two
    competitions therefore never becomes an unqualified global alias. Existing
    ``config`` aliases take precedence when the identity layer merges these.
    """
    entries = {}
    for competition in list_competitions(sport):
        league_id = competition.get("league_id")
        editions = competition.get("editions") if isinstance(competition.get("editions"), dict) else {}
        for record in editions.values():
            if not isinstance(record, dict):
                continue
            for participant in record.get("participants") or []:
                if not isinstance(participant, dict):
                    continue
                canonical_id = participant.get("canonical_id") or ""
                prefix = f"{sport}_team_"
                if not canonical_id.startswith(prefix):
                    continue
                target = canonical_id[len(prefix):]
                names = [participant.get("name")] + list(participant.get("aliases") or [])
                for alias in names:
                    if isinstance(alias, str) and alias.strip():
                        entries.setdefault((alias.strip(), league_id), target)
    return entries


def edition_years(sport, league_id, include_future=False, future_to=None):
    """
    Return the edition start years a competition stages, or None if unknown.

    ``annual`` rules return the configured inclusive range; edition-based
    competitions return their declared editions only. ``include_future=True``
    adds the updateable future horizon (open annual seasons and explicitly
    declared future editions) without ever inventing an unbounded range.
    """
    competition = get_competition(sport, league_id)
    if not competition:
        return None
    rule = competition.get("edition_rule") if isinstance(competition.get("edition_rule"), dict) else {}

    explicit = rule.get("editions")
    if isinstance(explicit, list) and explicit:
        years = []
        for value in explicit:
            try:
                years.append(int(value))
            except (TypeError, ValueError):
                continue
        years = sorted(set(years))
    else:
        try:
            first = int(rule.get("from"))
            last = int(rule.get("to"))
        except (TypeError, ValueError):
            years = None
        else:
            years = None if last < first else list(range(first, last + 1))

    if years is None and not include_future:
        return None
    if years is None:
        years = []
    if include_future:
        years = sorted(set(years) | set(_future_years(competition, future_to=future_to)))
    return years


def _parse_iso_date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    from datetime import datetime, timezone

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _window_from_offsets(season, window):
    """Materialise a ``(year_offset, month, day)`` window relative to a season."""
    from datetime import datetime, timezone

    if not isinstance(window, dict):
        return None
    start_spec = window.get("start")
    end_spec = window.get("end")
    if not (
        isinstance(start_spec, (list, tuple)) and len(start_spec) == 3
        and isinstance(end_spec, (list, tuple)) and len(end_spec) == 3
    ):
        return None
    try:
        start = datetime(season + int(start_spec[0]), int(start_spec[1]), int(start_spec[2]), tzinfo=timezone.utc)
        end = datetime(season + int(end_spec[0]), int(end_spec[1]), int(end_spec[2]), tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    if end <= start:
        return None
    return start, end


def _future_years(competition, future_to=None):
    """
    Return future edition years that are structurally expected but not evidenced.

    Annual competitions get an open-ended ``from = configured_to + 1`` future
    rule unless their catalogue supplies a stricter one. Quadrennial/biennial
    competitions only return years explicitly declared in ``future_editions`` or
    ``future.editions``; no calendar arithmetic is invented for them.
    """
    if not isinstance(competition, dict):
        return []
    rule = competition.get("edition_rule") if isinstance(competition.get("edition_rule"), dict) else {}
    horizon = int(future_to or KNOWLEDGE_FUTURE_HORIZON)
    years = set()

    for value in rule.get("future_editions") or []:
        try:
            years.add(int(value))
        except (TypeError, ValueError):
            continue

    # Cadence-based competitions can enumerate structurally expected future
    # editions from their own declared rhythm, while their windows, participants
    # and totals remain provisional until refreshed from a source.
    cadence = {"biennial_editions": 2, "quadrennial_editions": 4}.get(str(rule.get("type") or ""))
    explicit_editions = rule.get("editions") if isinstance(rule.get("editions"), list) else []
    if cadence and explicit_editions:
        try:
            latest = max(int(value) for value in explicit_editions)
            candidate = latest + cadence
            while candidate <= horizon:
                years.add(candidate)
                candidate += cadence
        except (TypeError, ValueError):
            pass

    future_rule = rule.get("future") if isinstance(rule.get("future"), dict) else None
    if future_rule is None and rule.get("type") == "annual_split_year" and rule.get("to") is not None:
        try:
            future_rule = {"from": int(rule.get("to")) + 1, "open_ended": True}
        except (TypeError, ValueError):
            future_rule = None

    if isinstance(future_rule, dict):
        explicit = future_rule.get("editions")
        if isinstance(explicit, list):
            for value in explicit:
                try:
                    years.add(int(value))
                except (TypeError, ValueError):
                    continue
        try:
            start = int(future_rule.get("from"))
            end = future_rule.get("to")
            end = int(end) if end is not None else horizon
        except (TypeError, ValueError):
            start = end = None
        if start is not None and end is not None and end >= start:
            years.update(range(start, end + 1))

    return sorted(years)


def _edition_evidence_status(record, is_future=False):
    """Classify one edition's evidence without turning gaps into successes."""
    gaps = []
    if record.get("status") != "KNOWN":
        gaps.append("edition_window")
    if record.get("participant_status") != PARTICIPANT_STATUS_VERIFIED:
        gaps.append("authoritative_participant_universe")
    requirements = record.get("coverage_requirements") if isinstance(record.get("coverage_requirements"), dict) else {}
    documented_expectation = record.get("fixture_total")
    if documented_expectation is None:
        documented_expectation = next(
            (
                requirements.get(key)
                for key in ("expected_fixture_count", "expected_regular_season_games")
                if isinstance(requirements.get(key), int)
                and not isinstance(requirements.get(key), bool)
                and requirements.get(key) > 0
            ),
            None,
        )
    if documented_expectation is None:
        gaps.append("format_specific_coverage_expectation")
    if is_future:
        gaps.append("future_edition_details_not_refreshed")

    if not gaps:
        status = EVIDENCE_CONFIRMED
    elif is_future or record.get("status") == "KNOWN":
        status = EVIDENCE_PROVISIONAL
    else:
        status = EVIDENCE_UNKNOWN
    return status, gaps


def _with_knowledge_evidence(record, is_future=False):
    """Attach evidence classification and explicit gaps to an edition record."""
    if record.get("exists") is False:
        record["evidence_status"] = EVIDENCE_NOT_APPLICABLE
        record["evidence_gaps"] = []
        record["knowledge_status"] = EVIDENCE_NOT_APPLICABLE
        return record
    status, gaps = _edition_evidence_status(record, is_future=is_future)
    record["evidence_status"] = status
    record["evidence_gaps"] = gaps
    record["knowledge_status"] = status
    return record


def edition(sport, league_id, season):
    """
    Return the catalogued edition record for a competition season.

    The record always distinguishes the three cases that matter downstream:
    ``exists`` (the competition stages this edition), ``status`` (KNOWN when the
    edition's window is documented, UNKNOWN when it is not) and, when known, the
    concrete ``start``/``end`` boundaries with the evidence behind them.
    """
    competition = get_competition(sport, league_id)
    try:
        ssn = int(season)
    except (TypeError, ValueError):
        return {
            "sport": _normalize_sport(sport),
            "league_id": league_id,
            "season": season,
            "exists": False,
            "status": STATUS_UNKNOWN,
            "start": None,
            "end": None,
            "evidence": None,
        }

    if not competition:
        return {
            "sport": _normalize_sport(sport),
            "league_id": league_id,
            "season": ssn,
            "exists": None,
            "status": STATUS_UNKNOWN,
            "start": None,
            "end": None,
            "evidence": None,
        }

    years = edition_years(sport, league_id) or []
    future_years = _future_years(competition)
    is_future = ssn in future_years and ssn not in years
    exists = ssn in years or is_future
    rule = competition.get("edition_rule") if isinstance(competition.get("edition_rule"), dict) else {}
    facts = _edition_facts(competition, ssn)
    record = {
        "sport": _normalize_sport(sport),
        "league_id": int(competition.get("league_id")),
        "season": ssn,
        "exists": exists,
        "is_future": is_future,
        "name": competition.get("name"),
        "format": competition.get("format"),
        "coverage_format": competition.get("coverage_format"),
        "status": STATUS_UNKNOWN,
        "start": None,
        "end": None,
        "evidence": None,
        "label": None,
        "format_details": facts.get("format_details"),
        "stages": facts.get("stages") or [],
        "fixture_total": facts.get("fixture_total"),
        "participants": facts.get("participants") or [],
        "canonical_participant_ids": facts.get("canonical_participant_ids") or [],
        "participant_status": facts.get("participant_status") or PARTICIPANT_STATUS_UNKNOWN,
        "participant_evidence": facts.get("participant_evidence"),
        "promoted": facts.get("promoted") or [],
        "relegated": facts.get("relegated") or [],
        "renames": facts.get("renames") or [],
        "coverage_requirements": facts.get("coverage_requirements") or {},
        "edition_notes": facts.get("edition_notes"),
    }

    if not exists:
        # Beyond the refresh horizon an annual competition still stages future
        # editions, but the catalogue has not enumerated them. Report UNKNOWN,
        # never NOT_APPLICABLE and never an invented roster/window.
        out_of_horizon = (
            rule.get("type") == "annual_split_year"
            and rule.get("to") is not None
            and ssn > int(rule.get("to"))
        )
        if out_of_horizon:
            record["exists"] = True
            record["is_future"] = True
            record["is_out_of_horizon"] = True
            record["status"] = STATUS_UNKNOWN
            record["evidence_status"] = EVIDENCE_UNKNOWN
            record["evidence_gaps"] = [
                "edition_window",
                "authoritative_participant_universe",
                "format_specific_coverage_expectation",
                "future_horizon_exceeded",
            ]
            record["knowledge_status"] = EVIDENCE_UNKNOWN
            return record
        record["status"] = STATUS_NOT_APPLICABLE
        record["participant_status"] = PARTICIPANT_STATUS_NOT_APPLICABLE
        record["participants"] = []
        record["canonical_participant_ids"] = []
        return _with_knowledge_evidence(record, is_future=False)

    label_style = rule.get("label")
    record["label"] = f"{ssn}/{ssn + 1}" if label_style == "split_year" else str(ssn)

    if is_future:
        # Future editions are structurally expected, but their participants,
        # fixture totals and exact windows are provisional until refreshed from a
        # source. No calendar arithmetic or roster is fabricated here.
        record["status"] = STATUS_UNKNOWN
        return _with_knowledge_evidence(record, is_future=True)

    overrides = rule.get("overrides") if isinstance(rule.get("overrides"), dict) else {}
    override = overrides.get(str(ssn)) if isinstance(overrides, dict) else None
    if isinstance(override, dict):
        start = _parse_iso_date(override.get("start"))
        end = _parse_iso_date(override.get("end"))
        if start and end and end > start:
            record.update(
                {
                    "status": "KNOWN",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "evidence": override.get("evidence"),
                }
            )
            return _with_knowledge_evidence(record, is_future=False)
        # An explicitly undated edition stays UNKNOWN; it is never given a
        # guessed window.
        return _with_knowledge_evidence(record, is_future=False)

    window = _window_from_offsets(ssn, rule.get("window"))
    if window is None:
        return _with_knowledge_evidence(record, is_future=False)
    start, end = window
    record.update(
        {
            "status": "KNOWN",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "evidence": rule.get("window_evidence"),
        }
    )
    return _with_knowledge_evidence(record, is_future=False)


def select_seasons(sport, league_id, requested_seasons):
    """
    Split requested seasons into the editions a competition stages.

    Returns ``supported`` (acquire these), ``not_applicable`` (the competition
    never staged that season) and ``unknown`` (an edition exists but its window is
    not documented, so completion fails closed instead of being assumed).
    """
    requested = []
    for value in requested_seasons or []:
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        requested.append(value)

    competition = get_competition(sport, league_id)
    if not competition:
        return {
            "supported": list(requested),
            "not_applicable": [],
            "unknown": [],
            "catalog_available": True,
            "catalog_known_competition": False,
        }

    supported = []
    not_applicable = []
    unknown = []
    for season in requested:
        record = edition(sport, league_id, season)
        if record.get("exists") is True:
            supported.append(season)
            if record.get("status") != "KNOWN":
                unknown.append(season)
        else:
            not_applicable.append(season)

    return {
        "supported": supported,
        "not_applicable": not_applicable,
        "unknown": unknown,
        "catalog_available": True,
        "catalog_known_competition": True,
    }


def expected_coverage_units(sport, league_id, season):
    """
    Return the catalogued coverage expectation for a competition season.

    The catalogue never states a production record count. It states the format
    (for example ``double_round_robin``) and, where a source documents it, an
    independent expected total. ``value`` is None whenever the expectation is not
    documented, so callers fail closed.
    """
    competition = get_competition(sport, league_id)
    if not competition:
        return {"type": None, "value": None, "evidence": None}
    expectations = competition.get("expected_units") if isinstance(competition.get("expected_units"), dict) else {}

    try:
        ssn = int(season)
    except (TypeError, ValueError):
        return {"type": expectations.get("type"), "value": None, "evidence": None}

    per_edition = expectations.get("per_edition")
    if isinstance(per_edition, dict) and str(ssn) in per_edition:
        value = per_edition[str(ssn)]
        return {
            "type": expectations.get("type"),
            "value": value if isinstance(value, int) and not isinstance(value, bool) else None,
            "evidence": expectations.get("evidence"),
        }

    # An edition-specific verified total outranks the generic competition
    # default. This is how a promoted/relegated roster's calculated total becomes
    # the acquisition expectation instead of a hand-maintained league constant.
    edition_total = _edition_facts(competition, ssn).get("fixture_total")
    if isinstance(edition_total, int) and not isinstance(edition_total, bool) and edition_total > 0:
        return {
            "type": expectations.get("type"),
            "value": edition_total,
            "evidence": "edition coverage_requirements / fixture_total",
        }

    fixed = expectations.get("value")
    return {
        "type": expectations.get("type"),
        "value": fixed if isinstance(fixed, int) and not isinstance(fixed, bool) else None,
        "evidence": expectations.get("evidence"),
    }


def provider_coverage(sport, league_id):
    """Return the per-provider coverage declaration for a competition."""
    competition = get_competition(sport, league_id)
    if not competition:
        return {}
    coverage = competition.get("provider_coverage")
    return coverage if isinstance(coverage, dict) else {}


def provider_route(sport, league_id, season):
    """
    Return the knowledge-driven acquisition route for one edition.

    Provider eligibility is filtered in the approved order. The route also
    reports edition evidence blockers, so an acquisition attempt can be
    traceable while completion still fails closed for an unverified edition.
    """
    key = _normalize_sport(sport)
    order = provider_order(key)
    coverage = provider_coverage(key, league_id)
    eligible = []
    unsupported = {}
    for provider in order:
        declaration = coverage.get(provider)
        if not isinstance(declaration, dict) or declaration.get("supported") is not True:
            unsupported[provider] = declaration.get("reason") if isinstance(declaration, dict) else "not_declared"
            continue
        edition_seasons = declaration.get("edition_seasons")
        if isinstance(edition_seasons, (list, tuple, set)):
            normalized = set()
            for value in edition_seasons:
                try:
                    normalized.add(int(value))
                except (TypeError, ValueError):
                    continue
            try:
                requested = int(season)
            except (TypeError, ValueError):
                requested = None
            if requested is None or requested not in normalized:
                unsupported[provider] = "verified_edition_unavailable"
                continue
        eligible.append(provider)
    record = edition(key, league_id, season)
    gaps = record.get("evidence_gaps") or []
    return {
        "sport": key,
        "league_id": league_id,
        "season": season,
        "approved_order": order,
        "eligible_providers": eligible,
        "unsupported_providers": unsupported,
        "edition_evidence_status": record.get("evidence_status") or EVIDENCE_UNKNOWN,
        "completion_allowed": not gaps,
        "blockers": list(gaps),
    }


def _recommended_knowledge_action(record, provider_support):
    """Return the next evidence action for one edition row."""
    if record.get("exists") is False:
        return "none; the competition does not stage this edition"
    gaps = record.get("evidence_gaps") or []
    if not gaps:
        return "acquire through the approved provider order, then validate persisted coverage"
    if record.get("is_future"):
        return "refresh future edition facts; do not acquire against an unrefreshed provisional edition"
    if "edition_window" in gaps:
        return "document the edition window from an authoritative source"
    if "authoritative_participant_universe" in gaps:
        return "verify the edition participant roster and canonical aliases"
    if "format_specific_coverage_expectation" in gaps:
        return "document the edition-specific format and coverage total"
    if provider_support and not any(item.get("supported") is True for item in provider_support.values()):
        return "verify an approved provider route before acquisition"
    return "complete the missing evidence before treating the edition as complete"


def knowledge_gap_report(horizon=None, include_not_applicable=True):
    """
    Return a complete, refreshable evidence report for every configured edition.

    The report covers every configured competition from
    ``HISTORICAL_KNOWLEDGE_START`` through the future horizon, plus explicitly
    documented editions outside that range (for example the 2018 World Cup).
    Missing evidence is represented as an explicit gap; no participant list,
    fixture total or provider route is inferred from acquired fixtures.
    """
    horizon = int(horizon or KNOWLEDGE_FUTURE_HORIZON)
    rows = []
    for sport in SPORT_FILES:
        for competition in list_competitions(sport):
            league_id = int(competition.get("league_id"))
            documented = set(edition_years(sport, league_id, include_future=True, future_to=horizon) or [])
            seasons = set(range(HISTORICAL_KNOWLEDGE_START, horizon + 1)) | documented
            provider_support = provider_coverage(sport, league_id)
            for season in sorted(seasons):
                record = edition(sport, league_id, season)
                if record.get("exists") is False and not include_not_applicable:
                    continue
                participant_count = (
                    len(record.get("participants") or [])
                    if record.get("participant_status") == PARTICIPANT_STATUS_VERIFIED
                    else None
                )
                expected = expected_coverage_units(sport, league_id, season)
                source_urls = []
                evidence = record.get("participant_evidence")
                if isinstance(evidence, dict) and evidence.get("source_url"):
                    source_urls.append(evidence.get("source_url"))
                for fact in competition.get("evidence") or []:
                    if isinstance(fact, dict) and fact.get("source_url") and fact.get("source_url") not in source_urls:
                        source_urls.append(fact.get("source_url"))
                rows.append(
                    {
                        "sport": sport,
                        "competition": competition.get("name"),
                        "league_id": league_id,
                        "season": season,
                        "edition_label": record.get("label"),
                        "exists": record.get("exists"),
                        "is_future": bool(record.get("is_future")),
                        "lifecycle_window_status": record.get("status"),
                        "evidence_status": record.get("evidence_status") or EVIDENCE_UNKNOWN,
                        "participant_status": record.get("participant_status") or PARTICIPANT_STATUS_UNKNOWN,
                        "expected_participant_count": participant_count,
                        "expected_fixture_count": expected.get("value"),
                        "fixture_total": record.get("fixture_total"),
                        "format": record.get("format"),
                        "coverage_format": record.get("coverage_format"),
                        "stages": record.get("stages") or [],
                        "provider_order": provider_order(sport),
                        "provider_support": provider_support,
                        "source_urls": source_urls,
                        "evidence_gaps": record.get("evidence_gaps") or [],
                        "recommended_action": _recommended_knowledge_action(record, provider_support),
                    }
                )
    return rows


def refresh_source(sport, source_id, retrieved_at, url=None, confidence=None):
    """
    Refresh one source's retrieval metadata without rewriting edition facts.

    This is the update path for new evidence dates/URLs. It never changes
    participants, fixture totals or lifecycle windows; those remain reviewable
    edition facts. Returns the updated source record.
    """
    key = _normalize_sport(sport)
    source_id = str(source_id or "").strip()
    retrieved_at = str(retrieved_at or "").strip()
    if not source_id or not retrieved_at:
        raise ValueError("source_id and retrieved_at are required")

    for filename in (SPORT_FILES[key], SPORT_EDITION_FILES[key]):
        path = os.path.join(KNOWLEDGE_DIR, filename)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
        if source_id not in sources:
            continue
        source = dict(sources[source_id])
        source["retrieved_at"] = retrieved_at
        if url is not None:
            source["url"] = str(url).strip()
        if confidence is not None:
            source["confidence"] = str(confidence).strip().lower()
        sources[source_id] = source
        payload["sources"] = sources
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        _CACHE.pop(key, None)
        return source
    raise KeyError(f"Source {source_id!r} is not registered for sport {key!r}")


def prediction_context(sport, league_id, season, team_ids=None):
    """Return read-only knowledge context for prediction provenance.

    This never blocks a prediction. It reports whether the edition, roster and
    coverage expectations are confirmed, provisional or unknown, and whether the
    supplied canonical team ids belong to a verified roster when one exists.
    """
    try:
        record = edition(sport, league_id, season)
    except Exception as exc:  # prediction must not fail because catalogue data changed
        return {
            "source": "sports_knowledge",
            "sport": str(sport or "football").lower(),
            "league_id": league_id,
            "season": season,
            "evidence_status": EVIDENCE_UNKNOWN,
            "error": str(exc),
        }
    verified_ids = set(record.get("canonical_participant_ids") or [])
    membership = None
    if team_ids is not None:
        if verified_ids:
            membership = {str(team_id): str(team_id) in verified_ids for team_id in team_ids}
        else:
            membership = {str(team_id): None for team_id in team_ids}
    return {
        "source": "sports_knowledge",
        "sport": record.get("sport"),
        "league_id": record.get("league_id"),
        "season": record.get("season"),
        "edition_label": record.get("label"),
        "is_future": bool(record.get("is_future")),
        "evidence_status": record.get("evidence_status") or EVIDENCE_UNKNOWN,
        "participant_status": record.get("participant_status") or PARTICIPANT_STATUS_UNKNOWN,
        "expected_participant_count": (
            len(record.get("participants") or [])
            if record.get("participant_status") == PARTICIPANT_STATUS_VERIFIED
            else None
        ),
        "expected_fixture_count": expected_coverage_units(sport, league_id, season).get("value"),
        "evidence_gaps": record.get("evidence_gaps") or [],
        "team_membership": membership,
    }


def validate():
    """
    Validate every catalogue file and its consistency with ``config``.

    Returns a list of human-readable problems; an empty list means the catalogue
    is internally coherent and agrees with the runtime configuration that the
    acquisition code reads.
    """
    import config

    problems = []
    seen = set()

    for sport_key in SPORT_FILES:
        try:
            catalogue = load_sport(sport_key)
        except Exception as exc:
            problems.append(f"{sport_key}: catalogue could not be loaded ({exc})")
            continue

        if catalogue.get("schema_version") != KNOWLEDGE_SCHEMA_VERSION:
            problems.append(
                f"{sport_key}: schema_version {catalogue.get('schema_version')!r} != {KNOWLEDGE_SCHEMA_VERSION}"
            )
        if str(catalogue.get("sport")).strip().lower() != sport_key:
            problems.append(f"{sport_key}: catalogue declares sport {catalogue.get('sport')!r}")
        if catalogue.get("edition_schema_version") is not None and catalogue.get("edition_schema_version") != KNOWLEDGE_SCHEMA_VERSION:
            problems.append(
                f"{sport_key}: edition schema_version {catalogue.get('edition_schema_version')!r} != {KNOWLEDGE_SCHEMA_VERSION}"
            )
        if catalogue.get("edition_sport") is not None and str(catalogue.get("edition_sport")).strip().lower() != sport_key:
            problems.append(f"{sport_key}: edition catalogue declares sport {catalogue.get('edition_sport')!r}")

        sources = catalogue.get("sources") if isinstance(catalogue.get("sources"), dict) else {}
        for source_id, source in sources.items():
            if not isinstance(source, dict):
                problems.append(f"{sport_key}: source {source_id!r} is not an object")
                continue
            for field in ("url", "retrieved_at", "confidence"):
                if not isinstance(source.get(field), str) or not str(source.get(field)).strip():
                    problems.append(f"{sport_key}: source {source_id!r} is missing {field}")

        allowed_ids = set(getattr(config, "ALLOWED_LEAGUE_IDS", []) if sport_key == "football" else getattr(config, "ALLOWED_BASKETBALL_LEAGUE_IDS", []))
        alias_map = getattr(config, "LEAGUE_NAME_TO_ID", {}) if sport_key == "football" else {}

        for entry in list_competitions(sport_key):
            league_id = entry.get("league_id")
            for field in _REQUIRED_COMPETITION_FIELDS:
                if field not in entry:
                    problems.append(f"{sport_key} league {league_id}: missing field {field}")
            if league_id in seen:
                problems.append(f"{sport_key}: duplicate league_id {league_id}")
            seen.add(league_id)
            if allowed_ids and league_id not in allowed_ids:
                problems.append(f"{sport_key} league {league_id}: not present in the configured allow-list")

            for alias in entry.get("aliases") or []:
                if not isinstance(alias, str) or not alias.strip():
                    problems.append(f"{sport_key} league {league_id}: invalid alias {alias!r}")
                    continue
                mapped = alias_map.get(alias.strip().lower())
                if mapped is not None and mapped != league_id:
                    problems.append(
                        f"{sport_key} league {league_id}: alias {alias!r} maps to {mapped} in config.LEAGUE_NAME_TO_ID"
                    )

            evidence = entry.get("evidence") or []
            if not isinstance(evidence, list) or not evidence:
                problems.append(f"{sport_key} league {league_id}: no evidence recorded")
            for fact in evidence if isinstance(evidence, list) else []:
                if not isinstance(fact, dict):
                    problems.append(f"{sport_key} league {league_id}: evidence entry is not an object")
                    continue
                for field in ("source_url", "retrieved_at", "confidence"):
                    if not isinstance(fact.get(field), str) or not str(fact.get(field)).strip():
                        problems.append(f"{sport_key} league {league_id}: evidence is missing {field}")

            rule = entry.get("edition_rule") if isinstance(entry.get("edition_rule"), dict) else {}
            if not rule.get("editions") and not (rule.get("from") and rule.get("to")):
                problems.append(f"{sport_key} league {league_id}: edition_rule has neither editions nor a year range")

            known_editions = set(edition_years(sport_key, league_id) or [])
            edition_aliases = {}
            for season_key, facts in (entry.get("editions") or {}).items():
                try:
                    season_year = int(season_key)
                except (TypeError, ValueError):
                    problems.append(f"{sport_key} league {league_id}: invalid edition key {season_key!r}")
                    continue
                if season_year not in known_editions:
                    problems.append(
                        f"{sport_key} league {league_id}: edition {season_year} is not staged by edition_rule"
                    )
                if not isinstance(facts, dict):
                    problems.append(f"{sport_key} league {league_id} season {season_year}: edition record is not an object")
                    continue

                status = str(facts.get("participant_status") or PARTICIPANT_STATUS_UNKNOWN).upper()
                participants = facts.get("participants") or []
                if status == PARTICIPANT_STATUS_VERIFIED and not participants:
                    problems.append(
                        f"{sport_key} league {league_id} season {season_year}: VERIFIED participant status has no participants"
                    )
                if participants and status != PARTICIPANT_STATUS_VERIFIED:
                    problems.append(
                        f"{sport_key} league {league_id} season {season_year}: participants are present but status is {status}"
                    )
                if participants:
                    evidence = facts.get("participant_evidence")
                    if not isinstance(evidence, dict):
                        problems.append(
                            f"{sport_key} league {league_id} season {season_year}: participant evidence is missing"
                        )
                    else:
                        for field in ("source_url", "retrieved_at", "confidence"):
                            if not isinstance(evidence.get(field), str) or not str(evidence.get(field)).strip():
                                problems.append(
                                    f"{sport_key} league {league_id} season {season_year}: participant evidence is missing {field}"
                                )

                    names = set()
                    canonical_ids = set()
                    for participant in participants:
                        if not isinstance(participant, dict):
                            problems.append(
                                f"{sport_key} league {league_id} season {season_year}: participant is not an object"
                            )
                            continue
                        name = str(participant.get("name") or "").strip()
                        canonical_id = str(participant.get("canonical_id") or "").strip()
                        if not name:
                            problems.append(
                                f"{sport_key} league {league_id} season {season_year}: participant has no name"
                            )
                        normalized = _canonical_slug(name)
                        if normalized in names:
                            problems.append(
                                f"{sport_key} league {league_id} season {season_year}: duplicate participant name {name!r}"
                            )
                        names.add(normalized)
                        if not canonical_id.startswith(f"{sport_key}_team_"):
                            problems.append(
                                f"{sport_key} league {league_id} season {season_year}: invalid canonical id {canonical_id!r}"
                            )
                        if canonical_id in canonical_ids:
                            problems.append(
                                f"{sport_key} league {league_id} season {season_year}: duplicate canonical id {canonical_id!r}"
                            )
                        canonical_ids.add(canonical_id)
                        for alias in [name] + list(participant.get("aliases") or []):
                            if not isinstance(alias, str) or not alias.strip():
                                continue
                            alias_key = _canonical_slug(alias)
                            previous = edition_aliases.get(alias_key)
                            if previous and previous != canonical_id:
                                problems.append(
                                    f"{sport_key} league {league_id}: alias {alias!r} resolves to both {previous} and {canonical_id}"
                                )
                            edition_aliases[alias_key] = canonical_id

                    requirements = facts.get("coverage_requirements")
                    if not isinstance(requirements, dict):
                        problems.append(
                            f"{sport_key} league {league_id} season {season_year}: coverage_requirements is missing"
                        )
                        requirements = {}
                    expected_count = requirements.get("expected_participant_count")
                    if expected_count is not None and expected_count != len(participants):
                        problems.append(
                            f"{sport_key} league {league_id} season {season_year}: expected participant count "
                            f"{expected_count!r} does not match {len(participants)} recorded participants"
                        )
                    fixture_total = facts.get("fixture_total")
                    if fixture_total is not None and not (
                        isinstance(fixture_total, int) and not isinstance(fixture_total, bool) and fixture_total > 0
                    ):
                        problems.append(
                            f"{sport_key} league {league_id} season {season_year}: invalid fixture_total {fixture_total!r}"
                        )
                    expected_fixtures = requirements.get("expected_fixture_count")
                    if expected_fixtures is not None and expected_fixtures != fixture_total:
                        problems.append(
                            f"{sport_key} league {league_id} season {season_year}: expected fixture count "
                            f"{expected_fixtures!r} does not match fixture_total {fixture_total!r}"
                        )
                    if entry.get("coverage_format") == "double_round_robin" and fixture_total is not None:
                        calculated = len(participants) * (len(participants) - 1)
                        if fixture_total != calculated:
                            problems.append(
                                f"{sport_key} league {league_id} season {season_year}: double round robin total "
                                f"{fixture_total} does not match {len(participants)} teams ({calculated})"
                            )
                    catalogued_total = expected_coverage_units(sport_key, league_id, season_year).get("value")
                    if catalogued_total is not None and fixture_total is not None and catalogued_total != fixture_total:
                        problems.append(
                            f"{sport_key} league {league_id} season {season_year}: fixture_total {fixture_total} "
                            f"disagrees with expected_units {catalogued_total}"
                        )

                stages = facts.get("stages")
                if stages is not None and not isinstance(stages, list):
                    problems.append(
                        f"{sport_key} league {league_id} season {season_year}: stages is not a list"
                    )
                stage_total = 0
                stage_total_known = bool(stages)
                for stage in stages or []:
                    if not isinstance(stage, dict):
                        problems.append(
                            f"{sport_key} league {league_id} season {season_year}: stage is not an object"
                        )
                        stage_total_known = False
                        continue
                    if not str(stage.get("id") or "").strip():
                        problems.append(
                            f"{sport_key} league {league_id} season {season_year}: stage has no id"
                        )
                    count = stage.get("fixture_count")
                    if isinstance(count, int) and not isinstance(count, bool):
                        stage_total += count
                    else:
                        stage_total_known = False
                if stage_total_known and isinstance(facts.get("fixture_total"), int) and stage_total != facts["fixture_total"]:
                    problems.append(
                        f"{sport_key} league {league_id} season {season_year}: stage fixture counts "
                        f"sum to {stage_total}, not {facts['fixture_total']}"
                    )

            for provider_name, declaration in (entry.get("provider_coverage") or {}).items():
                if not isinstance(declaration, dict):
                    problems.append(f"{sport_key} league {league_id}: provider_coverage[{provider_name}] is not an object")
                    continue
                if declaration.get("supported") is True and not isinstance(declaration.get("code"), str):
                    if provider_name in ("soccerdata_fbref", "soccerdata_match_history", "soccerdata_sofascore", "football_data_csv"):
                        problems.append(
                            f"{sport_key} league {league_id}: provider {provider_name} is marked supported without a code"
                        )

            # Config agreement: a competition with a declared runtime window must
            # agree with the catalogue's materialised window for that season.
            windows = getattr(config, "COMPETITION_SEASON_WINDOWS", {}) or {}
            overrides = getattr(config, "COMPETITION_SEASON_WINDOW_OVERRIDES", {}) or {}
            generic = windows.get((sport_key, league_id))
            for season_year in edition_years(sport_key, league_id) or []:
                config_window = overrides.get((sport_key, league_id, season_year))
                if not isinstance(config_window, dict):
                    config_window = generic
                if not isinstance(config_window, dict):
                    # No runtime window declared for this competition/season: the
                    # catalogue may still document it, and nothing needs to agree.
                    continue
                record = edition(sport_key, league_id, season_year)
                if record.get("status") != "KNOWN":
                    problems.append(
                        f"{sport_key} league {league_id} season {season_year}: config declares a window but the catalogue does not"
                    )
                    continue
                configured = _window_from_offsets(season_year, config_window)
                if configured is None:
                    continue
                start, end = configured
                if record.get("start") != start.isoformat() or record.get("end") != end.isoformat():
                    problems.append(
                        f"{sport_key} league {league_id} season {season_year}: catalogue window "
                        f"({record.get('start')}..{record.get('end')}) disagrees with config "
                        f"({start.isoformat()}..{end.isoformat()})"
                    )

    # Basketball expected regular-season totals must never exceed what the runtime
    # configuration independently established.
    nba = get_competition("basketball", 12)
    if nba:
        expectations = nba.get("expected_units") if isinstance(nba.get("expected_units"), dict) else {}
        per_edition = expectations.get("per_edition") if isinstance(expectations.get("per_edition"), dict) else {}
        runtime_expected = getattr(config, "NBA_EXPECTED_MIN_REGULAR_SEASON_GAMES", {}) or {}
        for season_key, value in per_edition.items():
            try:
                season_int = int(season_key)
            except (TypeError, ValueError):
                problems.append(f"basketball: invalid expected_units.per_edition key {season_key!r}")
                continue
            runtime_value = runtime_expected.get(season_int)
            if value is None:
                if runtime_value is not None:
                    problems.append(
                        f"basketball season {season_int}: catalogue has no expectation but config declares {runtime_value}"
                    )
                continue
            if runtime_value != value:
                problems.append(
                    f"basketball season {season_int}: catalogue expectation {value} disagrees with config {runtime_value!r}"
                )
        for season_int, runtime_value in runtime_expected.items():
            if str(season_int) not in per_edition:
                problems.append(
                    f"basketball season {season_int}: config declares {runtime_value} but the catalogue has no entry"
                )

    # Documented cup/tournament match totals must agree between the catalogue and
    # the runtime configuration that coverage validation reads. A disagreement
    # would let coverage be judged against a number the catalogue does not support.
    runtime_tournament = getattr(config, "FOOTBALL_TOURNAMENT_COVERAGE_EXPECTATIONS", {}) or {}
    for (sport_key, league_id, season_year), entry in runtime_tournament.items():
        runtime_value = entry.get("expected_fixture_count") if isinstance(entry, dict) else None
        catalogued = expected_coverage_units(sport_key, league_id, season_year)
        if catalogued.get("value") is None or catalogued.get("value") != runtime_value:
            problems.append(
                f"{sport_key} league {league_id} season {season_year}: config expects {runtime_value!r} "
                f"fixtures but the catalogue records {catalogued.get('value')!r}"
            )

    return problems
