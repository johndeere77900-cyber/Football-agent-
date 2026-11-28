"""
Shared UTC Timestamp Utility for Football Agent.

Centralizes ISO-8601 timestamp parsing, UTC normalization, strict point-in-time
chronological comparison, and the conversion of user-facing local calendar dates
into UTC filter intervals.

Two different things are kept apart on purpose:

* a **kickoff** is an absolute instant, stored and compared in UTC;
* a **calendar date** a user asks about ("today", "2026-11-28") is a local date
  in the configured IANA timezone (``config.PREDICTION_TIMEZONE``, default
  ``Africa/Lagos``).

Querying a local date therefore means converting that local day to the
half-open UTC interval ``[local midnight, next local midnight)`` and filtering
stored instants against it -- never comparing a local date string against the
UTC date of an instant.
"""

import config

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Optional, Tuple

DEFAULT_PREDICTION_TIMEZONE = "Africa/Lagos"


def parse_utc_datetime(ts_val: Any) -> Optional[datetime]:
    """
    Parse a timestamp into a timezone-aware UTC datetime.

    Rules:
    - Accepts ISO-8601 strings containing timezone offsets (e.g. 'Z', '+00:00', '-05:00').
    - Accepts timezone-aware datetime objects.
    - Rejects missing values, empty strings, booleans, and invalid types.
    - Rejects timezone-naive timestamps (dt.tzinfo is None or utcoffset is None).
    - Rejects malformed strings that fail ISO parsing.
    - Converts valid datetimes to UTC.

    This is the single parsing boundary for kickoff instants, and its policy is
    that a naive timestamp is ambiguous: it is rejected instead of being guessed
    as UTC, as the machine's local time, or as the prediction timezone. The same
    rule is enforced when a lifecycle kickoff is written and in the SQL date
    filter on both backends, so storage, SQLite, PostgreSQL and Telegram can
    never disagree about what a naive value means.
    """
    if ts_val is None or isinstance(ts_val, bool):
        return None

    if isinstance(ts_val, datetime):
        if ts_val.tzinfo is None or ts_val.tzinfo.utcoffset(ts_val) is None:
            return None
        return ts_val.astimezone(timezone.utc)

    if not isinstance(ts_val, str):
        return None

    ts_str = ts_val.strip()
    if not ts_str:
        return None

    try:
        iso_str = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            return None
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def is_strictly_before(
    ts_val: Any,
    cutoff_val: Any,
) -> bool:
    """
    Return True if ts_val represents an instant strictly earlier than cutoff_val.

    Enforces:
        ts_val < cutoff_val

    Both ts_val and cutoff_val must parse to valid timezone-aware UTC datetimes.
    Returns False if either timestamp fails parsing.
    """
    dt_ts = parse_utc_datetime(ts_val)
    dt_cutoff = parse_utc_datetime(cutoff_val)

    if dt_ts is None or dt_cutoff is None:
        return False

    return dt_ts < dt_cutoff


def prediction_timezone_name(name: Optional[str] = None) -> str:
    """Return the IANA timezone name used for user-facing calendar dates.

    ``name`` (when given) overrides the configured default. The configured value
    is ``config.PREDICTION_TIMEZONE``, falling back to ``Africa/Lagos``.
    """
    candidate = name if name is not None else getattr(config, "PREDICTION_TIMEZONE", None)
    resolved = str(candidate or "").strip() or DEFAULT_PREDICTION_TIMEZONE
    return resolved


def prediction_timezone(name: Optional[str] = None) -> ZoneInfo:
    """Resolve the user-facing calendar timezone as an IANA ``ZoneInfo``.

    A fixed ``UTC+1`` offset is never substituted for the real zone: DST rules
    and historical offsets must come from the tz database. An unknown name is
    reported instead of silently falling back, so a misconfigured deployment is
    visible rather than quietly selecting the wrong fixtures.
    """
    resolved = prediction_timezone_name(name)
    try:
        return ZoneInfo(resolved)
    except (ZoneInfoNotFoundError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            f"Unknown timezone {resolved!r}; PREDICTION_TIMEZONE must be an IANA name "
            f"such as {DEFAULT_PREDICTION_TIMEZONE!r}."
        ) from exc


def to_local_datetime(instant: Any, tz_name: Optional[str] = None) -> Optional[datetime]:
    """Convert an instant to an aware datetime in the user-facing timezone.

    Returns ``None`` for a value that is not a parseable timezone-aware
    timestamp, exactly like :func:`parse_utc_datetime`.
    """
    parsed = parse_utc_datetime(instant)
    if parsed is None:
        return None
    return parsed.astimezone(prediction_timezone(tz_name))


def local_calendar_date(instant: Any, tz_name: Optional[str] = None) -> Optional[date]:
    """The local calendar date an absolute instant falls on, or ``None``."""
    local = to_local_datetime(instant, tz_name)
    return local.date() if local is not None else None


def local_today(now: Optional[datetime] = None, tz_name: Optional[str] = None) -> date:
    """Today's calendar date in the user-facing timezone.

    ``now`` may be injected for deterministic tests; a naive ``now`` is read as
    UTC, matching the rest of the codebase.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(prediction_timezone(tz_name)).date()


def _coerce_local_date(local_date: Any) -> date:
    """Parse a ``YYYY-MM-DD`` string (or ``date``) as a local calendar date."""
    if isinstance(local_date, datetime):
        return local_date.date()
    if isinstance(local_date, date):
        return local_date
    if isinstance(local_date, str):
        candidate = local_date.strip()[:10]
        try:
            return date.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError(f"Invalid calendar date {local_date!r}; expected YYYY-MM-DD.") from exc
    raise ValueError(f"Invalid calendar date {local_date!r}; expected YYYY-MM-DD.")


def local_date_bounds_utc(
    local_date: Any,
    tz_name: Optional[str] = None,
) -> Tuple[datetime, datetime]:
    """The half-open UTC interval ``[start, end)`` covering one local calendar day.

    Both boundaries are computed in the configured zone and then converted to
    UTC, so a zone whose offset changes at that midnight still yields the exact
    instant of the local day boundary.
    """
    tz = prediction_timezone(tz_name)
    day = _coerce_local_date(local_date)
    start_local = datetime.combine(day, time(0, 0, 0), tzinfo=tz)
    end_local = datetime.combine(day + timedelta(days=1), time(0, 0, 0), tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def local_date_interval_iso(
    local_date: Any,
    tz_name: Optional[str] = None,
) -> Tuple[str, str]:
    """The same half-open UTC interval as ISO-8601 strings, for SQL binding."""
    start, end = local_date_bounds_utc(local_date, tz_name)
    return start.isoformat(), end.isoformat()


def in_local_day(
    instant: Any,
    local_date: Any,
    tz_name: Optional[str] = None,
) -> bool:
    """True when an instant falls inside the given local calendar date."""
    parsed = parse_utc_datetime(instant)
    if parsed is None:
        return False
    start, end = local_date_bounds_utc(local_date, tz_name)
    return start <= parsed < end


def format_utc_iso(dt: Optional[datetime]) -> Optional[str]:
    """
    Return ISO-8601 string representation in UTC or None if dt is None.
    Rejects timezone-naive datetimes.
    """
    if dt is None:
        return None
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("Timezone-naive datetime or non-datetime object rejected.")
    return dt.astimezone(timezone.utc).isoformat()
