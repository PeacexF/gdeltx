"""Events normalization and the `events` command.

Rows are built from the 61-column layout; see API-NOTES §7 on pinning it to
captured data.
"""

import csv
import io
import json
from datetime import UTC, date, datetime

import pytest
import respx
from typer.testing import CliRunner

from builders import event_line, mock_published
from gdeltx.cli import app
from gdeltx.errors import InputError
from gdeltx.parsers.events import parse_event, to_event
from gdeltx.sources.files import Dataset


def row(**values: str) -> dict:
    parsed = parse_event(event_line(**values))
    assert parsed is not None
    return parsed


SANCTION = {
    "GLOBALEVENTID": "1100",
    "SQLDATE": "20260908",
    "DATEADDED": "20260913011500",
    "Actor1Code": "USAGOV",
    "Actor1Name": "GOVERNMENT Y",
    "Actor1CountryCode": "USA",
    "Actor2Code": "BUS",
    "Actor2Name": "COMPANY X",
    "IsRootEvent": "1",
    "EventCode": "163",
    "EventBaseCode": "163",
    "EventRootCode": "16",
    "QuadClass": "4",
    "GoldsteinScale": "-8.0",
    "NumMentions": "12",
    "NumSources": "3",
    "NumArticles": "10",
    "AvgTone": "-4.25",
    "ActionGeo_FullName": "Washington, District of Columbia, United States",
    "ActionGeo_CountryCode": "US",
    "ActionGeo_Lat": "38.8951",
    "ActionGeo_Long": "-77.0364",
    "SOURCEURL": "https://www.reuters.com/world/company-x-sanctions",
}
MEETING = {
    "GLOBALEVENTID": "1200",
    "SQLDATE": "20260910",
    "Actor1Name": "COMPANY X",
    "Actor2Name": "GOVERNMENT Y",
    "EventCode": "036",
    "EventRootCode": "03",
    "ActionGeo_FullName": "London, London, City of, United Kingdom",
    "SOURCEURL": "https://bbc.co.uk/news/1",
}


# ---- normalization ----------------------------------------------------------


def test_exact_fields() -> None:
    event = to_event(row(**SANCTION), "Company X")
    assert event is not None
    assert event.global_event_id == "1100"
    assert event.date == date(2026, 9, 8)
    assert event.date_added == datetime(2026, 9, 13, 1, 15, tzinfo=UTC)
    assert (event.actor_1, event.actor_1_country, event.actor_2) == (
        "GOVERNMENT Y",
        "USA",
        "COMPANY X",
    )
    assert event.event_code == "163"
    assert event.action == "Impose embargo, boycott, or sanctions"
    assert event.event_root_code == "16"
    assert event.root_action is not None
    assert event.is_root_event is True
    assert (event.quad_class, event.goldstein_scale, event.avg_tone) == (4, -8.0, -4.25)
    assert (event.num_mentions, event.num_sources, event.num_articles) == (12, 3, 10)
    assert (event.latitude, event.longitude) == (38.8951, -77.0364)
    assert event.location_country_code == "US"
    assert event.source_domain == "reuters.com"
    assert event.raw is not None and event.raw["Actor1Code"] == "USAGOV"


def test_unknown_code_keeps_the_raw_code_and_no_label() -> None:
    event = to_event(row(GLOBALEVENTID="1", EventCode="9999"), "q")
    assert event is not None
    assert event.event_code == "9999"
    assert event.action is None


def test_damaged_values_become_none() -> None:
    event = to_event(
        row(
            GLOBALEVENTID="1",
            SQLDATE="2026",
            GoldsteinScale="n/a",
            NumMentions="",
            ActionGeo_Lat="x",
        ),
        "q",
    )
    assert event is not None
    assert (event.date, event.goldstein_scale, event.num_mentions, event.latitude) == (
        None,
        None,
        None,
        None,
    )
    assert event.is_root_event is None


def test_row_without_an_id_is_dropped() -> None:
    assert to_event(row(Actor1Name="X"), "q") is None


# ---- CLI ----------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("gdeltx.sources.http.time.sleep", lambda _: None)
    for name in ("FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(name, raising=False)


def invoke(*args: str):
    return runner.invoke(app, ["events", "Company X", "--since", "1h", *args])


OTHER = {"GLOBALEVENTID": "1300", "Actor1Name": "SOMEONE", "SOURCEURL": "https://company-x.com/"}


@respx.mock
def test_cli_table() -> None:
    mock_published(Dataset.EVENTS, {0: [event_line(**SANCTION), event_line(**OTHER)]})
    result = invoke()
    assert result.exit_code == 0, result.output
    header = result.stdout.splitlines()[0].split()
    assert header == ["DATE", "ACTOR", "1", "ACTION", "ACTOR", "2", "LOCATION"]
    assert "Impose embargo" in result.stdout
    assert "SOMEONE" not in result.stdout


@respx.mock
def test_cli_newest_files_come_first_and_duplicates_collapse() -> None:
    mock_published(
        Dataset.EVENTS,
        {
            0: [event_line(**MEETING)],
            2: [event_line(**SANCTION), event_line(**MEETING)],
        },
    )
    result = invoke("--jsonl")
    assert result.exit_code == 0, result.output
    ids = [json.loads(line)["global_event_id"] for line in result.stdout.splitlines()]
    assert ids == ["1200", "1100"]


@respx.mock
def test_cli_max_stops_before_reading_past_the_read_ahead_window() -> None:
    # 6h is 24 files; the fetcher reads ahead twice the worker count (8) at most.
    routes = mock_published(
        Dataset.EVENTS,
        {0: [event_line(**MEETING)], 20: [event_line(**SANCTION)]},
    )
    result = runner.invoke(app, ["events", "Company X", "--since", "6h", "--max", "1", "--json"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)["results"]) == 1
    assert routes[0].call_count == 1
    assert routes[20].call_count == 0


@respx.mock
def test_cli_json_carries_codes_labels_and_parameters() -> None:
    mock_published(Dataset.EVENTS, {0: [event_line(**SANCTION)]})
    result = invoke("--json")
    document = json.loads(result.stdout)
    assert document["endpoint"] == "events"
    assert document["parameters"]["files"] == 4
    record = document["results"][0]
    assert record["event_code"] == "163"
    assert record["action"] == "Impose embargo, boycott, or sanctions"
    assert record["date"] == "2026-09-08"
    assert "raw" not in record or record["raw"] is None


@respx.mock
def test_cli_csv_and_raw() -> None:
    mock_published(Dataset.EVENTS, {0: [event_line(**SANCTION)]})
    rows = list(csv.DictReader(io.StringIO(invoke("--csv").stdout)))
    assert rows[0]["event_code"] == "163"
    assert rows[0]["source_domain"] == "reuters.com"

    raw = json.loads(invoke("--jsonl", "--raw").stdout.splitlines()[0])["raw"]
    assert len(raw) == 61


@respx.mock
def test_cli_no_events() -> None:
    mock_published(Dataset.EVENTS, {0: [event_line(**OTHER)]})
    result = invoke()
    assert result.exit_code == 0
    assert "No events found." in result.stdout


def test_cli_rejects_query_syntax_before_any_request() -> None:
    with respx.mock(assert_all_called=False) as mock:
        result = runner.invoke(app, ["events", "COMPANY X OR GOVERNMENT Y"])
        assert mock.calls.call_count == 0
    assert isinstance(result.exception, InputError)
