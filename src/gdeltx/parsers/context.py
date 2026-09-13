"""Context 2.0 ``artlist`` JSON → :class:`ContextSnippet`.

A captured response carries both ``sentence`` (the matching sentence) and
``context`` (the surrounding passage). Neither is documented, so each falls
back to the other rather than leaving a record without text.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

from gdeltx.errors import ParseError
from gdeltx.models import ContextSnippet

SENTENCE_KEYS = ("sentence", "snippet")
PASSAGE_KEYS = ("context", "sentence", "snippet")

SEENDATE_FORMAT = "%Y%m%dT%H%M%SZ"


def parse_seendate(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), SEENDATE_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_articles(
    payload: Any,
    query: str,
    *,
    on_skip: Callable[[str], None] | None = None,
) -> Iterator[ContextSnippet]:
    if not isinstance(payload, dict):
        raise ParseError(
            f"unexpected Context response: expected a JSON object, got {type(payload).__name__}"
        )
    # GDELT answers a query with no matches with `{}` rather than an empty list.
    articles = payload.get("articles", [])
    if not isinstance(articles, list):
        raise ParseError("unexpected Context response: 'articles' is not a list")

    for index, row in enumerate(articles):
        if not isinstance(row, dict) or not row.get("url"):
            if on_skip is not None:
                on_skip(f"skipped Context record {index}: no url")
            continue
        yield ContextSnippet(
            url=str(row["url"]),
            title=_text(row.get("title")),
            domain=_text(row.get("domain")),
            language=_text(row.get("language")),
            published_at=parse_seendate(row.get("seendate")),
            sentence=next((str(row[k]) for k in SENTENCE_KEYS if row.get(k)), None),
            context=next((str(row[k]) for k in PASSAGE_KEYS if row.get(k)), ""),
            query=query,
            raw=row,
        )


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
