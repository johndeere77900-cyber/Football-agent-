"""
Unit tests for shared UTC timestamp utility (time_utils.py).
"""

import pytest
from datetime import datetime, timezone
import time_utils


def test_time_utils_parse_utc_datetime_valid():
    """Verify parsing valid ISO-8601 strings with 'Z' and offset format."""
    dt1 = time_utils.parse_utc_datetime("2025-01-01T12:00:00Z")
    assert dt1 is not None
    assert dt1.tzinfo == timezone.utc
    assert dt1.year == 2025 and dt1.hour == 12

    dt2 = time_utils.parse_utc_datetime("2025-01-01T07:00:00-05:00")
    assert dt2 is not None
    assert dt2.tzinfo == timezone.utc
    assert dt2.hour == 12  # 07:00-05:00 is 12:00 UTC


def test_time_utils_timezone_naive_rejection():
    """Verify timezone-naive timestamps are rejected."""
    assert time_utils.parse_utc_datetime("2025-01-01T12:00:00") is None


def test_time_utils_malformed_rejection():
    """Verify malformed timestamps, booleans, and non-strings are rejected."""
    assert time_utils.parse_utc_datetime("invalid-date") is None
    assert time_utils.parse_utc_datetime(True) is None
    assert time_utils.parse_utc_datetime(12345) is None
    assert time_utils.parse_utc_datetime(None) is None
    assert time_utils.parse_utc_datetime("") is None


def test_time_utils_is_strictly_before_matrix():
    """Verify is_strictly_before matrix conditions."""
    cutoff = "2025-01-01T12:00:00+00:00"

    # Historical timestamp before cutoff
    assert time_utils.is_strictly_before("2025-01-01T11:59:59+00:00", cutoff) is True

    # Historical timestamp exactly equal to cutoff -> False
    assert time_utils.is_strictly_before("2025-01-01T12:00:00+00:00", cutoff) is False

    # Historical timestamp after cutoff -> False
    assert time_utils.is_strictly_before("2025-01-01T12:00:01+00:00", cutoff) is False

    # Different timezone offset representing the same instant -> False (not strictly before)
    # 2025-01-01T13:00:00+01:00 == 2025-01-01T12:00:00Z
    assert time_utils.is_strictly_before("2025-01-01T13:00:00+01:00", cutoff) is False

    # Different timezone offset representing earlier instant -> True
    # 2025-01-01T12:30:00+01:00 == 2025-01-01T11:30:00Z < 12:00:00Z
    assert time_utils.is_strictly_before("2025-01-01T12:30:00+01:00", cutoff) is True


def test_time_utils_format_utc_iso():
    """Verify formatting UTC datetime to ISO string."""
    dt = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert time_utils.format_utc_iso(dt) == "2025-01-01T12:00:00+00:00"
