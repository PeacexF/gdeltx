from __future__ import annotations

from datetime import datetime

from gdeltx.models.result import Record


class Source(Record):
    domain: str
    articles: int
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    country: str | None = None
    language: str | None = None
    average_tone: float | None = None
    topics: dict[str, int] | None = None
    query: str
