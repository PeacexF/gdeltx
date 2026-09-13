"""Row streams over a file plan, with query filtering applied before parsing."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from gdeltx.console import Reporter
from gdeltx.parsers.events import parse_event
from gdeltx.parsers.gkg import parse_gkg
from gdeltx.sources.files.fetch import FileFetcher, FilePlan

Row = dict[str, str | None]


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
