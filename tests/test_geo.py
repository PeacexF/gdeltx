"""Location aggregation from GKG and the `geo` command."""

import csv
import io
import json
from datetime import UTC, datetime

import pytest
import respx
from typer.testing import CliRunner

from builders import gkg_line, mock_published
from gdeltx.analysis.aggregate import LocationTally
from gdeltx.cli import app
from gdeltx.commands.geo import tally
from gdeltx.errors import InputError
from gdeltx.parsers.gkg import parse_gkg, parse_locations
from gdeltx.sources.files import Dataset

MOSCOW = "4#Moscow, Moskva, Russia#RS#RS48##55.7522#37.6156#-2960561#{}"
RUSSIA = "1#Russia#RS#RS##60#100#RS#{}"
RUSSIAN = "1#Russian#RS#RS##60#100#RS#{}"
LONDON = "4#London, London, City of, United Kingdom#UK#UKH9##51.5#-0.1167#-2601889#{}"
# Same name, different country: must stay a separate place.
LONDON_ON = "4#London, Ontario, Canada#CA#CA08##42.9833#-81.25#-570760#{}"
UNGEOCODED = "4#Atlantis#XX######{}"


def locations(*entries: str) -> str:
    return ";".join(entry.format(i * 10) for i, entry in enumerate(entries))


def article(url: str, places: str, date: str = "20260912100000") -> str:
    return gkg_line(
        GKGRECORDID=url,
        V21DATE=date,
        V2SOURCECOMMONNAME=url.split("/")[2],
        V2DOCUMENTIDENTIFIER=url,
        V2ENHANCEDORGANIZATIONS="Company X,1",
        V21ALLNAMES="Company X,1",
        V2ENHANCEDLOCATIONS=places,
    )


ROWS = [
    article("https://a.com/1", locations(MOSCOW, MOSCOW, RUSSIA, LONDON)),
    article("https://b.com/2", locations(MOSCOW, RUSSIAN), date="20260912110000"),
    article("https://b.com/3", locations(LONDON_ON, UNGEOCODED)),
]


def ranked(rows: list[str] = ROWS, **kwargs):
    return tally(map(parse_gkg, rows)).ranked(query="Company X", **kwargs)


# ---- aggregation --------------------------------------------------------------


def test_counts_distinct_articles_with_sources_and_dates() -> None:
    moscow = next(loc for loc in ranked() if loc.name.startswith("Moscow"))
    assert (moscow.count, moscow.sources) == (2, 2)
    assert (moscow.latitude, moscow.longitude) == (55.7522, 37.6156)
    assert moscow.first_seen == datetime(2026, 9, 12, 10, tzinfo=UTC)
    assert moscow.last_seen == datetime(2026, 9, 12, 11, tzinfo=UTC)
    assert (moscow.level, moscow.location_type, moscow.country_code) == ("city", 4, "RS")
    assert (moscow.adm1, moscow.feature_id) == ("RS48", "-2960561")


def test_places_group_by_gdelt_feature_not_spelling() -> None:
    countries = ranked(levels={"country"})
    assert [(c.name, c.count) for c in countries] == [("Russia", 2)]


def test_same_name_in_different_countries_stays_apart() -> None:
    londons = [loc for loc in ranked() if loc.name.startswith("London")]
    assert {loc.country_code for loc in londons} == {"UK", "CA"}


def test_missing_coordinates_are_none_not_zero() -> None:
    atlantis = next(loc for loc in ranked() if loc.name == "Atlantis")
    assert (atlantis.latitude, atlantis.longitude, atlantis.feature_id) == (None, None, None)


def test_most_common_coordinates_win_with_stable_ties() -> None:
    counts = LocationTally()
    for entry in ("4#X#US#USNY##1#1#F1#0", "4#X#US#USNY##2#2#F1#0", "4#X#US#USNY##2#2#F1#0"):
        counts.add_article(parse_locations(entry), domain=None, seen_at=None)
    (place,) = counts.ranked(query="q")
    assert (place.latitude, place.count) == (2.0, 3)


