"""`gdeltx events` — structured CAMEO events whose actors match a query."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from itertools import islice
from typing import TextIO

from gdeltx.console import Reporter
from gdeltx.models import Event, Record, RequestMeta
from gdeltx.output import Column, Format, TableSpec, write
from gdeltx.parsers.events import MATCH_FIELDS, to_event
from gdeltx.sources.files import FileFetcher, FilePlan, contains_any, read_events, row_mentions
from gdeltx.timeparse import to_stamp


def _action(record: Record) -> str:
    if not isinstance(record, Event):
        return ""
    return record.action or record.event_code or ""


def _date(record: Record) -> str:
    return record.date.isoformat() if isinstance(record, Event) and record.date else ""


TABLE = TableSpec(
    columns=[
        Column("Date", _date, no_wrap=True),
        Column("Actor 1", "actor_1"),
        Column("Action", _action),
        Column("Actor 2", "actor_2"),
        Column("Location", "location"),
    ],
    empty_message="No events found.",
)


def matching_events(
    fetcher: FileFetcher, file_plan: FilePlan, term: str, reporter: Reporter
) -> Iterator[Event]:
    seen: set[str] = set()
    rows = read_events(fetcher, file_plan, reporter=reporter, match=contains_any([term]))
    try:
        for row in rows:
            if not row_mentions(row, MATCH_FIELDS, term):
                continue
            event = to_event(row, term)
            if event is None or event.global_event_id in seen:
                continue
            seen.add(event.global_event_id)
            yield event
    finally:
        rows.close()


def run(
    term: str,
    *,
    fetcher: FileFetcher,
    file_plan: FilePlan,
    reporter: Reporter,
    fmt: Format,
    start: datetime,
    end: datetime,
    max_records: int = 75,
    include_raw: bool = False,
    stream: TextIO | None = None,
) -> int:
    meta = RequestMeta(
        query=term,
        endpoint="events",
        parameters={
            "dataset": "events",
            "start": to_stamp(start),
            "end": to_stamp(end),
            "files": file_plan.count,
            "match": "actor names, plain text, case-insensitive",
            "max": max_records,
        },
    )
    events = matching_events(fetcher, file_plan, term, reporter)
    try:
        records = islice(events, max_records)
        if fmt is Format.TABLE:
            return write(records, fmt=fmt, meta=meta, spec=TABLE, stream=stream)
        return write(records, fmt=fmt, meta=meta, stream=stream, include_raw=include_raw)
    finally:
        events.close()
