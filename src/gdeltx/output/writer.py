"""Format dispatch.

Data goes to stdout and nothing else does. JSONL consumes its input lazily so
that large exports never materialise; JSON cannot, by nature of the format.
"""

from __future__ import annotations

import csv as csv_module
import json as json_module
import sys
from collections.abc import Iterable
from enum import StrEnum
from typing import TextIO

from gdeltx.models import Record, RequestMeta
from gdeltx.output.table import TableSpec, write_table


class Format(StrEnum):
    TABLE = "table"
    JSON = "json"
    JSONL = "jsonl"
    CSV = "csv"


def write(
    records: Iterable[Record],
    *,
    fmt: Format | str,
    meta: RequestMeta,
    spec: TableSpec | None = None,
    stream: TextIO | None = None,
    include_meta: bool = False,
    include_raw: bool = False,
) -> int:
    target = stream or sys.stdout
    fmt = Format(fmt)

    try:
        if fmt is Format.TABLE:
            return write_table(records, spec or TableSpec(), target)
        if fmt is Format.JSON:
            return _write_json(records, meta, target, include_raw)
        if fmt is Format.JSONL:
            return _write_jsonl(records, meta, target, include_meta, include_raw)
        return _write_csv(records, meta, spec, target)
    except BrokenPipeError:
        return 0


def _write_json(
    records: Iterable[Record], meta: RequestMeta, stream: TextIO, include_raw: bool
) -> int:
    rows = [record.export(include_raw=include_raw) for record in records]
    payload = {**meta.export(), "results": rows}
    json_module.dump(payload, stream, indent=2, ensure_ascii=False)
    stream.write("\n")
    return len(rows)


def _write_jsonl(
    records: Iterable[Record],
    meta: RequestMeta,
    stream: TextIO,
    include_meta: bool,
    include_raw: bool,
) -> int:
    if include_meta:
        stream.write(json_module.dumps({"_meta": meta.export()}, ensure_ascii=False) + "\n")

    count = 0
    for record in records:
        line = json_module.dumps(record.export(include_raw=include_raw), ensure_ascii=False)
        stream.write(line + "\n")
        count += 1
    return count


def _write_csv(
    records: Iterable[Record], meta: RequestMeta, spec: TableSpec | None, stream: TextIO
) -> int:
    iterator = iter(records)
    first = next(iterator, None)
    if first is None:
        return 0

    if spec is not None and spec.columns:
        headers = [column.header.lower().replace(" ", "_") for column in spec.columns]
        writer = csv_module.writer(stream, lineterminator="\n")
        writer.writerow(headers)
        count = 0
        for record in _prepend(first, iterator):
            writer.writerow([column.value(record) for column in spec.columns])
            count += 1
        return count

    fieldnames = [key for key in first.export(include_raw=False) if key != "raw"]
    writer = csv_module.DictWriter(
        stream, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    count = 0
    for record in _prepend(first, iterator):
        row = {k: _flatten(v) for k, v in record.export(include_raw=False).items()}
        writer.writerow(row)
        count += 1
    return count


def _prepend(first: Record, rest: Iterable[Record]) -> Iterable[Record]:
    yield first
    yield from rest


def _flatten(value: object) -> object:
    if isinstance(value, list | tuple):
        return "; ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json_module.dumps(value, ensure_ascii=False)
    return value
