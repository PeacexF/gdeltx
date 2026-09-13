"""Human-facing table rendering."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, TextIO

from rich.console import Console
from rich.table import Table

from gdeltx.models import Record


@dataclass(frozen=True, slots=True)
class Column:
    header: str
    accessor: str | Callable[[Record], Any]
    width: int | None = None
    no_wrap: bool = False
    justify: Literal["left", "right"] = "left"
    min_width: int | None = None
    max_width: int | None = None

    def value(self, record: Record) -> str:
        raw = (
            self.accessor(record)
            if callable(self.accessor)
            else getattr(record, self.accessor, None)
        )
        return "" if raw is None else str(raw)


@dataclass(frozen=True, slots=True)
class TableSpec:
    columns: list[Column] = field(default_factory=list)
    empty_message: str = "No results."


def write_table(
    records: Iterable[Record],
    spec: TableSpec,
    stream: TextIO,
) -> int:
    console = Console(file=stream, highlight=False)
    table = Table(box=None, pad_edge=False, show_edge=False)
    for column in spec.columns:
        table.add_column(
            column.header.upper(),
            width=column.width,
            no_wrap=column.no_wrap,
            overflow="ellipsis",
            justify=column.justify,
            min_width=column.min_width,
            max_width=column.max_width,
        )

    count = 0
    for record in records:
        table.add_row(*(column.value(record) for column in spec.columns))
        count += 1

    if count == 0:
        console.print(spec.empty_message)
        return 0

    console.print(table)
    return count
