"""Diagnostics channel.

Everything here writes to stderr so that stdout carries only data.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn

from gdeltx.errors import GdeltxError


class Reporter:
    def __init__(self, *, verbose: bool = False, quiet: bool = False) -> None:
        self.verbose = verbose
        self.quiet = quiet
        self._console = Console(stderr=True, highlight=False, soft_wrap=True)

    @property
    def show_progress(self) -> bool:
        return not self.quiet and sys.stderr.isatty()

    def debug(self, message: str) -> None:
        if self.verbose:
            self._console.print(f"[dim]{message}[/dim]")

    def warn(self, message: str) -> None:
        if not self.quiet:
            self._console.print(f"[yellow]WARNING:[/yellow] {message}")

    def error(self, exc: GdeltxError | str) -> None:
        if isinstance(exc, GdeltxError):
            self._console.print(f"[red]ERROR:[/red] {exc.message}")
            if exc.hint:
                self._console.print(exc.hint)
        else:
            self._console.print(f"[red]ERROR:[/red] {exc}")

    @contextmanager
    def progress(self, total: int, description: str) -> Iterator[Callable[[], None]]:
        with Progress(
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=self._console,
            transient=True,
            disable=not self.show_progress,
        ) as bar:
            task = bar.add_task(description, total=total)
            yield lambda: bar.advance(task)
