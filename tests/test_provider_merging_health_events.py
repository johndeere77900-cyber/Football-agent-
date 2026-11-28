"""Focused coverage for fixture-level merging, provider health and events."""

import datetime as dt

import lifecycle_storage as lstore
import provider_health
import provider_registry
import team_identity
from fixture_merger import cluster_fixtures, merge_stat_observations


def _iso(hours=24):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).isoformat()


def _football(provider, fixture_id, home, away, kickoff, status="FT", goals=(2, 1), home_id=1, away_id=2):
    return (
        provider,
        _iso(0),
        {
            "fixture": {"id": fixture_id, "date": kickoff, "status": {"short": status}},
            "league": {"id": 39, "season": 2026, "name": "Premier League"},
            "teams": {"home": {"id": home_id, "name": home}, "away": {"id": away_id, "name": away}},
            "goals": {"home": goals[0], "away": goals[1]},
            "provider_provenance": {"provider": provider, "provider_fixture_id": str(fixture_id)},
            "field_provenance": {},
            "provider_fixture_ids": {provider: str(fixture_id)},
            "source_observations": {},
            "conflicts": [],
        },
    )


def _canonical(monkeypatch):
    mapping = {
        "Arsenal": "arsenal",
        "Chelsea": "chelsea",
        "Leeds United": "leeds",
        "Coventry City": "coventry",
    }
    monkeypatch.setattr(
        team_identity,
        "resolve_canonical_team_id",
        lambda name=None, provider=None, provider_id=None, league_id=None, sport="football", **kw: mapping.get(
            name or ""
        ),
    )


def test_cross_provider_fields_merge_with_provenance_and_native_ids(monkeypatch):
    _canonical(monkeypatch)
    kickoff = _iso(26)
    espn = _football("espn_soccer", 401, "Arsenal", "Chelsea", kickoff, home_id=357, away_id=388)
    fotmob = _football("fotmob", 901, "Arsenal", "Chelsea", kickoff, home_id=11, away_id=12)

    result = cluster_fixtures("football", [espn, fotmob])
    assert len(result["fixtures"]) == 1
    merged = result["fixtures"][0]
    assert merged["match_status"] == "merged"
    assert merged["provider_fixture_ids"] == {"espn_soccer": "401", "fotmob": "901"}
    assert merged["provider_provenance"]["provider"] == "espn_soccer"
    assert set(merged["field_provenance"]) == {"kickoff", "status", "score"}
    assert merged["conflicts"] == []
    assert result["unmatched"] == []


def test_conflicting_scores_are_preserved_and_marked_unresolved(monkeypatch):
    _canonical(monkeypatch)
    kickoff = _iso(30)
    espn = _football("espn_soccer", 402, "Arsenal", "Chelsea", kickoff, goals=(2, 1))
    fotmob = _football("fotmob", 902, "Arsenal", "Chelsea", kickoff, goals=(1, 1))

    merged = cluster_fixtures("football", [espn, fotmob])["fixtures"][0]
    assert "score" in merged["unresolved_fields"]
    conflict = next(c for c in merged["conflicts"] if c["field"] == "score")
    assert conflict["resolution"] == "canonical_unresolved"
    assert conflict["observations"]["espn_soccer"]["value"] == (2, 1)
    assert conflict["observations"]["fotmob"]["value"] == (1, 1)


def test_home_away_swap_is_never_merged(monkeypatch):
    _canonical(monkeypatch)
    kickoff = _iso(28)
    espn = _football("espn_soccer", 403, "Arsenal", "Chelsea", kickoff)
    fotmob = _football("fotmob", 903, "Chelsea", "Arsenal", kickoff)

    result = cluster_fixtures("football", [espn, fotmob])
    assert result["fixtures"] == []
    reasons = {record["identity_unresolved_reason"] for record in result["unmatched"]}
    assert reasons == {"home_away_orientation_conflict"}


def test_provider_health_classifies_and_opens_the_circuit():
    assert provider_health.classify_http_status(401) == provider_health.HTTP_401
    assert provider_health.classify_http_status(429) == provider_health.HTTP_429
    assert provider_health.classify_http_status(503) == provider_health.HTTP_5XX
    assert provider_health.classify_exception(TimeoutError("timed out")) == provider_health.NETWORK_TIMEOUT
    assert provider_health.HTTP_401 in provider_health.PERMANENT_FAILURES
    assert provider_health.HTTP_404 in provider_health.EMPTY_NOT_PROOF_FAILURES

    for _ in range(3):
        state = provider_health.record("espn_soccer", ok=False, failure_category=provider_health.HTTP_404)
    assert state["state"] == provider_health.ISOLATED
    assert provider_health.is_isolated("espn_soccer")
    assert not provider_health.allows_request("espn_soccer")

    healed = provider_health.record("espn_soccer", ok=True)
    assert healed["state"] == provider_health.HEALTHY
    assert provider_health.allows_request("espn_soccer")


def test_retry_does_not_retry_permanent_auth_failure():
    calls = []

    def failing():
        calls.append(1)
        raise RuntimeError("HTTP 401 unauthorized")

    sleeps = []
    try:
        provider_health.call_with_retry(
            "api_football", failing, max_attempts=3, sleep_fn=sleeps.append, record_health=False
        )
    except RuntimeError:
        pass
    else:  # pragma: no cover - the call must fail
        raise AssertionError("expected the permanent failure to be raised")
    assert len(calls) == 1
    assert sleeps == []


