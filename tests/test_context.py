"""Context 2.0: parsing, window clamping, reading layout and the CLI command.

`context_artlist.synthetic.json` is hand-written: GDELT does not document the
record field names and no real artlist response has been captured yet
(API-NOTES §7). Replace it with a capture when one exists.
"""

import csv
import io
import json
import re
from datetime import UTC, datetime

import httpx
import pytest
import respx
from typer.testing import CliRunner

from gdeltx.cli import app
from gdeltx.commands.context import highlight_pattern, highlight_terms, write_reading
from gdeltx.console import Reporter
from gdeltx.errors import InputError, ParseError
from gdeltx.models import ContextSnippet
from gdeltx.parsers.context import parse_articles
from gdeltx.sources.context import ENDPOINT, Sort, build_params, window_params

NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)
ANSI = re.compile(r"\x1b\[")


def payload(fixtures_dir) -> dict:
    return json.loads((fixtures_dir / "context_artlist.synthetic.json").read_text("utf-8"))


@pytest.fixture(autouse=True)
def plain_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)


class Recorder(Reporter):
    def __init__(self) -> None:
        super().__init__(quiet=True)
        self.warnings: list[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)


# ---- parsing ---------------------------------------------------------------


def test_parses_exact_fields(fixtures_dir) -> None:
    first = next(parse_articles(payload(fixtures_dir), "OpenAI"))
    assert first.url == "https://www.reuters.com/technology/openai-regulation-2026-09-11/"
    assert first.title == "OpenAI faces new regulation push in Brussels"
    assert first.domain == "reuters.com"
    assert first.language == "English"
    assert first.published_at == datetime(2026, 9, 11, 12, 15, tzinfo=UTC)
    assert first.context.startswith("EU officials said on Thursday")
    assert first.query == "OpenAI"
    assert first.raw is not None and first.raw["isquote"] == 0


def test_snippet_read_from_either_key(fixtures_dir) -> None:
    records = list(parse_articles(payload(fixtures_dir), "OpenAI"))
    assert "hold OpenAI accountable" in records[1].context


def test_snippet_is_preserved_whole(fixtures_dir) -> None:
    data = payload(fixtures_dir)
    first = next(parse_articles(data, "OpenAI"))
    assert first.context == data["articles"][0]["sentence"]


def test_rows_without_url_are_skipped_with_a_warning(fixtures_dir) -> None:
    skipped: list[str] = []
    records = list(parse_articles(payload(fixtures_dir), "OpenAI", on_skip=skipped.append))
    assert len(records) == 3
    assert skipped == ["skipped Context record 2: no url"]


def test_blank_title_and_bad_date_become_none(fixtures_dir) -> None:
    last = list(parse_articles(payload(fixtures_dir), "OpenAI"))[-1]
    assert last.title is None
    assert last.published_at is None
    assert last.context == ""


def test_captured_empty_response(fixtures_dir) -> None:
    data = json.loads((fixtures_dir / "context_empty.json").read_text("utf-8"))
    assert list(parse_articles(data, "OpenAI")) == []


def test_empty_object_means_no_matches() -> None:
    assert list(parse_articles({}, "OpenAI")) == []


@pytest.mark.parametrize("bad", [[], "text", None, {"articles": {"url": "x"}}])
def test_unexpected_shapes_raise_parse_error(bad: object) -> None:
    with pytest.raises(ParseError):
        list(parse_articles(bad, "OpenAI"))


# ---- time window -----------------------------------------------------------


def test_default_window_is_24h() -> None:
    assert window_params(None, None, reporter=Recorder(), now=NOW) == {"timespan": "24h"}


def test_relative_range_within_coverage_is_untouched() -> None:
    reporter = Recorder()
    assert window_params("48h", None, reporter=reporter, now=NOW) == {"timespan": "48h"}
    assert reporter.warnings == []


def test_relative_range_beyond_72h_is_clamped_loudly() -> None:
    reporter = Recorder()
    assert window_params("30d", None, reporter=reporter, now=NOW) == {"timespan": "72h"}
    assert len(reporter.warnings) == 1
    assert "72 hours" in reporter.warnings[0]


def test_absolute_range_uses_datetimes() -> None:
    params = window_params(
        "2026-09-11T00:00:00Z", "2026-09-12T00:00:00Z", reporter=Recorder(), now=NOW
    )
    assert params == {"startdatetime": "20260911000000", "enddatetime": "20260912000000"}


def test_absolute_start_before_coverage_is_clamped_inside_the_window() -> None:
    reporter = Recorder()
    params = window_params("2026-09-01", "2026-09-12T06:00:00Z", reporter=reporter, now=NOW)
    assert params["startdatetime"] == "20260909120100"
    assert len(reporter.warnings) == 1


def test_range_entirely_outside_coverage_is_an_input_error() -> None:
    with pytest.raises(InputError, match="72-hour"):
        window_params("2026-08-01", "2026-08-02", reporter=Recorder(), now=NOW)


def test_params_carry_required_mode_and_sort() -> None:
    params = build_params("OpenAI", {"timespan": "24h"}, max_records=10, sort=Sort.DATEDESC)
    assert params == {
        "query": "OpenAI",
        "mode": "artlist",
        "format": "json",
        "maxrecords": 10,
        "timespan": "24h",
        "sort": "DateDesc",
    }


