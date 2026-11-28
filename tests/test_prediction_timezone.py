"""Calendar-date semantics for the configurable prediction timezone.

Two separate contracts are covered here:

* a kickoff is an absolute instant, normalized to UTC and never re-interpreted;
* a user-facing calendar date ("today", "tomorrow", "2026-11-28") is a local
  date in ``config.PREDICTION_TIMEZONE`` (default ``Africa/Lagos``) and is
  converted to a half-open UTC instant interval before it reaches SQL.

Every timestamp is deterministic; nothing here depends on the day the suite runs.
"""

import datetime as dt

import pytest

import config
import lifecycle_storage as lstore
import live_lifecycle
import storage
import telegram_bot
import time_utils

LAGOS = dt.timezone(dt.timedelta(hours=1))


@pytest.fixture(autouse=True)
def _default_zone(monkeypatch):
    """Pin the configured zone so a developer's environment cannot change it."""
    monkeypatch.setattr(config, "PREDICTION_TIMEZONE", "Africa/Lagos")


def _seed(lifecycle_id, kickoff_at, sport="football", provider_fixture_id="9001"):
    return lstore.upsert_lifecycle(
        {
            "lifecycle_id": f"football:api_football:{lifecycle_id}",
            "sport": sport,
            "canonical_fixture_key": f"api_football:{lifecycle_id}",
            "provider": "api_football",
            "provider_fixture_id": provider_fixture_id,
            "league_id": 39,
            "season": 2026,
            "home_team_id": "arsenal",
            "away_team_id": "chelsea",
            "kickoff_at": kickoff_at,
            "stage": "TRACKED",
            "provider_status": "NS",
        }
    )


# ---------------------------------------------------------------------------
# Local midnight -> UTC interval
# ---------------------------------------------------------------------------

def test_lagos_midnight_converts_to_utc_interval():
    start, end = time_utils.local_date_interval_iso("2026-11-28")
    assert start == "2026-11-27T23:00:00+00:00"
    assert end == "2026-11-28T23:00:00+00:00"


def test_half_open_interval_boundaries():
    start, end = time_utils.local_date_bounds_utc("2026-11-28")
    assert start == dt.datetime(2026, 11, 27, 23, 0, tzinfo=dt.timezone.utc)
    assert end == dt.datetime(2026, 11, 28, 23, 0, tzinfo=dt.timezone.utc)
    # Lower boundary is inclusive, upper boundary belongs to the next local day.
    assert time_utils.in_local_day("2026-11-27T23:00:00+00:00", "2026-11-28") is True
    assert time_utils.in_local_day("2026-11-27T22:59:59+00:00", "2026-11-28") is False
    assert time_utils.in_local_day("2026-11-28T22:59:59+00:00", "2026-11-28") is True
    assert time_utils.in_local_day("2026-11-28T23:00:00+00:00", "2026-11-28") is False


def test_instant_before_local_midnight_belongs_to_previous_local_date():
    assert time_utils.local_calendar_date("2026-11-27T22:59:59+00:00") == dt.date(2026, 11, 27)
    assert time_utils.local_calendar_date("2026-11-27T23:00:00+00:00") == dt.date(2026, 11, 28)


def test_fixtures_straddling_utc_midnight_share_the_same_local_date():
    """Both task examples are Lagos 28 November although their UTC dates differ."""
    before_utc_midnight = "2026-11-27T23:30:00+00:00"  # 00:30 on 28 Nov in Lagos
    after_utc_midnight = "2026-11-28T00:30:00+00:00"  # 01:30 on 28 Nov in Lagos
    assert time_utils.local_calendar_date(before_utc_midnight) == dt.date(2026, 11, 28)
    assert time_utils.local_calendar_date(after_utc_midnight) == dt.date(2026, 11, 28)
    assert time_utils.parse_utc_datetime(before_utc_midnight).date() == dt.date(2026, 11, 27)
    assert time_utils.in_local_day(before_utc_midnight, "2026-11-28") is True
    assert time_utils.in_local_day(after_utc_midnight, "2026-11-28") is True