def test_lifecycle_events_are_idempotent_and_correlated():
    first = lstore.record_event(
        "fixture_discovered",
        sport="football",
        lifecycle_id="football:espn_soccer:1",
        provider="espn_soccer",
        outcome="tracked",
        detail={"fixture_id": "1"},
        event_key="discover|football|1",
    )
    lstore.record_event(
        "fixture_discovered",
        sport="football",
        lifecycle_id="football:espn_soccer:1",
        provider="espn_soccer",
        outcome="tracked",
        detail={"fixture_id": "1"},
        event_key="discover|football|1",
    )
    events = lstore.get_events(lifecycle_id="football:espn_soccer:1")
    assert len(events) == 1
    assert first["correlation_id"] == "football:espn_soccer:1"
    assert lstore.event_metrics()["event_counts"]["fixture_discovered"] == 1


def test_stat_merge_prefers_field_specific_provider_and_records_conflicts():
    observations = [
        {
            "provider": "fotmob",
            "provider_fixture_id": "901",
            "retrieved_at": _iso(0),
            "statistics": {"xg": 1.02, "possession": 51.0},
        },
        {
            "provider": "espn_soccer",
            "provider_fixture_id": "401",
            "retrieved_at": _iso(0),
            "statistics": {"possession": 49.0, "corners": 6.0},
        },
    ]
    merged = merge_stat_observations(observations, sport="football", expected_fields=["xg", "possession", "corners", "cards"])
    assert merged["fields"]["xg"]["provider"] == "fotmob"
    assert merged["fields"]["corners"]["provider"] == "espn_soccer"
    assert merged["fields"]["cards"]["state"] == "provider_does_not_supply_field"
    assert any(conflict["field"] == "possession" for conflict in merged["conflicts"])
    assert merged["fields"]["possession"]["state"] == "conflicting_observations"
    # xg and corners are present; possession is present but conflict-marked.
    assert merged["coverage"] == {"present": 2, "conflicting_observations": 1, "provider_does_not_supply_field": 1}


def test_correction_and_replay_preserve_snapshots_and_original_grading():
    import corrections
    import storage

    row = lstore.upsert_lifecycle(
        {
            "lifecycle_id": "basketball:espn_nba:401909317",
            "sport": "basketball",
            "canonical_fixture_key": "espn_nba:401909317",
            "provider": "espn_nba",
            "provider_fixture_id": "401909317",
            "league_id": 12,
            "season": 2026,
            "home_team_id": "basketball_team_knicks",
            "away_team_id": "basketball_team_heat",
            "kickoff_at": _iso(-6),
            "stage": "FINAL_RESULT_VERIFIED",
            "provider_status": "FT",
        }
    )
    snapshot = storage.save_prediction_snapshot(
        fixture_id=401909317,
        sport="basketball",
        league_id=12,
        season=2026,
        kickoff_at=row["kickoff_at"],
        prediction_context="PRE_MATCH",
        data_cutoff_timestamp=row["kickoff_at"],
        markets={"match_result": {"home_win": 0.6, "away_win": 0.4}},
        prediction_record={"markets": {"match_result": {"home_win": 0.6, "away_win": 0.4}}},
        idempotency_key="correction-test",
        lifecycle_id=row["lifecycle_id"],
        model_inputs={"features": {"elo_diff": 2}},
    )
    payload = {
        "id": 401909317,
        "date": _iso(-6),
        "league": {"id": 12, "season": 2026},
        "teams": {"home": {"id": 18}, "away": {"id": 14}},
        "scores": {"home": {"total": 111}, "away": {"total": 104}},
        "status": {"short": "FT"},
        "provider_provenance": {"provider": "espn_nba"},
    }
    lstore.record_observation(
        lifecycle_id=row["lifecycle_id"],
        provider="espn_nba",
        context="final_result",
        observation_timestamp=_iso(0),
        provider_status="FT",
        payload=payload,
        observation_key="final|correction-test",
    )
    lstore.save_grading(
        {
            "snapshot_id": snapshot,
            "lifecycle_id": row["lifecycle_id"],
            "sport": "basketball",
            "market": "match_result",
            "prediction": {"selection": "home_win"},
            "settlement_status": "WON",
            "grading_key": "original-grading",
        }
    )
    before = [tuple(r) for r in storage.get_prediction_snapshots(fixture_id=401909317, sport="basketball")]

    impact = corrections.record_correction(
        row["lifecycle_id"],
        field="scores",
        corrected_value={"home": {"total": 111}, "away": {"total": 104}},
        reason="re-verification of the same provider payload",
    )
    assert impact["snapshots_preserved"] is True
    assert impact["correction_observation_key"]

    replay = corrections.replay_grading(row["lifecycle_id"], reason="re-verification")
    assert replay["replayed"] is True
    assert replay["graded_rows"] == 1

    after = [tuple(r) for r in storage.get_prediction_snapshots(fixture_id=401909317, sport="basketball")]
    assert after == before, "a correction must never rewrite an issued prediction snapshot"
    assert lstore.get_grading("original-grading")["settlement_status"] == "WON"
    replay_rows = [
        event for event in lstore.get_events(lifecycle_id=row["lifecycle_id"], event_type="prediction_graded")
    ]
    assert any(event.get("outcome") == "replay" for event in replay_rows)
