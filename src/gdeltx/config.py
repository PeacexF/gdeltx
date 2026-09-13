"""Configuration loading.

Precedence is CLI flags > config file > built-in defaults. A missing config
file is not an error.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from gdeltx.errors import ConfigError

CONFIG_FILENAME = "config.toml"
APP_NAME = "gdeltx"

VALID_FORMATS = ("table", "json", "jsonl", "csv")


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / APP_NAME


def cache_home() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / APP_NAME


def default_config_path() -> Path:
    return config_home() / CONFIG_FILENAME


@dataclass(frozen=True, slots=True)
class ApiConfig:
    timeout: float = 30.0
    retries: int = 3
    min_interval: float = 5.0


@dataclass(frozen=True, slots=True)
class CacheConfig:
    enabled: bool = True
    ttl: int = 3600
    directory: Path | None = None
    max_bytes: int = 5 * 1024**3

    def resolved_directory(self) -> Path:
        return self.directory or cache_home()


@dataclass(frozen=True, slots=True)
class OutputConfig:
    format: str = "table"


@dataclass(frozen=True, slots=True)
class Config:
    api: ApiConfig = ApiConfig()
    cache: CacheConfig = CacheConfig()
    output: OutputConfig = OutputConfig()
    source_path: Path | None = None


_SECTIONS: dict[str, type] = {"api": ApiConfig, "cache": CacheConfig, "output": OutputConfig}


def _expected_type(cls: type, key: str) -> type:
    annotation = str(cls.__dataclass_fields__[key].type)
    if "Path" in annotation:
        return Path
    return type(getattr(cls(), key))


def _coerce(section: str, key: str, value: Any, expected: type) -> Any:
    if expected is Path and isinstance(value, str):
        return Path(value).expanduser()
    if expected is float and type(value) is int:
        return float(value)
    if type(value) is not expected:
        raise ConfigError(
            f"invalid value for {section}.{key}: expected {expected.__name__}, got {value!r}"
        )
    return value


def _build_section(name: str, table: dict[str, Any]) -> Any:
    cls = _SECTIONS[name]
    fields = set(cls.__dataclass_fields__)
    kwargs: dict[str, Any] = {}
    for key, value in table.items():
        if key not in fields:
            raise ConfigError(
                f"unknown config key: {name}.{key}",
                hint=f"Known keys for [{name}]: {', '.join(sorted(fields))}",
            )
        kwargs[key] = _coerce(name, key, value, _expected_type(cls, key))
    return cls(**kwargs)


def load(path: Path | None = None) -> Config:
    target = path or default_config_path()
    if not target.is_file():
        if path is not None:
            raise ConfigError(f"config file not found: {target}")
        return Config()

    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {target}: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {target}: {exc}") from None

    sections: dict[str, Any] = {}
    for name, table in data.items():
        if name not in _SECTIONS:
            known = ", ".join(sorted(_SECTIONS))
            raise ConfigError(f"unknown config section: [{name}]", hint=f"Known sections: {known}")
        if not isinstance(table, dict):
            raise ConfigError(f"[{name}] must be a table, got {type(table).__name__}")
        sections[name] = _build_section(name, table)

    config = Config(**sections, source_path=target)
    _validate(config)
    return config


def _validate(config: Config) -> None:
    if config.output.format not in VALID_FORMATS:
        raise ConfigError(
            f"invalid output.format: {config.output.format!r}",
            hint=f"Expected one of: {', '.join(VALID_FORMATS)}",
        )
    if config.api.retries < 0:
        raise ConfigError("api.retries must not be negative")
    if config.api.timeout <= 0:
        raise ConfigError("api.timeout must be greater than zero")
    if config.cache.ttl < 0:
        raise ConfigError("cache.ttl must not be negative")


def apply_overrides(
    config: Config,
    *,
    output_format: str | None = None,
    no_cache: bool = False,
    cache_ttl: int | None = None,
) -> Config:
    api, cache, output = config.api, config.cache, config.output

    if output_format is not None:
        if output_format not in VALID_FORMATS:
            raise ConfigError(
                f"invalid format: {output_format!r}",
                hint=f"Expected one of: {', '.join(VALID_FORMATS)}",
            )
        output = replace(output, format=output_format)
    if no_cache:
        cache = replace(cache, enabled=False)
    if cache_ttl is not None:
        if cache_ttl < 0:
            raise ConfigError("--cache-ttl must not be negative")
        cache = replace(cache, ttl=cache_ttl)

    return replace(config, api=api, cache=cache, output=output)
