"""GDELTX — a CLI for OSINT investigation using the GDELT APIs."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gdeltx")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
