from datetime import datetime, timedelta, timezone

from mship.util.duration import format_relative


_NOW = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)


def test_just_now_for_future():
    result = format_relative(_NOW + timedelta(seconds=5), _now=_NOW)
    assert result == "just now"


def test_zero_seconds_is_just_now():
    assert format_relative(_NOW, _now=_NOW) == "just now"


def test_under_a_minute_shows_seconds():
    assert format_relative(_NOW - timedelta(seconds=30), _now=_NOW) == "30s ago"


def test_minutes():
    assert format_relative(_NOW - timedelta(minutes=5), _now=_NOW) == "5m ago"


def test_hours_and_minutes():
    assert format_relative(_NOW - timedelta(hours=3, minutes=12), _now=_NOW) == "3h 12m ago"


def test_hours_no_minutes():
    assert format_relative(_NOW - timedelta(hours=3), _now=_NOW) == "3h ago"


def test_days_and_hours():
    assert format_relative(_NOW - timedelta(days=2, hours=4), _now=_NOW) == "2d 4h ago"


def test_far_past():
    assert format_relative(_NOW - timedelta(days=45), _now=_NOW) == "30+ days ago"


def test_naive_datetime_treated_as_utc():
    # No tzinfo → interpret as UTC
    naive = datetime(2026, 4, 13, 11, 55, 0)
    assert format_relative(naive, _now=_NOW) == "5m ago"
