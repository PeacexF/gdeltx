"""Command-line entrypoint.

Holds argument wiring only. Investigation logic lives in ``commands``.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from gdeltx import __version__
from gdeltx.cache import CacheStore
from gdeltx.commands import context as context_cmd
from gdeltx.commands import entities as entities_cmd
from gdeltx.commands import events as events_cmd
from gdeltx.commands import search as search_cmd
from gdeltx.commands import sources as sources_cmd
from gdeltx.commands import timeline as timeline_cmd
from gdeltx.config import Config, apply_overrides, load
from gdeltx.console import Reporter
from gdeltx.errors import GdeltxError, InputError
from gdeltx.models import EntityType
from gdeltx.output import Format
from gdeltx.sources import HttpClient, RateLimiter
from gdeltx.sources.context import Sort as ContextSort
from gdeltx.sources.doc import Sort as DocSort
from gdeltx.sources.files import (
    Dataset,
    FileFetcher,
    FilePlan,
    contains_any,
    guard,
    latest_stamp,
    plain_query,
    plan,
)
from gdeltx.timeparse import resolve_range

app = typer.Typer(
    name="gdeltx",
    help="OSINT investigation over the GDELT APIs.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

QueryArg = Annotated[str, typer.Argument(help="GDELT query, passed through unchanged.")]
Since = Annotated[
    str | None, typer.Option("--since", help="Start of the range: 24h, 7d, 30d, 1y, or a date.")
]
Until = Annotated[str | None, typer.Option("--until", help="End of the range. Defaults to now.")]
Max = Annotated[int, typer.Option("--max", min=1, help="Maximum records to return.")]
FormatOpt = Annotated[
    str | None, typer.Option("--format", help="table, json, jsonl or csv.", metavar="FORMAT")
]
Json = Annotated[bool, typer.Option("--json", help="Shorthand for --format json.")]
Jsonl = Annotated[bool, typer.Option("--jsonl", help="Shorthand for --format jsonl.")]
Csv = Annotated[bool, typer.Option("--csv", help="Shorthand for --format csv.")]
NoCache = Annotated[bool, typer.Option("--no-cache", help="Bypass the local cache.")]
CacheTtl = Annotated[
    int | None, typer.Option("--cache-ttl", min=0, help="Cache lifetime in seconds.")
]
FILE_SPAN = timedelta(hours=24)

FileSince = Annotated[
    str | None,
    typer.Option("--since", help="Start of the range: 1h, 6h, 24h, 7d, or a date. Default 24h."),
]
AllowLarge = Annotated[
    bool, typer.Option("--allow-large", help="Allow ranges above files.max_files.")
]
IncludeRaw = Annotated[
    bool, typer.Option("--raw", help="Keep the original GDELT record in machine output.")
]


@dataclass(slots=True)
class Context:
    config: Config
    reporter: Reporter

    @property
    def format(self) -> Format:
        return Format(self.config.output.format)

    def cache(self) -> CacheStore:
        settings = self.config.cache
        return CacheStore(
            settings.resolved_directory() / "api",
            enabled=settings.enabled,
            ttl=settings.ttl,
            max_bytes=settings.max_bytes,
        )

    def file_cache(self) -> CacheStore:
        settings = self.config.cache
        # Published GDELT files never change, so they are evicted by size, not age.
        return CacheStore(
            settings.resolved_directory() / "files",
            enabled=settings.enabled,
            ttl=None,
            max_bytes=settings.max_bytes,
        )

    def plan_files(
        self,
        fetcher: FileFetcher,
        dataset: Dataset,
        start: datetime,
        end: datetime,
        *,
        allow_large: bool,
        newest_first: bool = False,
    ) -> FilePlan:
        file_plan = plan(
            dataset, start, end, latest=latest_stamp(fetcher.http), newest_first=newest_first
        )
        files = self.config.files
        guard(
            file_plan,
            warn_at=files.warn_files,
            refuse_at=files.max_files,
            allow_large=allow_large,
            reporter=self.reporter,
        )
        self.reporter.debug(f"reading {file_plan.describe()}")
        return file_plan

    def fetcher(self) -> FileFetcher:
        # data.gdeltproject.org is static hosting, separate from the rate-limited API.
        http = HttpClient(
            timeout=self.config.api.timeout,
            retries=self.config.api.retries,
            reporter=self.reporter,
            user_agent=self.config.api.user_agent,
        )
        return FileFetcher(
            http,
            cache=self.file_cache(),
            reporter=self.reporter,
            workers=self.config.files.workers,
        )

    def http(self) -> HttpClient:
        return HttpClient(
            timeout=self.config.api.timeout,
            retries=self.config.api.retries,
            rate_limiter=RateLimiter(self.config.api.min_interval),
            cache=self.cache(),
            reporter=self.reporter,
            user_agent=self.config.api.user_agent,
        )


def resolve_format(base: str, fmt: str | None, as_json: bool, as_jsonl: bool, as_csv: bool) -> str:
    chosen = [name for name, on in (("json", as_json), ("jsonl", as_jsonl), ("csv", as_csv)) if on]
    if len(chosen) > 1:
        raise typer.BadParameter(f"conflicting format flags: --{', --'.join(chosen)}")
    if fmt is not None and chosen:
        raise typer.BadParameter(f"--format conflicts with --{chosen[0]}")
    if chosen:
        return chosen[0]
    return fmt if fmt is not None else base


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def _root(
    ctx: typer.Context,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to a config.toml.")
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Report progress and cache hits on stderr.")
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress warnings and progress.")
    ] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the gdeltx version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    if verbose and quiet:
        raise typer.BadParameter("--verbose and --quiet are mutually exclusive")
    reporter = Reporter(verbose=verbose, quiet=quiet)
    config = load(config_path)
    if config.source_path is not None:
        reporter.debug(f"config: {config.source_path}")
    ctx.obj = Context(config=config, reporter=reporter)


def build_context(
    ctx: typer.Context,
    *,
    fmt: str | None = None,
    as_json: bool = False,
    as_jsonl: bool = False,
    as_csv: bool = False,
    no_cache: bool = False,
    cache_ttl: int | None = None,
) -> Context:
    parent: Context = ctx.obj
    resolved = resolve_format(parent.config.output.format, fmt, as_json, as_jsonl, as_csv)
    config = apply_overrides(
        parent.config, output_format=resolved, no_cache=no_cache, cache_ttl=cache_ttl
    )
    return Context(config=config, reporter=parent.reporter)


@app.command("search")
def search_command(
    ctx: typer.Context,
    query: QueryArg,
    since: Since = None,
    until: Until = None,
    max_records: Max = 75,
    sort: Annotated[
        DocSort, typer.Option("--sort", case_sensitive=False, help="Result order.")
    ] = DocSort.RELEVANCE,
    domains: Annotated[
        list[str] | None, typer.Option("--domain", help="Only this domain. Repeatable.")
    ] = None,
    languages: Annotated[
        list[str] | None,
        typer.Option("--language", help="Only this source language, e.g. english. Repeatable."),
    ] = None,
    countries: Annotated[
        list[str] | None,
        typer.Option("--country", help="Only this source country, e.g. france. Repeatable."),
    ] = None,
    fmt: FormatOpt = None,
    as_json: Json = False,
    as_jsonl: Jsonl = False,
    as_csv: Csv = False,
    no_cache: NoCache = False,
    cache_ttl: CacheTtl = None,
    include_raw: IncludeRaw = False,
) -> None:
    """Search news coverage from the last 3 months."""
    app_ctx = build_context(
        ctx,
        fmt=fmt,
        as_json=as_json,
        as_jsonl=as_jsonl,
        as_csv=as_csv,
        no_cache=no_cache,
        cache_ttl=cache_ttl,
    )
    with app_ctx.http() as http:
        search_cmd.run(
            query,
            http=http,
            reporter=app_ctx.reporter,
            fmt=app_ctx.format,
            since=since,
            until=until,
            max_records=max_records,
            sort=sort,
            domains=tuple(domains or ()),
            languages=tuple(languages or ()),
            countries=tuple(countries or ()),
            include_raw=include_raw,
        )


@app.command("context")
def context_command(
    ctx: typer.Context,
    query: QueryArg,
    since: Since = None,
    until: Until = None,
    max_records: Max = 75,
    sort: Annotated[
        ContextSort, typer.Option("--sort", case_sensitive=False, help="Result order.")
    ] = ContextSort.RELEVANCE,
    fmt: FormatOpt = None,
    as_json: Json = False,
    as_jsonl: Jsonl = False,
    as_csv: Csv = False,
    no_cache: NoCache = False,
    cache_ttl: CacheTtl = None,
    include_raw: IncludeRaw = False,
) -> None:
    """Show the passages where a query matches, from the last 72 hours."""
    app_ctx = build_context(
        ctx,
        fmt=fmt,
        as_json=as_json,
        as_jsonl=as_jsonl,
        as_csv=as_csv,
        no_cache=no_cache,
        cache_ttl=cache_ttl,
    )
    with app_ctx.http() as http:
        context_cmd.run(
            query,
            http=http,
            reporter=app_ctx.reporter,
            fmt=app_ctx.format,
            since=since,
            until=until,
            max_records=max_records,
            sort=sort,
            include_raw=include_raw,
        )


@app.command("sources")
def sources_command(
    ctx: typer.Context,
    query: Annotated[
        str,
        typer.Argument(help="A GDELT query (--from doc) or a plain phrase (--from gkg)."),
    ],
    origin: Annotated[
        sources_cmd.Origin,
        typer.Option(
            "--from",
            case_sensitive=False,
            help="doc: articles from `search` (country, language). "
            "gkg: GKG records from `entities` (tone, themes).",
        ),
    ] = sources_cmd.Origin.DOC,
    since: FileSince = None,
    until: Until = None,
    max_records: Annotated[
        int, typer.Option("--max", min=1, help="Articles to aggregate (--from doc).")
    ] = 250,
    top: Annotated[int, typer.Option("--top", min=1, help="Sources to show.")] = 25,
    domains: Annotated[
        list[str] | None, typer.Option("--domain", help="Only this domain (--from doc).")
    ] = None,
    languages: Annotated[
        list[str] | None, typer.Option("--language", help="Only this language (--from doc).")
    ] = None,
    countries: Annotated[
        list[str] | None, typer.Option("--country", help="Only this country (--from doc).")
    ] = None,
    allow_large: AllowLarge = False,
    fmt: FormatOpt = None,
    as_json: Json = False,
    as_jsonl: Jsonl = False,
    as_csv: Csv = False,
    no_cache: NoCache = False,
    cache_ttl: CacheTtl = None,
) -> None:
    """Coverage of the query grouped by publishing domain."""
    app_ctx = build_context(
        ctx,
        fmt=fmt,
        as_json=as_json,
        as_jsonl=as_jsonl,
        as_csv=as_csv,
        no_cache=no_cache,
        cache_ttl=cache_ttl,
    )

    if origin is sources_cmd.Origin.DOC:
        if allow_large:
            raise InputError("--allow-large only applies with --from gkg")
        with app_ctx.http() as http:
            sources_cmd.run_doc(
                query,
                http=http,
                reporter=app_ctx.reporter,
                fmt=app_ctx.format,
                since=since,
                until=until,
                max_records=max_records,
                domains=tuple(domains or ()),
                languages=tuple(languages or ()),
                countries=tuple(countries or ()),
                top=top,
            )
        return

    if domains or languages or countries:
        raise InputError(
            "--domain, --language and --country only apply with --from doc",
            hint="GKG matching is plain text; see `gdeltx entities --help`.",
        )
    term = plain_query(query)
    start, end = resolve_range(since, until, default=FILE_SPAN)
    fetcher = app_ctx.fetcher()
    with fetcher.http:
        file_plan = app_ctx.plan_files(fetcher, Dataset.GKG, start, end, allow_large=allow_large)
        sources_cmd.run_gkg(
            term,
            fetcher=fetcher,
            file_plan=file_plan,
            reporter=app_ctx.reporter,
            fmt=app_ctx.format,
            start=start,
            end=end,
            top=top,
        )


TIMELINE_SPAN = timedelta(days=30)


@app.command("timeline")
def timeline_command(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="A name or phrase, matched as plain text.")],
    since: Annotated[
        str | None,
        typer.Option(
            "--since", help="Start of the range: 24h, 7d, 30d, 1y, or a date. Default 30d."
        ),
    ] = None,
    until: Until = None,
    bucket: Annotated[
        timeline_cmd.Bucket | None,
        typer.Option(
            "--bucket", case_sensitive=False, help="Override the automatic day/week/month size."
        ),
    ] = None,
    bars: Annotated[
        bool, typer.Option("--bars", help="Show a bar column for articles in the table view.")
    ] = False,
    allow_large: AllowLarge = False,
    fmt: FormatOpt = None,
    as_json: Json = False,
    as_jsonl: Jsonl = False,
    as_csv: Csv = False,
    no_cache: NoCache = False,
    cache_ttl: CacheTtl = None,
) -> None:
    """Article and event activity bucketed over time."""
    app_ctx = build_context(
        ctx,
        fmt=fmt,
        as_json=as_json,
        as_jsonl=as_jsonl,
        as_csv=as_csv,
        no_cache=no_cache,
        cache_ttl=cache_ttl,
    )
    term = plain_query(query)
    start, end = resolve_range(since, until, default=TIMELINE_SPAN)
    with app_ctx.http() as http:
        fetcher = app_ctx.fetcher()
        with fetcher.http:
            events_start = timeline_cmd.clip_event_window(
                start,
                end,
                max_files=app_ctx.config.files.max_files,
                allow_large=allow_large,
                reporter=app_ctx.reporter,
            )
            file_plan = app_ctx.plan_files(
                fetcher, Dataset.EVENTS, events_start, end, allow_large=allow_large
            )
            timeline_cmd.run(
                term,
                http=http,
                fetcher=fetcher,
                file_plan=file_plan,
                reporter=app_ctx.reporter,
                fmt=app_ctx.format,
                start=start,
                end=end,
                events_start=events_start,
                bucket=bucket,
                bars=bars,
            )


@app.command("_files", hidden=True)
def files_command(
    ctx: typer.Context,
    dataset: Annotated[Dataset, typer.Argument(case_sensitive=False)],
    since: FileSince = "1h",
    until: Until = None,
    match: Annotated[
        list[str] | None, typer.Option("--match", help="Count rows containing this text.")
    ] = None,
    allow_large: AllowLarge = False,
    no_cache: NoCache = False,
) -> None:
    """Inspect the bulk file layer: one JSON line per file."""
    app_ctx = build_context(ctx, no_cache=no_cache)
    start, end = resolve_range(since, until, default=FILE_SPAN)
    fetcher = app_ctx.fetcher()
    with fetcher.http:
        file_plan = app_ctx.plan_files(fetcher, dataset, start, end, allow_large=allow_large)
        matches = contains_any(match or [])
        for content in fetcher.iter_files(file_plan):
            lines = matched = 0
            for line in content.lines:
                lines += 1
                matched += matches(line)
            record = {
                "stamp": content.stamp.isoformat(),
                "url": content.url,
                "cached": content.cached,
                "lines": lines,
                "matched": matched,
            }
            sys.stdout.write(json.dumps(record) + "\n")


@app.command("entities")
def entities_command(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="A name or phrase, matched as plain text.")],
    since: FileSince = None,
    until: Until = None,
    top: Annotated[int, typer.Option("--top", min=1, help="Entities shown per category.")] = 10,
    types: Annotated[
        list[EntityType] | None,
        typer.Option("--type", case_sensitive=False, help="Only this category. Repeatable."),
    ] = None,
    mentions: Annotated[
        bool, typer.Option("--mentions", help="One record per article and entity, not totals.")
    ] = False,
    allow_large: AllowLarge = False,
    fmt: FormatOpt = None,
    as_json: Json = False,
    as_jsonl: Jsonl = False,
    as_csv: Csv = False,
    no_cache: NoCache = False,
    include_raw: IncludeRaw = False,
) -> None:
    """People, organizations, places and themes in GKG coverage naming the query."""
    app_ctx = build_context(
        ctx, fmt=fmt, as_json=as_json, as_jsonl=as_jsonl, as_csv=as_csv, no_cache=no_cache
    )
    term = plain_query(query)
    start, end = resolve_range(since, until, default=FILE_SPAN)
    fetcher = app_ctx.fetcher()
    with fetcher.http:
        file_plan = app_ctx.plan_files(fetcher, Dataset.GKG, start, end, allow_large=allow_large)
        entities_cmd.run(
            term,
            fetcher=fetcher,
            file_plan=file_plan,
            reporter=app_ctx.reporter,
            fmt=app_ctx.format,
            start=start,
            end=end,
            top=top,
            types=set(types) if types else None,
            mentions=mentions,
            include_raw=include_raw,
        )


@app.command("events")
def events_command(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="An actor name, matched as plain text.")],
    since: FileSince = None,
    until: Until = None,
    max_records: Max = 75,
    allow_large: AllowLarge = False,
    fmt: FormatOpt = None,
    as_json: Json = False,
    as_jsonl: Jsonl = False,
    as_csv: Csv = False,
    no_cache: NoCache = False,
    include_raw: IncludeRaw = False,
) -> None:
    """Structured CAMEO events whose actors match the query, newest first."""
    app_ctx = build_context(
        ctx, fmt=fmt, as_json=as_json, as_jsonl=as_jsonl, as_csv=as_csv, no_cache=no_cache
    )
    term = plain_query(query)
    start, end = resolve_range(since, until, default=FILE_SPAN)
    fetcher = app_ctx.fetcher()
    with fetcher.http:
        file_plan = app_ctx.plan_files(
            fetcher, Dataset.EVENTS, start, end, allow_large=allow_large, newest_first=True
        )
        events_cmd.run(
            term,
            fetcher=fetcher,
            file_plan=file_plan,
            reporter=app_ctx.reporter,
            fmt=app_ctx.format,
            start=start,
            end=end,
            max_records=max_records,
            include_raw=include_raw,
        )


def main() -> None:
    reporter = Reporter()
    try:
        app()
    except GdeltxError as exc:
        reporter.error(exc)
        raise SystemExit(exc.exit_code) from None
    except KeyboardInterrupt:
        reporter.error("interrupted")
        raise SystemExit(130) from None
    except BrokenPipeError:
        # Keep the interpreter from reporting the dead pipe again during shutdown.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0) from None
