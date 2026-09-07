import zoneinfo
from datetime import datetime, timedelta, timezone

import pytest

from app.resources.service import (
    InvalidWindowError,
    parse_and_validate_timestamp,
    validate_window,
)


def test_aligned_windows_accepted() -> None:
    """Aligned UTC 30-minute boundaries are accepted."""
    s = "2026-06-01T10:00:00Z"
    e = "2026-06-01T12:00:00Z"
    s_utc, e_utc = validate_window(s, e)
    assert s_utc == datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    assert e_utc == datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    # Minute 30 boundary
    s30 = "2026-06-01T10:30:00Z"
    e30 = "2026-06-01T11:00:00Z"
    s_res, e_res = validate_window(s30, e30)
    assert s_res == datetime(2026, 6, 1, 10, 30, 0, tzinfo=timezone.utc)
    assert e_res == datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)

    # Datetime instances with tzinfo
    s_dt = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    e_dt = datetime(2026, 6, 1, 10, 30, 0, tzinfo=timezone.utc)
    s_dt_res, e_dt_res = validate_window(s_dt, e_dt)
    assert s_dt_res == s_dt
    assert e_dt_res == e_dt


def test_unaligned_minute_rejected() -> None:
    """Non-30-minute boundaries (e.g. 15 or 45) are rejected."""
    with pytest.raises(InvalidWindowError):
        parse_and_validate_timestamp("2026-06-01T10:15:00Z")

    with pytest.raises(InvalidWindowError):
        parse_and_validate_timestamp("2026-06-01T10:45:00Z")

    with pytest.raises(InvalidWindowError):
        validate_window("2026-06-01T10:00:00Z", "2026-06-01T10:15:00Z")

    with pytest.raises(InvalidWindowError):
        validate_window("2026-06-01T10:15:00Z", "2026-06-01T11:00:00Z")


def test_nonzero_second_rejected() -> None:
    """Non-zero seconds are rejected."""
    with pytest.raises(InvalidWindowError):
        parse_and_validate_timestamp("2026-06-01T10:00:01Z")

    with pytest.raises(InvalidWindowError):
        validate_window("2026-06-01T10:00:01Z", "2026-06-01T11:00:00Z")

    with pytest.raises(InvalidWindowError):
        validate_window("2026-06-01T10:00:00Z", "2026-06-01T11:00:59Z")


def test_nonzero_microsecond_rejected() -> None:
    """Non-zero microseconds are rejected."""
    with pytest.raises(InvalidWindowError):
        parse_and_validate_timestamp("2026-06-01T10:00:00.000001Z")

    with pytest.raises(InvalidWindowError):
        parse_and_validate_timestamp("2026-06-01T10:00:00.123456Z")

    with pytest.raises(InvalidWindowError):
        validate_window("2026-06-01T10:00:00.000001Z", "2026-06-01T11:00:00Z")


def test_naive_timestamp_rejected() -> None:
    """Timestamps without an explicit timezone offset are rejected."""
    with pytest.raises(InvalidWindowError):
        parse_and_validate_timestamp("2026-06-01T10:00:00")

    naive_dt = datetime(2026, 6, 1, 10, 0, 0)
    with pytest.raises(InvalidWindowError):
        parse_and_validate_timestamp(naive_dt)

    with pytest.raises(InvalidWindowError):
        validate_window("2026-06-01T10:00:00", "2026-06-01T11:00:00Z")


def test_ends_equal_starts_rejected() -> None:
    """Zero-duration window (ends_at == starts_at) is rejected."""
    with pytest.raises(InvalidWindowError):
        validate_window("2026-06-01T10:00:00Z", "2026-06-01T10:00:00Z")


def test_ends_less_than_starts_rejected() -> None:
    """Negative-duration window (ends_at < starts_at) is rejected."""
    with pytest.raises(InvalidWindowError):
        validate_window("2026-06-01T12:00:00Z", "2026-06-01T10:00:00Z")


def test_exactly_seven_days_accepted() -> None:
    """A window spanning exactly 7 days is accepted."""
    s = "2026-06-01T10:00:00Z"
    e = "2026-06-08T10:00:00Z"
    s_utc, e_utc = validate_window(s, e)
    assert e_utc - s_utc == timedelta(days=7)


def test_seven_days_plus_thirty_minutes_rejected() -> None:
    """A window spanning 7 days + 30 minutes is rejected."""
    s = "2026-06-01T10:00:00Z"
    e = "2026-06-08T10:30:00Z"
    with pytest.raises(InvalidWindowError):
        validate_window(s, e)


def test_dst_offset_windows() -> None:
    """Explicit-offset timestamps across DST boundaries normalize to correct UTC instants."""
    eastern = zoneinfo.ZoneInfo("America/New_York")

    # Spring-forward transition: 2026-03-08.
    # At 02:00 EST (-05:00), clocks move forward to 03:00 EDT (-04:00).
    dt_before = datetime(2026, 3, 8, 1, 30, tzinfo=eastern)
    dt_after = datetime(2026, 3, 8, 3, 30, tzinfo=eastern)

    s_utc, e_utc = validate_window(dt_before, dt_after)
    assert s_utc == datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)
    assert e_utc == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)
    assert e_utc - s_utc == timedelta(hours=1)

    # Fall-back transition: 2026-11-01.
    # 2026-11-01 00:30 is EDT (-04:00), 2026-11-02 00:30 is EST (-05:00).
    dt_fall1 = datetime(2026, 11, 1, 0, 30, tzinfo=eastern)
    dt_fall2 = datetime(2026, 11, 2, 0, 30, tzinfo=eastern)

    s_fall_utc, e_fall_utc = validate_window(dt_fall1, dt_fall2)
    assert s_fall_utc == datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc)
    assert e_fall_utc == datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc)
    assert e_fall_utc - s_fall_utc == timedelta(hours=25)

    # Assert that the same wall-clock time maps to differing UTC boundaries across DST:
    # 12:00 PM EST (-05:00) -> 17:00:00Z
    # 12:00 PM EDT (-04:00) -> 16:00:00Z
    dt_std_noon = datetime(2026, 3, 7, 12, 0, tzinfo=eastern)
    dt_dst_noon = datetime(2026, 3, 9, 12, 0, tzinfo=eastern)
    assert parse_and_validate_timestamp(dt_std_noon) == datetime(
        2026, 3, 7, 17, 0, tzinfo=timezone.utc
    )
    assert parse_and_validate_timestamp(dt_dst_noon) == datetime(
        2026, 3, 9, 16, 0, tzinfo=timezone.utc
    )

    # Explicit string inputs with -04:00 and -05:00 normalize to correct distinct UTC instants:
    ts_neg4 = "2026-06-01T12:00:00-04:00"
    ts_neg5 = "2026-06-01T12:00:00-05:00"
    utc_neg4 = parse_and_validate_timestamp(ts_neg4)
    utc_neg5 = parse_and_validate_timestamp(ts_neg5)
    assert utc_neg4 == datetime(2026, 6, 1, 16, 0, tzinfo=timezone.utc)
    assert utc_neg5 == datetime(2026, 6, 1, 17, 0, tzinfo=timezone.utc)
    assert utc_neg4 != utc_neg5
