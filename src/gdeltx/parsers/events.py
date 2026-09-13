"""Events 2.0 export rows: 61 tab-separated columns, no header."""

from __future__ import annotations

from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from gdeltx.models import Event
from gdeltx.parsers.cameo import event_label

# Where a plain-text query must appear for an event to count as matching.
MATCH_FIELDS = ("Actor1Name", "Actor2Name")

EVENT_COLUMNS = (
    "GLOBALEVENTID",
    "SQLDATE",
    "MonthYear",
    "Year",
    "FractionDate",
    "Actor1Code",
    "Actor1Name",
    "Actor1CountryCode",
    "Actor1KnownGroupCode",
    "Actor1EthnicCode",
    "Actor1Religion1Code",
    "Actor1Religion2Code",
    "Actor1Type1Code",
    "Actor1Type2Code",
    "Actor1Type3Code",
    "Actor2Code",
    "Actor2Name",
    "Actor2CountryCode",
    "Actor2KnownGroupCode",
    "Actor2EthnicCode",
    "Actor2Religion1Code",
    "Actor2Religion2Code",
    "Actor2Type1Code",
    "Actor2Type2Code",
    "Actor2Type3Code",
    "IsRootEvent",
    "EventCode",
    "EventBaseCode",
    "EventRootCode",
    "QuadClass",
    "GoldsteinScale",
    "NumMentions",
    "NumSources",
    "NumArticles",
    "AvgTone",
    "Actor1Geo_Type",
    "Actor1Geo_FullName",
    "Actor1Geo_CountryCode",
    "Actor1Geo_ADM1Code",
    "Actor1Geo_ADM2Code",
    "Actor1Geo_Lat",
    "Actor1Geo_Long",
    "Actor1Geo_FeatureID",
    "Actor2Geo_Type",
    "Actor2Geo_FullName",
    "Actor2Geo_CountryCode",
    "Actor2Geo_ADM1Code",
    "Actor2Geo_ADM2Code",
    "Actor2Geo_Lat",
    "Actor2Geo_Long",
    "Actor2Geo_FeatureID",
    "ActionGeo_Type",
    "ActionGeo_FullName",
    "ActionGeo_CountryCode",
    "ActionGeo_ADM1Code",
    "ActionGeo_ADM2Code",
    "ActionGeo_Lat",
    "ActionGeo_Long",
    "ActionGeo_FeatureID",
    "DATEADDED",
    "SOURCEURL",
)


def parse_columns(line: str, columns: tuple[str, ...]) -> dict[str, str | None] | None:
    """Map a tab-separated row onto column names.

    Rows shorter than the layout are malformed and return None. Extra trailing
    columns are ignored so an upstream addition does not break parsing.
    """
    fields = line.rstrip("\r\n").split("\t")
    if len(fields) < len(columns):
        return None
    return {name: (value or None) for name, value in zip(columns, fields, strict=False)}


def parse_event(line: str) -> dict[str, str | None] | None:
    return parse_columns(line, EVENT_COLUMNS)


def to_event(row: dict[str, str | None], query: str) -> Event | None:
    event_id = row.get("GLOBALEVENTID")
    if not event_id:
        return None
    url = row.get("SOURCEURL")
    return Event(
        global_event_id=event_id,
        date=_date(row.get("SQLDATE")),
        date_added=_timestamp(row.get("DATEADDED")),
        actor_1=row.get("Actor1Name"),
        actor_1_code=row.get("Actor1Code"),
        actor_1_country=row.get("Actor1CountryCode"),
        actor_2=row.get("Actor2Name"),
        actor_2_code=row.get("Actor2Code"),
        actor_2_country=row.get("Actor2CountryCode"),
        action=event_label(row.get("EventCode")),
        event_code=row.get("EventCode"),
        event_base_code=row.get("EventBaseCode"),
        event_root_code=row.get("EventRootCode"),
        root_action=event_label(row.get("EventRootCode")),
        is_root_event=None if row.get("IsRootEvent") is None else row.get("IsRootEvent") == "1",
        quad_class=_int(row.get("QuadClass")),
        goldstein_scale=_float(row.get("GoldsteinScale")),
        num_mentions=_int(row.get("NumMentions")),
        num_sources=_int(row.get("NumSources")),
        num_articles=_int(row.get("NumArticles")),
        avg_tone=_float(row.get("AvgTone")),
        location=row.get("ActionGeo_FullName"),
        # Geo country codes are FIPS 10-4, not CAMEO, so there is no label table for them.
        location_country_code=row.get("ActionGeo_CountryCode"),
        latitude=_float(row.get("ActionGeo_Lat")),
        longitude=_float(row.get("ActionGeo_Long")),
        source_url=url,
        source_domain=_domain(url),
        query=query,
        raw=row,
    )


def _date(text: str | None) -> date | None:
    try:
        return datetime.strptime(text or "", "%Y%m%d").date()
    except ValueError:
        return None


def _timestamp(text: str | None) -> datetime | None:
    try:
        return datetime.strptime(text or "", "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _int(text: str | None) -> int | None:
    try:
        return int(text or "")
    except ValueError:
        return None


def _float(text: str | None) -> float | None:
    try:
        return float(text or "")
    except ValueError:
        return None


def _domain(url: str | None) -> str | None:
    host = urlsplit(url).hostname if url else None
    return host.removeprefix("www.") if host else None
