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

KNOWLEDGE_SCHEMA_VERSION = 1

SPORT_FILES = {
    "football": "football_competitions.json",
    "basketball": "basketball_competitions.json",
}

KNOWLEDGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sports_knowledge")

# Acquisition/verification status vocabulary shared with the Phase 1 inventory.
STATUS_EXPECTED = "EXPECTED"
STATUS_UPCOMING = "UPCOMING"
STATUS_ONGOING = "ONGOING"
STATUS_COMPLETED = "COMPLETED"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

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

    sources = raw.get("sources") if isinstance(raw.get("sources"), dict) else {}
    competitions = []
    for entry in raw.get("competitions") or []:
        if not isinstance(entry, dict):
            continue
        resolved = dict(entry)
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


def edition_years(sport, league_id):
    """
    Return the edition start years a competition stages, or None if unknown.

    ``annual`` rules return the configured inclusive range; edition-based
    competitions return their declared editions only.
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
        return sorted(set(years))

    try:
        first = int(rule.get("from"))
        last = int(rule.get("to"))
    except (TypeError, ValueError):
        return None
    if last < first:
        return None
    return list(range(first, last + 1))


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
    exists = ssn in years
    rule = competition.get("edition_rule") if isinstance(competition.get("edition_rule"), dict) else {}
    record = {
        "sport": _normalize_sport(sport),
        "league_id": int(competition.get("league_id")),
        "season": ssn,
        "exists": exists,
        "name": competition.get("name"),
        "format": competition.get("format"),
        "coverage_format": competition.get("coverage_format"),
        "status": STATUS_UNKNOWN,
        "start": None,
        "end": None,
        "evidence": None,
        "label": None,
    }

    if not exists:
        record["status"] = STATUS_NOT_APPLICABLE
        return record

    label_style = rule.get("label")
    record["label"] = f"{ssn}/{ssn + 1}" if label_style == "split_year" else str(ssn)

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
            return record
        # An explicitly undated edition stays UNKNOWN; it is never given a
        # guessed window.
        return record

    window = _window_from_offsets(ssn, rule.get("window"))
    if window is None:
        return record
    start, end = window
    record.update(
        {
            "status": "KNOWN",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "evidence": rule.get("window_evidence"),
        }
    )
    return record


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
