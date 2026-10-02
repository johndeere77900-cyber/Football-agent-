import json
import os

import pytest
import requests

import basketball_api
import config


class FakeResponse:
    def __init__(
        self,
        status_code=200,
        payload=None,
        headers=None,
    ):
        self.status_code = status_code
        self._payload = (
            payload
            if payload is not None
            else {"response": []}
        )
        self.headers = headers or {}
        self.ok = 200 <= status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(
                f"HTTP {self.status_code}"
            )


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config,
        "CACHE_DIR",
        str(tmp_path),
    )
    monkeypatch.setattr(
        config,
        "CACHE_TTL_HOURS",
        20,
    )
    monkeypatch.setattr(
        config,
        "API_FOOTBALL_KEY",
        "test-key",
    )


def test_get_games_by_date_builds_expected_request(
    isolated_cache,
    monkeypatch,
):
    calls = []

    def fake_get(
        url,
        headers,
        params,
        timeout,
    ):
        calls.append(
            {
                "url": url,
                "headers": headers,
                "params": params,
                "timeout": timeout,
            }
        )

        return FakeResponse(
            payload={
                "response": [
                    {
                        "id": 1,
                    }
                ]
            }
        )

    monkeypatch.setattr(
        basketball_api.requests,
        "get",
        fake_get,
    )

    result = basketball_api.get_games_by_date(
        "2026-09-28",
        12,
    )

    assert result == [{"id": 1}]
    assert len(calls) == 1
    assert calls[0]["params"] == {
        "date": "2026-09-28",
        "league": 12,
        "season": 2026,
    }


def test_basketball_season_rolls_back_before_august():
    assert (
        basketball_api._season_for_date(
            __import__("datetime").date(
                2026,
                7,
                31,
            )
        )
        == 2025
    )

    assert (
        basketball_api._season_for_date(
            __import__("datetime").date(
                2026,
                8,
                1,
            )
        )
        == 2026
    )


def test_empty_successful_response_is_cached(
    isolated_cache,
    monkeypatch,
):
    calls = []

    def fake_get(
        url,
        headers,
        params,
        timeout,
    ):
        calls.append(1)

        return FakeResponse(
            payload={
                "response": [],
            }
        )

    monkeypatch.setattr(
        basketball_api.requests,
        "get",
        fake_get,
    )

    first = basketball_api.get_games_by_date(
        "2026-09-28",
        12,
    )

    second = basketball_api.get_games_by_date(
        "2026-09-28",
        12,
    )

    assert first == []
    assert second == []

    # The second identical request must come from cache.
    assert len(calls) == 1


def test_network_failure_is_retried(
    isolated_cache,
    monkeypatch,
):
    calls = []

    def fake_get(
        url,
        headers,
        params,
        timeout,
    ):
        calls.append(1)

        if len(calls) == 1:
            raise requests.ConnectionError(
                "temporary network failure"
            )

        return FakeResponse(
            payload={
                "response": [
                    {
                        "id": 123,
                    }
                ]
            }
        )

    monkeypatch.setattr(
        basketball_api.requests,
        "get",
        fake_get,
    )

    monkeypatch.setattr(
        basketball_api,
        "_sleep_before_retry",
        lambda seconds: None,
    )

    result = basketball_api.get_games_by_date(
        "2026-09-28",
        12,
    )

    assert result == [{"id": 123}]
    assert len(calls) == 2


def test_429_uses_retry_after(
    isolated_cache,
    monkeypatch,
):
    calls = []
    delays = []

    def fake_get(
        url,
        headers,
        params,
        timeout,
    ):
        calls.append(1)

        if len(calls) == 1:
            return FakeResponse(
                status_code=429,
                payload={
                    "response": [],
                },
                headers={
                    "Retry-After": "7",
                },
            )

        return FakeResponse(
            payload={
                "response": [],
            }
        )

    monkeypatch.setattr(
        basketball_api.requests,
        "get",
        fake_get,
    )

    monkeypatch.setattr(
        basketball_api,
        "_sleep_before_retry",
        lambda seconds: delays.append(
            seconds
        ),
    )

    result = basketball_api.get_games_by_date(
        "2026-09-28",
        12,
    )

    assert result == []
    assert calls == [1, 1]
    assert delays == [7.0]


def test_invalid_api_envelope_is_rejected(
    isolated_cache,
    monkeypatch,
):
    monkeypatch.setattr(
        basketball_api.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            payload={
                "unexpected": [],
            }
        ),
    )

    with pytest.raises(ValueError):
        basketball_api.get_games_by_date(
            "2026-09-28",
            12,
        )


def test_api_error_envelope_is_rejected(
    isolated_cache,
    monkeypatch,
):
    monkeypatch.setattr(
        basketball_api.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            payload={
                "response": [],
                "errors": {
                    "token": "invalid",
                },
            }
        ),
    )

    with pytest.raises(RuntimeError):
        basketball_api.get_games_by_date(
            "2026-09-28",
            12,
        )


def test_live_games_uses_live_all(
    isolated_cache,
    monkeypatch,
):
    calls = []

    def fake_get(
        url,
        headers,
        params,
        timeout,
    ):
        calls.append(params)

        return FakeResponse(
            payload={
                "response": [
                    {
                        "id": 55,
                    }
                ]
            }
        )

    monkeypatch.setattr(
        basketball_api.requests,
        "get",
        fake_get,
    )

    result = basketball_api.get_live_games()

    assert result == [{"id": 55}]
    assert calls == [{"live": "all"}]


