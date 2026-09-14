"""Bucket math, DOC timeline parsing/fetching, and the `timeline` command."""

import csv
import io
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from typer.testing import CliRunner

from builders import event_line, mock_published
from gdeltx.cli import app
from gdeltx.commands.timeline import (
    Bucket,
    bucket_edges,
    build_buckets,
    choose_bucket,
    clip_event_window,
)
from gdeltx.console import Reporter
from gdeltx.errors import InputError, ParseError
from gdeltx.parsers.doc import parse_timeline
from gdeltx.sources.doc import ENDPOINT, TIMELINE_MIN
from gdeltx.sources.doc import timeline as doc_timeline
from gdeltx.sources.files import Dataset
from gdeltx.sources.http import HttpClient


class Recorder(Reporter):
    def __init__(self) -> None:
        super().__init__(quiet=True)
        self.warnings: list[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)


# ---- bucket choice -----------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (10, Bucket.DAY),
        (14, Bucket.DAY),
        (15, Bucket.WEEK),
        (120, Bucket.WEEK),
        (121, Bucket.MONTH),
    ],
)
def test_choose_bucket_thresholds(days: int, expected: Bucket) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert choose_bucket(start, start + timedelta(days=days)) is expected


# ---- bucket edges --------------------------------------------------------------


def test_day_edges_cover_a_boundary_crossing_range() -> None:
    start = datetime(2026, 9, 13, 5, tzinfo=UTC)
    end = datetime(2026, 9, 14, 5, tzinfo=UTC)
    edges = bucket_edges(start, end, Bucket.DAY)
    assert edges == [
        (datetime(2026, 9, 13, tzinfo=UTC), datetime(2026, 9, 14, tzinfo=UTC)),
        (datetime(2026, 9, 14, tzinfo=UTC), datetime(2026, 9, 15, tzinfo=UTC)),
    ]


def test_week_edges_align_to_monday() -> None:
    start = datetime(2026, 9, 16, 10, tzinfo=UTC)  # a Wednesday
    end = start + timedelta(days=10)
    edges = bucket_edges(start, end, Bucket.WEEK)
    first_start, first_end = edges[0]
    assert first_start.weekday() == 0
    assert first_start <= start < first_end
    assert first_end - first_start == timedelta(days=7)
    assert edges[-1][1] > end


def test_month_edges_are_calendar_months() -> None:
    start = datetime(2026, 1, 15, tzinfo=UTC)
    end = datetime(2026, 3, 10, tzinfo=UTC)
    edges = bucket_edges(start, end, Bucket.MONTH)
    assert edges == [
        (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)),
        (datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC)),
        (datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)),
    ]


def test_december_rolls_over_into_next_year() -> None:
    edges = bucket_edges(
        datetime(2026, 12, 20, tzinfo=UTC), datetime(2027, 1, 5, tzinfo=UTC), Bucket.MONTH
    )
    assert edges == [
        (datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)),
        (datetime(2027, 1, 1, tzinfo=UTC), datetime(2027, 2, 1, tzinfo=UTC)),
    ]


# ---- bucket records ------------------------------------------------------------


def test_build_buckets_labels_and_preserves_none_events() -> None:
    edges = [
        (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)),
        (datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC)),
    ]
    records = list(build_buckets("q", edges, Bucket.MONTH, [3, 0], [None, 5]))
    assert [r.bucket for r in records] == ["2026-01", "2026-02"]
    assert [r.articles for r in records] == [3, 0]
    assert [r.events for r in records] == [None, 5]


# ---- event window clipping -----------------------------------------------------


def test_small_range_is_not_clipped() -> None:
    start = datetime(2026, 9, 13, tzinfo=UTC)
    end = start + timedelta(hours=2)
    reporter = Recorder()
    assert clip_event_window(start, end, max_files=1000, allow_large=False, reporter=reporter) == (
        start
    )
    assert not reporter.warnings


def test_large_range_is_clipped_and_warns() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=2)  # 8 fifteen-minute slots
    reporter = Recorder()
    clipped = clip_event_window(start, end, max_files=4, allow_large=False, reporter=reporter)
    assert clipped == end - timedelta(minutes=15 * 4)
    assert "limited to the last 4 files" in reporter.warnings[0]


def test_allow_large_skips_clipping() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=2)
    reporter = Recorder()
    assert clip_event_window(start, end, max_files=4, allow_large=True, reporter=reporter) == start
    assert not reporter.warnings


# ---- DOC timelinevolraw parsing -------------------------------------------------


def test_parse_timeline_fixture(fixtures_dir) -> None:
    payload = json.loads((fixtures_dir / "doc_timeline_2017.json").read_text("utf-8"))
    points = list(parse_timeline(payload))
    assert len(points) == 32
    assert points[0] == (datetime(2017, 1, 1, tzinfo=UTC), 3)
    assert points[-1] == (datetime(2017, 2, 1, tzinfo=UTC), 5)


def test_parse_timeline_empty_series_is_empty() -> None:
    assert list(parse_timeline({"timeline": []})) == []


@pytest.mark.parametrize(
    "bad", [[], "nope", {"timeline": "nope"}, {"timeline": [{"data": "nope"}]}]
)
def test_parse_timeline_unexpected_shapes_raise(bad: object) -> None:
    with pytest.raises(ParseError):
        list(parse_timeline(bad))


@pytest.fixture
def http() -> HttpClient:
    return HttpClient(retries=0, reporter=Reporter(quiet=True))


