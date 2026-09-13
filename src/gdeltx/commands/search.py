"""`gdeltx search` — article search over the DOC API."""

from __future__ import annotations

from typing import TextIO

from gdeltx.console import Reporter
from gdeltx.models import Article, Record
from gdeltx.output import Column, Format, TableSpec, write
from gdeltx.sources.doc import Sort, search
from gdeltx.sources.http import HttpClient


def _date(record: Record) -> str:
    moment = record.published_at if isinstance(record, Article) else None
    return moment.strftime("%Y-%m-%d %H:%M") if moment else ""


TABLE = TableSpec(
    columns=[
        Column("Date", _date, no_wrap=True),
        Column("Source", "domain", no_wrap=True),
        Column("Title", "title"),
    ],
    empty_message="No results.",
)


def run(
    query: str,
    *,
    http: HttpClient,
    reporter: Reporter,
    fmt: Format,
    since: str | None = None,
    until: str | None = None,
    max_records: int = 75,
    sort: Sort = Sort.RELEVANCE,
    domains: tuple[str, ...] = (),
    languages: tuple[str, ...] = (),
    countries: tuple[str, ...] = (),
    include_raw: bool = False,
    stream: TextIO | None = None,
) -> int:
    meta, records = search(
        http,
        query,
        reporter=reporter,
        since=since,
        until=until,
        max_records=max_records,
        sort=sort,
        domains=domains,
        languages=languages,
        countries=countries,
    )
    if fmt is Format.TABLE:
        return write(records, fmt=fmt, meta=meta, spec=TABLE, stream=stream)
    return write(records, fmt=fmt, meta=meta, stream=stream, include_raw=include_raw)
