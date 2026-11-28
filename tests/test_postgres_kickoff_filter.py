"""PostgreSQL verification of the local-calendar-date kickoff filter.

The lifecycle date filter is executed on both backends, so SQLite-only proof is
not enough. This module provisions a **throwaway** PostgreSQL (never Neon, never
production) through the optional ``pgserver`` package, applies the reviewed
migration, and asserts that the PostgreSQL query selects exactly the same
fixture identities as the SQLite implementation for the same local dates.

``pgserver`` is not a runtime dependency: where it is not installed the module
skips instead of pretending the check ran.
"""

import os

import pytest

pgserver = pytest.importorskip(
    "pgserver", reason="install the optional 'pgserver' package for disposable PostgreSQL verification"
)

import datetime as dt  # noqa: E402

import config  # noqa: E402
import lifecycle_storage as lstore  # noqa: E402
import storage  # noqa: E402
import time_utils  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations", "phase2_lifecycle_postgres.sql")


@pytest.fixture()
def postgres_backend(tmp_path, monkeypatch):
    server = pgserver.get_server(str(tmp_path / "pgdata"), cleanup_mode="stop")
    uri = server.get_uri()
    monkeypatch.setattr(config, "NEON_DATABASE_URL", uri)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "REQUIRE_NEON", False)
    storage.init_db()
    storage.init_basketball_db()
    with open(MIGRATION, encoding="utf-8") as handle:
        ddl = handle.read()
    conn, db_type = storage._connect()
    assert db_type == "postgres"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            version = cur.fetchone()[0]
            cur.execute(ddl)
        conn.commit()
    finally:
        conn.close()
    lstore.ensure_schema()
    yield version
    server.cleanup()


def _seed(lifecycle_id, kickoff_at):
    return lstore.upsert_lifecycle(
        {
            "lifecycle_id": f"football:api_football:{lifecycle_id}",
            "sport": "football",
            "canonical_fixture_key": f"api_football:{lifecycle_id}",
            "provider": "api_football",
            "provider_fixture_id": str(7000 + len(lifecycle_id)),
            "league_id": 39,
            "season": 2026,
            "home_team_id": "arsenal",
            "away_team_id": "chelsea",
            "kickoff_at": kickoff_at,
            "stage": "TRACKED",
            "provider_status": "NS",
        }
    )


def _expected_ids(local_date):
    return sorted(
        row["lifecycle_id"]
        for row in lstore.list_lifecycles(limit=100)
        if time_utils.in_local_day(row["kickoff_at"], local_date)
    )


FIXTURES = {
    "utc_eve": "2026-11-27T22:59:59+00:00",         # 23:59:59 Lagos on the 27th
    "lagos_midnight": "2026-11-27T23:00:00+00:00",  # 00:00 Lagos on the 28th
    "plus_two": "2026-11-28T01:30:00+02:00",        # 00:30 Lagos on the 28th
    "utc_late": "2026-11-28T22:59:59+00:00",        # 23:59:59 Lagos on the 28th
    "lagos_end": "2026-11-28T23:00:00+00:00",       # 00:00 Lagos on the 29th
    "minus_three": "2026-11-27T21:00:00-03:00",     # 01:00 Lagos on the 28th
}


def test_postgres_selects_the_same_local_dates_as_sqlite(postgres_backend):
    assert "PostgreSQL" in postgres_backend
    for name, kickoff in FIXTURES.items():
        _seed(name, kickoff)

    # A legacy row written outside the application keeps a non-UTC offset; a
    # malformed row must never match. Both are inserted verbatim.
    conn, _ = storage._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO live_match_lifecycle (lifecycle_id, sport, canonical_fixture_key, provider, "
                "provider_fixture_id, league_id, season, home_team_id, away_team_id, kickoff_at, stage, "
                "provider_status, first_discovered_at, created_at, updated_at) VALUES "
                "('football:api_football:legacy_offset','football','api_football:legacy_offset','api_football',"
                "'6101',39,2026,'arsenal','chelsea','2026-11-27T20:00:00-05:00','TRACKED','NS',"
                "now()::text,now()::text,now()::text),"
                "('football:api_football:malformed','football','api_football:malformed','api_football',"
                "'6102',39,2026,'arsenal','chelsea','not-a-timestamp','TRACKED','NS',"
                "now()::text,now()::text,now()::text)"
            )
        conn.commit()
    finally:
        conn.close()

    assert [row["lifecycle_id"] for row in lstore.list_lifecycles(kickoff_on="2026-11-27")] == [
        "football:api_football:utc_eve"
    ]
    # Ordering follows the stored text column, so compare selected identities as
    # sets: selection, not tie order, is what the date filter decides.
    assert sorted(row["lifecycle_id"] for row in lstore.list_lifecycles(kickoff_on="2026-11-28")) == [
        "football:api_football:lagos_midnight",
        "football:api_football:legacy_offset",
        "football:api_football:minus_three",
        "football:api_football:plus_two",
        "football:api_football:utc_late",
    ]
    assert [row["lifecycle_id"] for row in lstore.list_lifecycles(kickoff_on="2026-11-29")] == [
        "football:api_football:lagos_end"
    ]
    # Never selected, on any date in the matrix.
    for local_date in ("2026-11-27", "2026-11-28", "2026-11-29"):
        assert "football:api_football:malformed" not in _expected_ids(local_date)
        assert sorted(row["lifecycle_id"] for row in lstore.list_lifecycles(kickoff_on=local_date)) == _expected_ids(
            local_date
        )


def test_postgres_half_open_window_boundaries(postgres_backend):
    for name, kickoff in FIXTURES.items():
        _seed(name, kickoff)

    # kickoff_from is inclusive of the local midnight: the 00:00 Lagos fixture is
    # the first row of 2026-11-28.
    from_ids = [row["lifecycle_id"] for row in lstore.list_lifecycles(kickoff_from="2026-11-28", limit=50)]
    assert "football:api_football:lagos_midnight" in from_ids
    assert "football:api_football:utc_eve" not in from_ids
    assert "football:api_football:lagos_end" in from_ids

    # kickoff_to is exclusive of the next local midnight: the 00:00 Lagos fixture
    # on the 29th belongs to the next day and is excluded.
    to_ids = [row["lifecycle_id"] for row in lstore.list_lifecycles(kickoff_to="2026-11-28", limit=50)]
    assert "football:api_football:utc_late" in to_ids
    assert "football:api_football:lagos_end" not in to_ids
    assert sorted(to_ids) == sorted(
        [row["lifecycle_id"] for row in lstore.list_lifecycles(kickoff_on="2026-11-27")]
        + [row["lifecycle_id"] for row in lstore.list_lifecycles(kickoff_on="2026-11-28")]
    )


def test_postgres_stores_kickoff_instants_in_utc(postgres_backend):
    row = _seed("offset_write", "2026-11-28T01:30:00+02:00")
    assert row["kickoff_at"] == "2026-11-27T23:30:00+00:00"
    stored = lstore.get_lifecycle("football:api_football:offset_write")
    assert stored["kickoff_at"] == "2026-11-27T23:30:00+00:00"
    assert time_utils.local_calendar_date(stored["kickoff_at"]) == dt.date(2026, 11, 28)
