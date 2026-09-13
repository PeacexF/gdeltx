"""`gdeltx sources` — coverage of a query grouped by publishing domain.

``--from doc`` aggregates the exact articles ``search`` returns for the same
arguments, so the counts reconcile with it. ``--from gkg`` aggregates the GKG
records ``entities`` reads, which adds tone and themes but not language or
country.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import TextIO

from rich.console import Console
from rich.text import Text

from gdeltx.analysis.aggregate import SourceTally
from gdeltx.console import Reporter
from gdeltx.models import Record, RequestMeta, Source
from gdeltx.output import Column, Format, TableSpec, write
from gdeltx.parsers.gkg import parse_mentions, parse_tone, published_at
from gdeltx.sources import doc
from gdeltx.sources.files import FileFetcher, FilePlan, read_matching_gkg
from gdeltx.sources.http import HttpClient
from gdeltx.timeparse import to_stamp


class Origin(StrEnum):
    DOC = "doc"
    GKG = "gkg"


def _moment(attribute: str) -> Callable[[Record], str]:
    def render(record: Record) -> str:
        value = getattr(record, attribute, None)
        return value.strftime("%Y-%m-%d %H:%M") if isinstance(value, datetime) else ""

    return render


def _tone(record: Record) -> str:
    tone = record.average_tone if isinstance(record, Source) else None
    return "" if tone is None else f"{tone:+.2f}"


def _top_theme(record: Record) -> str:
    topics = record.topics if isinstance(record, Source) else None
    return next(iter(topics), "") if topics else ""


# Tables fit 80 columns: first-seen and the topic breakdown stay in machine output,
# and free-text columns are capped. Rich only shrinks wrapping columns, so without
# the caps it would crop rows or squeeze a column out silently.
_SOURCE = Column("Source", "domain", no_wrap=True, min_width=14, max_width=20)
_ARTICLES = Column("Articles", "articles", justify="right", no_wrap=True, min_width=8)
_LAST_SEEN = Column("Last seen", _moment("last_seen"), no_wrap=True, min_width=16)

TABLES = {
    Origin.DOC: TableSpec(
        columns=[
            _SOURCE,
            _ARTICLES,
            Column("Country", "country", no_wrap=True, min_width=10, max_width=16),
            Column("Language", "language", no_wrap=True, min_width=8, max_width=10),
            _LAST_SEEN,
        ],
        empty_message="No sources found.",
    ),
    Origin.GKG: TableSpec(
        columns=[
            _SOURCE,
            _ARTICLES,
            Column("Tone", _tone, justify="right", no_wrap=True, min_width=6),
            Column("Top theme", _top_theme, no_wrap=True, min_width=12, max_width=20),
            _LAST_SEEN,
        ],
        empty_message="No sources found.",
    ),
}


def _emit(
    tally: SourceTally,
    *,
    origin: Origin,
    query: str,
    meta: RequestMeta,
    fmt: Format,
    top: int,
    footer: str,
    reporter: Reporter,
    stream: TextIO | None,
) -> int:
    if tally.unattributed:
        reporter.warn(f"{tally.unattributed} articles had no domain and are not counted.")
    sources = tally.ranked(query=query, top=top)
    if fmt is not Format.TABLE:
        return write(sources, fmt=fmt, meta=meta, stream=stream)
    target = stream or sys.stdout
    written = write(sources, fmt=fmt, meta=meta, spec=TABLES[origin], stream=target)
    if written:
        console = Console(file=target, highlight=False)
        console.print()
        console.print(Text(footer, style="dim"), soft_wrap=True)
    return written


def run_doc(
    query: str,
    *,
    http: HttpClient,
    reporter: Reporter,
    fmt: Format,
    since: str | None = None,
    until: str | None = None,
    max_records: int = 250,
    sort: doc.Sort = doc.Sort.RELEVANCE,
    domains: tuple[str, ...] = (),
    languages: tuple[str, ...] = (),
    countries: tuple[str, ...] = (),
    top: int = 25,
    stream: TextIO | None = None,
) -> int:
    meta, articles = doc.search(
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
    tally = SourceTally()
    for article in articles:
        tally.add(
            domain=article.domain,
            seen_at=article.published_at,
            country=article.source_country,
            language=article.language,
        )
    meta.parameters = {**meta.parameters, "group_by": "domain", "max": max_records, "top": top}
    footer = (
        f"{tally.articles} articles from {len(tally.ranked(query=query))} sources; "
        f"the same set `search --max {max_records}` returns."
    )
    return _emit(
        tally,
        origin=Origin.DOC,
        query=query,
        meta=meta,
        fmt=fmt,
        top=top,
        footer=footer,
        reporter=reporter,
        stream=stream,
    )


def run_gkg(
    term: str,
    *,
    fetcher: FileFetcher,
    file_plan: FilePlan,
    reporter: Reporter,
    fmt: Format,
    start: datetime,
    end: datetime,
    top: int = 25,
    stream: TextIO | None = None,
) -> int:
    tally = SourceTally()
    rows = read_matching_gkg(fetcher, file_plan, term, reporter=reporter)
    try:
        for row in rows:
            tone = parse_tone(row.get("V15TONE"))
            themes = parse_mentions(row.get("V2ENHANCEDTHEMES") or row.get("V1THEMES"))
            tally.add(
                domain=row.get("V2SOURCECOMMONNAME"),
                seen_at=published_at(row),
                tone=tone.tone if tone else None,
                themes=(theme.name for theme in themes),
            )
    finally:
        rows.close()

    meta = RequestMeta(
        query=term,
        endpoint="gkg",
        parameters={
            "dataset": "gkg",
            "start": to_stamp(start),
            "end": to_stamp(end),
            "files": file_plan.count,
            "match": "plain text, case-insensitive",
            "group_by": "domain",
            "top": top,
        },
    )
    footer = (
        f"{tally.articles} matching GKG records from {len(tally.ranked(query=term))} sources "
        f"in {file_plan.count} files; tone is the mean GKG document tone."
    )
    return _emit(
        tally,
        origin=Origin.GKG,
        query=term,
        meta=meta,
        fmt=fmt,
        top=top,
        footer=footer,
        reporter=reporter,
        stream=stream,
    )