def test_ranking_is_deterministic_and_respects_top() -> None:
    assert ranked() == ranked(ROWS[::-1])
    assert [loc.name for loc in ranked(top=2)] == ["Moscow, Moskva, Russia", "Russia"]


def test_levels_filter() -> None:
    assert {loc.level for loc in ranked(levels={"city", "state"})} == {"city"}


def test_captured_locations_are_geocoded(fixtures_dir) -> None:
    lines = (fixtures_dir / "gkg.sample.tsv").read_text("utf-8").splitlines()
    places = tally(map(parse_gkg, lines)).ranked(query="q")
    boston = next(p for p in places if p.name.startswith("Boston"))
    assert (boston.latitude, boston.longitude, boston.level) == (42.3584, -71.0598, "city")
    australia = [p for p in places if p.feature_id == "AS"]
    assert len(australia) == 1


# ---- CLI ------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("gdeltx.sources.http.time.sleep", lambda _: None)
    for name in ("FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(name, raising=False)


def invoke(*args: str):
    return runner.invoke(app, ["geo", "Company X", "--since", "1h", *args])


@respx.mock
def test_cli_table() -> None:
    mock_published(Dataset.GKG, {0: ROWS})
    result = invoke()
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["LOCATION", "ARTICLES", "LATITUDE", "LONGITUDE"]
    assert lines[1].startswith("Moscow, Moskva, Russia")
    assert lines[1].split()[-3:] == ["2", "55.7522", "37.6156"]
    assert "from 3 matching GKG records in 4 files" in result.stdout


@respx.mock
def test_cli_json_carries_coordinates() -> None:
    mock_published(Dataset.GKG, {0: ROWS})
    document = json.loads(invoke("--json").stdout)
    assert document["parameters"]["field"] == "V2ENHANCEDLOCATIONS"
    first = document["results"][0]
    assert {k: first[k] for k in ("name", "latitude", "longitude", "count")} == {
        "name": "Moscow, Moskva, Russia",
        "latitude": 55.7522,
        "longitude": 37.6156,
        "count": 2,
    }


@respx.mock
def test_cli_csv_and_jsonl() -> None:
    mock_published(Dataset.GKG, {0: ROWS})
    rows = list(csv.DictReader(io.StringIO(invoke("--csv", "--level", "country").stdout)))
    assert [(r["name"], r["latitude"], r["count"]) for r in rows] == [("Russia", "60.0", "2")]
    lines = invoke("--jsonl").stdout.splitlines()
    assert len(lines) == 5 and all(json.loads(line)["query"] == "Company X" for line in lines)


@respx.mock
def test_cli_geojson_is_longitude_first_and_skips_ungeocoded() -> None:
    mock_published(Dataset.GKG, {0: ROWS})
    result = invoke("--geojson")
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["type"] == "FeatureCollection"
    assert document["gdeltx"]["query"] == "Company X"
    moscow = document["features"][0]
    assert moscow["geometry"] == {"type": "Point", "coordinates": [37.6156, 55.7522]}
    assert moscow["properties"]["count"] == 2
    assert "latitude" not in moscow["properties"]
    assert "Atlantis" not in result.stdout
    assert "1 locations had no coordinates" in result.stderr


def test_cli_geojson_conflicts_with_other_formats() -> None:
    result = invoke("--geojson", "--csv")
    assert result.exit_code != 0
    assert result.stdout == ""


def test_cli_rejects_query_syntax_before_any_request() -> None:
    with respx.mock(assert_all_called=False) as mock:
        result = runner.invoke(app, ["geo", "a OR b"])
        assert mock.calls.call_count == 0
    assert isinstance(result.exception, InputError)


@respx.mock
def test_cli_refuses_a_month_of_gkg() -> None:
    mock_published(Dataset.GKG, {})
    result = runner.invoke(app, ["geo", "Company X", "--since", "30d"])
    assert isinstance(result.exception, InputError)
    assert "--allow-large" in (result.exception.hint or "")
