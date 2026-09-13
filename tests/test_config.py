from pathlib import Path

import pytest

from gdeltx.config import Config, apply_overrides, load
from gdeltx.errors import ConfigError


def write(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "config.toml"
    target.write_text(body, encoding="utf-8")
    return target


def test_defaults_without_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = load()
    assert config.api.timeout == 30.0
    assert config.api.retries == 3
    assert config.cache.enabled is True
    assert config.output.format == "table"
    assert config.source_path is None


def test_explicit_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "absent.toml")


def test_file_values_override_defaults(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "[api]\ntimeout = 15\nretries = 5\n\n"
        "[cache]\nenabled = false\n\n"
        '[output]\nformat = "csv"\n',
    )
    config = load(path)
    assert config.api.timeout == 15.0
    assert config.api.retries == 5
    assert config.cache.enabled is False
    assert config.output.format == "csv"
    assert config.source_path == path


def test_cli_overrides_beat_file(tmp_path: Path) -> None:
    config = load(write(tmp_path, '[output]\nformat = "csv"\n'))
    result = apply_overrides(config, output_format="jsonl", no_cache=True, cache_ttl=60)
    assert result.output.format == "jsonl"
    assert result.cache.enabled is False
    assert result.cache.ttl == 60


def test_overrides_leave_config_untouched(tmp_path: Path) -> None:
    config = load(write(tmp_path, '[output]\nformat = "csv"\n'))
    apply_overrides(config, output_format="json")
    assert config.output.format == "csv"


def test_unknown_section_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown config section"):
        load(write(tmp_path, "[nope]\nx = 1\n"))


def test_unknown_key_rejected_with_hint(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load(write(tmp_path, "[api]\ntimeuot = 1\n"))
    assert "unknown config key" in str(excinfo.value)
    assert "timeout" in (excinfo.value.hint or "")


def test_wrong_type_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="expected float"):
        load(write(tmp_path, '[api]\ntimeout = "abc"\n'))


def test_bool_is_not_an_int(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, "[cache]\nttl = true\n"))


def test_int_widens_to_float(tmp_path: Path) -> None:
    assert load(write(tmp_path, "[api]\ntimeout = 45\n")).api.timeout == 45.0


def test_malformed_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid TOML"):
        load(write(tmp_path, "[api\n"))


def test_invalid_format_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"invalid output\.format"):
        load(write(tmp_path, '[output]\nformat = "yaml"\n'))


def test_negative_retries_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="retries"):
        load(write(tmp_path, "[api]\nretries = -1\n"))


def test_invalid_override_format_rejected() -> None:
    with pytest.raises(ConfigError, match="invalid format"):
        apply_overrides(Config(), output_format="yaml")


def test_cache_directory_expands_user(tmp_path: Path) -> None:
    config = load(write(tmp_path, '[cache]\ndirectory = "~/somewhere"\n'))
    assert config.cache.resolved_directory().is_absolute()
    assert "~" not in str(config.cache.resolved_directory())
