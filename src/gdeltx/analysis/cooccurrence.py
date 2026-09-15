"""Co-occurrence scoring for `related`.

Raw co-occurrence rewards whatever GDELT mentions everywhere: nearly every
query "co-occurs" with the United States. The score is the Jaccard overlap of
two article sets within the same files,

    together / (query_articles + entity_articles - together)

so an entity scores high only when it appears mostly alongside the query,
not merely often. Every input to the score is exported next to it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime

from gdeltx.analysis.aggregate import EntityTally, entity_key
from gdeltx.models import EntityType, RelatedEntity

RELATION = "co-occurs with"
SCORE_FORMULA = "together / (query_articles + entity_articles - together)"
# Rounded so float arithmetic can never reorder otherwise equal results.
SCORE_DIGITS = 6


class Cooccurrence:
    """Fed twice: the articles naming the query, then every article in the same files."""

    def __init__(self, term: str) -> None:
        self.term = term
        self.matches = EntityTally()
        self._baseline: Counter[tuple[EntityType, str]] = Counter()
        self.total_articles = 0

    @property
    def query_articles(self) -> int:
        return self.matches.articles

    def add_match(
        self,
        entities: Iterable[tuple[EntityType, str]],
        *,
        domain: str | None,
        seen_at: datetime | None,
    ) -> None:
        needle = " ".join(self.term.split()).casefold()
        # The query's own spellings would otherwise top every category.
        self.matches.add_article(
            ((kind, name) for kind, name in entities if needle not in entity_key(kind, name)[1]),
            domain=domain,
            seen_at=seen_at,
        )

    def add_article(self, entities: Iterable[tuple[EntityType, str]]) -> None:
        self.total_articles += 1
        for key in {entity_key(kind, name) for kind, name in entities}:
            if key in self.matches:
                self._baseline[key] += 1

    def ranked(
        self, kind: EntityType, *, top: int | None = None, min_count: int = 1
    ) -> list[RelatedEntity]:
        related = []
        for entity in self.matches.ranked(kind, query=self.term):
            if entity.count < min_count:
                continue
            # Only differs when a file was readable in one pass and not the other.
            entity_articles = max(self._baseline[entity_key(kind, entity.name)], entity.count)
            union = self.query_articles + entity_articles - entity.count
            related.append(
                RelatedEntity(
                    query=self.term,
                    relation=RELATION,
                    name=entity.name,
                    type=kind,
                    score=round(entity.count / union, SCORE_DIGITS),
                    together=entity.count,
                    query_articles=self.query_articles,
                    entity_articles=entity_articles,
                    total_articles=max(self.total_articles, self.query_articles),
                    sources=entity.sources,
                    first_seen=entity.first_seen,
                    last_seen=entity.last_seen,
                )
            )
        related.sort(key=lambda r: (-r.score, -r.together, r.name.casefold(), r.name))
        return related if top is None else related[:top]
