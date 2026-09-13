"""Filesystem response cache.

Entries are split into a JSON sidecar and a raw body file, so bulk downloads
never round-trip through a text encoding. Keys are hex digests, which keeps
user input out of the path.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gdeltx.errors import CacheError


@dataclass(frozen=True, slots=True)
class CacheEntry:
    key: str
    body: bytes
    metadata: dict[str, Any]
    stored_at: float

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.stored_at)


def cache_key(endpoint: str, params: dict[str, Any] | None = None) -> str:
    normalized = sorted((str(k), str(v)) for k, v in (params or {}).items())
    payload = json.dumps([endpoint, normalized], separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CacheStore:
    def __init__(
        self,
        directory: Path,
        *,
        enabled: bool = True,
        ttl: int | None = 3600,
        max_bytes: int = 5 * 1024**3,
    ) -> None:
        self.directory = directory
        self.enabled = enabled
        self.ttl = ttl
        self.max_bytes = max_bytes

    def _paths(self, key: str) -> tuple[Path, Path]:
        shard = self.directory / key[:2]
        return shard / f"{key}.json", shard / f"{key}.body"

    def get(self, key: str) -> CacheEntry | None:
        if not self.enabled:
            return None
        meta_path, body_path = self._paths(key)
        if not (meta_path.is_file() and body_path.is_file()):
            return None

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            stored_at = float(meta["stored_at"])
            body = body_path.read_bytes()
        except OSError, ValueError, KeyError:
            self._discard(key)
            return None

        if self.ttl is not None and time.time() - stored_at > self.ttl:
            return None

        return CacheEntry(
            key=key,
            body=body,
            metadata=meta.get("request", {}),
            stored_at=stored_at,
        )

    def set(self, key: str, body: bytes, *, metadata: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        meta_path, body_path = self._paths(key)
        try:
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = body_path.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(body_path)
            meta_path.write_text(
                json.dumps(
                    {"stored_at": time.time(), "bytes": len(body), "request": metadata or {}},
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            raise CacheError(f"cannot write cache entry {key}: {exc}") from None

    def _discard(self, key: str) -> None:
        for path in self._paths(key):
            path.unlink(missing_ok=True)

    def total_bytes(self) -> int:
        if not self.directory.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.directory.rglob("*") if p.is_file())

    def evict_to_limit(self) -> int:
        """Drop least-recently-stored entries until the store fits max_bytes."""
        if not self.directory.is_dir():
            return 0
        bodies = sorted(
            (p for p in self.directory.rglob("*.body") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        total = self.total_bytes()
        removed = 0
        for body_path in bodies:
            if total <= self.max_bytes:
                break
            key = body_path.stem
            meta_path = body_path.with_suffix(".json")
            total -= body_path.stat().st_size
            if meta_path.is_file():
                total -= meta_path.stat().st_size
            self._discard(key)
            removed += 1
        return removed

    def clear(self) -> None:
        if self.directory.is_dir():
            shutil.rmtree(self.directory)
