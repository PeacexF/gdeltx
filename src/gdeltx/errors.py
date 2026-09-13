"""Exception hierarchy. Exit codes follow sysexits(3) where a code fits."""

from __future__ import annotations


class GdeltxError(Exception):
    """Base for every expected failure. Never rendered as a traceback."""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class InputError(GdeltxError):
    exit_code = 64


class ParseError(GdeltxError):
    exit_code = 65


class APIError(GdeltxError):
    exit_code = 70


class CacheError(GdeltxError):
    exit_code = 74


class NotFoundError(APIError):
    pass


class RateLimitError(APIError):
    exit_code = 75


class ConfigError(GdeltxError):
    exit_code = 78
