"""Map time ranges onto GDELT's 15-minute bulk files.

A file stamped ``T`` holds what GDELT added during the 15 minutes ending at
``T``, so a range ``(start, end]`` needs every stamp after ``start`` up to and
including ``end``. URLs are derived from stamps; ``lastupdate.txt`` supplies
the newest stamp that has actually been published.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from gdeltx.errors import ParseError
from gdeltx.sources.http import HttpClient

BASE_URL = "https://data.gdeltproject.org/gdeltv2"
LASTUPDATE_URL = f"{BASE_URL}/lastupdate.txt"
SLOT = timedelta(minutes=15)
STAMP = "%Y%m%d%H%M%S"


class Dataset(StrEnum):
    EVENTS = "events"
    MENTIONS = "mentions"
    GKG = "gkg"

    @property
    def suffix(self) -> str:
        return _SUFFIX[self]


_SUFFIX = {
    Dataset.EVENTS: "export.CSV.zip",
    Dataset.MENTIONS: "mentions.CSV.zip",
    Dataset.GKG: "gkg.csv.zip",
}


@dataclass(frozen=True, slots=True)
class Published:
    dataset: Dataset
    stamp: datetime
    url: str
    size: int
    md5: str


def floor_slot(moment: datetime) -> datetime:
    moment = moment.astimezone(UTC).replace(second=0, microsecond=0)
    return moment - timedelta(minutes=moment.minute % 15)


def slot_url(dataset: Dataset, stamp: datetime) -> str:
    return f"{BASE_URL}/{stamp.astimezone(UTC).strftime(STAMP)}.{dataset.suffix}"


def slots(start: datetime, end: datetime) -> list[datetime]:
    stamp = floor_slot(start) + SLOT
    last = floor_slot(end)
    stamps: list[datetime] = []
    while stamp <= last:
        stamps.append(stamp)
        stamp += SLOT
    return stamps


def parse_lastupdate(text: str) -> list[Published]:
    published: list[Published] = []
    by_suffix = {dataset.suffix: dataset for dataset in Dataset}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 3:
            raise ParseError(f"unexpected lastupdate.txt line: {line!r}")
        size, md5, url = parts
        name = url.rsplit("/", 1)[-1]
        stamp_text, _, suffix = name.partition(".")
        dataset = by_suffix.get(suffix)
        if dataset is None:
            continue
        try:
            stamp = datetime.strptime(stamp_text, STAMP).replace(tzinfo=UTC)
            published.append(Published(dataset, stamp, url, int(size), md5))
        except ValueError:
            raise ParseError(f"unexpected lastupdate.txt line: {line!r}") from None
    if not published:
        raise ParseError("lastupdate.txt listed no known GDELT files")
    return published


def latest_stamp(http: HttpClient) -> datetime:
    fetched = http.get(LASTUPDATE_URL, label="lastupdate.txt", use_cache=False)
    return max(item.stamp for item in parse_lastupdate(fetched.text))
