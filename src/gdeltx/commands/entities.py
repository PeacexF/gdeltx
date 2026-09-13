"""`gdeltx entities` — who and what GKG coverage of a query names.

The table aggregates; ``--mentions`` streams one record per article and entity
instead, so the source-level detail behind every count stays exportable.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import datetime
from typing import TextIO

from rich.console import Console
from rich.text import Text

from gdeltx.analysis.aggregate import EntityTally
from gdeltx.console import Reporter
from gdeltx.models import Entity, EntityMention, EntityType, RequestMeta
from gdeltx.output import Format, write
from gdeltx.parsers.gkg import MATCH_FIELDS, published_at, row_entities
from gdeltx.sources.files import FileFetcher, FilePlan, contains_any, read_gkg, row_mentions
from gdeltx.sources.files.readers import Row
from gdeltx.timeparse import to_stamp

SECTIONS = (
    (EntityType.PERSON, "PEOPLE"),
    (EntityType.ORGANIZATION, "ORGANIZATIONS"),
    (EntityType.LOCATION, "LOCATIONS"),
    (EntityType.COUNTRY, "COUNTRIES"),
    (EntityType.THEME, "THEMES"),
)
NAME_WIDTH = 40
EMPTY_MESSAGE = "No entities found."


def matching_rows(
    fetcher: FileFetcher, file_plan: FilePlan, term: str, reporter: Reporter
) -> Iterator[Row]:
    # The cheap line filter can match anywhere in the row (URLs, GCAM); the
    # field check keeps only records that actually name the query.
    for row in read_gkg(fetcher, file_plan, reporter=reporter, match=contains_any([term])):
        if row_mentions(row, MATCH_FIELDS, term):
            yield row


def _domain(row: Row) -> str | None:
    return row.get("V2SOURCECOMMONNAME")


def iter_mentions(
    rows: Iterator[Row], term: str, types: set[EntityType], *, include_raw: bool
) -> Iterator[EntityMention]:
    for row in rows:
        for kind, name in row_entities(row):
            if kind in types:
                yield EntityMention(
                    name=name,
                    type=kind,
                    url=row.get("V2DOCUMENTIDENTIFIER"),
                    domain=_domain(row),
                    published_at=published_at(row),
                    query=term,
                    raw=row if include_raw else None,
                )


def tally(rows: Iterator[Row], types: set[EntityType]) -> EntityTally:
    counts = EntityTally()
    for row in rows:
        counts.add_article(
            (entry for entry in row_entities(row) if entry[0] in types),
            domain=_domain(row),
            seen_at=published_at(row),
        )
    return counts


def write_sections(
    groups: list[tuple[str, list[Entity]]], stream: TextIO, *, footer: str | None = None
) -> int:
    console = Console(file=stream, highlight=False)
    written = 0
    for title, entities in groups:
        if not entities:
            continue
        if written:
            console.print()
        console.print(Text(title, style="bold"))
        console.print("─" * (NAME_WIDTH + 8))
        for entity in entities:
            name = entity.name
            if len(name) > NAME_WIDTH:
                name = name[: NAME_WIDTH - 1] + "…"
            console.print(Text(f"{name:<{NAME_WIDTH}}{entity.count:>8}"))
        written += len(entities)
    if not written:
        console.print(EMPTY_MESSAGE)
    elif footer:
        console.print()
        console.print(Text(footer, style="dim"))
    return written


def run(
    term: str,
    *,
    fetcher: FileFetcher,
    file_plan: FilePlan,
    reporter: Reporter,
    fmt: Format,
    start: datetime,
    end: datetime,
    top: int = 10,
    types: set[EntityType] | None = None,
    mentions: bool = False,
    include_raw: bool = False,
    stream: TextIO | None = None,
) -> int:
    wanted = types or set(EntityType)
    meta = RequestMeta(
        query=term,
        endpoint="gkg",
        parameters={
            "dataset": "gkg",
            "start": to_stamp(start),
            "end": to_stamp(end),
            "files": file_plan.count,
            "match": "plain text, case-insensitive",
            "types": sorted(wanted),
            **({} if mentions else {"top": top}),
        },
    )
    rows = matching_rows(fetcher, file_plan, term, reporter)
    try:
        if mentions:
            records = iter_mentions(rows, term, wanted, include_raw=include_raw)
            return write(records, fmt=fmt, meta=meta, stream=stream, include_raw=include_raw)

        counts = tally(rows, wanted)
        groups = [
            (title, counts.ranked(kind, query=term, top=top))
            for kind, title in SECTIONS
            if kind in wanted
        ]
        if fmt is Format.TABLE:
            footer = (
                f"Counts are articles, from {counts.articles} matching GKG records "
                f"in {file_plan.count} files."
            )
            return write_sections(groups, stream or sys.stdout, footer=footer)
        flat = [entity for _, entities in groups for entity in entities]
        return write(flat, fmt=fmt, meta=meta, stream=stream)
    finally:
        rows.close()
