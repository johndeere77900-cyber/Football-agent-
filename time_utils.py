"""
Shared UTC Timestamp Utility for Football Agent.

Centralizes ISO-8601 timestamp parsing, UTC normalization,
and strict point-in-time chronological comparison across all historical modules.
"""

from datetime import datetime, timezone
from typing import Any, Optional


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
