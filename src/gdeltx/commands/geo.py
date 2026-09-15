"""`gdeltx geo` — where GKG coverage naming a query takes place.

The GEO 2.0 API no longer answers (API-NOTES §6), so locations come from the
geocoded ``V2ENHANCEDLOCATIONS`` of the same GKG records `entities` reads.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable
from datetime import datetime
from enum import StrEnum
from typing import TextIO

from rich.console import Console
from rich.text import Text

from gdeltx.analysis.aggregate import LocationTally
from gdeltx.console import Reporter
from gdeltx.models import Location, Record, RequestMeta
from gdeltx.output import Column, Format, TableSpec, write
from gdeltx.parsers.gkg import parse_locations, published_at
from gdeltx.sources.files import FileFetcher, FilePlan, read_matching_gkg
from gdeltx.sources.files.readers import Row
from gdeltx.timeparse import to_stamp


class Level(StrEnum):
    COUNTRY = "country"
    STATE = "state"
    CITY = "city"


def _coordinate(attribute: str) -> Callable[[Record], str]:
    def render(record: Record) -> str:
        value = getattr(record, attribute, None)
        return "" if value is None else f"{value:.4f}"

    return render


TABLE = TableSpec(
    columns=[
        Column("Location", "name", no_wrap=True, min_width=20, max_width=38),
        Column("Articles", "count", justify="right", no_wrap=True, min_width=8),
        Column("Latitude", _coordinate("latitude"), justify="right", no_wrap=True),
        Column("Longitude", _coordinate("longitude"), justify="right", no_wrap=True),
    ],
    empty_message="No locations found.",
)


def tally(rows: Iterable[Row]) -> LocationTally:
    counts = LocationTally()
    for row in rows:
        counts.add_article(
            parse_locations(row.get("V2ENHANCEDLOCATIONS")),
            domain=row.get("V2SOURCECOMMONNAME"),
            seen_at=published_at(row),
        )
    return counts


def write_geojson(locations: list[Location], meta: RequestMeta, stream: TextIO) -> int:
    """RFC 7946 FeatureCollection; places GDELT could not geocode are left out."""
    features = [
        {
            "type": "Feature",
            # GeoJSON positions are longitude first.
            "geometry": {"type": "Point", "coordinates": [loc.longitude, loc.latitude]},
            "properties": {
                k: v
                for k, v in loc.export(include_raw=False).items()
                if k not in {"latitude", "longitude", "raw"}
            },
        }
        for loc in locations
        if loc.latitude is not None and loc.longitude is not None
    ]
    document = {"type": "FeatureCollection", "gdeltx": meta.export(), "features": features}
    try:
        json.dump(document, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    except BrokenPipeError:
        return 0
    return len(features)


def run(
    term: str,
    *,
    fetcher: FileFetcher,
    file_plan: FilePlan,
    reporter: Reporter,
    fmt: Format,
    start: datetime,
    end: datetime,
    top: int = 25,
    levels: set[str] | None = None,
    geojson: bool = False,
    stream: TextIO | None = None,
) -> int:
    rows = read_matching_gkg(fetcher, file_plan, term, reporter=reporter)
    try:
        counts = tally(rows)
    finally:
        rows.close()
    locations = counts.ranked(query=term, levels=levels, top=top)

    meta = RequestMeta(
        query=term,
        endpoint="gkg",
        parameters={
            "dataset": "gkg",
            "start": to_stamp(start),
            "end": to_stamp(end),
            "files": file_plan.count,
            "match": "plain text, case-insensitive",
            "field": "V2ENHANCEDLOCATIONS",
            "levels": sorted(levels) if levels else "all",
            "top": top,
        },
    )
    target = stream or sys.stdout
    if geojson:
        if skipped := sum(loc.latitude is None or loc.longitude is None for loc in locations):
            reporter.warn(f"{skipped} locations had no coordinates and are not in the GeoJSON.")
        return write_geojson(locations, meta, target)
    if fmt is not Format.TABLE:
        return write(locations, fmt=fmt, meta=meta, stream=target)

    written = write(locations, fmt=fmt, meta=meta, spec=TABLE, stream=target)
    if written:
        console = Console(file=target, highlight=False)
        console.print()
        footer = (
            f"Counts are articles, from {counts.articles} matching GKG records in "
            f"{file_plan.count} files; coordinates are GDELT's geocoding."
        )
        console.print(Text(footer, style="dim"), soft_wrap=True)
    return written
