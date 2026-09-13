"""Deterministic entity counting.

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

from gdeltx.models import Entity, EntityType


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
