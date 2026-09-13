"""Bulk file layer: slots, lastupdate, range guard, fetching, caching, parsers.

Row layouts are exercised with rows built from the column tuples, which only
proves internal consistency. Pinning the layouts to GDELT needs a captured
file (API-NOTES §7).
"""

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from gdeltx.cache import CacheStore
from gdeltx.cache.store import cache_key
from gdeltx.cli import app
from gdeltx.console import Reporter
from gdeltx.errors import APIError, InputError, ParseError
from gdeltx.parsers.cameo import actor_type_label, country_label, event_label
from gdeltx.parsers.events import EVENT_COLUMNS, parse_event
from gdeltx.parsers.gkg import (
    GKG_COLUMNS,
    Mention,
    parse_gkg,
    parse_locations,
    parse_mentions,
    parse_tone,
)
from gdeltx.sources.files import (
    Dataset,
    FileFetcher,
    contains_any,
    guard,
    latest_stamp,
    plan,
    read_events,
    slot_url,
    slots,
)
from gdeltx.sources.files.index import LASTUPDATE_URL, floor_slot, parse_lastupdate
from gdeltx.sources.http import HttpClient

T0 = datetime(2026, 9, 13, 1, 0, tzinfo=UTC)


class Recorder(Reporter):
    def __init__(self) -> None:
        super().__init__(quiet=True)
        self.warnings: list[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def zipped(name: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


def event_line(**values: str) -> str:
    fields = dict.fromkeys(EVENT_COLUMNS, "")
    fields.update(values)
    return "\t".join(fields[name] for name in EVENT_COLUMNS) + "\n"


def events_zip(stamp: datetime, *lines: str) -> bytes:
    return zipped(f"{stamp:%Y%m%d%H%M%S}.export.CSV", "".join(lines))


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gdeltx.sources.http.time.sleep", lambda _: None)


@pytest.fixture
def reporter() -> Recorder:
    return Recorder()


def fetcher(tmp_path: Path | None, reporter: Reporter, *, workers: int = 2) -> FileFetcher:
    cache = CacheStore(tmp_path, ttl=None) if tmp_path is not None else None
    http = HttpClient(retries=1, reporter=reporter)
    return FileFetcher(http, cache=cache, reporter=reporter, workers=workers)


# ---- slots and URLs ---------------------------------------------------------


def test_slot_url_uses_https_and_dataset_suffix() -> None:
    assert slot_url(Dataset.EVENTS, T0) == (
        "https://data.gdeltproject.org/gdeltv2/20260913010000.export.CSV.zip"
    )
    assert slot_url(Dataset.GKG, T0).endswith("20260913010000.gkg.csv.zip")
    assert slot_url(Dataset.MENTIONS, T0).endswith("20260913010000.mentions.CSV.zip")


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 9, 13, 1, 14, 59, tzinfo=UTC), datetime(2026, 9, 13, 1, 0, tzinfo=UTC)),
        (datetime(2026, 9, 13, 1, 15, tzinfo=UTC), datetime(2026, 9, 13, 1, 15, tzinfo=UTC)),
        (datetime(2026, 9, 13, 1, 59, tzinfo=UTC), datetime(2026, 9, 13, 1, 45, tzinfo=UTC)),
    ],
)
def test_floor_slot(moment: datetime, expected: datetime) -> None:
    assert floor_slot(moment) == expected


def test_one_hour_is_four_files_ending_at_the_boundary() -> None:
    stamps = slots(T0, T0 + timedelta(hours=1))
    assert [f"{s:%H%M}" for s in stamps] == ["0115", "0130", "0145", "0200"]


def test_off_grid_range() -> None:
    stamps = slots(T0 + timedelta(minutes=7), T0 + timedelta(minutes=38))
    assert [f"{s:%H%M}" for s in stamps] == ["0115", "0130"]


