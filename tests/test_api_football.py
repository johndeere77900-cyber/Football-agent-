import requests

import api_football
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


def test_successful_empty_response_is_cached(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        config,
        "CACHE_DIR",
        str(tmp_path),
    )
    monkeypatch.setattr(
        config,
        "API_FOOTBALL_KEY",
        "test-key",
    )

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
        api_football.requests,
        "get",
        fake_get,
    )

    first = api_football.get_fixtures_by_date(
        "2026-09-28",
        39,
    )

    second = api_football.get_fixtures_by_date(
        "2026-09-28",
        39,
    )

    assert first == []
    assert second == []
    assert len(calls) == 1


def test_live_fixtures_use_live_all(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        config,
        "CACHE_DIR",
        str(tmp_path),
    )
    monkeypatch.setattr(
        config,
        "API_FOOTBALL_KEY",
        "test-key",
    )

    calls = []

    monkeypatch.setattr(
        api_football.requests,
        "get",
        lambda url, headers, params, timeout: (
            calls.append(params)
            or FakeResponse(
                payload={
                    "response": [
                        {"fixture": {"id": 1}}
                    ]
                }
            )
        ),
    )

    result = api_football.get_live_fixtures()

    assert result == [
        {"fixture": {"id": 1}}
    ]

    assert calls == [
        {"live": "all"}
    ]


def test_network_failure_retries(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        config,
        "CACHE_DIR",
        str(tmp_path),
    )
    monkeypatch.setattr(
        config,
        "API_FOOTBALL_KEY",
        "test-key",
    )

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
                "temporary failure"
            )

        return FakeResponse(
            payload={
                "response": [
                        {"fixture": {"id": 5}, "league": {"id": 39}}
                ]
            }
        )

    monkeypatch.setattr(
        api_football.requests,
        "get",
        fake_get,
    )

    monkeypatch.setattr(
        api_football.time,
        "sleep",
        lambda seconds: None,
    )

    result = api_football.get_fixtures_by_date(
        "2026-09-28",
        39,
    )

    assert result == [
        {"fixture": {"id": 5}, "league": {"id": 39}}
    ]

    assert len(calls) == 2


def test_get_league_standings_passes_max_budget(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")

    budget_checks = []

    def mock_check_quota(endpoint, max_budget=None, **kwargs):
        budget_checks.append((endpoint, max_budget))

    monkeypatch.setattr(api_football, "_check_and_consume_quota", mock_check_quota)

    def fake_get(url, headers, params, timeout):
        return FakeResponse(payload={"response": [{"league": {"standings": [[{"team": {"id": 10, "name": "Arsenal"}}]]}}]})

    monkeypatch.setattr(api_football.requests, "get", fake_get)

    res = api_football.get_league_standings(39, 2024, max_budget=25)

    assert len(res) > 0
    assert ("standings", 25) in budget_checks


def test_historical_budget_limit_clamping(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")
    monkeypatch.setattr(config, "API_FOOTBALL_DAILY_CREDIT_LIMIT", 100)
    monkeypatch.setattr(config, "API_FOOTBALL_HISTORICAL_DAILY_BUDGET", 50)

    reserved_limits = []

    def fake_reserve(provider, date1, date2, endpoint, limit, request_type=None, global_limit=None, *args, **kwargs):
        reserved_limits.append(limit)
        return True

    monkeypatch.setattr(api_football.storage, "reserve_api_request", fake_reserve)

    # When max_budget is 0 (deliberate zero remaining budget), limit is 0
    api_football._check_and_consume_quota("fixtures", max_budget=0)
    assert reserved_limits[-1] == 0

    api_football._check_and_consume_quota("fixtures", max_budget=-10)
    assert reserved_limits[-1] == 50

    api_football._check_and_consume_quota("fixtures", max_budget=100)
    assert reserved_limits[-1] == 50

    api_football._check_and_consume_quota("fixtures", max_budget=20)
    assert reserved_limits[-1] == 20

    # When request_type is historical (default) and max_budget is None, it clamps to historical_limit (50)
    api_football._check_and_consume_quota("fixtures", max_budget=None)
    assert reserved_limits[-1] == 50

    # When request_type is live and max_budget is None, it uses live global limit (100)
    api_football._check_and_consume_quota("fixtures", max_budget=None, request_type="live")
    assert reserved_limits[-1] == 100


def test_permanent_http_error_is_not_retried(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        config,
        "CACHE_DIR",
        str(tmp_path),
    )
    monkeypatch.setattr(
        config,
        "API_FOOTBALL_KEY",
        "test-key",
    )

    calls = []

    def fake_get(
        url,
        headers,
        params,
        timeout,
    ):
        calls.append(1)

        return FakeResponse(
            status_code=400,
            payload={
                "errors": {
                    "query": "bad",
                },
                "response": [],
            },
        )

    monkeypatch.setattr(
        api_football.requests,
        "get",
        fake_get,
    )

    try:
        api_football.get_fixtures_by_date(
            "2026-09-28",
            39,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError(
            "Expected RuntimeError"
        )

    assert len(calls) == 1


def test_recent_form_uses_finished_status(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        config,
        "CACHE_DIR",
        str(tmp_path),
    )
    monkeypatch.setattr(
        config,
        "API_FOOTBALL_KEY",
        "test-key",
    )

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
                "response": [],
            }
        )

    monkeypatch.setattr(
        api_football.requests,
        "get",
        fake_get,
    )

    result = api_football.get_recent_form(
        123,
        last=8,
    )

    assert result == []

    assert calls == [
        {
            "team": 123,
            "last": 8,
            "status": "FT",
        }
    ]


def test_get_enriched_fixtures_batches_and_deduplicates(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        config,
        "CACHE_DIR",
        str(tmp_path),
    )
    monkeypatch.setattr(
        config,
        "API_FOOTBALL_KEY",
        "test-key",
    )

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
                        "fixture": {
                            "id": int(
                                value
                            )
                        }
                    }
                    for value in params[
                        "ids"
                    ].split("-")
                ]
            }
        )

    monkeypatch.setattr(
        api_football.requests,
        "get",
        fake_get,
    )

    result = api_football.get_enriched_fixtures(
        [3, 1, 2, 2, 3],
        batch_size=2,
    )

    assert sorted(result) == [
        1,
        2,
        3,
    ]

    assert calls == [
        {"ids": "1-2"},
        {"ids": "3"},
    ]


