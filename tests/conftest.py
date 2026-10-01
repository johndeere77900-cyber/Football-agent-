import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
import config
import storage


@pytest.fixture(autouse=True)
def isolate_test_db(tmp_path, monkeypatch):
    """
    Ensure every test runs against a clean, isolated temporary database and cache directory.
    This prevents cross-test contamination and ensures persistent DB cache hits/misses are deterministic.
    """
    db_file = tmp_path / "test_isolated.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()
