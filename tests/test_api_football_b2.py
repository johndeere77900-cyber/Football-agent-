"""
New B2 unit tests for API-Football pagination and result cache TTL policy.
"""

import os
import time
import pytest
import api_football
import config


def test_api_pagination_multi_page(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))

    requested_pages = []

    def mock_get(endpoint, params, ttl_hours=None):
        page = params.get("page", 1)
        requested_pages.append(page)
        if page == 1:
            return {
                "paging": {"current": 1, "total": 2},
                "response": [
                    {"fixture": {"id": 101, "date": "2025-01-01"}},
                    {"fixture": {"id": 102, "date": "2025-01-02"}},
                ],
            }
        elif page == 2:
            return {
                "paging": {"current": 2, "total": 2},
                "response": [
                    {"fixture": {"id": 102, "date": "2025-01-02"}},  # Duplicate
                    {"fixture": {"id": 103, "date": "2025-01-03"}},
                ],
            }
        return {"paging": {"current": page, "total": 2}, "response": []}

    monkeypatch.setattr(api_football, "_get", mock_get)

    fixtures = api_football.get_league_fixtures(league_id=39, season=2025)

    assert requested_pages == [1, 2]
    # Unique fixtures returned (101, 102, 103)
    assert len(fixtures) == 3
    f_ids = [f["fixture"]["id"] for f in fixtures]
    assert f_ids == [101, 102, 103]


def test_api_pagination_failure_raises_error(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))

    def mock_get(endpoint, params, ttl_hours=None):
        page = params.get("page", 1)
        if page == 1:
            return {
                "paging": {"current": 1, "total": 2},
                "response": [{"fixture": {"id": 101}}],
            }
        raise api_football.APIFootballError("Network failure on page 2")

    monkeypatch.setattr(api_football, "_get", mock_get)

    with pytest.raises(api_football.APIFootballError) as exc_info:
        api_football.get_league_fixtures(league_id=39, season=2025)

    assert "Failed retrieving page 2 of 2" in str(exc_info.value)


def test_fixture_result_short_ttl_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))

    calls = []

    def mock_get(endpoint, params, ttl_hours=None):
        calls.append((endpoint, params, ttl_hours))
        return {"response": [{"fixture": {"id": 999, "status": {"short": "FT"}}}]}

    monkeypatch.setattr(api_football, "_get", mock_get)

    api_football.get_fixture_result(999)

    assert len(calls) == 1
    assert calls[0][2] == pytest.approx(0.1)