def test_get_league_fixtures_page_does_not_send_page_param(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")

    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append(params)
        return FakeResponse(
            payload={
                "response": [{"fixture": {"id": 101}}],
                "paging": {"current": 1, "total": 1},
            }
        )

    monkeypatch.setattr(api_football.requests, "get", fake_get)

    res1 = api_football.get_league_fixtures_page(39, 2024, page=1)

    assert len(calls) == 1
    assert "page" not in calls[0]
    assert calls[0] == {
        "league": 39,
        "season": 2024,
        "status": "FT-AET-PEN",
    }
    assert res1["expected_pages"] == 1
    assert len(res1["fixtures"]) == 1

    # Requesting page 2 uses cached single response and returns empty list
    res2 = api_football.get_league_fixtures_page(39, 2024, page=2)

    assert len(calls) == 1
    assert res2["expected_pages"] == 1
    assert res2["fixtures"] == []


def test_get_league_fixtures_page_provider_error_visible(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")

    def fake_get(url, headers, params, timeout):
        return FakeResponse(
            status_code=403,
            payload={"errors": {"token": "Invalid API key"}},
        )

    monkeypatch.setattr(api_football.requests, "get", fake_get)

    try:
        api_football.get_league_fixtures_page(39, 2024, page=1)
        assert False, "Expected APIFootballError for provider error"
    except api_football.APIFootballError as exc:
        assert "Invalid API key" in str(exc) or "403" in str(exc)


def test_get_league_fixtures_page_pagination_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "API_FOOTBALL_KEY", "test-key")

    # A. Missing paging object
    monkeypatch.setattr(api_football.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": []}))
    try:
        api_football.get_league_fixtures_page(39, 2020, page=1)
        assert False, "Expected APIFootballError for missing paging"
    except api_football.APIFootballError:
        pass

    # Null paging object
    monkeypatch.setattr(api_football.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": [], "paging": None}))
    try:
        api_football.get_league_fixtures_page(39, 2020, page=1)
        assert False, "Expected APIFootballError for null paging"
    except api_football.APIFootballError:
        pass

    # B. Malformed paging object (string values)
    monkeypatch.setattr(api_football.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": [], "paging": {"current": "1", "total": 1}}))
    try:
        api_football.get_league_fixtures_page(39, 2021, page=1)
        assert False, "Expected APIFootballError for string current page"
    except api_football.APIFootballError:
        pass

    # Zero or negative paging metadata
    monkeypatch.setattr(api_football.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": [], "paging": {"current": 0, "total": 1}}))
    try:
        api_football.get_league_fixtures_page(39, 2021, page=1)
        assert False, "Expected APIFootballError for zero current page"
    except api_football.APIFootballError:
        pass

    monkeypatch.setattr(api_football.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": [], "paging": {"current": 1, "total": -5}}))
    try:
        api_football.get_league_fixtures_page(39, 2021, page=1)
        assert False, "Expected APIFootballError for negative total pages"
    except api_football.APIFootballError:
        pass

    # C. Current page mismatch when page <= total
    monkeypatch.setattr(api_football.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": [], "paging": {"current": 2, "total": 5}}))
    try:
        api_football.get_league_fixtures_page(39, 2022, page=1)
        assert False, "Expected APIFootballError for page mismatch"
    except api_football.APIFootballError:
        pass

    # E. Empty response with valid pagination beyond total pages
    monkeypatch.setattr(api_football.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": [], "paging": {"current": 1, "total": 1}}))
    res_empty = api_football.get_league_fixtures_page(39, 2023, page=13)
    assert res_empty["fixtures"] == []
    assert res_empty["expected_pages"] == 1

    # F. Genuine valid single-page response
    monkeypatch.setattr(api_football.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": [{"fixture": {"id": 101}}], "paging": {"current": 1, "total": 1}}))
    res_single = api_football.get_league_fixtures_page(39, 2024, page=1)
    assert res_single["expected_pages"] == 1
    assert len(res_single["fixtures"]) == 1

    # G. Genuine valid multi-page response
    monkeypatch.setattr(api_football.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"response": [{"fixture": {"id": 102}}], "paging": {"current": 2, "total": 5}}))
    res_multi = api_football.get_league_fixtures_page(39, 2025, page=2)
    assert res_multi["expected_pages"] == 5
    assert len(res_multi["fixtures"]) == 1


def test_raw_debug_call_bypasses_cache(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        config,
        "CACHE_DIR",
        str(tmp_path),
    )
    monkeypatch.setattr(
        config,
        "API_FOOTBALL_KEY",
        "test-key",
    )

    calls = []

    monkeypatch.setattr(
        api_football.requests,
        "get",
        lambda url, headers, params, timeout: (
            calls.append(1)
            or FakeResponse(
                payload={
                    "response": [
                        {"debug": True}
                    ]
                }
            )
        ),
    )

    first = api_football.raw_debug_call(
        "fixtures",
        {"date": "2026-09-28"},
    )

    second = api_football.raw_debug_call(
        "fixtures",
        {"date": "2026-09-28"},
    )

    assert first == second
    assert len(calls) == 2
