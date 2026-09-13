from __future__ import annotations

from datetime import datetime

from gdeltx.models.result import Record


class ContextSnippet(Record):
    url: str
    title: str | None = None
    domain: str | None = None
    language: str | None = None
    published_at: datetime | None = None
    context: str = ""
    query: str
