"""DOC 2.0 search: parsing, windows, filters, time-cursor paging and the CLI.

`doc_artlist.synthetic.json` is hand-written. Every real DOC `artlist` request
from this machine has been refused with 429 (API-NOTES §2), so the field names
follow the documented artlist shape and the captured Context response. Replace
it with a capture when one exists.
"""

import csv
import io
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from typer.testing import CliRunner

from gdeltx.cli import app
from gdeltx.console import Reporter
from gdeltx.errors import APIError, InputError, ParseError, RateLimitError
from gdeltx.parsers.doc import parse_articles
from gdeltx.sources.doc import (
    ENDPOINT,
    PAGE_SIZE,
    Sort,
    build_params,
    compose_query,
    resolve_window,
    search,
)
from gdeltx.sources.http import HttpClient

NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)
STAMP = "%Y%m%dT%H%M%SZ"


class Recorder(Reporter):
    def __init__(self) -> None:
        super().__init__(quiet=True)
        self.warnings: list[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def payload(fixtures_dir) -> dict:
    return json.loads((fixtures_dir / "doc_artlist.synthetic.json").read_text("utf-8"))


def page(newest: datetime, count: int, *, prefix: str, step: timedelta) -> dict:
    return {
        "articles": [
            {
                "url": f"https://example.com/{prefix}/{i}",
                "title": f"{prefix} {i}",
                "seendate": (newest - step * i).strftime(STAMP),
                "domain": "example.com",
            }
            for i in range(count)
        ]
    }


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gdeltx.sources.http.time.sleep", lambda _: None)


@pytest.fixture
def http() -> HttpClient:
    return HttpClient(retries=0, reporter=Reporter(quiet=True))


# ---- parsing ---------------------------------------------------------------


def test_parses_exact_fields(fixtures_dir) -> None:
    first = next(parse_articles(payload(fixtures_dir), "OpenAI"))
    assert first.url == "https://www.reuters.com/technology/openai-regulation-2026-09-11/"
    assert first.title == "OpenAI faces new regulation push in Brussels"
    assert first.domain == "reuters.com"
    assert first.language == "English"
    assert first.source_country == "United States"
    assert first.published_at == datetime(2026, 9, 11, 12, 15, tzinfo=UTC)
    assert first.query == "OpenAI"
    assert first.raw is not None and first.raw["socialimage"].endswith("openai.jpg")


def test_rows_without_url_are_skipped_with_a_warning(fixtures_dir) -> None:
    skipped: list[str] = []
    records = list(parse_articles(payload(fixtures_dir), "OpenAI", on_skip=skipped.append))
    assert len(records) == 4
    assert skipped == ["skipped DOC record 2: no url"]


def test_blank_title_and_bad_date_become_none(fixtures_dir) -> None:
    last = list(parse_articles(payload(fixtures_dir), "OpenAI"))[-1]
    assert last.title is None
    assert last.published_at is None
    assert last.source_country is None


def test_empty_object_means_no_matches() -> None:
    assert list(parse_articles({}, "OpenAI")) == []


@pytest.mark.parametrize("bad", [[], "nope", {"articles": "nope"}])
def test_unexpected_shapes_raise_parse_error(bad: object) -> None:
    with pytest.raises(ParseError):
        list(parse_articles(bad, "OpenAI"))


# ---- time window -----------------------------------------------------------


def test_default_window_is_one_day() -> None:
    assert resolve_window(None, None, reporter=Recorder(), now=NOW).params() == {"timespan": "1d"}


@pytest.mark.parametrize(("since", "sent"), [("7d", "7d"), ("36h", "36h"), ("2w", "14d")])
def test_relative_ranges_go_out_as_timespan(since: str, sent: str) -> None:
    window = resolve_window(since, None, reporter=Recorder(), now=NOW)
    assert window.params() == {"timespan": sent}


def test_month_shorthand_is_not_sent_as_gdelt_months() -> None:
    assert resolve_window("2m", None, reporter=Recorder(), now=NOW).params() == {"timespan": "60d"}


def test_relative_range_beyond_three_months_is_clamped_loudly() -> None:
    reporter = Recorder()
    window = resolve_window("1y", None, reporter=reporter, now=NOW)
    assert window.params() == {"timespan": "90d"}
    assert "3 months" in reporter.warnings[0]


def test_absolute_range_uses_datetimes() -> None:
    window = resolve_window("2026-09-01", "2026-09-02", reporter=Recorder(), now=NOW)
    assert window.params() == {
        "startdatetime": "20260901000000",
        "enddatetime": "20260902000000",
    }


def test_absolute_start_before_coverage_is_clamped() -> None:
    reporter = Recorder()
    window = resolve_window("2026-01-01", "2026-09-01", reporter=reporter, now=NOW)
    assert window.start > NOW - timedelta(days=90)
    assert reporter.warnings


def test_range_entirely_outside_coverage_is_an_input_error() -> None:
    with pytest.raises(InputError, match="3-month window"):
        resolve_window("2025-01-01", "2025-02-01", reporter=Recorder(), now=NOW)


# ---- query composition ------------------------------------------------------


def test_query_passes_through_unchanged() -> None:
    query = 'near10:"OpenAI Microsoft" (regulation OR lawsuit)'
    assert compose_query(query) == query


def test_single_filters_are_appended_as_operators() -> None:
    assert (
        compose_query("OpenAI", domains=("reuters.com",), languages=("English",))
        == "OpenAI domain:reuters.com sourcelang:english"
    )


def test_repeated_filters_become_an_or_group() -> None:
    assert (
        compose_query("OpenAI", domains=("reuters.com", "bbc.co.uk"))
        == "OpenAI (domain:reuters.com OR domain:bbc.co.uk)"
    )


def test_multi_word_country_loses_its_spaces() -> None:
    assert compose_query("x", countries=("United Kingdom",)) == "x sourcecountry:unitedkingdom"


@pytest.mark.parametrize("value", ["", "reuters.com OR x", 'a"b', "a)b"])
def test_filter_values_cannot_inject_query_syntax(value: str) -> None:
    with pytest.raises(InputError):
        compose_query("OpenAI", domains=(value,))


def test_empty_query_rejected() -> None:
    with pytest.raises(InputError, match="empty query"):
        compose_query("   ")


def test_params_carry_mode_and_sort() -> None:
    params = build_params("q", {"timespan": "1d"}, max_records=10, sort=Sort.DATEDESC)
    assert params == {
        "query": "q",
        "mode": "artlist",
        "format": "json",
        "maxrecords": 10,
        "timespan": "1d",
        "sort": "DateDesc",
    }


def test_relevance_sort_is_not_sent() -> None:
    assert "sort" not in build_params("q", {}, max_records=10, sort=Sort.RELEVANCE)


# ---- single request --------------------------------------------------------


@respx.mock
def test_single_request_dedupes_and_respects_max(http: HttpClient, fixtures_dir) -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=payload(fixtures_dir)))
    meta, records = search(http, "OpenAI", reporter=Recorder(), max_records=2, now=NOW)
    urls = [record.url for record in records]
    assert urls == [
        "https://www.reuters.com/technology/openai-regulation-2026-09-11/",
        "https://www.bbc.co.uk/news/articles/openai-lawsuit",
    ]
    assert route.call_count == 1
    assert route.calls.last.request.url.params["maxrecords"] == "2"
    assert meta.endpoint == "doc"