def test_slots_cross_the_year_boundary() -> None:
    stamps = slots(
        datetime(2026, 12, 31, 23, 40, tzinfo=UTC), datetime(2027, 1, 1, 0, 15, tzinfo=UTC)
    )
    assert [s.strftime("%Y%m%d%H%M") for s in stamps] == [
        "202612312345",
        "202701010000",
        "202701010015",
    ]


def test_non_utc_input_is_normalised() -> None:
    plus_three = T0.astimezone(timezone(timedelta(hours=3)))
    assert plus_three.hour == 4
    assert slots(plus_three, plus_three + timedelta(minutes=15)) == [T0 + timedelta(minutes=15)]


def test_empty_range_has_no_slots() -> None:
    assert slots(T0, T0 + timedelta(minutes=10)) == []


# ---- lastupdate.txt ---------------------------------------------------------


def test_parses_captured_lastupdate(fixtures_dir) -> None:
    published = parse_lastupdate((fixtures_dir / "lastupdate.txt").read_text())
    assert {item.dataset for item in published} == set(Dataset)
    gkg = next(item for item in published if item.dataset is Dataset.GKG)
    assert gkg.stamp == datetime(2026, 9, 13, 1, 30, tzinfo=UTC)
    assert gkg.size == 1431371
    assert gkg.md5 == "5f01c75a8e19d884ac891eb48f261cba"


@pytest.mark.parametrize("text", ["", "garbage", "12 abc http://x/notastamp.export.CSV.zip"])
def test_malformed_lastupdate(text: str) -> None:
    with pytest.raises(ParseError):
        parse_lastupdate(text)


@respx.mock
def test_latest_stamp_bypasses_the_cache(tmp_path: Path, fixtures_dir) -> None:
    body = (fixtures_dir / "lastupdate.txt").read_bytes()
    route = respx.get(LASTUPDATE_URL).mock(return_value=httpx.Response(200, content=body))
    http = HttpClient(cache=CacheStore(tmp_path), reporter=Reporter(quiet=True))
    latest_stamp(http)
    latest_stamp(http)
    assert route.call_count == 2


def test_plan_stops_at_the_newest_published_file() -> None:
    file_plan = plan(Dataset.GKG, T0, T0 + timedelta(hours=5), latest=T0 + timedelta(minutes=30))
    assert file_plan.count == 2


# ---- range guard ------------------------------------------------------------


def test_guard_is_silent_for_small_ranges(reporter: Recorder) -> None:
    file_plan = plan(Dataset.EVENTS, T0, T0 + timedelta(hours=2), latest=T0 + timedelta(days=1))
    guard(file_plan, warn_at=192, refuse_at=1000, allow_large=False, reporter=reporter)
    assert reporter.warnings == []


def test_guard_warns_with_an_estimate(reporter: Recorder) -> None:
    file_plan = plan(Dataset.GKG, T0, T0 + timedelta(days=3), latest=T0 + timedelta(days=9))
    guard(file_plan, warn_at=192, refuse_at=1000, allow_large=False, reporter=reporter)
    assert "288 gkg files" in reporter.warnings[0]
    assert "MB" in reporter.warnings[0]


def test_guard_refuses_a_year_of_gkg(reporter: Recorder) -> None:
    file_plan = plan(Dataset.GKG, T0, T0 + timedelta(days=365), latest=T0 + timedelta(days=400))
    with pytest.raises(InputError) as excinfo:
        guard(file_plan, warn_at=192, refuse_at=1000, allow_large=False, reporter=reporter)
    assert "35040 gkg files" in str(excinfo.value)
    assert "--allow-large" in (excinfo.value.hint or "")


def test_guard_override(reporter: Recorder) -> None:
    file_plan = plan(Dataset.GKG, T0, T0 + timedelta(days=30), latest=T0 + timedelta(days=40))
    guard(file_plan, warn_at=192, refuse_at=1000, allow_large=True, reporter=reporter)
    assert reporter.warnings


# ---- fetching ---------------------------------------------------------------


