from datetime import UTC, datetime, timedelta

import pytest

from gdeltx.errors import InputError
from gdeltx.timeparse import parse_duration, parse_instant, resolve_range, to_stamp

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("30d", timedelta(days=30)),
        ("1y", timedelta(days=365)),
        ("2w", timedelta(days=14)),
        ("3M", timedelta(days=90)),
    ],
)
def test_parse_duration(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["banana", "", "7", "d7", "-1d", "1.5d", "7 d"])
def test_parse_duration_rejects_junk(text: str) -> None:
    with pytest.raises(InputError) as excinfo:
        parse_duration(text)
    assert "invalid time range" in str(excinfo.value)


def test_parse_duration_hint_matches_plan() -> None:
    with pytest.raises(InputError) as excinfo:
        parse_duration("banana")
    assert excinfo.value.hint == "Expected examples: 24h, 7d, 30d, 1y"


def test_parse_duration_rejects_zero() -> None:
    with pytest.raises(InputError):
        parse_duration("0d")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("20260131120000", datetime(2026, 1, 31, 12, 0, tzinfo=UTC)),
        ("2026-01-31", datetime(2026, 1, 31, 0, 0, tzinfo=UTC)),
        ("2026-01-31T12:00:00Z", datetime(2026, 1, 31, 12, 0, tzinfo=UTC)),
        ("2026-01-31T12:00:00+00:00", datetime(2026, 1, 31, 12, 0, tzinfo=UTC)),
    ],
)
def test_parse_instant(text: str, expected: datetime) -> None:
    assert parse_instant(text) == expected


def test_parse_instant_accepts_iso_basic_date() -> None:
    assert parse_instant("20260131") == datetime(2026, 1, 31, tzinfo=UTC)


def test_parse_instant_converts_to_utc() -> None:
    assert parse_instant("2026-01-31T13:00:00+01:00") == datetime(2026, 1, 31, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("text", ["banana", "2026-13-01", "2026-01-32", "12345"])
def test_parse_instant_rejects_junk(text: str) -> None:
    with pytest.raises(InputError):
        parse_instant(text)


def test_resolve_range_relative() -> None:
    start, end = resolve_range("7d", now=NOW)
    assert end == NOW
    assert start == NOW - timedelta(days=7)


def test_resolve_range_defaults_to_one_day() -> None:
    start, end = resolve_range(now=NOW)
    assert end - start == timedelta(days=1)


def test_resolve_range_absolute_bounds() -> None:
    start, end = resolve_range("2026-01-01", "2026-02-01", now=NOW)
    assert start == datetime(2026, 1, 1, tzinfo=UTC)
    assert end == datetime(2026, 2, 1, tzinfo=UTC)


def test_resolve_range_rejects_inverted() -> None:
    with pytest.raises(InputError) as excinfo:
        resolve_range("2026-02-01", "2026-01-01", now=NOW)
    assert "empty time range" in str(excinfo.value)


def test_to_stamp_round_trips() -> None:
    assert to_stamp(NOW) == "20260913120000"
    assert parse_instant(to_stamp(NOW)) == NOW


def test_resolve_range_reports_bad_since_as_a_time_range() -> None:
    with pytest.raises(InputError) as excinfo:
        resolve_range("banana", now=NOW)
    assert str(excinfo.value) == 'invalid time range: "banana"'
    assert "24h, 7d, 30d, 1y" in (excinfo.value.hint or "")


def test_resolve_range_reports_bad_until_as_a_timestamp() -> None:
    with pytest.raises(InputError, match="invalid timestamp"):
        resolve_range("7d", "banana", now=NOW)