@respx.mock
def test_gdelt_plain_text_error_raises_before_any_record(http: HttpClient, fixtures_dir) -> None:
    body = (fixtures_dir / "doc_error_maxrecords.txt").read_bytes()
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, content=body))
    with pytest.raises(APIError, match="maximum of 250"):
        search(http, "OpenAI", reporter=Recorder(), now=NOW)


# ---- paging ----------------------------------------------------------------


@respx.mock
def test_paging_walks_backwards_and_stops_when_exhausted(http: HttpClient) -> None:
    step = timedelta(minutes=1)
    first = page(NOW, PAGE_SIZE, prefix="a", step=step)
    oldest = NOW - step * (PAGE_SIZE - 1)
    # The second page overlaps the boundary article, which must not repeat.
    second = page(oldest, 40, prefix="b", step=step)
    second["articles"][0]["url"] = first["articles"][-1]["url"]
    route = respx.get(ENDPOINT).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )

    reporter = Recorder()
    _, records = search(
        http, "OpenAI", reporter=reporter, since="7d", max_records=1000, sort=Sort.DATEDESC, now=NOW
    )
    urls = [record.url for record in records]

    assert route.call_count == 2
    assert len(urls) == PAGE_SIZE + 39
    assert len(set(urls)) == len(urls)
    second_request = route.calls[1].request.url.params
    assert second_request["enddatetime"] == (oldest + timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")
    assert second_request["maxrecords"] == str(PAGE_SIZE)
    assert any("up to 4 DOC API requests" in w for w in reporter.warnings)


@respx.mock
def test_paging_stops_requesting_once_max_is_reached(http: HttpClient) -> None:
    step = timedelta(minutes=1)
    route = respx.get(ENDPOINT).mock(
        side_effect=[
            httpx.Response(200, json=page(NOW, PAGE_SIZE, prefix="a", step=step)),
            httpx.Response(200, json=page(NOW - step * 300, PAGE_SIZE, prefix="b", step=step)),
            httpx.Response(200, json=page(NOW - step * 600, PAGE_SIZE, prefix="c", step=step)),
        ]
    )
    _, records = search(
        http,
        "OpenAI",
        reporter=Recorder(),
        since="30d",
        max_records=300,
        sort=Sort.DATEDESC,
        now=NOW,
    )
    assert len(list(records)) == 300
    assert route.call_count == 2


@respx.mock
def test_paging_oldest_first_moves_the_start_forward(http: HttpClient) -> None:
    step = timedelta(minutes=1)
    start = NOW - timedelta(days=7)
    first = {
        "articles": [
            {"url": f"https://e.com/{i}", "seendate": (start + step * i).strftime(STAMP)}
            for i in range(PAGE_SIZE)
        ]
    }
    route = respx.get(ENDPOINT).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json={})]
    )
    _, records = search(
        http, "OpenAI", reporter=Recorder(), since="7d", max_records=500, sort=Sort.DATEASC, now=NOW
    )
    list(records)
    newest = start + step * (PAGE_SIZE - 1)
    params = route.calls[1].request.url.params
    assert params["startdatetime"] == (newest - timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")
    assert params["sort"] == "DateAsc"


@respx.mock
def test_paging_that_cannot_advance_stops_with_a_warning(http: HttpClient) -> None:
    same = page(NOW, PAGE_SIZE, prefix="a", step=timedelta(0))
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=same))
    reporter = Recorder()
    _, records = search(
        http, "OpenAI", reporter=reporter, max_records=1000, sort=Sort.DATEDESC, now=NOW
    )
    assert len(list(records)) == PAGE_SIZE
    assert route.call_count == 2
    assert any("stopped paging" in w for w in reporter.warnings)


