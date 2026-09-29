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
                    {"fixture": {"id": 5}}
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
        {"fixture": {"id": 5}}
    ]

    assert len(calls) == 2


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
