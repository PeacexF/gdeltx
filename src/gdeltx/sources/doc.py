"""DOC 2.0 article search.

One request returns at most 250 articles and the API has no offset, so a
larger ``--max`` walks through time instead: each page's window ends at the
oldest article the previous page returned (or starts at the newest, when
sorting oldest first). Only the last three months are indexed.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import islice
from typing import Any

from gdeltx.console import Reporter
from gdeltx.errors import InputError
from gdeltx.models import Article, RequestMeta
from gdeltx.parsers.doc import article_rows, parse_articles
from gdeltx.sources.http import HttpClient
from gdeltx.timeparse import is_duration, parse_duration, resolve_range, to_stamp

ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
LABEL = "DOC API"
# Documented as "the last 3 months"; 90 days keeps a clamped range inside it.
COVERAGE = timedelta(days=90)
DEFAULT_SPAN = timedelta(hours=24)
PAGE_SIZE = 250


class Sort(StrEnum):
    RELEVANCE = "relevance"
    DATEDESC = "datedesc"
    DATEASC = "dateasc"
    TONEDESC = "tonedesc"
    TONEASC = "toneasc"


_SORT_PARAM = {
    Sort.DATEDESC: "DateDesc",
    Sort.DATEASC: "DateAsc",
    Sort.TONEDESC: "ToneDesc",
    Sort.TONEASC: "ToneAsc",
}

_OPERATORS = {"domain": "domain", "language": "sourcelang", "country": "sourcecountry"}
_FORBIDDEN = set(' \t\n"():')


@dataclass(frozen=True, slots=True)
class Window:
    start: datetime
    end: datetime
    span: timedelta | None = None

    def params(self) -> dict[str, str]:
        if self.span is not None:
            return {"timespan": _timespan(self.span)}
        return {"startdatetime": to_stamp(self.start), "enddatetime": to_stamp(self.end)}


def _timespan(span: timedelta) -> str:
    # GDELT reads "m" as months, so spans are always sent in days or hours.
    hours = int(span.total_seconds() // 3600)
    return f"{hours // 24}d" if hours % 24 == 0 else f"{hours}h"


def resolve_window(
    since: str | None,
    until: str | None,
    *,
    reporter: Reporter,
    now: datetime | None = None,
) -> Window:
    reference = (now or datetime.now(UTC)).replace(second=0, microsecond=0)

    if until is None and (since is None or is_duration(since)):
        span = parse_duration(since) if since else DEFAULT_SPAN
        if span > COVERAGE:
            reporter.warn(
                f"DOC API only indexes the last 3 months; --since {since} clamped to 90d."
            )
            span = COVERAGE
        return Window(start=reference - span, end=reference, span=span)

    start, end = resolve_range(since, until, default=DEFAULT_SPAN, now=reference)
    floor = reference - COVERAGE + timedelta(minutes=1)
    if end <= floor:
        raise InputError(
            f"time range ends at {to_stamp(end)}, outside the DOC API's 3-month window",
            hint="Article search only reaches back 3 months.",
        )
    if start < floor:
        reporter.warn(
            f"DOC API only indexes the last 3 months; start clamped to {to_stamp(floor)}."
        )
        start = floor
    return Window(start=start, end=end)


def compose_query(
    query: str,
    *,
    domains: tuple[str, ...] = (),
    languages: tuple[str, ...] = (),
    countries: tuple[str, ...] = (),
) -> str:
    if not query.strip():
        raise InputError("empty query")
    parts = [query.strip()]
    for option, values in (("domain", domains), ("language", languages), ("country", countries)):
        terms = [f"{_OPERATORS[option]}:{_operator_value(option, value)}" for value in values]
        if len(terms) == 1:
            parts.append(terms[0])
        elif terms:
            parts.append("(" + " OR ".join(terms) + ")")
    return " ".join(parts)


def _operator_value(option: str, value: str) -> str:
    cleaned = value.strip().lower()
    if option != "domain":
        # GDELT spells multi-word names without spaces: "unitedkingdom".
        cleaned = cleaned.replace(" ", "")
    if not cleaned or _FORBIDDEN & set(cleaned):
        raise InputError(
            f'invalid --{option} value: "{value}"',
            hint="Pass one plain value per flag, e.g. --domain reuters.com --domain bbc.co.uk",
        )
    return cleaned


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
    domains: tuple[str, ...] = (),
    languages: tuple[str, ...] = (),
    countries: tuple[str, ...] = (),
    now: datetime | None = None,
) -> tuple[RequestMeta, Iterator[Article]]:
    composed = compose_query(query, domains=domains, languages=languages, countries=countries)
    window = resolve_window(since, until, reporter=reporter, now=now)
    paginate = max_records > PAGE_SIZE

    if paginate:
        if sort in (Sort.TONEDESC, Sort.TONEASC):
            raise InputError(
                f"--max above {PAGE_SIZE} cannot be combined with --sort {sort}",
                hint="Paging walks through time, so it needs --sort datedesc or dateasc.",
            )
        if sort is Sort.RELEVANCE:
            reporter.warn(f"--max above {PAGE_SIZE} pages through time; results are newest first.")
            sort = Sort.DATEDESC
        pages = math.ceil(max_records / PAGE_SIZE)
        reporter.warn(
            f"--max {max_records} may take up to {pages} DOC API requests; "
            "GDELT rate-limits article search heavily."
        )
        window = Window(start=window.start, end=window.end)

    params = build_params(
        composed, window.params(), max_records=min(max_records, PAGE_SIZE), sort=sort
    )
    fetched = http.get(ENDPOINT, params=params, label=LABEL)
    payload = fetched.json()
    meta = RequestMeta(query=query, endpoint="doc", parameters=params, cached=fetched.from_cache)

    if not paginate:
        records = _unique(parse_articles(payload, query, on_skip=reporter.warn))
        return meta, islice(records, max_records)

    pages_iter = _pages(
        http,
        payload,
        meta,
        composed=composed,
        query=query,
        window=window,
        sort=sort,
        reporter=reporter,
    )
    return meta, islice(pages_iter, max_records)


def _unique(records: Iterator[Article]) -> Iterator[Article]:
    seen: set[str] = set()
    for record in records:
        if record.url not in seen:
            seen.add(record.url)
            yield record


def _pages(
    http: HttpClient,
    payload: Any,
    meta: RequestMeta,
    *,
    composed: str,
    query: str,
    window: Window,
    sort: Sort,
    reporter: Reporter,
) -> Iterator[Article]:
    start, end = window.start, window.end
    seen: set[str] = set()

    while True:
        rows = list(parse_articles(payload, query, on_skip=reporter.warn))
        fresh = 0
        for record in rows:
            if record.url in seen:
                continue
            seen.add(record.url)
            fresh += 1
            yield record

        if len(article_rows(payload)) < PAGE_SIZE:
            return
        dates = [record.published_at for record in rows if record.published_at is not None]
        if not fresh or not dates:
            reporter.warn(
                f"stopped paging after {len(seen)} articles: GDELT returned a page with "
                "no new articles to advance from."
            )
            return

        # Overlap each boundary by a second; duplicates are dropped above.
        if sort is Sort.DATEDESC:
            end = min(dates) + timedelta(seconds=1)
        else:
            start = max(dates) - timedelta(seconds=1)
        if start >= end:
            return

        reporter.debug(f"next DOC page: {to_stamp(start)} to {to_stamp(end)}")
        params = build_params(
            composed,
            Window(start=start, end=end).params(),
            max_records=PAGE_SIZE,
            sort=sort,
        )
        fetched = http.get(ENDPOINT, params=params, label=LABEL)
        meta.cached = meta.cached and fetched.from_cache
        payload = fetched.json()
