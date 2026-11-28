"""PostgreSQL verification of the local-calendar-date kickoff filter.

The lifecycle date filter is executed on both backends, so SQLite-only proof is
not enough. This module runs against a **disposable** PostgreSQL (never Neon,
never production) and asserts that the PostgreSQL query selects exactly the same
fixture identities as the SQLite implementation for the same local dates.

Two provisioning paths exist, and neither one is a silent skip in CI:

* ``TEST_POSTGRES_DSN`` points at a disposable server, which is how the
  dedicated ``postgres`` CI job runs (a pinned ``postgres:16`` service). The job
  fails if the server is unreachable, so PostgreSQL coverage cannot disappear.
* Without that variable, the optional ``pgserver`` package starts a throwaway
  local cluster, so a developer can run the same checks offline. Where it is not
  installed the module skips and the CI job is the authoritative check.
"""

import datetime
import os
import uuid

import pytest

import config
import lifecycle_storage as lstore
import storage
import time_utils

DSN_ENV = "TEST_POSTGRES_DSN"

MIGRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migrations",
    "phase2_lifecycle_postgres.sql",
)


def _admin_dsn():
    return os.environ.get(DSN_ENV) or ""


def _create_database(admin_dsn):
    """Create a fresh database so tests cannot see each other's rows."""
    import psycopg
    from psycopg import conninfo

    name = "kickoff_filter_" + uuid.uuid4().hex[:12]
    with psycopg.connect(admin_dsn, connect_timeout=15, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    return name, conninfo.make_conninfo(admin_dsn, dbname=name)


def _drop_database(admin_dsn, name):
    import psycopg

    with psycopg.connect(admin_dsn, connect_timeout=15, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture()
def postgres_backend(tmp_path, monkeypatch):
    import psycopg

    admin_dsn = _admin_dsn()
    server = None
    created_database = None
    if admin_dsn:
        # CI path: a real, pinned PostgreSQL service. Connectivity is required,
        # so an unavailable environment is a failure and never a skip.
        psycopg.connect(admin_dsn, connect_timeout=15).close()
        created_database, uri = _create_database(admin_dsn)
    else:
        pgserver = pytest.importorskip(
            "pgserver",
            reason=(
                "set TEST_POSTGRES_DSN (the CI postgres job does) or install the optional "
                "'pgserver' package for disposable PostgreSQL verification"
            ),
        )
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
    try:
        yield version
    finally:
        if created_database:
            _drop_database(admin_dsn, created_database)
        elif server is not None:
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


def _insert_legacy_row(lifecycle_id, kickoff_at):
    """Insert a row verbatim, as an older writer would have left it."""
    conn, _ = storage._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO live_match_lifecycle (lifecycle_id, sport, canonical_fixture_key, provider, "
                "provider_fixture_id, league_id, season, home_team_id, away_team_id, kickoff_at, stage, "
                "provider_status, first_discovered_at, created_at, updated_at) VALUES "
                "(%s,'football',%s,'api_football','6101',39,2026,'arsenal','chelsea',%s,'TRACKED','NS',"
                "now()::text,now()::text,now()::text)",
                (f"football:api_football:{lifecycle_id}", f"api_football:{lifecycle_id}", kickoff_at),
            )
        conn.commit()
    finally:
        conn.close()


def _selected(local_date, limit=100):
    return sorted(row["lifecycle_id"] for row in lstore.list_lifecycles(kickoff_on=local_date, limit=limit))


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
    # naive and a malformed row must never match on either backend.
    _insert_legacy_row("legacy_offset", "2026-11-27T20:00:00-05:00")
    _insert_legacy_row("legacy_naive", "2026-11-28T00:30:00")
    _insert_legacy_row("malformed", "not-a-timestamp")

    assert _selected("2026-11-27") == [
        "football:api_football:utc_eve"
    ]
    # Ordering follows the stored text column, so compare selected identities as
    # sets: selection, not tie order, is what the date filter decides.
    assert _selected("2026-11-28") == [
        "football:api_football:lagos_midnight",
        "football:api_football:legacy_offset",
        "football:api_football:minus_three",
        "football:api_football:plus_two",
        "football:api_football:utc_late",
    ]
    assert _selected("2026-11-29") == ["football:api_football:lagos_end"]
    # Never selected, on any date in the matrix.
    for local_date in ("2026-11-27", "2026-11-28", "2026-11-29"):
        assert "football:api_football:malformed" not in _expected_ids(local_date)
        assert "football:api_football:legacy_naive" not in _expected_ids(local_date)
        assert _selected(local_date) == _expected_ids(local_date)


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
    assert time_utils.local_calendar_date(stored["kickoff_at"]) == datetime.date(2026, 11, 28)
    # The write boundary rejects an ambiguous kickoff before it can be stored.
    with pytest.raises(ValueError, match="explicit UTC offset"):
        _seed("naive_write", "2026-11-28T00:30:00")
    assert lstore.get_lifecycle("football:api_football:naive_write") is None


def test_postgres_session_timezone_does_not_change_selection(postgres_backend, monkeypatch):
    """The session timezone must not be able to read a stored kickoff.

    A naive legacy value previously followed the session timezone through
    ``::timestamptz``, so the same row could land on a different local day on a
    different server. The filter now requires the offset in the text, and this
    check proves the selection is identical under a shifted session.
    """
    from psycopg import conninfo

    for name, kickoff in FIXTURES.items():
        _seed(name, kickoff)
    _insert_legacy_row("legacy_naive", "2026-11-28T00:30:00")

    baseline = {local_date: _selected(local_date) for local_date in ("2026-11-27", "2026-11-28", "2026-11-29")}

    shifted_dsn = conninfo.make_conninfo(
        config.NEON_DATABASE_URL, options="-c timezone=America/New_York"
    )
    monkeypatch.setattr(config, "NEON_DATABASE_URL", shifted_dsn)
    conn, _ = storage._connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TimeZone;")
            assert cur.fetchone()[0] == "America/New_York"
    finally:
        conn.close()

    for local_date, expected in baseline.items():
        assert _selected(local_date) == expected
    assert "football:api_football:legacy_naive" not in _selected("2026-11-28")
