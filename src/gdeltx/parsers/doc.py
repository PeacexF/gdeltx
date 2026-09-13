"""DOC 2.0 ``artlist`` JSON → :class:`Article`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
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
