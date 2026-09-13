from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from gdeltx.models.result import Record


class EntityType(StrEnum):
    PERSON = "person"
    ORGANIZATION = "organization"
    LOCATION = "location"
    COUNTRY = "country"
    THEME = "theme"


class Entity(Record):
    name: str
    type: EntityType
    count: int
    sources: int
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    query: str


class EntityMention(Record):
    name: str
    type: EntityType
    url: str | None = None
    domain: str | None = None
    published_at: datetime | None = None
    query: str