# ---------------------------------------------------------------------------
# Provider offsets and malformed values
# ---------------------------------------------------------------------------

def test_provider_offsets_are_normalized_and_keep_their_local_day():
    positive = _seed("positive", "2026-11-28T01:30:00+02:00")
    negative = _seed("negative", "2026-11-28T23:00:00-03:00")
    assert positive["kickoff_at"] == "2026-11-27T23:30:00+00:00"
    assert negative["kickoff_at"] == "2026-11-29T02:00:00+00:00"
    # The instant moved to UTC; the local calendar date is still the one stored.
    assert time_utils.local_calendar_date(positive["kickoff_at"]) == dt.date(2026, 11, 28)
    assert time_utils.local_calendar_date(negative["kickoff_at"]) == dt.date(2026, 11, 29)
    assert [row["lifecycle_id"] for row in lstore.list_lifecycles(kickoff_on="2026-11-28")] == [
        "football:api_football:positive"
    ]
    assert [row["lifecycle_id"] for row in lstore.list_lifecycles(kickoff_on="2026-11-29")] == [
        "football:api_football:negative"
    ]


def test_naive_timestamps_follow_the_documented_utc_interpretation():
    # The repository's documented rule: a naive timestamp is rejected by the
    # strict parser and read as UTC by the Telegram kickoff formatter.
    assert time_utils.parse_utc_datetime("2026-11-28T00:30:00") is None
    assert telegram_bot._parse_kickoff("2026-11-28T00:30:00") == dt.datetime(
        2026, 11, 28, 0, 30, tzinfo=dt.timezone.utc
    )
    assert telegram_bot.format_fixture_date("2026-11-28T00:30:00") == "2026-11-28"
    assert telegram_bot.format_fixture_time({"fixture": {"date": "2026-11-28T00:30:00"}}) == "01:30 WAT"


def test_malformed_timestamps_are_never_selected():
    broken = _seed("broken", "not-a-timestamp")
    valid = _seed("valid", "2026-11-28T12:00:00+00:00")
    assert broken["kickoff_at"] == "not-a-timestamp"  # preserved, never invented
    assert time_utils.parse_utc_datetime("not-a-timestamp") is None
    assert time_utils.local_calendar_date("not-a-timestamp") is None
    assert time_utils.in_local_day("not-a-timestamp", "2026-11-28") is False
    selected = lstore.list_lifecycles(kickoff_on="2026-11-28")
    assert [row["lifecycle_id"] for row in selected] == [valid["lifecycle_id"]]
    with pytest.raises(ValueError):
        lstore.list_lifecycles(kickoff_on="28-11-2026")


# ---------------------------------------------------------------------------
# Configured override and explicit UTC
# ---------------------------------------------------------------------------

def test_configured_timezone_overrides_default(monkeypatch):
    monkeypatch.setattr(config, "PREDICTION_TIMEZONE", "Europe/London")
    start, end = time_utils.local_date_interval_iso("2026-11-28")
    assert (start, end) == ("2026-11-28T00:00:00+00:00", "2026-11-29T00:00:00+00:00")
    assert time_utils.prediction_timezone_name() == "Europe/London"
    assert time_utils.local_calendar_date("2026-11-27T23:30:00+00:00") == dt.date(2026, 11, 27)


def test_explicit_utc_behaviour_is_unchanged():
    start, end = time_utils.local_date_interval_iso("2026-11-28", tz_name="UTC")
    assert (start, end) == ("2026-11-28T00:00:00+00:00", "2026-11-29T00:00:00+00:00")
    assert time_utils.local_calendar_date("2026-11-27T23:30:00+00:00", "UTC") == dt.date(2026, 11, 27)
    # An unknown zone is reported, never silently replaced by a fixed offset.
    with pytest.raises(ValueError):
        time_utils.local_date_bounds_utc("2026-11-28", tz_name="Africa/Atlantis")


