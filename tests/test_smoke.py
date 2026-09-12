import subprocess
import sys

from typer.testing import CliRunner

from gdeltx import __version__
from gdeltx.cli import app

runner = CliRunner()


def test_version_is_populated() -> None:
    assert __version__
    assert __version__ != "0.0.0.dev0", "package should be installed, not run from source"


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "gdeltx" in result.output


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_module_entrypoint() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "gdeltx", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == __version__