def test_relevance_sort_is_gdelts_default_and_not_sent() -> None:
    params = build_params("OpenAI", {"timespan": "24h"}, max_records=10, sort=Sort.RELEVANCE)
    assert "sort" not in params


# ---- highlighting ----------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("OpenAI", ["OpenAI"]),
        ('"Company X" sanctions', ["Company X", "sanctions"]),
        ("OpenAI (regulation OR lawsuit)", ["OpenAI", "regulation", "lawsuit"]),
        ('near10:"OpenAI Microsoft"', ["OpenAI", "Microsoft"]),
        ("OpenAI -Musk domain:bbc.co.uk sourcecountry:UK", ["OpenAI"]),
        ('"" OR', []),
    ],
)
def test_highlight_terms(query: str, expected: list[str]) -> None:
    assert highlight_terms(query) == expected


def test_highlight_matches_whole_words_case_insensitively() -> None:
    pattern = highlight_pattern("AI")
    assert pattern is not None
    assert pattern.findall("ai said AI, not said") == ["ai", "AI"]


# ---- reading layout --------------------------------------------------------


def snippet(context: str) -> ContextSnippet:
    return ContextSnippet(
        url="https://reuters.com/a",
        title="Title",
        domain="reuters.com",
        published_at=datetime(2026, 9, 11, 12, 15, tzinfo=UTC),
        context=context,
        query="OpenAI",
    )


def test_reading_layout_order() -> None:
    stream = io.StringIO()
    write_reading([snippet("OpenAI said so.")], stream, pattern=highlight_pattern("OpenAI"))
    assert stream.getvalue().splitlines() == [
        "Title",
        "reuters.com · 2026-09-11 12:15 UTC",
        "",
        "OpenAI said so.",
        "",
        "https://reuters.com/a",
    ]


def test_records_are_blank_line_separated() -> None:
    stream = io.StringIO()
    assert write_reading([snippet("one"), snippet("two")], stream) == 2
    assert "https://reuters.com/a\n\nTitle\n" in stream.getvalue()


def test_piped_output_has_no_ansi_and_long_snippets_stay_on_one_line() -> None:
    long = "OpenAI " + "word " * 60 + "end."
    stream = io.StringIO()
    write_reading([snippet(long)], stream, pattern=highlight_pattern("OpenAI"))
    output = stream.getvalue()
    assert not ANSI.search(output)
    assert long in output.splitlines()


def test_terminal_output_highlights_and_wraps_at_80(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLUMNS", "80")

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    stream = Tty()
    write_reading(
        [snippet("Talks with OpenAI " + "stalled " * 30)], stream, pattern=re.compile("OpenAI")
    )
    output = stream.getvalue()
    assert ANSI.search(output)
    plain = ANSI.sub("", re.sub(r"\x1b\[[0-9;]*m", "", output))
    assert max(len(line) for line in plain.splitlines()) <= 80
    assert "stalled" in plain


def test_empty_result_says_so() -> None:
    stream = io.StringIO()
    assert write_reading([], stream) == 0
    assert stream.getvalue().strip() == "No results."


# ---- CLI -------------------------------------------------------------------


runner = CliRunner()


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("gdeltx.sources.http.time.sleep", lambda _: None)


@respx.mock
def test_cli_json_is_valid_and_reproducible(isolated, fixtures_dir) -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=payload(fixtures_dir)))
    result = runner.invoke(app, ["context", "OpenAI", "--since", "30d", "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["endpoint"] == "context"
    assert document["parameters"]["timespan"] == "72h"
    assert document["parameters"]["mode"] == "artlist"
    assert len(document["results"]) == 3
    assert "raw" not in document["results"][0] or document["results"][0]["raw"] is None
    assert route.calls.last.request.url.params["mode"] == "artlist"
    assert "72 hours" in result.stderr


@respx.mock
def test_cli_jsonl_and_csv(isolated, fixtures_dir) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=payload(fixtures_dir)))

    jsonl = runner.invoke(app, ["context", "OpenAI", "--jsonl", "--raw", "--no-cache"])
    assert jsonl.exit_code == 0
    lines = [json.loads(line) for line in jsonl.stdout.splitlines()]
    assert len(lines) == 3 and lines[0]["raw"]["domain"] == "reuters.com"

    as_csv = runner.invoke(app, ["context", "OpenAI", "--csv", "--no-cache"])
    assert as_csv.exit_code == 0
    rows = list(csv.DictReader(io.StringIO(as_csv.stdout)))
    assert rows[1]["context"].startswith('"We are asking')


@respx.mock
def test_cli_default_is_reading_layout(isolated, fixtures_dir) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=payload(fixtures_dir)))
    result = runner.invoke(app, ["context", "OpenAI"])
    assert result.exit_code == 0
    assert "reuters.com · 2026-09-11 12:15 UTC" in result.stdout
    assert not ANSI.search(result.stdout)


@respx.mock
def test_cli_max_above_cap_is_clamped(isolated) -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={}))
    result = runner.invoke(app, ["context", "OpenAI", "--max", "500", "--json"])
    assert result.exit_code == 0
    assert route.calls.last.request.url.params["maxrecords"] == "200"
    assert "at most 200" in result.stderr


@respx.mock
def test_cli_api_failure_is_never_an_empty_result(isolated) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(500))
    result = runner.invoke(app, ["context", "OpenAI", "--json"])
    assert result.exit_code != 0
    assert result.stdout == ""
