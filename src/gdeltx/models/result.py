"""Result envelope and the base record type.

Records stay permissive: GDELT changes field sets without notice, and an
unexpected key should widen a record rather than abort a run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Record(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    raw: dict[str, Any] | None = Field(default=None, repr=False, exclude=False)

    def export(self, *, include_raw: bool = True) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude=set() if include_raw else {"raw"})


class RequestMeta(BaseModel):
    query: str
    endpoint: str
    source: str = "gdelt"
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    parameters: dict[str, Any] = Field(default_factory=dict)
    cached: bool = False

    def export(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Result(BaseModel):
    meta: RequestMeta
    results: list[Record] = Field(default_factory=list)
