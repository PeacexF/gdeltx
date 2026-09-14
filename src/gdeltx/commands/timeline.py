"""`gdeltx timeline` — article and event activity bucketed over time.

Articles come from DOC `timelinevolraw`, which reaches back to 2017 but only
ever reports counts, never records. Events come from the Phase 4 file layer,
which is bounded by the range guard: a request wider than `files.max_files`
gets its event coverage clipped to the most recent slice that fits, while the
article series still covers the full requested range. Clipped buckets carry
`events=None` rather than `0`, so the two series are never misread as equally
covered.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TextIO

from gdeltx.console import Reporter
from gdeltx.models import Record, RequestMeta, TimelineBucket
from gdeltx.output import Column, Format, TableSpec, write
from gdeltx.parsers.events import MATCH_FIELDS, to_event
from gdeltx.sources import doc
from gdeltx.sources.files import FileFetcher, FilePlan, contains_any, read_events, row_mentions
from gdeltx.sources.files.index import SLOT, slots
from gdeltx.sources.http import HttpClient
from gdeltx.timeparse import to_stamp

BAR_WIDTH = 20


class Bucket(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


# Thresholds are on the requested span, so the choice is stable regardless of
# where the range happens to fall.
_AUTO_DAY = timedelta(days=14)
_AUTO_WEEK = timedelta(days=120)


def choose_bucket(start: datetime, end: datetime) -> Bucket:
    span = end - start
    if span <= _AUTO_DAY:
        return Bucket.DAY
    if span <= _AUTO_WEEK:
        return Bucket.WEEK
    return Bucket.MONTH


def _bucket_start(moment: datetime, bucket: Bucket) -> datetime:
    moment = moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket is Bucket.DAY:
        return moment
    if bucket is Bucket.WEEK:
        return moment - timedelta(days=moment.weekday())
    return moment.replace(day=1)


def _bucket_end(start: datetime, bucket: Bucket) -> datetime:
    if bucket is Bucket.DAY:
        return start + timedelta(days=1)
    if bucket is Bucket.WEEK:
        return start + timedelta(days=7)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def _label(start: datetime, bucket: Bucket) -> str:
    return start.strftime("%Y-%m" if bucket is Bucket.MONTH else "%Y-%m-%d")


def bucket_edges(start: datetime, end: datetime, bucket: Bucket) -> list[tuple[datetime, datetime]]:
    """Calendar-aligned ``(bucket_start, bucket_end)`` pairs covering ``[start, end)``."""
    edges: list[tuple[datetime, datetime]] = []
    cursor = _bucket_start(start, bucket)
    while cursor < end:
        following = _bucket_end(cursor, bucket)
        edges.append((cursor, following))
        cursor = following
    return edges


def _index_of(moment: datetime, starts: list[datetime]) -> int | None:
    index = bisect.bisect_right(starts, moment) - 1
    return index if index >= 0 else None


def _event_counts(
    fetcher: FileFetcher,
    file_plan: FilePlan,
    term: str,
    reporter: Reporter,
    starts: list[datetime],
) -> list[int]:
    counts = [0] * len(starts)
    seen: set[str] = set()
    rows = read_events(fetcher, file_plan, reporter=reporter, match=contains_any([term]))
    try:
        for row in rows:
            if not row_mentions(row, MATCH_FIELDS, term):
                continue
            event = to_event(row, term)
            if event is None or event.global_event_id in seen or event.date_added is None:
                continue
            seen.add(event.global_event_id)
            index = _index_of(event.date_added, starts)
            if index is not None:
                counts[index] += 1
    finally:
        rows.close()
    return counts


def clip_event_window(
    start: datetime,
    end: datetime,
    *,
    max_files: int,
    allow_large: bool,
    reporter: Reporter,
) -> datetime:
    """Narrow the event window to what the range guard allows, ending at ``end``.

    Uses the theoretical slot count rather than a real ``FilePlan`` so the
    decision needs no network call; the plan built afterwards can only be
    smaller once capped to what is actually published.
    """
    if allow_large:
        return start
    total = len(slots(start, end))
    if total <= max_files:
        return start
    clipped = end - max_files * SLOT
    reporter.warn(
        f"event coverage limited to the last {max_files} files "
        f"({to_stamp(clipped)} to {to_stamp(end)}); the article series still covers the full "
        "range. Pass --allow-large to widen it."
    )
    return clipped


def _bar(value: int, peak: int) -> str:
    if peak <= 0 or value <= 0:
        return ""
    length = max(1, round(value / peak * BAR_WIDTH))
    return "█" * length


def _make_table(bucket: Bucket) -> TableSpec:
    # Every bucket in the range is always emitted (zero, not omitted), so
    # `empty_message` never actually renders; the default is fine as-is.
    columns = [
        Column(bucket.value.upper(), "bucket", no_wrap=True),
        Column("Articles", "articles", justify="right", no_wrap=True),
        Column("Events", _events_cell, justify="right", no_wrap=True),
    ]
    return TableSpec(columns=columns)


def _events_cell(record: Record) -> str:
    events = record.events if isinstance(record, TimelineBucket) else None
    return "" if events is None else str(events)


def build_buckets(
    query: str,
    edges: list[tuple[datetime, datetime]],
    bucket: Bucket,
    articles: list[int],
    events: list[int | None],
) -> Iterator[TimelineBucket]:
    for (start, end), article_count, event_count in zip(edges, articles, events, strict=True):
        yield TimelineBucket(
            bucket=_label(start, bucket),
            start=start,
            end=end,
            articles=article_count,
            events=event_count,
            query=query,
        )


def run(
    term: str,
    *,
    http: HttpClient,
    fetcher: FileFetcher,
    file_plan: FilePlan,
    reporter: Reporter,
    fmt: Format,
    start: datetime,
    end: datetime,
    events_start: datetime,
    bucket: Bucket | None = None,
    bars: bool = False,
    stream: TextIO | None = None,
) -> int:
    chosen = bucket or choose_bucket(start, end)
    edges = bucket_edges(start, end, chosen)
    starts = [edge[0] for edge in edges]

    doc_meta, points = doc.timeline(http, term, reporter=reporter, start=start, end=end)
    article_counts = [0] * len(edges)
    for when, value in points:
        index = _index_of(when, starts)
        if index is not None:
            article_counts[index] += value

    raw_event_counts = _event_counts(fetcher, file_plan, term, reporter, starts)
    event_counts: list[int | None] = [
        raw_event_counts[i] if bucket_start >= events_start else None
        for i, bucket_start in enumerate(starts)
    ]

    meta = RequestMeta(
        query=term,
        endpoint="timeline",
        parameters={
            "bucket": chosen.value,
            "start": to_stamp(start),
            "end": to_stamp(end),
            "events_start": to_stamp(events_start),
            "events_files": file_plan.count,
            "doc": doc_meta.parameters,
        },
    )

    records = list(build_buckets(term, edges, chosen, article_counts, event_counts))
    if fmt is not Format.TABLE:
        return write(records, fmt=fmt, meta=meta, stream=stream)

    spec = _make_table(chosen)
    if bars:
        peak = max(article_counts, default=0)
        bar_by_bucket = {record.bucket: _bar(record.articles, peak) for record in records}
        bar_column = Column("", lambda r: bar_by_bucket.get(r.bucket, ""), no_wrap=True)
        spec = TableSpec(columns=[*spec.columns, bar_column], empty_message=spec.empty_message)
    return write(records, fmt=fmt, meta=meta, spec=spec, stream=stream)
