"""Command-line entrypoint.

Holds argument wiring only. Investigation logic lives in ``commands``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from gdeltx import __version__
from gdeltx.cache import CacheStore
from gdeltx.config import Config, apply_overrides, load
from gdeltx.console import Reporter
from gdeltx.errors import GdeltxError
from gdeltx.output import Format
from gdeltx.sources import HttpClient, RateLimiter

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
            settings.resolved_directory(),
            enabled=settings.enabled,
            ttl=settings.ttl,
            max_bytes=settings.max_bytes,
        )

    def http(self) -> HttpClient:
        return HttpClient(
            timeout=self.config.api.timeout,
            retries=self.config.api.retries,
            rate_limiter=RateLimiter(self.config.api.min_interval),
            cache=self.cache(),
            reporter=self.reporter,
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
