"""`gdeltx context` — read how a query appears inside articles."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from datetime import datetime
from typing import TextIO

from rich.console import Console
from rich.text import Text

from gdeltx.console import Reporter
from gdeltx.models import ContextSnippet
from gdeltx.output import Format, write
from gdeltx.sources.context import Sort, search
from gdeltx.sources.http import HttpClient

EMPTY_MESSAGE = "No results."

_TOKEN = re.compile(
    r'(?P<neg>-?)(?:(?P<op>[A-Za-z]+\d*):)?(?:"(?P<phrase>[^"]*)"|(?P<word>[^\s()"]+))'
)
_BOOLEAN = frozenset({"OR", "AND", "NOT"})
# Operators whose argument is still text the article must contain.
_TEXT_OPERATORS = re.compile(r"^(near|repeat)\d*$", re.IGNORECASE)


def highlight_terms(query: str) -> list[str]:
    terms: list[str] = []
    for match in _TOKEN.finditer(query):
        if match["neg"]:
            continue
        op, phrase, word = match["op"], match["phrase"], match["word"]
        if op is not None:
            if _TEXT_OPERATORS.match(op) and phrase:
                terms.extend(phrase.split())
            continue
        if phrase:
            terms.append(phrase)
        elif word and word not in _BOOLEAN:
            terms.append(word)
    return list(dict.fromkeys(t for t in terms if t.strip()))


def highlight_pattern(query: str) -> re.Pattern[str] | None:
    terms = sorted(highlight_terms(query), key=len, reverse=True)
    if not terms:
        return None
    alternation = "|".join(re.escape(term) for term in terms)
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)


def write_reading(
    records: Iterable[ContextSnippet],
    stream: TextIO,
    *,
    pattern: re.Pattern[str] | None = None,
) -> int:
    console = Console(file=stream, highlight=False)
    # Hard-wrapping only helps a human at a terminal; in a pipe it would split
    # snippets across lines and break grep.
    soft_wrap = not console.is_terminal

    count = 0
    for record in records:
        if count:
            console.print()
        if record.title:
            console.print(Text(record.title, style="bold"), soft_wrap=soft_wrap)
        console.print(Text(_byline(record), style="dim"), soft_wrap=soft_wrap)
        console.print()
        snippet = Text(record.context or "(no snippet)")
        if pattern is not None:
            snippet.highlight_regex(pattern, style="bold yellow")
        console.print(snippet, soft_wrap=soft_wrap)
        console.print()
        console.print(Text(record.url), soft_wrap=True)
        count += 1

    if count == 0:
        console.print(EMPTY_MESSAGE)
    return count


def _byline(record: ContextSnippet) -> str:
    parts = [record.domain or "unknown source", _format_date(record.published_at)]
    return " · ".join(part for part in parts if part)


def _format_date(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else ""


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
    )
    if fmt is Format.TABLE:
        return write_reading(records, stream or sys.stdout, pattern=highlight_pattern(query))
    return write(records, fmt=fmt, meta=meta, stream=stream, include_raw=include_raw)