def test_get_games_for_date_alias_matches_primary_function(
    monkeypatch,
):
    called = []

    monkeypatch.setattr(
        basketball_api,
        "get_games_by_date",
        lambda date_str, league_id: called.append(
            (date_str, league_id)
        )
        or ["game"],
    )

    result = basketball_api.get_games_for_date(
        "2026-09-28",
        12,
    )

    assert result == ["game"]
    assert called == [
        ("2026-09-28", 12)
    ]


def test_get_league_games_page_unpaginated_response_handling(isolated_cache, monkeypatch):
    # Unpaginated response (missing or None 'paging') for page 1 defaults to current=1, total=1
    monkeypatch.setattr(
        basketball_api.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            payload={"get": "games", "parameters": {"league": "12", "season": "2024"}, "errors": [], "results": 2, "response": [{"id": 1}, {"id": 2}]}
        ),
    )
    res = basketball_api.get_league_games_page(12, 2024, page=1)
    assert res["games"] == [{"id": 1}, {"id": 2}]
    assert res["page"] == 1
    assert res["expected_pages"] == 1

    # Unpaginated response with paging: None explicitly (using season 2025 to avoid cache key collision)
    monkeypatch.setattr(
        basketball_api.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            payload={"get": "games", "parameters": {"league": "12", "season": "2025"}, "errors": [], "results": 1, "paging": None, "response": [{"id": 3}]}
        ),
    )
    res_none = basketball_api.get_league_games_page(12, 2025, page=1)
    assert res_none["games"] == [{"id": 3}]
    assert res_none["page"] == 1
    assert res_none["expected_pages"] == 1


def test_get_league_games_page_pagination_validation(isolated_cache, monkeypatch):
    # A. Missing paging object when requesting page > 1 fails closed
    monkeypatch.setattr(basketball_api.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": []}))
    try:
        basketball_api.get_league_games_page(12, 2024, page=10)
        assert False, "Expected APIBasketballError for missing paging when page > 1"
    except basketball_api.APIBasketballError:
        pass

    # B. Malformed paging object
    monkeypatch.setattr(basketball_api.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": [], "paging": {"current": None, "total": 1}}))
    try:
        basketball_api.get_league_games_page(12, 2024, page=11)
        assert False, "Expected APIBasketballError for malformed paging"
    except basketball_api.APIBasketballError:
        pass

    # C. Page mismatch
    monkeypatch.setattr(basketball_api.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": [], "paging": {"current": 3, "total": 3}}))
    try:
        basketball_api.get_league_games_page(12, 2024, page=12)
        assert False, "Expected APIBasketballError for page mismatch"
    except basketball_api.APIBasketballError:
        pass

    # E. Empty response with valid pagination
    monkeypatch.setattr(basketball_api.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": [], "paging": {"current": 13, "total": 13}}))
    res_empty = basketball_api.get_league_games_page(12, 2024, page=13)
    assert res_empty["games"] == []
    assert res_empty["expected_pages"] == 13

    # F. Genuine valid single-page response
    monkeypatch.setattr(basketball_api.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": [{"id": 201}], "paging": {"current": 14, "total": 14}}))
    res_single = basketball_api.get_league_games_page(12, 2024, page=14)
    assert res_single["expected_pages"] == 14
    assert len(res_single["games"]) == 1

    # G. Genuine valid multi-page response
    monkeypatch.setattr(basketball_api.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": [{"id": 202}], "paging": {"current": 15, "total": 20}}))
    res_multi = basketball_api.get_league_games_page(12, 2024, page=15)
    assert res_multi["expected_pages"] == 20
    assert len(res_multi["games"]) == 1


def test_get_league_games_page_omits_page_param_in_http_request(isolated_cache, monkeypatch):
    recorded_params = []

    def fake_get(url, headers, params, timeout):
        recorded_params.append(params)
        return FakeResponse(
            payload={
                "response": [{"id": 101}],
                "paging": {"current": 1, "total": 1},
            }
        )

    monkeypatch.setattr(basketball_api.requests, "get", fake_get)

    res = basketball_api.get_league_games_page(12, 2024, page=1)

    assert res["games"] == [{"id": 101}]
    assert res["expected_pages"] == 1
    assert len(recorded_params) == 1
    # Verify 'page' parameter was filtered out from HTTP GET request
    assert "page" not in recorded_params[0]
    assert recorded_params[0] == {"league": 12, "season": 2024}


def test_get_game_result_returns_first_game(
    isolated_cache,
    monkeypatch,
):
    monkeypatch.setattr(
        basketball_api.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            payload={
                "response": [
                    {
                        "id": 77,
                    }
                ]
            }
        ),
    )

    result = basketball_api.get_game_result(77)

    assert result == {
        "id": 77,
    }


@pytest.mark.parametrize(
    "date_str",
    [
        "",
        "2026-9-28",
        "28-09-2026",
        "2026-02-30",
    ],
)
def test_invalid_date_is_rejected(
    isolated_cache,
    date_str,
):
    with pytest.raises(ValueError):
        basketball_api.get_games_by_date(
            date_str,
            12,
      )
