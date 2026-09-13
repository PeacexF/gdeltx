"""Row streams over a file plan, with query filtering applied before parsing."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator

from gdeltx.console import Reporter
from gdeltx.errors import InputError
from gdeltx.parsers.events import parse_event
from gdeltx.parsers.gkg import names_query, parse_gkg
from gdeltx.sources.files.fetch import FileFetcher, FilePlan, contains_any

Row = dict[str, str | None]

# Boolean keywords are only operators in upper case, so "Procter and Gamble" is plain text.
_QUERY_SYNTAX = re.compile(r'[()"]|\b(?:OR|AND|NOT)\b|(?:^|\s)-\S|\b[A-Za-z]+\d*:')


def plain_query(query: str) -> str:
    """Bulk files have no query engine: accept one plain phrase, matched as a substring."""
    term = " ".join(query.split())
    if len(term) >= 2 and term[0] == term[-1] == '"':
        term = term[1:-1].strip()
    if not term:
        raise InputError("empty query")
    if _QUERY_SYNTAX.search(term):
        raise InputError(
            f"this command matches plain text only, but the query uses query syntax: {query}",
            hint='Pass a single name or phrase, e.g. "Company X". '
            "Boolean and operator queries work with `search` and `context`.",
        )
    return term


def row_mentions(row: Row, fields: Iterable[str], term: str) -> bool:
    needle = term.casefold()
    return any(needle in (row.get(field) or "").casefold() for field in fields)


def read_rows(
    fetcher: FileFetcher,
    file_plan: FilePlan,
    parse: Callable[[str], Row | None],
    *,
    reporter: Reporter,
    match: Callable[[str], bool] | None = None,
) -> Iterator[Row]:
    malformed = 0
    try:
        for content in fetcher.iter_files(file_plan):
            for line in content.lines:
                if match is not None and not match(line):
                    continue
                row = parse(line)
                if row is None:
                    malformed += 1
                    continue
                yield row
    finally:
        if malformed:
            reporter.warn(f"skipped {malformed} malformed {file_plan.dataset} rows")


def read_events(fetcher: FileFetcher, file_plan: FilePlan, **kwargs) -> Iterator[Row]:
    return read_rows(fetcher, file_plan, parse_event, **kwargs)


def read_gkg(fetcher: FileFetcher, file_plan: FilePlan, **kwargs) -> Iterator[Row]:
    return read_rows(fetcher, file_plan, parse_gkg, **kwargs)


def read_matching_gkg(
    fetcher: FileFetcher, file_plan: FilePlan, term: str, *, reporter: Reporter
) -> Iterator[Row]:
    # The line filter can hit anywhere in a row (URLs, GCAM); the field check
    # keeps only records that actually name the query.
    rows = read_gkg(fetcher, file_plan, reporter=reporter, match=contains_any([term]))
    try:
        for row in rows:
            if names_query(row, term):
                yield row
    finally:
        rows.close()
