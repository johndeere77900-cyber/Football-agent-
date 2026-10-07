import pytest
from unittest.mock import patch, MagicMock

import backtest
import config
import historical_sync
import storage
import api_football
import data_resolver


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "predictions.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    storage.init_db()
    return db_path


def sample_fixture(fid, date="2025-01-10T15:00:00+00:00", home_id=1, away_id=2):
    return {
        "fixture": {"id": fid, "date": date, "status": {"short": "FT"}},
        "teams": {
            "home": {"id": home_id, "name": f"Team {home_id}"},
            "away": {"id": away_id, "name": f"Team {away_id}"},
        },
        "goals": {"home": 1, "away": 0},
    }


def dataset_with_history():
    # Generate 10 fixtures with enough prior matches for teams 1,2,3,4
    fixtures = []
    for i in range(12):
        d = f"2025-01-{i+1:02d}T15:00:00+00:00"
        h = (i % 4) + 1
        a = ((i + 1) % 4) + 1
        if h == a:
            a = (a % 4) + 1
        fixtures.append(sample_fixture(9100 + i, date=d, home_id=h, away_id=a))
    return fixtures


def test_complete_dataset_zero_api_calls(temp_db):
    fixtures = [sample_fixture(9001), sample_fixture(9002)]
    storage.save_historical_fixtures(fixtures, league_id=39, season=2024)
    storage.mark_historical_dataset_complete(league_id=39, season=2024, fixture_count=2)

    with patch("api_football.get_league_fixtures") as mock_get:
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024)
        mock_get.assert_not_called()

    assert report["status"] == "COMPLETE"
    assert report["api_requests_consumed"] == 0
    assert report["newly_stored"] == 0


def test_partial_dataset_leaves_status_incomplete_and_backtest_refuses(temp_db):
    fixtures = [sample_fixture(9003)]
    storage.save_historical_fixtures(fixtures, league_id=39, season=2024)
    storage.mark_historical_dataset_incomplete(league_id=39, season=2024, fixture_count=1)

    status = storage.get_historical_dataset_status(39, 2024)
    assert status["status"] == "INCOMPLETE"

    with pytest.raises(RuntimeError) as exc_info:
        backtest.run_real_backtest(league_id=39, season=2024)

    assert "missing or incomplete" in str(exc_info.value)


def test_quota_ceiling_stops_paginated_fetch_at_budget(temp_db, monkeypatch):
    monkeypatch.setattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 3)

    current_page_call = 0

    def fake_get(endpoint, params, max_budget=None):
        nonlocal current_page_call
        current_page_call += 1
        if current_page_call > 3:
            raise api_football.APIFootballQuotaExhaustedError("Historical quota budget reached (3).")
        page = params.get("page", 1) if isinstance(params, dict) else 1
        return {
            "paging": {"current": page, "total": 5},
            "response": [sample_fixture(9010 + current_page_call)],
        }

    with patch("api_football._get", side_effect=fake_get):
        report = historical_sync.sync_historical_fixtures(league_id=39, season=2024, historical_budget=3)

    assert report["status"] == "INCOMPLETE"
    assert report["quota_budget_stopped"] is True


def test_enrichment_quota_ceiling_stops_batches(temp_db, monkeypatch):
    monkeypatch.setattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 2)

    fixtures = [sample_fixture(9020 + i) for i in range(10)]
    storage.save_historical_fixtures(fixtures, league_id=39, season=2024)
    storage.mark_historical_dataset_incomplete(league_id=39, season=2024)

    # Pre-record 2 API requests to exhaust budget
    storage.record_api_request("api_football", "fixtures")
    storage.record_api_request("api_football", "fixtures")

    with patch("api_football.get_league_fixtures", return_value=fixtures):
        report = historical_sync.sync_historical_fixtures(
            league_id=39, season=2024, with_enrichment=True, historical_budget=2
        )

    assert report["quota_budget_stopped"] is True
    assert report["status"] == "INCOMPLETE"


