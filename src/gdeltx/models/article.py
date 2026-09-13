from __future__ import annotations

from datetime import datetime

from gdeltx.models.result import Record


class Article(Record):
    url: str
    title: str | None = None
    domain: str | None = None
    language: str | None = None
    source_country: str | None = None
    published_at: datetime | None = None
    query: str
