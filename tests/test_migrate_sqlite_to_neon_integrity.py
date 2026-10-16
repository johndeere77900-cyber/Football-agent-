"""
Unit tests for SQLite to Neon PostgreSQL migration integrity (Phase 5).
"""

from unittest.mock import patch, MagicMock
import sqlite3
import pytest

import config
import migrate_sqlite_to_neon
import storage


@pytest.fixture(autouse=True)
def init_test_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_migration_integrity.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    storage.init_db()
    return db_file


def test_dry_run_performs_no_writes(init_test_db):
    """
    1. dry_run=True performs no target writes and returns DRY_RUN_PASSED.
    """
    report = migrate_sqlite_to_neon.migrate(
        sqlite_path=str(init_test_db),
        target_url="postgresql://fake:fake@localhost:5432/fakedb",
        dry_run=True,
    )

    assert report["verification"] == "DRY_RUN_PASSED"
    assert "team_identities" in report
    assert "prediction_snapshots" in report
    assert "operation_requests" in report


def test_verify_only_performs_no_writes(init_test_db, monkeypatch):
    """
    2. verify_only=True performs no target writes and validates source vs destination.
    """
    mock_pg_conn = MagicMock()
    mock_cur = MagicMock()
    mock_pg_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_cur.fetchall.return_value = []

    executed_sqls = []
    def track_execute(sql, *args, **kwargs):
        executed_sqls.append(sql)

    mock_cur.execute.side_effect = track_execute

    monkeypatch.setattr("psycopg.connect", lambda url: mock_pg_conn)

    with patch("storage.init_db") as mock_init_db:
        report = migrate_sqlite_to_neon.migrate(
            sqlite_path=str(init_test_db),
            target_url="postgresql://fake:fake@localhost:5432/fakedb",
            verify_only=True,
        )
        mock_init_db.assert_not_called()

    assert report["verification"] == "VERIFIED_ALL_DATASETS"
    assert "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY" in executed_sqls
