"""DOC 2.0 ``artlist`` and ``timelinevolraw`` JSON → models."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

from gdeltx.errors import ParseError
from gdeltx.models import Article
from gdeltx.parsers.context import parse_seendate


def article_rows(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        raise ParseError(
            f"unexpected DOC response: expected a JSON object, got {type(payload).__name__}"
        )
    # A query with no matches comes back as `{}`, not an empty list.
    rows = payload.get("articles", [])
    if not isinstance(rows, list):
        raise ParseError("unexpected DOC response: 'articles' is not a list")
    return rows


def parse_articles(
    payload: Any,
    query: str,
    *,
    on_skip: Callable[[str], None] | None = None,
) -> Iterator[Article]:
    for index, row in enumerate(article_rows(payload)):
        if not isinstance(row, dict) or not row.get("url"):
            if on_skip is not None:
                on_skip(f"skipped DOC record {index}: no url")
            continue
        yield Article(
            url=str(row["url"]),
            title=_text(row.get("title")),
            domain=_text(row.get("domain")),
            language=_text(row.get("language")),
            source_country=_text(row.get("sourcecountry")),
            published_at=parse_seendate(row.get("seendate")),
            query=query,
            raw=row,
        )


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_timeline(payload: Any) -> Iterator[tuple[datetime, int]]:
    """``timelinevolraw`` → ``(date, article_count)`` points, oldest first.

    The response nests one series per requested mode; ``timelinevolraw`` asks
    for exactly one ("Article Count"), so only ``timeline[0]`` is read.
    """
    if not isinstance(payload, dict):
        raise ParseError(
            f"unexpected DOC response: expected a JSON object, got {type(payload).__name__}"
        )
    series = payload.get("timeline", [])
    if not isinstance(series, list):
        raise ParseError("unexpected DOC response: 'timeline' is not a list")
    if not series:
        return
    first = series[0]
    points = first.get("data", []) if isinstance(first, dict) else None
    if not isinstance(points, list):
        raise ParseError("unexpected DOC response: 'timeline[0].data' is not a list")
    for point in points:
        if not isinstance(point, dict):
            continue
        when = parse_seendate(point.get("date"))
        value = point.get("value")
        if when is None or not isinstance(value, int | float):
            continue
        yield when, int(value)