def test_weekly_parser_uses_the_local_clock(monkeypatch):
    frozen = dt.datetime(2026, 11, 28, 23, 30, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(time_utils, "local_today", lambda now=None, tz_name=None: dt.date(2026, 11, 29))
    assert frozen.astimezone(LAGOS).date() == dt.date(2026, 11, 29)
    assert time_utils.local_today() == dt.date(2026, 11, 29)
    assert telegram_bot.resolve_date("tomorrow")[0] == "2026-11-30"
    assert telegram_bot.resolve_date("2026-11-28")[0] == "2026-11-28"


# ---------------------------------------------------------------------------
# Autonomous discovery horizon
# ---------------------------------------------------------------------------

def test_calendar_days_counts_from_the_local_date():
    monkeypatch_days = live_lifecycle.calendar_days(14, today=dt.date(2026, 11, 29))
    assert len(monkeypatch_days) == 15
    assert monkeypatch_days[0] == "2026-11-29"
    assert monkeypatch_days[-1] == "2026-12-13"


def test_calendar_days_start_date_is_not_reinterpreted():
    days = live_lifecycle.calendar_days(14, start_date="2026-11-28")
    assert days[0] == "2026-11-28"
    assert days[-1] == "2026-12-12"


# ---------------------------------------------------------------------------
# SQL selection agrees with the local calendar date
# ---------------------------------------------------------------------------

def test_sql_selection_matches_local_calendar_date_matrix():
    instants = {
        "utc_eve": "2026-11-27T22:59:59+00:00",  # 23:59:59 local on the 27th
        "lagos_midnight": "2026-11-27T23:00:00+00:00",  # 00:00 local on the 28th
        "utc_late": "2026-11-28T22:59:59+00:00",  # 23:59:59 local on the 28th
        "lagos_end": "2026-11-28T23:00:00+00:00",  # 00:00 local on the 29th
        "plus_two": "2026-11-28T01:30:00+02:00",  # 00:30 local on the 28th
        "minus_three": "2026-11-27T21:00:00-03:00",  # 01:00 local on the 28th
    }
    for name, instant in instants.items():
        _seed(name, instant)

    for local_date in ("2026-11-27", "2026-11-28", "2026-11-29"):
        expected = sorted(
            f"football:api_football:{name}"
            for name in instants
            if time_utils.in_local_day(lstore.get_lifecycle(f"football:api_football:{name}")["kickoff_at"], local_date)
        )
        selected = sorted(row["lifecycle_id"] for row in lstore.list_lifecycles(kickoff_on=local_date))
        assert selected == expected, f"SQL selection disagreed for {local_date}"

    assert [row["lifecycle_id"] for row in lstore.list_lifecycles(kickoff_on="2026-11-27")] == [
        "football:api_football:utc_eve"
    ]
    assert sorted(row["lifecycle_id"] for row in lstore.list_lifecycles(kickoff_on="2026-11-28")) == [
        "football:api_football:lagos_midnight",
        "football:api_football:minus_three",
        "football:api_football:plus_two",
        "football:api_football:utc_late",
    ]


def test_storage_keeps_the_kickoff_instant_unchanged():
    """Formatting/selecting a date never rewrites the stored instant."""
    seeded = _seed("stable", "2026-11-28T00:30:00+00:00")
    assert seeded["kickoff_at"] == "2026-11-28T00:30:00+00:00"
    assert telegram_bot.format_fixture_date(seeded["kickoff_at"]) == "2026-11-28"
    assert telegram_bot.format_fixture_time({"fixture": {"date": seeded["kickoff_at"]}}) == "01:30 WAT"
    assert lstore.get_lifecycle("football:api_football:stable")["kickoff_at"] == "2026-11-28T00:30:00+00:00"