def hour_plan(dataset: Dataset = Dataset.EVENTS):
    return plan(dataset, T0, T0 + timedelta(hours=1), latest=T0 + timedelta(days=1))


def mock_hour(bodies: dict[int, httpx.Response] | None = None) -> list[respx.Route]:
    routes = []
    for i, stamp in enumerate(hour_plan().stamps):
        response = (bodies or {}).get(i) or httpx.Response(
            200, content=events_zip(stamp, event_line(GLOBALEVENTID=str(i), SOURCEURL=f"u{i}"))
        )
        routes.append(respx.get(slot_url(Dataset.EVENTS, stamp)).mock(return_value=response))
    return routes


@respx.mock
def test_files_arrive_in_time_order(tmp_path: Path, reporter: Recorder) -> None:
    mock_hour()
    contents = list(fetcher(tmp_path, reporter, workers=4).iter_files(hour_plan()))
    assert [c.stamp for c in contents] == hour_plan().stamps
    assert [len(list(c.lines)) for c in contents] == [1, 1, 1, 1]


@respx.mock
def test_cache_reuse_skips_the_network(tmp_path: Path, reporter: Recorder) -> None:
    routes = mock_hour()
    for content in fetcher(tmp_path, reporter).iter_files(hour_plan()):
        list(content.lines)
    again = list(fetcher(tmp_path, reporter).iter_files(hour_plan()))
    assert all(route.call_count == 1 for route in routes)
    assert all(content.cached for content in again)


@respx.mock
def test_missing_files_are_gaps_not_failures(tmp_path: Path, reporter: Recorder) -> None:
    mock_hour({1: httpx.Response(404), 2: httpx.Response(404)})
    contents = list(fetcher(tmp_path, reporter).iter_files(hour_plan()))
    assert len(contents) == 2
    assert "2 of 4 events files were not published" in reporter.warnings[-1]


@respx.mock
def test_corrupt_archive_is_skipped_and_evicted(tmp_path: Path, reporter: Recorder) -> None:
    mock_hour({0: httpx.Response(200, content=b"PK\x03\x04 not really a zip")})
    contents = list(fetcher(tmp_path, reporter).iter_files(hour_plan()))
    assert len(contents) == 3
    assert any("not a readable zip archive" in w for w in reporter.warnings)
    corrupt_url = slot_url(Dataset.EVENTS, hour_plan().stamps[0])
    assert CacheStore(tmp_path, ttl=None).get(cache_key(corrupt_url)) is None


