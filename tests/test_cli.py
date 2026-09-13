import io

import pytest
import typer
from typer.testing import CliRunner

from gdeltx import __version__
from gdeltx.cli import app, resolve_format
from gdeltx.console import Reporter
from gdeltx.errors import APIError, ConfigError, GdeltxError, InputError

runner = CliRunner()


@pytest.fixture
def probe_app():
    """Register a throwaway command so the root callback actually runs."""

    @app.command("probe")
    def _probe(ctx: typer.Context) -> None:
        context = ctx.obj
        typer.echo(f"{context.config.output.format}:{context.reporter.verbose}")

    yield app
    app.registered_commands.pop()


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "OSINT investigation" in result.output


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_no_args_shows_help_and_fails() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code != 0


def test_callback_builds_context(probe_app) -> None:
    result = runner.invoke(probe_app, ["probe"])
    assert result.exit_code == 0
    assert result.output.strip() == "table:False"


def test_verbose_reaches_the_reporter(probe_app) -> None:
    result = runner.invoke(probe_app, ["--verbose", "probe"])
    assert result.exit_code == 0
    assert result.output.strip().endswith(":True")


def test_verbose_and_quiet_conflict(probe_app) -> None:
    result = runner.invoke(probe_app, ["--verbose", "--quiet", "probe"])
    assert result.exit_code != 0


def test_missing_config_is_reported(probe_app) -> None:
    result = runner.invoke(probe_app, ["--config", "/nonexistent/config.toml", "probe"])
    assert result.exit_code != 0
    assert isinstance(result.exception, (ConfigError, SystemExit))


def test_config_file_is_honoured(probe_app, tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[output]\nformat = "jsonl"\n', encoding="utf-8")
    result = runner.invoke(probe_app, ["--config", str(config), "probe"])
    assert result.exit_code == 0
    assert result.output.strip().startswith("jsonl")


@pytest.mark.parametrize(
    ("fmt", "flags", "expected"),
    [
        (None, (False, False, False), "table"),
        ("json", (False, False, False), "json"),
        (None, (True, False, False), "json"),
        (None, (False, True, False), "jsonl"),
        (None, (False, False, True), "csv"),
    ],
)
def test_resolve_format(fmt: str | None, flags: tuple[bool, bool, bool], expected: str) -> None:
    assert resolve_format("table", fmt, *flags) == expected


def test_resolve_format_defaults_to_configured_base() -> None:
    assert resolve_format("jsonl", None, False, False, False) == "jsonl"


def test_conflicting_shorthand_flags_rejected() -> None:
    with pytest.raises(typer.BadParameter, match="conflicting"):
        resolve_format("table", None, True, True, False)


def test_format_conflicts_with_shorthand() -> None:
    with pytest.raises(typer.BadParameter, match="conflicts"):
        resolve_format("table", "csv", True, False, False)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (InputError("bad"), 64),
        (ConfigError("bad"), 78),
        (APIError("bad"), 70),
        (GdeltxError("bad"), 1),
    ],
)
def test_exit_codes_are_distinct(error: GdeltxError, code: int) -> None:
    assert error.exit_code == code


def test_reporter_writes_errors_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    Reporter().error(InputError('invalid time range: "banana"', hint="Expected examples: 7d"))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR" in captured.err
    assert "banana" in captured.err
    assert "Expected examples: 7d" in captured.err


def test_reporter_debug_requires_verbose(capsys: pytest.CaptureFixture[str]) -> None:
    Reporter(verbose=False).debug("hidden")
    assert capsys.readouterr().err == ""
    Reporter(verbose=True).debug("shown")
    assert "shown" in capsys.readouterr().err


def test_reporter_quiet_suppresses_warnings(capsys: pytest.CaptureFixture[str]) -> None:
    Reporter(quiet=True).warn("hush")
    assert capsys.readouterr().err == ""


def test_reporter_warns_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    Reporter().warn("listen")
    assert "listen" in capsys.readouterr().err


def test_broken_pipe_is_silent() -> None:
    from gdeltx.models import RequestMeta
    from gdeltx.output import write

    class Broken(io.StringIO):
        def write(self, _: str) -> int:
            raise BrokenPipeError

    meta = RequestMeta(query="x", endpoint="doc")
    assert write([], fmt="json", meta=meta, stream=Broken()) == 0
