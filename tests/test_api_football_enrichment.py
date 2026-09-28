import api_football


def enriched_fixture(fixture_id):
    return {
        "fixture": {
            "id": fixture_id,
        },
        "teams": {
            "home": {
                "id": 1,
            },
            "away": {
                "id": 2,
            },
        },
        "goals": {
            "home": 1,
            "away": 0,
        },
        "statistics": [],
    }


def test_enriched_fixture_ids_are_batched(monkeypatch):
    calls = []

    def fake_get(endpoint, params):
        calls.append(
            (
                endpoint,
                params,
            )
        )

        ids = [
            int(value)
            for value in params["ids"].split("-")
        ]

        return {
            "response": [
                enriched_fixture(value)
                for value in ids
            ]
        }

    monkeypatch.setattr(
        api_football,
        "_get",
        fake_get,
    )

    result = api_football.get_enriched_fixtures(
        list(range(1, 46)),
        batch_size=20,
    )

    assert len(calls) == 3

    assert calls[0][0] == "fixtures"
    assert calls[1][0] == "fixtures"
    assert calls[2][0] == "fixtures"

    assert len(result) == 45


def test_enriched_fixture_ids_are_deduplicated():
    calls = []

    def fake_get(endpoint, params):
        calls.append(params)

        return {
            "response": [
                enriched_fixture(1),
                enriched_fixture(2),
            ]
        }

    original = api_football._get

    try:
        api_football._get = fake_get

        result = api_football.get_enriched_fixtures(
            [1, 1, 2, 2],
            batch_size=20,
        )
    finally:
        api_football._get = original

    assert len(calls) == 1
    assert len(result) == 2


def test_empty_fixture_ids_make_no_api_call(monkeypatch):
    calls = []

    monkeypatch.setattr(
        api_football,
        "_get",
        lambda *args, **kwargs: calls.append(
            args
        ),
    )

    result = api_football.get_enriched_fixtures([])

    assert result == {}
    assert calls == []


def test_invalid_fixture_ids_are_ignored(monkeypatch):
    calls = []

    def fake_get(endpoint, params):
        calls.append(params)

        return {
            "response": [
                enriched_fixture(10),
            ]
        }

    monkeypatch.setattr(
        api_football,
        "_get",
        fake_get,
    )

    result = api_football.get_enriched_fixtures(
        ["bad", None, "", 10],
    )

    assert len(calls) == 1
    assert list(result) == [10]
