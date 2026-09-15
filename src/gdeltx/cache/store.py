"""Filesystem response cache.

Entries are split into a JSON sidecar and a raw body file, so bulk downloads
never round-trip through a text encoding. Keys are hex digests, which keeps
user input out of the path.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gdeltx.errors import CacheError

_KEY = re.compile(r"[0-9a-f]{64}")


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
        # Only digests become paths, so no key can name a file outside the store.
        if not _KEY.fullmatch(key):
            raise CacheError(f"invalid cache key: {key!r}")
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
            self.discard(key)
            return None

        if self.ttl is not None and time.time() - stored_at > self.ttl:
            return None

        body_path.touch()
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

    def discard(self, key: str) -> None:
        for path in self._paths(key):
            path.unlink(missing_ok=True)

    def _entry_files(self, suffix: str) -> list[Path]:
        # cache.directory is user-configurable; never count or evict files the store did not write.
        if not self.directory.is_dir():
            return []
        return [
            path
            for path in self.directory.glob(f"*/*.{suffix}")
            if path.is_file() and _KEY.fullmatch(path.stem) and path.parent.name == path.stem[:2]
        ]

    def total_bytes(self) -> int:
        files = self._entry_files("body") + self._entry_files("json")
        return sum(path.stat().st_size for path in files)

    def evict_to_limit(self) -> int:
        """Drop least-recently-used entries until the store fits max_bytes."""
        bodies = sorted(self._entry_files("body"), key=lambda p: p.stat().st_mtime)
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
            self.discard(key)
            removed += 1
        return removed