@respx.mock
def test_doc_timeline_sends_mode_and_range(http: HttpClient, fixtures_dir) -> None:
    payload = json.loads((fixtures_dir / "doc_timeline_2017.json").read_text("utf-8"))
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    start, end = datetime(2017, 1, 1, tzinfo=UTC), datetime(2017, 2, 1, tzinfo=UTC)
    meta, points = doc_timeline(http, "OpenAI", reporter=Recorder(), start=start, end=end)
    sent = route.calls.last.request.url.params
    assert sent["mode"] == "timelinevolraw"
    assert sent["startdatetime"] == "20170101000000"
    assert sent["enddatetime"] == "20170201000000"
    assert meta.endpoint == "doc"
    assert len(points) == 32


@respx.mock
def test_doc_timeline_clamps_start_before_2017(http: HttpClient) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"timeline": []}))
    reporter = Recorder()
    doc_timeline(
        http,
        "OpenAI",
        reporter=reporter,
        start=datetime(2010, 1, 1, tzinfo=UTC),
        end=datetime(2017, 3, 1, tzinfo=UTC),
    )
    assert reporter.warnings
    assert "2017" in reporter.warnings[0]


def test_doc_timeline_rejects_empty_query(http: HttpClient) -> None:
    with pytest.raises(InputError, match="empty query"):
        doc_timeline(
            http, "   ", reporter=Recorder(), start=TIMELINE_MIN, end=TIMELINE_MIN + timedelta(1)
        )


# ---- CLI ------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("gdeltx.sources.http.time.sleep", lambda _: None)
    for name in ("FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(name, raising=False)


START = "2026-09-13T00:00:00Z"
END = "2026-09-13T02:00:00Z"
NOW = datetime(2026, 9, 13, 2, tzinfo=UTC)

MATCHING_EVENT = {
    "GLOBALEVENTID": "1",
    "DATEADDED": "20260913015000",
    "Actor1Name": "COMPANY X",
    "SOURCEURL": "https://example.com/a",
}
OTHER_EVENT = {
    "GLOBALEVENTID": "2",
    "DATEADDED": "20260913015500",
    "Actor1Name": "SOMEONE ELSE",
    "SOURCEURL": "https://example.com/b",
}


def _doc_payload() -> dict:
    return {
        "timeline": [
            {"series": "Article Count", "data": [{"date": "20260913T010000Z", "value": 5}]}
        ]
    }


def invoke(*args: str):
    return runner.invoke(app, ["timeline", "Company X", "--since", START, "--until", END, *args])


@respx.mock
def test_cli_json_combines_both_series() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_doc_payload()))
    mock_published(
        Dataset.EVENTS, {0: [event_line(**MATCHING_EVENT), event_line(**OTHER_EVENT)]}, now=NOW
    )
    result = invoke("--json")
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["endpoint"] == "timeline"
    assert document["parameters"]["bucket"] == "day"
    assert len(document["results"]) == 1
    bucket = document["results"][0]
    assert bucket["bucket"] == "2026-09-13"
    assert bucket["articles"] == 5
    assert bucket["events"] == 1


@respx.mock
def test_cli_table_shows_bucket_header_and_counts() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_doc_payload()))
    mock_published(Dataset.EVENTS, {0: [event_line(**MATCHING_EVENT)]}, now=NOW)
    result = invoke()
    assert result.exit_code == 0, result.output
    header = result.stdout.splitlines()[0].split()
    assert header == ["DAY", "ARTICLES", "EVENTS"]
    assert "2026-09-13" in result.stdout
    assert "5" in result.stdout


@respx.mock
def test_cli_bars_appear_in_table_but_never_in_json() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_doc_payload()))
    mock_published(Dataset.EVENTS, {0: [event_line(**MATCHING_EVENT)]}, now=NOW)

    with_bars = invoke("--bars")
    assert "█" in with_bars.stdout

    without_bars = invoke()
    assert "█" not in without_bars.stdout

    as_json = invoke("--bars", "--json")
    assert "█" not in as_json.stdout
    json.loads(as_json.stdout)  # still valid JSON


@respx.mock
def test_cli_jsonl_and_csv() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_doc_payload()))
    mock_published(Dataset.EVENTS, {0: [event_line(**MATCHING_EVENT)]}, now=NOW)

    jsonl = invoke("--jsonl")
    assert jsonl.exit_code == 0, jsonl.output
    lines = [json.loads(line) for line in jsonl.stdout.splitlines()]
    assert lines[0]["articles"] == 5

    as_csv = invoke("--csv")
    assert as_csv.exit_code == 0
    rows = list(csv.DictReader(io.StringIO(as_csv.stdout)))
    assert rows[0]["bucket"] == "2026-09-13"


@respx.mock
def test_cli_empty_bucket_is_zero_not_a_gap() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"timeline": []}))
    mock_published(Dataset.EVENTS, {}, now=NOW)
    result = invoke()
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["DAY", "ARTICLES", "EVENTS"]
    assert lines[1].split() == ["2026-09-13", "0", "0"]


def test_cli_rejects_query_syntax_before_any_request() -> None:
    with respx.mock(assert_all_called=False) as mock:
        result = runner.invoke(app, ["timeline", "COMPANY X OR GOVERNMENT Y"])
        assert mock.calls.call_count == 0
    assert isinstance(result.exception, InputError)


@respx.mock
def test_cli_bucket_override() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"timeline": []}))
    mock_published(Dataset.EVENTS, {}, now=NOW)
    result = invoke("--bucket", "month", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["parameters"]["bucket"] == "month"
