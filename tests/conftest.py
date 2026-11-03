import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
import config
import storage


# Legacy acquisition suites were written against the retired CSV -> SoccerData
# chain. They exercise resolver integration, not the new OpenFootball/OpenLigaDB
# stages, so those stages are stubbed for those modules only. Tests that patch a
# provider explicitly override these defaults. This keeps the suites offline and
# prevents a provider timeout from being mistaken for an assertion failure.
_LEGACY_RESOLVER_MODULES = {
    "test_historical_sync",
    "test_historical_completion_integrity",
    "test_historical_hardening",
    "test_historical_ingestion_integrity",
    "test_p0_p1_integrity_repair",
    "test_phase3_production_integration",
    "test_direct_csv_and_resiliency",
    "test_provider_neutral_identity",
    "test_resume_queue_and_cli",
    "test_rolling_backtest_window",
    "test_database_first_production",
    "test_efficiency_and_telegram",
    "test_data_resolver",
    "test_soccerdata_provider",
}

# These modules do not test the SoccerData adapters directly, so their fallback
# legs can be stubbed too. test_direct_csv_and_resiliency and test_data_resolver
# assert on those legs themselves and keep their own patches.
_SOCCERDATA_STUB_MODULES = _LEGACY_RESOLVER_MODULES - {
    "test_direct_csv_and_resiliency",
    "test_data_resolver",
    "test_soccerdata_provider",
}

# Modules whose sync calls do not always patch the CSV provider. An empty CSV
# response is the honest offline default; explicit test patches still win.
_CSV_STUB_MODULES = {
    "test_historical_hardening",
    "test_efficiency_and_telegram",
    "test_database_first_production",
    "test_provider_neutral_identity",
    "test_resume_queue_and_cli",
    "test_rolling_backtest_window",
}


@pytest.fixture(autouse=True)
def stub_unmocked_historical_stages(monkeypatch, request):
    module = request.module.__name__.rsplit(".", 1)[-1]
    if module not in _LEGACY_RESOLVER_MODULES:
        return

    def unavailable(*args, **kwargs):
        return "SOURCE_NOT_AVAILABLE", [], {"status": "SOURCE_NOT_AVAILABLE"}

    for name in ("get_json_games", "get_txt_games", "get_international_games"):
        monkeypatch.setattr(f"openfootball_provider.{name}", unavailable)
    monkeypatch.setattr("openligadb_provider.get_games", unavailable)

    if module in _SOCCERDATA_STUB_MODULES:
        for name in ("get_fbref_games", "get_match_history_games", "get_sofascore_historical_games"):
            monkeypatch.setattr(f"soccerdata_provider.{name}", unavailable)

    if module in _CSV_STUB_MODULES:
        monkeypatch.setattr("data_resolver._fetch_direct_csv_league_fixtures", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def isolate_test_db(tmp_path, monkeypatch):
    """
    Ensure every test runs against a clean, isolated temporary database and cache directory.
    This prevents cross-test contamination and ensures persistent DB cache hits/misses are deterministic.
    """
    db_file = tmp_path / "test_isolated.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    storage.init_db()