def test_provider_header_remaining_zero_raises_exhausted(temp_db, monkeypatch):
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test_key")

    class FakeResponse:
        status_code = 200
        ok = True
        headers = {"x-ratelimit-requests-remaining": "0", "x-ratelimit-requests-limit": "100"}
        def json(self):
            return {"response": []}

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(api_football.APIFootballQuotaExhaustedError):
        api_football._get("fixtures", {"date": "2025-01-01"})


def test_backtest_fails_if_api_football_called(temp_db, monkeypatch):
    fixtures = dataset_with_history()
    storage.save_historical_fixtures(fixtures, league_id=39, season=2024)
    storage.mark_historical_dataset_complete(league_id=39, season=2024, fixture_count=len(fixtures))

    def fail_call(*args, **kwargs):
        raise AssertionError("API-Football should NOT be called by backtest!")

    import data_resolver
    monkeypatch.setattr(data_resolver.api_football, "get_league_fixtures", fail_call)
    monkeypatch.setattr(data_resolver.api_football, "get_enriched_fixtures", fail_call)

    result = backtest.run_real_backtest(
        league_id=39, season=2024, sample_size=2, min_prior_matches=1
    )
    assert result["graded"] > 0


def _provenance_fixture(fid, provider, home_id=1, away_id=2):
    record = sample_fixture(
        fid,
        home_id=home_id,
        away_id=away_id,
    )
    record["provider_provenance"] = {
        "provider": provider,
    }
    return record


def test_reconciliation_metadata_single_provider_is_not_reconciled():
    record = _provenance_fixture(9201, "api_football")

    result = data_resolver.reconcile_fixture_records([record])

    assert result is not None
    metadata = result["reconciliation_metadata"]
    assert metadata["providers_used"] == ["api_football"]
    assert metadata["provider_count"] == 1
    assert metadata["is_reconciled"] is False


def test_reconciliation_metadata_two_providers_is_reconciled():
    api_record = _provenance_fixture(9202, "api_football")
    fd_record = _provenance_fixture(9202, "football_data_org")

    result = data_resolver.reconcile_fixture_records(
        [api_record, fd_record]
    )

    assert result is not None
    metadata = result["reconciliation_metadata"]
    assert metadata["providers_used"] == [
        "api_football",
        "football_data_org",
    ]
    assert metadata["provider_count"] == 2
    assert metadata["is_reconciled"] is True


def test_reconciliation_metadata_three_providers_is_reconciled():
    api_record = _provenance_fixture(9203, "api_football")
    fd_record = _provenance_fixture(9203, "football_data_org")
    sd_record = _provenance_fixture(9203, "soccerdata")

    result = data_resolver.reconcile_fixture_records(
        [api_record, fd_record, sd_record]
    )

    assert result is not None
    metadata = result["reconciliation_metadata"]
    assert metadata["providers_used"] == [
        "api_football",
        "football_data_org",
        "soccerdata",
    ]
    assert metadata["provider_count"] == 3
    assert metadata["is_reconciled"] is True


def test_single_provider_enrichment_persists_actual_provider_source(temp_db):
    record = _provenance_fixture(9204, "api_football")

    inserted = storage.save_historical_enrichment(
        [record],
        source="api_football",
    )

    assert inserted == 1

    api_result = storage.get_historical_enrichment(
        [9204],
        source="api_football",
    )
    reconciled_result = storage.get_historical_enrichment(
        [9204],
        source="reconciled",
    )

    assert 9204 in api_result
    assert 9204 not in reconciled_result


def test_multi_provider_enrichment_persists_reconciled_source(temp_db):
    api_record = _provenance_fixture(9205, "api_football")
    fd_record = _provenance_fixture(9205, "football_data_org")

    reconciled = data_resolver.reconcile_fixture_records(
        [api_record, fd_record]
    )

    assert reconciled is not None
    assert reconciled["reconciliation_metadata"]["is_reconciled"] is True

    inserted = storage.save_historical_enrichment(
        [reconciled],
        source="reconciled",
    )

    assert inserted == 1

    result = storage.get_historical_enrichment(
        [9205],
        source="reconciled",
    )

    assert 9205 in result
    assert result[9205]["reconciliation_metadata"]["provider_count"] == 2
