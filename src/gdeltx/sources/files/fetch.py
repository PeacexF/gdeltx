"""Download, cache and stream GDELT bulk files.

Files are fetched on a small thread pool but handed out strictly in time
order, with only a bounded window of downloads held at once. Each archive is
decompressed lazily while its lines are consumed, so a range of any size runs
in roughly constant memory.
"""

from __future__ import annotations

import io
import zipfile
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from gdeltx.cache import CacheStore
from gdeltx.cache.store import cache_key
from gdeltx.console import Reporter
from gdeltx.errors import APIError, InputError, NotFoundError
from gdeltx.sources.files.index import Dataset, slot_url, slots
from gdeltx.sources.http import HttpClient

# Compressed sizes from a single lastupdate.txt sample; estimates only.
ESTIMATED_BYTES = {
    Dataset.EVENTS: 25_000,
    Dataset.MENTIONS: 32_000,
    Dataset.GKG: 1_450_000,
}


@dataclass(frozen=True, slots=True)
class FilePlan:
    dataset: Dataset
    stamps: list[datetime]

    @property
    def count(self) -> int:
        return len(self.stamps)

    @property
    def estimated_bytes(self) -> int:
        return self.count * ESTIMATED_BYTES[self.dataset]

    def describe(self) -> str:
        megabytes = self.estimated_bytes / 1_000_000
        return f"{self.count} {self.dataset} files (about {megabytes:,.0f} MB compressed)"


@dataclass(frozen=True, slots=True)
class FileContent:
    stamp: datetime
    url: str
    cached: bool
    lines: Iterator[str]


def plan(dataset: Dataset, start: datetime, end: datetime, *, latest: datetime) -> FilePlan:
    return FilePlan(dataset=dataset, stamps=slots(start, min(end, latest)))


def guard(
    file_plan: FilePlan,
    *,
    warn_at: int,
    refuse_at: int,
    allow_large: bool,
    reporter: Reporter,
) -> None:
    if file_plan.count > refuse_at and not allow_large:
        raise InputError(
            f"this range needs {file_plan.describe()}, above the limit of {refuse_at}",
            hint="Narrow --since/--until, raise files.max_files, or pass --allow-large.",
        )
    if file_plan.count > warn_at:
        reporter.warn(f"downloading {file_plan.describe()}; cached files are reused.")


def contains_any(terms: list[str]) -> Callable[[str], bool]:
    needles = [term.casefold() for term in terms if term.strip()]
    if not needles:
        return lambda _line: True
    return lambda line: any(needle in line.casefold() for needle in needles)


class FileFetcher:
    def __init__(
        self,
        http: HttpClient,
        *,
        cache: CacheStore | None,
        reporter: Reporter,
        workers: int = 4,
    ) -> None:
        self.http = http
        self.cache = cache
        self.reporter = reporter
        self.workers = workers

    def _download(self, url: str) -> tuple[bytes | None, bool]:
        key = cache_key(url)
        if self.cache is not None:
            entry = self.cache.get(key)
            if entry is not None:
                return entry.body, True
        try:
            body = self.http.get(url, label=url.rsplit("/", 1)[-1], use_cache=False).body
        except NotFoundError:
            return None, False
        if self.cache is not None:
            self.cache.set(key, body, metadata={"url": url})
        return body, False

    def iter_files(self, file_plan: FilePlan) -> Iterator[FileContent]:
        missing = failed = delivered = 0
        last_error: APIError | None = None
        window = self.workers * 2
        stamps = iter(file_plan.stamps)
        pending: deque[tuple[datetime, str, Future[tuple[bytes | None, bool]]]] = deque()
        executor = ThreadPoolExecutor(max_workers=self.workers)

        def submit() -> None:
            for stamp in stamps:
                url = slot_url(file_plan.dataset, stamp)
                pending.append((stamp, url, executor.submit(self._download, url)))
                return

        try:
            for _ in range(window):
                submit()
            with self.reporter.progress(file_plan.count, f"{file_plan.dataset} files") as advance:
                while pending:
                    stamp, url, future = pending.popleft()
                    submit()
                    try:
                        body, cached = future.result()
                    except APIError as exc:
                        failed += 1
                        last_error = exc
                        self.reporter.warn(f"skipped {url}: {exc.message}")
                        advance()
                        continue
                    advance()
                    if body is None:
                        missing += 1
                        continue
                    lines = self._lines(url, body)
                    if lines is None:
                        failed += 1
                        continue
                    delivered += 1
                    yield FileContent(stamp=stamp, url=url, cached=cached, lines=lines)
        finally:
            # Wait out in-flight downloads (at most `workers`) so none outlive the stream.
            executor.shutdown(wait=True, cancel_futures=True)
            if missing:
                self.reporter.warn(
                    f"{missing} of {file_plan.count} {file_plan.dataset} files were not "
                    "published upstream; their slots are empty."
                )
            if self.cache is not None:
                self.cache.evict_to_limit()

        if failed and not delivered:
            raise last_error or APIError(
                f"every one of {failed} {file_plan.dataset} files failed to download or unpack"
            )

    def _lines(self, url: str, body: bytes) -> Iterator[str] | None:
        try:
            archive = zipfile.ZipFile(io.BytesIO(body))
            member = archive.namelist()[0]
        except zipfile.BadZipFile, IndexError:
            self._discard(url, "not a readable zip archive")
            return None
        return self._read_member(url, archive, member)

    def _read_member(self, url: str, archive: zipfile.ZipFile, member: str) -> Iterator[str]:
        try:
            with archive.open(member) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
                for line in text:
                    # Only newlines are blank; a row of empty tab-separated fields is still a row.
                    if line.rstrip("\r\n"):
                        yield line
        except (zipfile.BadZipFile, EOFError, OSError) as exc:
            self._discard(url, f"corrupt archive ({exc}); rows after the damage were skipped")

    def _discard(self, url: str, reason: str) -> None:
        self.reporter.warn(f"skipped {url}: {reason}")
        if self.cache is not None:
            self.cache.discard(cache_key(url))
