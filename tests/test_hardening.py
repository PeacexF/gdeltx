"""Process-level behaviour: broken pipes, the module entry point, streaming memory, no shell."""

import ast
import io
import os
import subprocess
import sys
import textwrap
import tracemalloc
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from builders import gkg_line
from gdeltx.commands import entities as entities_cmd
from gdeltx.console import Reporter
from gdeltx.output import Format
from gdeltx.sources.files import Dataset, FileContent, FilePlan

SOURCE = Path(__file__).parents[1] / "src" / "gdeltx"

# The child mocks HTTP itself, so a real pipe can be broken without touching the network.
CHILD = textwrap.dedent(
    """
    import sys, httpx, respx
    from gdeltx.cli import main
    from gdeltx.sources.doc import ENDPOINT

    articles = [
        {"url": f"https://e.com/{i}", "title": "t" * 2000,
         "seendate": "20260912T083000Z", "domain": "e.com"}
        for i in range(250)
    ]
    with respx.mock(assert_all_called=False) as router:
        router.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"articles": articles}))
        sys.argv = ["gdeltx", *sys.argv[1:]]
        main()
    """
)


def child_env(tmp_path: Path) -> dict[str, str]:
    config = tmp_path / "config" / "gdeltx"
    config.mkdir(parents=True)
    (config / "config.toml").write_text("[api]\nmin_interval = 0.0\n", encoding="utf-8")
    return {
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        # Anything that slips past the mocks fails fast instead of reaching GDELT.
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
    }


@pytest.mark.parametrize("fmt", ["--jsonl", "--json", "--csv", "--format=table"])
def test_a_reader_that_exits_early_gets_no_traceback(tmp_path: Path, fmt: str) -> None:
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    with subprocess.Popen(
        [sys.executable, str(script), "search", "x", "--max", "250", "--no-cache", fmt],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env(tmp_path),
    ) as proc:
        assert proc.stdout is not None and proc.stderr is not None
        # ~500 KB of output cannot fit a pipe buffer, so the child is still writing here.
        assert proc.stdout.read(1024)
        proc.stdout.close()
        stderr = proc.stderr.read()
        proc.wait(timeout=30)
    assert stderr == b""
    assert proc.returncode == 0


def test_module_entry_point_renders_errors_like_the_script(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "gdeltx", "search", "x", "--since", "banana"],
        capture_output=True,
        text=True,
        env=child_env(tmp_path),
        check=False,
    )
    assert proc.returncode == 64
    assert proc.stdout == ""
    assert proc.stderr.startswith('ERROR: invalid time range: "banana"')
    assert "Traceback" not in proc.stderr


# ---- streaming ------------------------------------------------------------------------


class GeneratedFetcher:
    """Serves one GKG file whose rows are produced on demand and never held."""

    cache = None

    def __init__(self, rows: int) -> None:
        self.rows = rows

    def _lines(self) -> Iterator[str]:
        for i in range(self.rows):
            yield gkg_line(
                V21DATE="20260912100000",
                V2SOURCECOMMONNAME=f"site{i % 97}.com",
                V2DOCUMENTIDENTIFIER=f"https://site{i % 97}.com/{i}",
                V2ENHANCEDPERSONS=f"Person {i},1;Other {i},9",
                V2ENHANCEDORGANIZATIONS="Company X,5",
                V21ALLNAMES="Company X,5",
            )

    def iter_files(self, file_plan: FilePlan) -> Iterator[FileContent]:
        yield FileContent(file_plan.stamps[0], "generated", True, self._lines())


class Discard(io.TextIOBase):
    def write(self, text: str) -> int:
        return len(text)


def peak_bytes_exporting(rows: int) -> int:
    stamp = datetime(2026, 9, 12, 10, tzinfo=UTC)
    tracemalloc.start()
    try:
        written = entities_cmd.run(
            "Company X",
            fetcher=GeneratedFetcher(rows),
            file_plan=FilePlan(Dataset.GKG, [stamp]),
            reporter=Reporter(quiet=True),
            fmt=Format.JSONL,
            start=stamp,
            end=stamp,
            mentions=True,
            stream=Discard(),
        )
        assert written == rows * 3
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_jsonl_export_memory_does_not_grow_with_the_row_count() -> None:
    small, large = peak_bytes_exporting(500), peak_bytes_exporting(5000)
    assert large < small * 1.5, f"peak grew from {small} to {large} bytes for 10x the rows"


# ---- no shell ---------------------------------------------------------------------------

FORBIDDEN_CALLS = {"system", "popen", "eval", "exec", "execv", "execvp", "spawnv"}


def test_nothing_in_the_package_can_run_a_shell() -> None:
    offences = []
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                names += [alias.name for alias in node.names]
                if any(name.split(".")[0] in {"subprocess", "pty", "shlex"} for name in names):
                    offences.append(f"{path.name}:{node.lineno} imports {names}")
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in FORBIDDEN_CALLS:
                    offences.append(f"{path.name}:{node.lineno} calls {name}")
    assert offences == []
