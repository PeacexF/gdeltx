"""The PLAN.md §4 investigation, end to end, and a CLI-conventions audit of every command.

Runs through ``gdeltx.cli.main`` (the installed entry point), not the bare typer app, so
exit codes and error rendering are exactly what a shell sees. Upstream is the recorded
fixtures: captured GKG and Events rows, the captured Context response and DOC timeline,
and the synthetic DOC artlist (a real one has never been captured, see ROADMAP Phase 2).
"""

import json
import sys
from datetime import UTC, datetime

import httpx
import pytest
import respx

from builders import event_line, zipped
from gdeltx.cli import main
from gdeltx.sources import context, doc
from gdeltx.sources.files.index import LASTUPDATE_URL, Dataset, floor_slot, slot_url

TERM = "Dario Amodei"
ENVELOPE = {"query", "endpoint", "source", "retrieved_at", "parameters", "cached", "results"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("gdeltx.sources.http.time.sleep", lambda _: None)
    for name in ("FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(name, raising=False)


class Upstream:
    def __init__(self, router: respx.MockRouter, fixtures_dir) -> None:
        latest = floor_slot(datetime.now(UTC))
        stamp = f"{latest:%Y%m%d%H%M%S}"
        gkg_rows = (fixtures_dir / "gkg.sample.tsv").read_text("utf-8").splitlines(keepends=True)
        event_rows = [
            *(fixtures_dir / "events_export.sample.tsv").read_text("utf-8").splitlines(True),
            event_line(
                GLOBALEVENTID="9000000001",
                SQLDATE=stamp[:8],
                DATEADDED=stamp,
                Actor1Name="DARIO AMODEI",
                Actor2Name="UNITED STATES",
                EventCode="036",
                EventRootCode="03",
                ActionGeo_FullName="Washington, District of Columbia, United States",
                SOURCEURL="https://example.com/amodei",
            ),
        ]

        self.lastupdate = router.get(LASTUPDATE_URL).mock(
            return_value=httpx.Response(
                200,
                text="".join(
                    f"1 x http://data.gdeltproject.org/gdeltv2/{stamp}.{d.suffix}\n"
                    for d in (Dataset.EVENTS, Dataset.GKG)
                ),
            )
        )
        self.data = [
            router.get(slot_url(Dataset.GKG, latest)).mock(
                return_value=httpx.Response(200, content=zipped("gkg.csv", gkg_rows))
            ),
            router.get(slot_url(Dataset.EVENTS, latest)).mock(
                return_value=httpx.Response(200, content=zipped("export.CSV", event_rows))
            ),
            router.get(doc.ENDPOINT, params__contains={"mode": "artlist"}).mock(
                return_value=httpx.Response(
                    200, content=(fixtures_dir / "doc_artlist.synthetic.json").read_bytes()
                )
            ),
            router.get(doc.ENDPOINT, params__contains={"mode": "timelinevolraw"}).mock(
                return_value=httpx.Response(
                    200, content=(fixtures_dir / "doc_timeline_2017.json").read_bytes()
                )
            ),
            router.get(context.ENDPOINT).mock(
                return_value=httpx.Response(
                    200, content=(fixtures_dir / "context_artlist.json").read_bytes()
                )
            ),
        ]
        router.get(url__startswith="https://data.gdeltproject.org/gdeltv2/2").mock(
            return_value=httpx.Response(404)
        )

    @property
    def data_calls(self) -> int:
        return sum(route.call_count for route in self.data)


@pytest.fixture
def upstream(fixtures_dir):
    with respx.mock(assert_all_called=False) as router:
        yield Upstream(router, fixtures_dir)


@pytest.fixture
def gdeltx(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    def run(*args: str) -> tuple[int, str, str]:
        capsys.readouterr()
        monkeypatch.setattr(sys, "argv", ["gdeltx", *args])
        try:
            main()
            code = 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        out, err = capsys.readouterr()
        return code, out, err

    return run


# ---- PLAN.md §4 ---------------------------------------------------------------------

WORKFLOW = [
    ("search", f'"{TERM}" AI'),
    ("context", f'"{TERM}" AI'),
    ("entities", TERM, "--since", "1h"),
    ("events", TERM, "--since", "1h"),
    ("sources", TERM, "--from", "gkg", "--since", "1h"),
    ("timeline", TERM, "--since", "1d"),
    ("related", TERM, "--since", "1h", "--min-count", "1"),
    ("geo", TERM, "--since", "1h"),
]


def test_the_investigation_workflow_end_to_end(upstream, gdeltx) -> None:
    for step in WORKFLOW:
        code, out, _ = gdeltx(*step)
        assert code == 0, step
        assert out.strip(), f"{step[0]} printed nothing"

        code, out, _ = gdeltx(*step, "--json")
        assert code == 0, step
        document = json.loads(out)
        assert set(document) >= ENVELOPE, step
        assert document["results"], f"{step[0]} found nothing in the fixtures"

    code, out, _ = gdeltx("search", TERM, "--jsonl")
    assert code == 0
    exported = [json.loads(line) for line in out.splitlines()]
    assert exported and all(record["url"] for record in exported)


def test_workflow_steps_agree_on_the_same_coverage(upstream, gdeltx) -> None:
    entities = json.loads(gdeltx(*WORKFLOW[2], "--json", "--type", "person")[1])["results"]
    related = json.loads(gdeltx(*WORKFLOW[6], "--json", "--type", "person")[1])["results"]
    named = {e["name"] for e in entities} - {"Dario Amodei"}
    assert {r["name"] for r in related} == named
    events = json.loads(gdeltx(*WORKFLOW[3], "--json")[1])["results"]
    assert [e["actor_1"] for e in events] == ["DARIO AMODEI"]


# ---- CLI conventions audit (ROADMAP §7, PLAN.md §27-28) -------------------------------

COMMANDS = [
    ("search", TERM),
    ("context", TERM),
    ("entities", TERM, "--since", "1h"),
    ("events", TERM, "--since", "1h"),
    ("sources", TERM),
    ("sources", TERM, "--from", "gkg", "--since", "1h"),
    ("timeline", TERM, "--since", "1d"),
    ("related", TERM, "--since", "1h"),
    ("geo", TERM, "--since", "1h"),
]
ids = [" ".join(command[:1] + command[2:4]) for command in COMMANDS]


@pytest.mark.parametrize("command", COMMANDS, ids=ids)
@pytest.mark.parametrize("fmt", ["--json", "--jsonl", "--csv"])
def test_machine_output_is_uncontaminated(upstream, gdeltx, command, fmt) -> None:
    code, out, _ = gdeltx("--verbose", *command, fmt)
    assert code == 0
    if fmt == "--json":
        json.loads(out)
    elif fmt == "--jsonl":
        for line in out.splitlines():
            json.loads(line)
    else:
        assert "WARNING" not in out and "GET " not in out


@pytest.mark.parametrize("command", COMMANDS, ids=ids)
def test_verbose_reports_on_stderr(upstream, gdeltx, command) -> None:
    code, _, err = gdeltx("--verbose", *command, "--json")
    assert code == 0
    assert "GET " in err


@pytest.mark.parametrize("command", COMMANDS, ids=ids)
def test_quiet_silences_stderr(upstream, gdeltx, command) -> None:
    code, out, err = gdeltx("--quiet", *command)
    assert code == 0
    assert out and err == ""


@pytest.mark.parametrize("command", COMMANDS, ids=ids)
@pytest.mark.parametrize("status", [500, 429])
def test_upstream_failure_is_an_error_never_an_empty_result(gdeltx, command, status) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.route().mock(return_value=httpx.Response(status, text="upstream down"))
        code, out, err = gdeltx(*command, "--json")
    assert code == (75 if status == 429 else 70)
    assert out == ""
    lines = err.splitlines()
    assert any(line.startswith("ERROR:") for line in lines) and "Traceback" not in err
    assert all(line.startswith(("WARNING:", "ERROR:")) for line in lines[:-1])


@pytest.mark.parametrize("command", COMMANDS, ids=ids)
def test_invalid_input_fails_before_any_request(gdeltx, command) -> None:
    with respx.mock(assert_all_called=False) as router:
        code, out, err = gdeltx(*command, "--until", "banana", "--json")
        assert not router.calls
    assert code == 64
    assert out == "" and "banana" in err


@pytest.mark.parametrize("command", COMMANDS, ids=ids)
def test_cache_reuse_and_no_cache(upstream, gdeltx, command) -> None:
    assert gdeltx(*command, "--json")[0] == 0
    first = upstream.data_calls
    assert first > 0

    assert gdeltx(*command, "--json")[0] == 0
    assert upstream.data_calls == first, "a repeated run went back to the network"

    assert gdeltx(*command, "--json", "--no-cache")[0] == 0
    assert upstream.data_calls > first, "--no-cache was served from the cache"
