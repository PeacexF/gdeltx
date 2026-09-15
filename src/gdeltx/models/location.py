from __future__ import annotations

from datetime import datetime

from gdeltx.models.result import Record


class Location(Record):
    name: str
    level: str
    location_type: int | None = None
    country_code: str | None = None
    adm1: str | None = None
    feature_id: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    count: int
    sources: int
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    query: str
