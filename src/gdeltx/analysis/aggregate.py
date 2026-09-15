"""Deterministic entity and source counting.

``count`` is the number of distinct articles naming an entity, not raw
mentions. Names that differ only in case or spacing are one entity, shown in
their most common spelling; nothing fuzzier is merged. Ties always break the
same way, so identical input gives identical output.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from gdeltx.models import Entity, EntityType, Location, Source
from gdeltx.parsers.gkg import LOCATION_LEVELS, GkgLocation


@dataclass(slots=True)
class _Tally:
    articles: int = 0
    spellings: Counter[str] = field(default_factory=Counter)
    domains: set[str] = field(default_factory=set)
    first_seen: datetime | None = None
    last_seen: datetime | None = None


def entity_key(kind: EntityType, name: str) -> tuple[EntityType, str]:
    return kind, " ".join(name.split()).casefold()


class EntityTally:
    def __init__(self) -> None:
        self._tallies: dict[tuple[EntityType, str], _Tally] = {}
        self.articles = 0

    def add_article(
        self,
        entities: Iterable[tuple[EntityType, str]],
        *,
        domain: str | None,
        seen_at: datetime | None,
    ) -> None:
        self.articles += 1
        seen: set[tuple[EntityType, str]] = set()
        for kind, name in entities:
            key = entity_key(kind, name)
            if key in seen or not key[1]:
                continue
            seen.add(key)
            tally = self._tallies.setdefault(key, _Tally())
            tally.articles += 1
            tally.spellings[" ".join(name.split())] += 1
            if domain:
                tally.domains.add(domain)
            if seen_at is not None:
                if tally.first_seen is None or seen_at < tally.first_seen:
                    tally.first_seen = seen_at
                if tally.last_seen is None or seen_at > tally.last_seen:
                    tally.last_seen = seen_at

    def __contains__(self, key: tuple[EntityType, str]) -> bool:
        return key in self._tallies

    def ranked(self, kind: EntityType, *, query: str, top: int | None = None) -> list[Entity]:
        entities = [
            Entity(
                name=_spelling(tally.spellings),
                type=kind,
                count=tally.articles,
                sources=len(tally.domains),
                first_seen=tally.first_seen,
                last_seen=tally.last_seen,
                query=query,
            )
            for (entry_kind, _), tally in self._tallies.items()
            if entry_kind is kind
        ]
        entities.sort(key=lambda entity: (-entity.count, entity.name.casefold(), entity.name))
        return entities if top is None else entities[:top]


def _spelling(spellings: Counter[str]) -> str:
    return min(spellings.items(), key=lambda item: (-item[1], item[0]))[0]


@dataclass(slots=True)
class _LocationTally:
    location: GkgLocation
    articles: int = 0
    spellings: Counter[str] = field(default_factory=Counter)
    coordinates: Counter[tuple[float, float]] = field(default_factory=Counter)
    domains: set[str] = field(default_factory=set)
    first_seen: datetime | None = None
    last_seen: datetime | None = None


class LocationTally:
    """Places grouped by GDELT's own geocoding, not by spelling.

    GKG resolves "Australian" and "Australia" to the same feature, so they are
    one location; two places sharing a name in different countries are not.
    Names without a feature id fall back to case- and space-insensitive name.
    """

    def __init__(self) -> None:
        self._places: dict[tuple[object, ...], _LocationTally] = {}
        self.articles = 0

    def add_article(
        self,
        locations: Iterable[GkgLocation],
        *,
        domain: str | None,
        seen_at: datetime | None,
    ) -> None:
        self.articles += 1
        seen: set[tuple[object, ...]] = set()
        for location in locations:
            key = _place_key(location)
            if key in seen:
                continue
            seen.add(key)
            tally = self._places.setdefault(key, _LocationTally(location))
            tally.articles += 1
            tally.spellings[" ".join(location.name.split())] += 1
            if location.latitude is not None and location.longitude is not None:
                tally.coordinates[(location.latitude, location.longitude)] += 1
            if domain:
                tally.domains.add(domain)
            if seen_at is not None:
                if tally.first_seen is None or seen_at < tally.first_seen:
                    tally.first_seen = seen_at
                if tally.last_seen is None or seen_at > tally.last_seen:
                    tally.last_seen = seen_at

    def ranked(
        self, *, query: str, levels: set[str] | None = None, top: int | None = None
    ) -> list[Location]:
        locations = []
        for tally in self._places.values():
            place = tally.location
            level = LOCATION_LEVELS.get(place.type or 0, "unknown")
            if levels is not None and level not in levels:
                continue
            coordinates = (
                min(tally.coordinates.items(), key=lambda item: (-item[1], item[0]))[0]
                if tally.coordinates
                else (None, None)
            )
            locations.append(
                Location(
                    name=_spelling(tally.spellings),
                    level=level,
                    location_type=place.type,
                    country_code=place.country_code,
                    adm1=place.adm1,
                    feature_id=place.feature_id,
                    latitude=coordinates[0],
                    longitude=coordinates[1],
                    count=tally.articles,
                    sources=len(tally.domains),
                    first_seen=tally.first_seen,
                    last_seen=tally.last_seen,
                    query=query,
                )
            )
        locations.sort(
            key=lambda loc: (-loc.count, loc.name.casefold(), loc.name, loc.feature_id or "")
        )
        return locations if top is None else locations[:top]


def _place_key(location: GkgLocation) -> tuple[object, ...]:
    if location.feature_id:
        return (location.type, location.country_code, location.adm1, location.feature_id)
    return (location.type, location.country_code, " ".join(location.name.split()).casefold())


TOPICS_PER_SOURCE = 5
# Tone averages are rounded so float summation order cannot change the output.
TONE_DIGITS = 4


@dataclass(slots=True)
class _SourceTally:
    articles: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    countries: Counter[str] = field(default_factory=Counter)
    languages: Counter[str] = field(default_factory=Counter)
    tone_total: float = 0.0
    toned: int = 0
    themes: Counter[str] = field(default_factory=Counter)


class SourceTally:
    """Articles grouped by the domain exactly as GDELT reports it.

    Domains are not rewritten (no ``www.`` stripping), so per-domain counts
    reconcile with a plain group-by over the same articles.
    """

    def __init__(self) -> None:
        self._domains: dict[str, _SourceTally] = {}
        self.articles = 0
        self.unattributed = 0

    def add(
        self,
        *,
        domain: str | None,
        seen_at: datetime | None,
        country: str | None = None,
        language: str | None = None,
        tone: float | None = None,
        themes: Iterable[str] = (),
    ) -> None:
        self.articles += 1
        if not domain or not domain.strip():
            self.unattributed += 1
            return
        tally = self._domains.setdefault(domain.strip(), _SourceTally())
        tally.articles += 1
        if seen_at is not None:
            if tally.first_seen is None or seen_at < tally.first_seen:
                tally.first_seen = seen_at
            if tally.last_seen is None or seen_at > tally.last_seen:
                tally.last_seen = seen_at
        if country:
            tally.countries[country] += 1
        if language:
            tally.languages[language] += 1
        if tone is not None:
            tally.tone_total += tone
            tally.toned += 1
        tally.themes.update(set(themes))

    def ranked(self, *, query: str, top: int | None = None) -> list[Source]:
        sources = [
            Source(
                domain=domain,
                articles=tally.articles,
                first_seen=tally.first_seen,
                last_seen=tally.last_seen,
                country=_most_common(tally.countries),
                language=_most_common(tally.languages),
                average_tone=round(tally.tone_total / tally.toned, TONE_DIGITS)
                if tally.toned
                else None,
                topics=dict(_top(tally.themes, TOPICS_PER_SOURCE)) if tally.themes else None,
                query=query,
            )
            for domain, tally in self._domains.items()
        ]
        sources.sort(key=lambda source: (-source.articles, source.domain))
        return sources if top is None else sources[:top]


def _top(counter: Counter[str], limit: int) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]


def _most_common(counter: Counter[str]) -> str | None:
    return _top(counter, 1)[0][0] if counter else None