@respx.mock
def test_truncated_archive_is_skipped(tmp_path: Path, reporter: Recorder) -> None:
    whole = events_zip(T0, event_line(GLOBALEVENTID="1"))
    mock_hour({3: httpx.Response(200, content=whole[: len(whole) // 2])})
    assert len(list(fetcher(tmp_path, reporter).iter_files(hour_plan()))) == 3


@respx.mock
def test_one_failed_download_degrades_the_run(tmp_path: Path, reporter: Recorder) -> None:
    mock_hour({2: httpx.Response(500)})
    assert len(list(fetcher(tmp_path, reporter).iter_files(hour_plan()))) == 3
    assert any("skipped" in w and "HTTP 500" in w for w in reporter.warnings)


@respx.mock
def test_every_file_failing_is_an_error_not_an_empty_result(reporter: Recorder) -> None:
    mock_hour({i: httpx.Response(500) for i in range(4)})
    with pytest.raises(APIError):
        list(fetcher(None, reporter).iter_files(hour_plan()))


@respx.mock
def test_stopping_early_does_not_download_the_whole_range(reporter: Recorder) -> None:
    day = plan(Dataset.EVENTS, T0, T0 + timedelta(days=1), latest=T0 + timedelta(days=2))
    route = respx.get(url__startswith="https://data.gdeltproject.org/").mock(
        return_value=httpx.Response(200, content=events_zip(T0, event_line()))
    )
    files = fetcher(None, reporter, workers=2).iter_files(day)
    next(files)
    files.close()
    assert day.count == 96
    assert route.call_count <= 2 * 2 + 2


@respx.mock
def test_lines_are_decompressed_lazily(reporter: Recorder) -> None:
    lines = "".join(event_line(GLOBALEVENTID=str(i)) for i in range(1000))
    respx.get(url__startswith="https://data.gdeltproject.org/").mock(
        return_value=httpx.Response(200, content=events_zip(T0, lines))
    )
    content = next(fetcher(None, reporter).iter_files(hour_plan()))
    first = next(content.lines)
    assert first.startswith("0\t")


@respx.mock
def test_read_events_filters_before_parsing(reporter: Recorder) -> None:
    body = events_zip(
        T0,
        event_line(GLOBALEVENTID="1", Actor1Name="OPENAI"),
        event_line(GLOBALEVENTID="2", Actor1Name="SOMEONE ELSE"),
        "too\tfew\tcolumns\n",
        "OpenAI\tbut\tmalformed\n",
    )
    respx.get(url__startswith="https://data.gdeltproject.org/").mock(
        return_value=httpx.Response(200, content=body)
    )
    one_file = plan(Dataset.EVENTS, T0, T0 + timedelta(minutes=15), latest=T0 + timedelta(days=1))
    rows = list(
        read_events(
            fetcher(None, reporter), one_file, reporter=reporter, match=contains_any(["openai"])
        )
    )
    assert [row["GLOBALEVENTID"] for row in rows] == ["1"]
    assert "skipped 1 malformed events rows" in reporter.warnings


def test_contains_any_is_case_insensitive_and_empty_matches_all() -> None:
    assert contains_any(["OpenAI"])("... openai ...")
    assert not contains_any(["OpenAI"])("nothing here")
    assert contains_any([])("anything")


# ---- row layouts ------------------------------------------------------------


def test_layouts_have_documented_widths() -> None:
    assert len(EVENT_COLUMNS) == 61
    assert len(GKG_COLUMNS) == 27
    assert len(set(EVENT_COLUMNS)) == 61


def test_event_row_maps_names_and_blanks() -> None:
    row = parse_event(event_line(GLOBALEVENTID="1234", EventCode="036", SOURCEURL="https://x"))
    assert row is not None
    assert row["GLOBALEVENTID"] == "1234"
    assert row["EventCode"] == "036"
    assert row["SOURCEURL"] == "https://x"
    assert row["Actor2Name"] is None


def test_extra_trailing_columns_are_tolerated() -> None:
    row = parse_event(event_line(SOURCEURL="https://x").rstrip("\n") + "\tnew-column\n")
    assert row is not None and row["SOURCEURL"] == "https://x"


def test_short_rows_are_malformed() -> None:
    assert parse_event("1\t2\t3\n") is None
    assert parse_gkg("1\t2\t3\n") is None


def test_gkg_row_and_packed_fields() -> None:
    fields = dict.fromkeys(GKG_COLUMNS, "")
    fields.update(
        GKGRECORDID="20260913013000-0",
        V2DOCUMENTIDENTIFIER="https://example.com/a",
        V2ENHANCEDPERSONS="Dario Amodei,120;Sam Altman,340",
        V2ENHANCEDORGANIZATIONS="Openai,12",
        V2ENHANCEDTHEMES="TAX_FNCACT_CEO,98;ECON_STOCKMARKET,4",
        V2ENHANCEDLOCATIONS="1#France#FR#FR##46#2#FR#77",
        V15TONE="-1.2,2.0,3.2,5.2,21.0,0.5,640",
    )
    row = parse_gkg("\t".join(fields[c] for c in GKG_COLUMNS))
    assert row is not None
    assert [m.name for m in parse_mentions(row["V2ENHANCEDPERSONS"])] == [
        "Dario Amodei",
        "Sam Altman",
    ]
    assert parse_mentions(row["V2ENHANCEDORGANIZATIONS"])[0].offset == 12
    assert parse_locations(row["V2ENHANCEDLOCATIONS"])[0].latitude == 46.0
    tone = parse_tone(row["V15TONE"])
    assert tone is not None and tone.tone == -1.2 and tone.word_count == 640


def test_packed_fields_tolerate_damage() -> None:
    assert parse_mentions(None) == []
    assert parse_mentions("NoOffset;;Name,") == [Mention("NoOffset", None), Mention("Name", None)]
    assert parse_locations("1#too#short") == []
    assert parse_tone("") is None
    assert parse_tone("-1.5").negative is None


# ---- layouts against captured rows -------------------------------------------


def captured_rows(fixtures_dir, name: str) -> list[str]:
    path = fixtures_dir / name
    if not path.is_file():
        pytest.skip(f"{name} not captured yet; run scripts/capture-fixtures.sh")
    return path.read_text("utf-8", errors="replace").splitlines()


def test_captured_events_land_in_the_right_columns(fixtures_dir) -> None:
    for line in captured_rows(fixtures_dir, "events_export.sample.tsv"):
        row = parse_event(line)
        assert row is not None, "captured Events row is shorter than 61 columns"
        assert (row["GLOBALEVENTID"] or "").isdigit()
        assert len(row["SQLDATE"] or "") == 8
        assert len(row["DATEADDED"] or "") == 14
        assert (row["SOURCEURL"] or "").startswith("http")
        assert row["EventRootCode"] and (row["EventCode"] or "").startswith(row["EventRootCode"])


def test_captured_gkg_lands_in_the_right_columns(fixtures_dir) -> None:
    for line in captured_rows(fixtures_dir, "gkg.sample.tsv"):
        row = parse_gkg(line)
        assert row is not None, "captured GKG row is shorter than 27 columns"
        assert (row["GKGRECORDID"] or "").split("-")[0].isdigit()
        assert len(row["V21DATE"] or "") == 14
        assert row["V2DOCUMENTIDENTIFIER"]
        assert len((row["V15TONE"] or "").split(",")) == 7


# ---- CAMEO ------------------------------------------------------------------


def test_cameo_labels() -> None:
    assert event_label("036") == "Express intent to meet or negotiate"
    assert event_label("163") == "Impose embargo, boycott, or sanctions"
    assert event_label("173") == "Arrest, detain, or charge with legal action"
    assert country_label("GBR") == "United Kingdom"
    assert actor_type_label("GOV") == "Government"


@pytest.mark.parametrize("code", ["999", "", None])
def test_unknown_cameo_codes_are_none(code: str | None) -> None:
    assert event_label(code) is None


# ---- debug command ----------------------------------------------------------

runner = CliRunner()


@respx.mock
def test_files_debug_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    latest = floor_slot(datetime.now(UTC))
    respx.get(LASTUPDATE_URL).mock(
        return_value=httpx.Response(
            200,
            text=f"1 abc http://data.gdeltproject.org/gdeltv2/{latest:%Y%m%d%H%M%S}.export.CSV.zip\n",
        )
    )
    respx.get(url__startswith="https://data.gdeltproject.org/gdeltv2/2").mock(
        return_value=httpx.Response(
            200, content=events_zip(T0, event_line(Actor1Name="OPENAI"), event_line())
        )
    )
    result = runner.invoke(app, ["_files", "events", "--since", "1h", "--match", "openai"])
    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(records) == 4
    assert records[0] == {**records[0], "lines": 2, "matched": 1, "cached": False}
    assert (tmp_path / "cache" / "gdeltx" / "files").is_dir()


def test_files_debug_command_refuses_huge_ranges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    latest = floor_slot(datetime.now(UTC))
    with respx.mock:
        respx.get(LASTUPDATE_URL).mock(
            return_value=httpx.Response(
                200, text=f"1 abc http://x/{latest:%Y%m%d%H%M%S}.gkg.csv.zip\n"
            )
        )
        result = runner.invoke(app, ["_files", "gkg", "--since", "1y"])
    assert isinstance(result.exception, InputError)
    assert result.stdout == ""
