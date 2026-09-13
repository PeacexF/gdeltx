"""Shared HTTP transport for every GDELT source.

Owns timeouts, bounded retries, rate limiting and cache lookup. No source
module implements its own retry policy.
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from gdeltx import __version__
from gdeltx.cache import CacheStore
from gdeltx.cache.store import cache_key
from gdeltx.console import Reporter
from gdeltx.errors import APIError, ParseError, RateLimitError

USER_AGENT = f"gdeltx/{__version__} (+https://github.com/PeacexF/gdeltx)"

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)

MAX_BACKOFF = 60.0


class RateLimiter:
    """Spaces requests by at least ``min_interval`` seconds."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> float:
        if self.min_interval <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self.min_interval
        if wait > 0:
            time.sleep(wait)
        return wait


@dataclass(frozen=True, slots=True)
class Fetched:
    body: bytes
    url: str
    from_cache: bool = False

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError as exc:
            raise ParseError(
                f"GDELT returned a response that is not valid JSON ({exc})",
                hint=f"Endpoint: {self.url}",
            ) from None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        rate_limiter: RateLimiter | None = None,
        cache: CacheStore | None = None,
        reporter: Reporter | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.retries = retries
        self.cache = cache
        self.reporter = reporter or Reporter()
        self.rate_limiter = rate_limiter
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        label: str | None = None,
    ) -> Fetched:
        endpoint = label or url
        key = cache_key(url, params)

        if self.cache is not None:
            entry = self.cache.get(key)
            if entry is not None:
                self.reporter.debug(f"cache hit ({int(entry.age)}s old): {endpoint}")
                return Fetched(body=entry.body, url=url, from_cache=True)

        body = self._request_with_retries(url, params, endpoint)

        if self.cache is not None:
            self.cache.set(key, body, metadata={"url": url, "params": params or {}})

        return Fetched(body=body, url=url, from_cache=False)

    def _request_with_retries(
        self, url: str, params: dict[str, Any] | None, endpoint: str
    ) -> bytes:
        last_error: str = "unknown error"

        for attempt in range(self.retries + 1):
            if self.rate_limiter is not None:
                waited = self.rate_limiter.acquire()
                if waited > 0.1:
                    self.reporter.debug(f"rate limit: waited {waited:.1f}s")

            retry_after: float | None = None
            try:
                self.reporter.debug(f"GET {endpoint} (attempt {attempt + 1})")
                response = self._client.get(url, params=params)
            except RETRYABLE_EXCEPTIONS as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    if response.is_success:
                        return response.content
                    raise APIError(
                        f"GDELT returned HTTP {response.status_code} while querying {endpoint}.",
                        hint=_body_hint(response),
                    )
                last_error = f"HTTP {response.status_code}"
                retry_after = _retry_after(response)

            if attempt < self.retries:
                delay = retry_after if retry_after is not None else _backoff(attempt)
                self.reporter.warn(
                    f"GDELT request failed: {last_error}. Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)

        raise _final_error(last_error, endpoint, self.retries + 1)


def _backoff(attempt: int) -> float:
    return min(MAX_BACKOFF, 2.0**attempt) + random.uniform(0, 0.5)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return min(MAX_BACKOFF, float(raw))
    except ValueError:
        return None


def _body_hint(response: httpx.Response) -> str | None:
    snippet = response.text.strip()
    if not snippet:
        return None
    return f"Response: {snippet[:200]}"


def _final_error(last_error: str, endpoint: str, attempts: int) -> APIError:
    message = (
        f"GDELT request failed after {attempts} attempts ({last_error}) while querying {endpoint}."
    )
    if last_error.startswith("HTTP 429"):
        return RateLimitError(
            message,
            hint="GDELT is rate limiting this client. "
            "Wait a few minutes, or raise api.min_interval.",
        )
    return APIError(message, hint="Check connectivity, then retry with --verbose for detail.")
