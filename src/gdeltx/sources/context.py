"""Context 2.0 client.

The API only reaches back 72 hours and has no paging, so both the time range
and ``maxrecords`` are clamped here, loudly, rather than failing upstream.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from gdeltx.console import Reporter
from gdeltx.errors import InputError
from gdeltx.models import ContextSnippet, RequestMeta
from gdeltx.parsers.context import parse_articles
from gdeltx.sources.http import HttpClient
from gdeltx.timeparse import is_duration, parse_duration, resolve_range, to_stamp

ENDPOINT = "https://api.gdeltproject.org/api/v2/context/context"
COVERAGE = timedelta(hours=72)
DEFAULT_SPAN = timedelta(hours=24)
MAX_RECORDS = 200


class Sort(StrEnum):
    RELEVANCE = "relevance"
    DATEDESC = "datedesc"
    DATEASC = "dateasc"


_SORT_PARAM = {Sort.DATEDESC: "DateDesc", Sort.DATEASC: "DateAsc"}


def window_params(
    since: str | None,
    until: str | None,
    *,
    reporter: Reporter,
    now: datetime | None = None,
) -> dict[str, str]:
    # A relative range goes out as `timespan` so the cache key is stable
    # between runs; only absolute ranges need explicit datetimes.
    if until is None and (since is None or is_duration(since)):
        span = parse_duration(since) if since else DEFAULT_SPAN
        if span > COVERAGE:
            reporter.warn(
                f"Context API only covers the last 72 hours; --since {since} clamped to 72h."
            )
            span = COVERAGE
        return {"timespan": f"{int(span.total_seconds() // 3600)}h"}

    reference = now or datetime.now(UTC)
    start, end = resolve_range(since, until, default=DEFAULT_SPAN, now=reference)
    # Round the floor up a minute so it is still inside the window on arrival.
    floor = (reference - COVERAGE + timedelta(minutes=1)).replace(second=0, microsecond=0)
    if end <= floor:
        raise InputError(
            f"time range ends at {to_stamp(end)}, outside the Context API's 72-hour window",
            hint="Use `gdeltx search` for older coverage.",
        )
    if start < floor:
        reporter.warn(
            f"Context API only covers the last 72 hours; start clamped to {to_stamp(floor)}."
        )
        start = floor
    return {"startdatetime": to_stamp(start), "enddatetime": to_stamp(end)}


def build_params(
    query: str,
    window: dict[str, str],
    *,
    max_records: int,
    sort: Sort,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": max_records,
        **window,
    }
    if sort in _SORT_PARAM:
        params["sort"] = _SORT_PARAM[sort]
    return params


def search(
    http: HttpClient,
    query: str,
    *,
    reporter: Reporter,
    since: str | None = None,
    until: str | None = None,
    max_records: int = 75,
    sort: Sort = Sort.RELEVANCE,
    now: datetime | None = None,
) -> tuple[RequestMeta, Iterator[ContextSnippet]]:
    if max_records > MAX_RECORDS:
        reporter.warn(
            f"Context API returns at most {MAX_RECORDS} records; --max {max_records} clamped."
        )
        max_records = MAX_RECORDS

    window = window_params(since, until, reporter=reporter, now=now)
    params = build_params(query, window, max_records=max_records, sort=sort)
    fetched = http.get(ENDPOINT, params=params, label="Context API")
    payload = fetched.json()

    meta = RequestMeta(
        query=query, endpoint="context", parameters=params, cached=fetched.from_cache
    )
    return meta, parse_articles(payload, query, on_skip=reporter.warn)
