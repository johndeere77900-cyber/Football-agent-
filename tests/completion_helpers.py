"""
Helpers for historical dataset completion in tests.

The storage completion contract (``storage.mark_historical_dataset_complete``)
requires real persisted records, internally consistent provider coverage with at
least one VALIDATED outcome, and an independently COMPLETED competition
lifecycle. Two helpers keep existing tests honest without weakening that
contract:

``mark_complete``
    Used by tests that intend a genuinely valid completed dataset. It derives the
    provider coverage evidence from the records the test actually persisted and
    passes it through the real, unmodified completion guard.

``force_manifest``
    Used by tests that must simulate a pre-existing (legacy, stale or corrupt)
    manifest row - for example "a COMPLETE manifest whose fixtures are missing".
    It writes the manifest row directly, exactly like a manifest produced by an
    older version, so the production revalidation path can be tested against it.
    It never weakens the guard: the guard simply is not the behaviour under test.
"""

import inspect
import json
import sqlite3

import config
import storage

__all__ = [
    "allow_synthetic_nba_season",
    "force_manifest",
    "mark_complete",
    "provider_coverage",
]

_COMPLETE_SIGNATURE = inspect.signature(storage.mark_historical_dataset_complete)


def provider_coverage(
    source=None,
    sport="football",
    expected=1,
    completed=None,
    valid_empty_units=0,
    status="VALIDATED",
    coverage_unit="pages",
):
    """Build an internally consistent per-provider coverage record."""
    if source is None:
        source = "api_basketball" if str(sport).lower() == "basketball" else "api_football"
    expected = int(expected or 1)
    if completed is None:
        completed = expected
    return {
        source: {
            "coverage_unit": coverage_unit,
            "expected": expected,
            "completed": int(completed),
            "valid_empty_units": int(valid_empty_units or 0),
            "status": status,
        }
    }


def mark_complete(*args, **kwargs):
    """
    Mark a dataset COMPLETE through the real guard, supplying the coverage
    evidence the acquisition path would have recorded for the persisted records.
    """
    bound = _COMPLETE_SIGNATURE.bind(*args, **kwargs)
    bound.apply_defaults()
    arguments = bound.arguments

    sport = (arguments.get("sport") or "football").lower()
    arguments["sport"] = sport

    if not arguments.get("provider_coverage"):
        arguments["provider_coverage"] = provider_coverage(
            source=arguments.get("source"),
            sport=sport,
            expected=arguments.get("expected_pages") or 1,
            completed=arguments.get("pages_completed"),
            valid_empty_units=arguments.get("empty_pages_count") or 0,
            coverage_unit=arguments.get("coverage_unit") or "pages",
        )

    return storage.mark_historical_dataset_complete(**arguments)


def allow_synthetic_nba_season(monkeypatch, season, games):
    """
    Bound the configured independent NBA season expectation to a synthetic fixture
    set. Production keeps the published totals (config.NBA_EXPECTED_MIN_REGULAR_SEASON_GAMES);
    a test that persists only a handful of games states its own scale explicitly
    instead of the production minimum being loosened.
    """
    expected = dict(getattr(config, "NBA_EXPECTED_MIN_REGULAR_SEASON_GAMES", {}) or {})
    expected[season] = int(games)
    monkeypatch.setattr(config, "NBA_EXPECTED_MIN_REGULAR_SEASON_GAMES", expected, raising=False)


_MANIFEST_COLUMNS = (
    "sport",
    "league_id",
    "season",
    "status",
    "fixture_count",
    "expected_pages",
    "pages_completed",
    "acquisition_complete",
    "enrichment_status",
    "rejected_count",
    "empty_pages_count",
    "completed_at",
    "updated_at",
    "source",
    "error_reason",
    "expected_team_ids_json",
    "coverage_unit",
    "provider_coverage_json",
)


def force_manifest(
    league_id,
    season,
    sport="football",
    status="COMPLETE",
    fixture_count=0,
    expected_pages=1,
    pages_completed=1,
    acquisition_complete=True,
    enriched=False,
    rejected_count=0,
    empty_pages_count=0,
    source=None,
    error_reason=None,
    expected_team_ids=None,
    coverage_unit="pages",
    provider_coverage=None,
):
    """
    Write a historical_datasets manifest row directly, simulating a manifest
    produced by an earlier version or by an interrupted run.
    """
    sport = str(sport).lower()
    if source is None:
        source = "api_basketball" if sport == "basketball" else "api_football"

    now = storage._utc_now()
    values = (
        sport,
        int(league_id),
        int(season),
        str(status),
        int(fixture_count or 0),
        int(expected_pages or 0),
        int(pages_completed or 0),
        1 if acquisition_complete else 0,
        "COMPLETE" if enriched else "NONE",
        int(rejected_count or 0),
        int(empty_pages_count or 0),
        now if status == "COMPLETE" else None,
        now,
        source,
        error_reason,
        json.dumps(expected_team_ids) if expected_team_ids is not None else None,
        coverage_unit,
        json.dumps(provider_coverage) if provider_coverage is not None else None,
    )

    placeholders = ", ".join(["?"] * len(_MANIFEST_COLUMNS))
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in _MANIFEST_COLUMNS
        if column not in ("sport", "league_id", "season")
    )

    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            f"INSERT INTO historical_datasets ({', '.join(_MANIFEST_COLUMNS)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (sport, league_id, season) DO UPDATE SET {updates}",
            values,
        )
        conn.commit()
    finally:
        conn.close()
