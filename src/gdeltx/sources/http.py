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
from gdeltx.errors import APIError, NotFoundError, ParseError, RateLimitError

USER_AGENT = f"gdeltx/{__version__} (+https://github.com/PeacexF/gdeltx)"

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)

MAX_BACKOFF = 60.0

ALLOWED_HOSTS = frozenset({"api.gdeltproject.org", "data.gdeltproject.org"})

# GDELT answers some failures with HTTP 200 and a one-line plain-text body
# rather than an error status, so a short non-JSON body is treated as an error.
PLAIN_ERROR_MAX_BYTES = 600


def gdelt_error_message(body: bytes) -> str | None:
    text = body.decode("utf-8", errors="replace").strip()
    if not text or len(body) > PLAIN_ERROR_MAX_BYTES:
        return None
    if text[0] in "{[<":
        return None
    return " ".join(text.split())


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
            message = gdelt_error_message(self.body)
            if message is not None:
                raise APIError(f"GDELT rejected the query: {message}") from None
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
        user_agent: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.retries = retries
        self.user_agent = user_agent or USER_AGENT
        self.cache = cache
        self.reporter = reporter or Reporter()
        self.rate_limiter = rate_limiter
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": self.user_agent},
            # Request hooks also run for each redirect hop.
            event_hooks={"request": [_require_gdelt]},
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
        use_cache: bool = True,
    ) -> Fetched:
        endpoint = label or url
        key = cache_key(url, params)

        cache = self.cache if use_cache else None
        if cache is not None:
            entry = cache.get(key)
            if entry is not None:
                self.reporter.debug(f"cache hit ({int(entry.age)}s old): {endpoint}")
                return Fetched(body=entry.body, url=url, from_cache=True)

        body = self._request_with_retries(url, params, endpoint)

        if cache is not None:
            cache.set(key, body, metadata={"url": url, "params": params or {}})

        return Fetched(body=body, url=url, from_cache=False)

    def _request_with_retries(
        self, url: str, params: dict[str, Any] | None, endpoint: str
    ) -> bytes:
        last_error: str = "unknown error"
        last_body: str | None = None

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
                    error = NotFoundError if response.status_code == 404 else APIError
                    raise error(
                        f"GDELT returned HTTP {response.status_code} while querying {endpoint}.",
                        hint=_body_hint(response),
                    )
                last_error = f"HTTP {response.status_code}"
                last_body = gdelt_error_message(response.content)
                retry_after = _retry_after(response)

            if attempt < self.retries:
                delay = retry_after if retry_after is not None else _backoff(attempt)
                self.reporter.warn(
                    f"GDELT request failed: {last_error}. Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)

        raise _final_error(last_error, endpoint, self.retries + 1, last_body)


def _require_gdelt(request: httpx.Request) -> None:
    if request.url.scheme != "https" or request.url.host not in ALLOWED_HOSTS:
        raise APIError(
            f"refusing to contact {request.url.scheme}://{request.url.host}: not a GDELT host"
        )


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


def _final_error(last_error: str, endpoint: str, attempts: int, last_body: str | None) -> APIError:
    message = (
        f"GDELT request failed after {attempts} attempts ({last_error}) while querying {endpoint}."
    )
    if last_error.startswith("HTTP 429"):
        return RateLimitError(
            message,
            hint=last_body
            or "GDELT is rate limiting this client. Raise api.min_interval and try again.",
        )
    return APIError(
        message,
        hint=last_body or "Check connectivity, then retry with --verbose for detail.",
    )
