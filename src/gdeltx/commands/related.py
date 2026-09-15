"""`gdeltx related` — entities that co-occur with a query in GKG coverage.

Two passes over the same files: the first collects the articles naming the
query, the second counts how many articles in the whole window name each
candidate, which is what the score normalizes by. Cached files make the
second pass local.
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import TextIO

from rich.console import Console
from rich.text import Text

from gdeltx.analysis.cooccurrence import RELATION, SCORE_FORMULA, Cooccurrence
from gdeltx.commands.entities import SECTIONS
from gdeltx.console import Reporter
from gdeltx.models import EntityType, RelatedEntity, RequestMeta
from gdeltx.output import Format, write
from gdeltx.parsers.gkg import published_at, row_entities
from gdeltx.sources.files import FileFetcher, FilePlan, read_gkg, read_matching_gkg
from gdeltx.sources.files.readers import Row
from gdeltx.timeparse import to_stamp

HEADING = f"{RELATION}, in GDELT coverage"
DISCLAIMER = (
    "Co-occurrence in news coverage is not evidence of a real-world relationship, "
    "and GDELT mentions are not verified facts."
)
NAME_WIDTH = 36
EMPTY_MESSAGE = "No co-occurring entities found."


def _entities(row: Row, types: set[EntityType]) -> list[tuple[EntityType, str]]:
    return [entry for entry in row_entities(row) if entry[0] in types]


def score(
    fetcher: FileFetcher,
    file_plan: FilePlan,
    term: str,
    types: set[EntityType],
    reporter: Reporter,
) -> Cooccurrence:
    counts = Cooccurrence(term)
    rows = read_matching_gkg(fetcher, file_plan, term, reporter=reporter)
    try:
        for row in rows:
            counts.add_match(
                _entities(row, types),
                domain=row.get("V2SOURCECOMMONNAME"),
                seen_at=published_at(row),
            )
    finally:
        rows.close()
    if not counts.query_articles:
        return counts

    rows = read_gkg(fetcher, file_plan, reporter=reporter)
    try:
        for row in rows:
            counts.add_article(_entities(row, types))
    finally:
        rows.close()
    return counts


def write_tree(
    term: str,
    groups: list[tuple[str, list[RelatedEntity]]],
    stream: TextIO,
    *,
    footer: str,
) -> int:
    console = Console(file=stream, highlight=False)
    groups = [(title, related) for title, related in groups if related]
    if not groups:
        console.print(EMPTY_MESSAGE)
        return 0

    console.print(Text(term, style="bold"), Text(HEADING, style="dim"), sep="  ")
    console.print(Text(f"{'':<{NAME_WIDTH + 8}}{'ARTICLES':>9}{'SCORE':>8}", style="dim"))
    written = 0
    for index, (title, related) in enumerate(groups):
        last_group = index == len(groups) - 1
        console.print("│")
        console.print(Text(f"{'└──' if last_group else '├──'} {title}"))
        stem = "    " if last_group else "│   "
        for position, entity in enumerate(related):
            branch = "└── " if position == len(related) - 1 else "├── "
            name = entity.name
            if len(name) > NAME_WIDTH:
                name = name[: NAME_WIDTH - 1] + "…"
            console.print(
                Text(f"{stem}{branch}{name:<{NAME_WIDTH}}{entity.together:>9}{entity.score:>8.3f}")
            )
        written += len(related)
    console.print()
    console.print(Text(footer, style="dim"), soft_wrap=True)
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
    min_count: int = 2,
    types: set[EntityType] | None = None,
    stream: TextIO | None = None,
) -> int:
    wanted = types or set(EntityType)
    if fetcher.cache is None or not fetcher.cache.enabled:
        reporter.warn("the cache is off, so every file is downloaded twice.")

    counts = score(fetcher, file_plan, term, wanted, reporter)
    groups = [
        (title, counts.ranked(kind, top=top, min_count=min_count))
        for kind, title in SECTIONS
        if kind in wanted
    ]

    if fmt is Format.TABLE:
        footer = (
            f"{counts.query_articles} of {counts.total_articles} GKG articles in "
            f"{file_plan.count} files name the query. ARTICLES counts those that also name the "
            f"entity; SCORE is {SCORE_FORMULA}, so entities common everywhere rank low. "
            f"{DISCLAIMER}"
        )
        return write_tree(term, groups, stream or sys.stdout, footer=footer)

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
            "top": top,
            "min_count": min_count,
            "relation": RELATION,
            "score": SCORE_FORMULA,
            "excluded": "entities whose name contains the query",
            "note": DISCLAIMER,
        },
    )
    flat = [entity for _, related in groups for entity in related]
    return write(flat, fmt=fmt, meta=meta, stream=stream)
