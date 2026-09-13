"""GKG 2.1 rows: 27 tab-separated columns, no header.

Several columns pack lists: entries separated by ``;``, fields inside an entry
by ``,`` (names, themes, tone) or ``#`` (locations). The ``V2Enhanced*``
variants append the character offset of each mention in the article.
"""

from __future__ import annotations

from dataclasses import dataclass

from gdeltx.parsers.events import parse_columns

GKG_COLUMNS = (
    "GKGRECORDID",
    "V21DATE",
    "V2SOURCECOLLECTIONIDENTIFIER",
    "V2SOURCECOMMONNAME",
    "V2DOCUMENTIDENTIFIER",
    "V1COUNTS",
    "V21COUNTS",
    "V1THEMES",
    "V2ENHANCEDTHEMES",
    "V1LOCATIONS",
    "V2ENHANCEDLOCATIONS",
    "V1PERSONS",
    "V2ENHANCEDPERSONS",
    "V1ORGANIZATIONS",
    "V2ENHANCEDORGANIZATIONS",
    "V15TONE",
    "V21ENHANCEDDATES",
    "V2GCAM",
    "V21SHARINGIMAGE",
    "V21RELATEDIMAGES",
    "V21SOCIALIMAGEEMBEDS",
    "V21SOCIALVIDEOEMBEDS",
    "V21QUOTATIONS",
    "V21ALLNAMES",
    "V21AMOUNTS",
    "V21TRANSLATIONINFO",
    "V2EXTRASXML",
)


@dataclass(frozen=True, slots=True)
class Mention:
    name: str
    offset: int | None


@dataclass(frozen=True, slots=True)
class GkgLocation:
    type: int | None
    name: str
    country_code: str | None
    adm1: str | None
    adm2: str | None
    latitude: float | None
    longitude: float | None
    feature_id: str | None
    offset: int | None


@dataclass(frozen=True, slots=True)
class GkgTone:
    tone: float | None
    positive: float | None
    negative: float | None
    polarity: float | None
    activity_density: float | None
    self_group_density: float | None
    word_count: int | None


def parse_gkg(line: str) -> dict[str, str | None] | None:
    return parse_columns(line, GKG_COLUMNS)


def parse_mentions(value: str | None) -> list[Mention]:
    """``V2ENHANCEDPERSONS``, ``V2ENHANCEDORGANIZATIONS``, ``V2ENHANCEDTHEMES``."""
    mentions: list[Mention] = []
    for entry in _entries(value):
        name, _, offset = entry.rpartition(",")
        if not name:
            name, offset = entry, ""
        if name.strip():
            mentions.append(Mention(name=name.strip(), offset=_int(offset)))
    return mentions


def parse_locations(value: str | None) -> list[GkgLocation]:
    """``V2ENHANCEDLOCATIONS``: type#name#country#adm1#adm2#lat#long#feature#offset."""
    locations: list[GkgLocation] = []
    for entry in _entries(value):
        parts = entry.split("#")
        if len(parts) < 9 or not parts[1].strip():
            continue
        locations.append(
            GkgLocation(
                type=_int(parts[0]),
                name=parts[1].strip(),
                country_code=parts[2] or None,
                adm1=parts[3] or None,
                adm2=parts[4] or None,
                latitude=_float(parts[5]),
                longitude=_float(parts[6]),
                feature_id=parts[7] or None,
                offset=_int(parts[8]),
            )
        )
    return locations


def parse_tone(value: str | None) -> GkgTone | None:
    """``V15TONE``: tone, positive, negative, polarity, activity, self/group, words."""
    if not value:
        return None
    parts = (value.split(",") + [""] * 7)[:7]
    return GkgTone(
        tone=_float(parts[0]),
        positive=_float(parts[1]),
        negative=_float(parts[2]),
        polarity=_float(parts[3]),
        activity_density=_float(parts[4]),
        self_group_density=_float(parts[5]),
        word_count=_int(parts[6]),
    )


def _entries(value: str | None) -> list[str]:
    return [entry for entry in (value or "").split(";") if entry]


def _int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def _float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None
