from __future__ import annotations

from datetime import datetime

from gdeltx.models.result import Record


class TimelineBucket(Record):
    bucket: str
    start: datetime
    end: datetime
    articles: int
    # None means the bucket falls outside the file layer's (possibly clipped)
    # event coverage window, never that zero events were counted there.
    events: int | None = None
    query: str
