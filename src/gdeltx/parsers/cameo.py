"""CAMEO code → label lookups from the vendored GDELT tables."""

from __future__ import annotations

from functools import cache
from importlib.resources import files


@cache
def _table(name: str) -> dict[str, str]:
    text = files("gdeltx").joinpath(f"data/cameo/{name}.tsv").read_text(encoding="utf-8")
    table: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        code, _, label = line.partition("\t")
        if code.strip() and label.strip():
            table[code.strip()] = label.strip()
    return table


def event_label(code: str | None) -> str | None:
    return _table("eventcodes").get(code or "")


def country_label(code: str | None) -> str | None:
    return _table("countries").get(code or "")


def actor_type_label(code: str | None) -> str | None:
    return _table("actor_types").get(code or "")
