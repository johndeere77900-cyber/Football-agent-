"""Explicit, field-level missing-data classification for enrichment.

Historical and pre-match enrichment must never collapse every absent value into
"missing statistics". This module gives every field an explainable state so a
provider limitation can be told apart from a parser bug, an ambiguous fixture,
or data that simply has not been acquired yet.

States are stable machine-readable strings and are safe to persist in
provenance JSON.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

PRESENT = "present"

# The observation exists but the provider does not publish this field.
PROVIDER_FIELD_ABSENT = "provider_does_not_supply_field"
# The provider only started publishing the field after this historical period.
PROVIDER_PREDATES_COVERAGE = "provider_predates_field_coverage"
NO_MATCHING_FIXTURE = "no_matching_fixture"
AMBIGUOUS_IDENTITY = "ambiguous_identity"
MISSING_REQUEST_EVIDENCE = "missing_request_evidence"
PROVIDER_REQUEST_FAILED = "provider_request_failed"
STATISTICS_BLOCK_UNAVAILABLE = "statistics_block_unavailable"
PARSER_FAILED = "parser_failed"
NOT_ACQUIRED = "not_acquired"
POLICY_EXCLUDED = "intentionally_excluded_by_policy"
CONFLICTING_OBSERVATIONS = "conflicting_observations"

# Legacy reason codes used by the existing enrichment modules.
_REASON_MAP = {
    "provider_absent": PROVIDER_FIELD_ABSENT,
    "not_supplied": PROVIDER_FIELD_ABSENT,
    "unsupported_field": PROVIDER_FIELD_ABSENT,
    "not_covered": PROVIDER_PREDATES_COVERAGE,
    "before_coverage": PROVIDER_PREDATES_COVERAGE,
    "no_fixture": NO_MATCHING_FIXTURE,
    "unmatched": NO_MATCHING_FIXTURE,
    "ambiguous": AMBIGUOUS_IDENTITY,
    "identity_ambiguous": AMBIGUOUS_IDENTITY,
    "no_request": MISSING_REQUEST_EVIDENCE,
    "request_failed": PROVIDER_REQUEST_FAILED,
    "http_error": PROVIDER_REQUEST_FAILED,
    "block_unavailable": STATISTICS_BLOCK_UNAVAILABLE,
    "no_statistics": STATISTICS_BLOCK_UNAVAILABLE,
    "parser_error": PARSER_FAILED,
    "not_acquired": NOT_ACQUIRED,
    "policy": POLICY_EXCLUDED,
    "excluded": POLICY_EXCLUDED,
    "conflict": CONFLICTING_OBSERVATIONS,
}

ALL_STATES = (
    PRESENT,
    PROVIDER_FIELD_ABSENT,
    PROVIDER_PREDATES_COVERAGE,
    NO_MATCHING_FIXTURE,
    AMBIGUOUS_IDENTITY,
    MISSING_REQUEST_EVIDENCE,
    PROVIDER_REQUEST_FAILED,
    STATISTICS_BLOCK_UNAVAILABLE,
    PARSER_FAILED,
    NOT_ACQUIRED,
    POLICY_EXCLUDED,
    CONFLICTING_OBSERVATIONS,
)


def state_for_reason(reason: Optional[str]) -> str:
    """Translate a legacy/free-text reason into a stable classification state."""
    if not reason:
        return NOT_ACQUIRED
    return _REASON_MAP.get(str(reason).strip().lower(), NOT_ACQUIRED)


def describe(
    field: str,
    state: str,
    *,
    detail: Optional[str] = None,
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """One field-level outcome record suitable for provenance JSON."""
    return {
        "field": field,
        "state": state,
        "detail": detail,
        "provider": provider,
    }


def classify_fields(
    fields: Dict[str, Any],
    expected: Iterable[str],
    *,
    provider: Optional[str] = None,
    block_available: bool = True,
    absent_fields: Iterable[str] = (),
    failed_fields: Optional[Dict[str, str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Classify each expected field as present or absent with an explicit state.

    ``absent_fields`` marks fields the provider is known not to publish.
    ``failed_fields`` maps a field to a specific failure reason.
    """
    absent = {str(f) for f in absent_fields}
    failures = {str(k): str(v) for k, v in (failed_fields or {}).items()}
    outcomes: Dict[str, Dict[str, Any]] = {}
    for field in expected:
        value = fields.get(field) if isinstance(fields, dict) else None
        if value is not None and value != "" and value != []:
            outcomes[field] = describe(field, PRESENT, provider=provider)
        elif field in failures:
            outcomes[field] = describe(field, state_for_reason(failures[field]), detail=failures[field], provider=provider)
        elif not block_available:
            outcomes[field] = describe(field, STATISTICS_BLOCK_UNAVAILABLE, provider=provider)
        elif field in absent:
            outcomes[field] = describe(field, PROVIDER_FIELD_ABSENT, provider=provider)
        else:
            outcomes[field] = describe(field, PROVIDER_FIELD_ABSENT, provider=provider)
    return outcomes


def coverage_summary(outcomes: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """Count field outcomes by state for metrics and diagnostics."""
    summary: Dict[str, int] = {}
    for outcome in (outcomes or {}).values():
        state = outcome.get("state") or NOT_ACQUIRED
        summary[state] = summary.get(state, 0) + 1
    return summary


def unavailable_states(states: Iterable[str]) -> List[str]:
    return [str(s) for s in states if str(s) in ALL_STATES and str(s) != PRESENT]
