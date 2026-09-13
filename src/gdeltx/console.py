"""Diagnostics channel.

Everything here writes to stderr so that stdout carries only data.
"""

from __future__ import annotations

import sys

from rich.console import Console

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
