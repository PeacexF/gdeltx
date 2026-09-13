from __future__ import annotations

import datetime as dt

from gdeltx.models.result import Record


class Event(Record):
    global_event_id: str
    date: dt.date | None = None
    date_added: dt.datetime | None = None
    actor_1: str | None = None
    actor_1_code: str | None = None
    actor_1_country: str | None = None
    actor_2: str | None = None
    actor_2_code: str | None = None
    actor_2_country: str | None = None
    action: str | None = None
    event_code: str | None = None
    event_base_code: str | None = None
    event_root_code: str | None = None
    root_action: str | None = None
    is_root_event: bool | None = None
    quad_class: int | None = None
    goldstein_scale: float | None = None
    num_mentions: int | None = None
    num_sources: int | None = None
    num_articles: int | None = None
    avg_tone: float | None = None
    location: str | None = None
    location_country_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    source_url: str | None = None
    source_domain: str | None = None
    query: str
