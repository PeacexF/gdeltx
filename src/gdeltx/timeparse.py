"""Time range resolution.

Accepts relative shorthands (``24h``, ``7d``, ``30d``, ``1y``), ISO-8601
instants, and GDELT's own ``YYYYMMDDHHMMSS`` stamp. All results are UTC.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from gdeltx.errors import InputError

GDELT_STAMP = "%Y%m%d%H%M%S"

_DURATION = re.compile(r"^(?P<value>\d+)(?P<unit>[hdwmy])$", re.IGNORECASE)

_UNIT_HOURS = {"h": 1, "d": 24, "w": 24 * 7, "m": 24 * 30, "y": 24 * 365}

_DURATION_HINT = "Expected examples: 24h, 7d, 30d, 1y"
_INSTANT_HINT = (
    "Expected an ISO-8601 timestamp (2026-01-31, 2026-01-31T12:00:00Z) or 20260131120000"
)


def is_duration(text: str) -> bool:
    return _DURATION.match(text.strip()) is not None


def parse_duration(text: str) -> timedelta:
    match = _DURATION.match(text.strip())
    if match is None:
        raise InputError(f'invalid time range: "{text}"', hint=_DURATION_HINT)
    value = int(match["value"])
    if value == 0:
        raise InputError(f'invalid time range: "{text}" (must be greater than zero)')
    return timedelta(hours=value * _UNIT_HOURS[match["unit"].lower()])


def parse_instant(text: str) -> datetime:
    raw = text.strip()
    if re.fullmatch(r"\d{14}", raw):
        return datetime.strptime(raw, GDELT_STAMP).replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise InputError(f'invalid timestamp: "{text}"', hint=_INSTANT_HINT) from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def resolve_range(
    since: str | None = None,
    until: str | None = None,
    *,
    default: timedelta = timedelta(days=1),
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    reference = now or datetime.now(UTC)
    end = parse_instant(until) if until else reference

    if since is None:
        start = end - default
    elif _DURATION.match(since.strip()):
        start = end - parse_duration(since)
    else:
        start = parse_instant(since)

    if start >= end:
        raise InputError(
            f"empty time range: {to_stamp(start)} is not before {to_stamp(end)}",
            hint="--since must resolve to a moment before --until.",
        )
    return start, end


def to_stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(GDELT_STAMP)
