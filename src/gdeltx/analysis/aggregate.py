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

from gdeltx.models import Entity, EntityType, Source


@dataclass(slots=True)
class _Tally:
    articles: int = 0
    spellings: Counter[str] = field(default_factory=Counter)
    domains: set[str] = field(default_factory=set)
    first_seen: datetime | None = None
    last_seen: datetime | None = None


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
            key = (kind, " ".join(name.split()).casefold())
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