def test_relevance_with_large_max_switches_to_newest_first() -> None:
    reporter = Recorder()
    with respx.mock:
        route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={}))
        http = HttpClient(retries=0, reporter=reporter)
        list(search(http, "OpenAI", reporter=reporter, max_records=500, now=NOW)[1])
    assert route.calls.last.request.url.params["sort"] == "DateDesc"
    assert any("newest first" in w for w in reporter.warnings)


@pytest.mark.parametrize("sort", [Sort.TONEDESC, Sort.TONEASC])
def test_tone_sort_cannot_page(http: HttpClient, sort: Sort) -> None:
    with pytest.raises(InputError, match="cannot be combined"):
        search(http, "OpenAI", reporter=Recorder(), max_records=500, sort=sort, now=NOW)


# ---- CLI -------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for name in ("FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(name, raising=False)


@respx.mock
def test_cli_json_is_valid_and_reproducible(isolated, fixtures_dir) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=payload(fixtures_dir)))
    result = runner.invoke(app, ["search", "OpenAI", "--since", "7d", "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["query"] == "OpenAI"
    assert document["endpoint"] == "doc"
    assert document["parameters"]["timespan"] == "7d"
    assert document["parameters"]["mode"] == "artlist"
    assert len(document["results"]) == 3
    assert document["results"][0]["published_at"] == "2026-09-11T12:15:00Z"


@respx.mock
def test_cli_jsonl_and_csv(isolated, fixtures_dir) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=payload(fixtures_dir)))

    jsonl = runner.invoke(app, ["search", "OpenAI", "--jsonl", "--raw"])
    assert jsonl.exit_code == 0
    lines = [json.loads(line) for line in jsonl.stdout.splitlines()]
    assert len(lines) == 3
    assert lines[0]["raw"]["sourcecountry"] == "United States"

    as_csv = runner.invoke(app, ["search", "OpenAI", "--csv"])
    assert as_csv.exit_code == 0
    rows = list(csv.DictReader(io.StringIO(as_csv.stdout)))
    assert rows[0]["url"].startswith("https://www.reuters.com/")
    assert rows[1]["source_country"] == "United Kingdom"


@respx.mock
def test_cli_default_table(isolated, fixtures_dir) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=payload(fixtures_dir)))
    result = runner.invoke(app, ["search", "OpenAI"])
    assert result.exit_code == 0
    header = result.stdout.splitlines()[0]
    assert header.split() == ["DATE", "SOURCE", "TITLE"]
    assert "2026-09-11 12:15" in result.stdout
    assert "reuters.com" in result.stdout


@respx.mock
def test_cli_filters_reach_the_request(isolated) -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={}))
    result = runner.invoke(
        app,
        ["search", "OpenAI", "--domain", "reuters.com", "--country", "united kingdom", "--json"],
    )
    assert result.exit_code == 0, result.output
    sent = route.calls.last.request.url.params["query"]
    assert sent == "OpenAI domain:reuters.com sourcecountry:unitedkingdom"
    assert json.loads(result.stdout)["query"] == "OpenAI"


@respx.mock
def test_cli_rate_limit_is_an_error_not_an_empty_result(isolated, fixtures_dir) -> None:
    body = (fixtures_dir / "doc_rate_limited.txt").read_bytes()
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(429, content=body))
    result = runner.invoke(app, ["search", "OpenAI", "--json"])
    assert isinstance(result.exception, RateLimitError)
    assert "one every 5 seconds" in (result.exception.hint or "")
    assert route.call_count == 4
    assert result.stdout == ""


@respx.mock
def test_cli_api_failure_leaves_stdout_empty(isolated) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(500))
    result = runner.invoke(app, ["search", "OpenAI", "--json"])
    assert isinstance(result.exception, APIError)
    assert result.stdout == ""


def test_cli_invalid_range(isolated) -> None:
    result = runner.invoke(app, ["search", "OpenAI", "--since", "banana"])
    assert isinstance(result.exception, InputError)
    assert result.stdout == ""
