"""GKG entity extraction, aggregation and the `entities` command.

GKG rows are built from the column layout; see API-NOTES §7 on pinning the
layout to captured data.
"""

import csv
import io
import json
from datetime import UTC, datetime

import pytest
import respx
from typer.testing import CliRunner

from builders import gkg_line, mock_published
from gdeltx.analysis.aggregate import EntityTally
from gdeltx.cli import app
from gdeltx.commands.entities import write_sections
from gdeltx.errors import InputError
from gdeltx.models import Entity, EntityType
from gdeltx.parsers.gkg import MATCH_FIELDS, parse_gkg, published_at, row_entities
from gdeltx.sources.files import Dataset, plain_query, row_mentions

T1 = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
T2 = datetime(2026, 9, 12, 11, 0, tzinfo=UTC)


def article(
    url: str,
    domain: str,
    *,
    persons: str = "",
    orgs: str = "",
    locations: str = "",
    themes: str = "",
    date: str = "20260912100000",
    title: str = "",
) -> str:
    return gkg_line(
        GKGRECORDID=f"{date}-{abs(hash(url)) % 1000}",
        V21DATE=date,
        V2SOURCECOMMONNAME=domain,
        V2DOCUMENTIDENTIFIER=url,
        V2ENHANCEDPERSONS=persons,
        V2ENHANCEDORGANIZATIONS=orgs,
        V2ENHANCEDLOCATIONS=locations,
        V2ENHANCEDTHEMES=themes,
        V21ALLNAMES=";".join(filter(None, [persons, orgs])),
        V2EXTRASXML=f"<PAGE_TITLE>{title}</PAGE_TITLE>" if title else "",
    )


ROWS = [
    article(
        "https://reuters.com/1",
        "reuters.com",
        persons="John Smith,10;John Smith,90;Jane Doe,40",
        orgs="Company X,5;Ministry Z,70",
        locations=(
            "4#Moscow, Moskva, Russia#RS#RS48##55.75#37.62#-2960561#30;1#Russia#RS###60#100#RS#12"
        ),
        themes="SANCTIONS,3;ECON_TRADE,50",
    ),
    article(
        "https://bbc.com/2",
        "bbc.com",
        persons="john  smith,4",
        orgs="Company X,1",
        locations="4#London, London, City of, United Kingdom#UK#UK##51.5#-0.12#-2601889#8",
        themes="SANCTIONS,9",
        date="20260912110000",
    ),
    article(
        "https://example.ru/3",
        "example.ru",
        persons="Jane Doe,2",
        orgs="Company Y,3",
        title="Nothing about the query",
    ),
]


# ---- extraction ---------------------------------------------------------------


def test_row_entities_counts_each_entity_once_per_article() -> None:
    row = parse_gkg(ROWS[0])
    assert row is not None
    found = row_entities(row)
    assert found.count((EntityType.PERSON, "John Smith")) == 1
    assert (EntityType.LOCATION, "Moscow, Moskva, Russia") in found
    assert (EntityType.COUNTRY, "Russia") in found
    assert (EntityType.THEME, "SANCTIONS") in found


def test_v1_fields_are_the_fallback() -> None:
    row = parse_gkg(gkg_line(V1PERSONS="barack obama;angela merkel", V1ORGANIZATIONS="nato"))
    assert row is not None
    assert set(row_entities(row)) == {
        (EntityType.PERSON, "barack obama"),
        (EntityType.PERSON, "angela merkel"),
        (EntityType.ORGANIZATION, "nato"),
    }


def test_query_must_be_named_not_just_present_in_the_url() -> None:
    row = parse_gkg(article("https://company-x-news.com/a", "company-x-news.com", persons="A,1"))
    assert row is not None
    assert not row_mentions(row, MATCH_FIELDS, "company-x")
    assert row_mentions(parse_gkg(ROWS[0]), MATCH_FIELDS, "company x")


def test_page_title_counts_as_naming_the_query() -> None:
    row = parse_gkg(article("https://e.com/t", "e.com", title="Company X fined"))
    assert row is not None and row_mentions(row, MATCH_FIELDS, "company x")


@pytest.mark.parametrize("query", ["a OR b", "(Company X)", "domain:bbc.com", '"x" -y', ""])
def test_query_syntax_is_refused(query: str) -> None:
    with pytest.raises(InputError):
        plain_query(query)


# ---- aggregation --------------------------------------------------------------


def tally_rows(rows: list[str]) -> EntityTally:
    counts = EntityTally()
    for line in rows:
        row = parse_gkg(line)
        assert row is not None
        counts.add_article(
            row_entities(row), domain=row["V2SOURCECOMMONNAME"], seen_at=published_at(row)
        )
    return counts


def test_counts_are_articles_and_spellings_merge_on_case_and_spacing() -> None:
    people = tally_rows(ROWS).ranked(EntityType.PERSON, query="Company X")
    assert [(p.name, p.count, p.sources) for p in people] == [
        ("Jane Doe", 2, 2),
        ("John Smith", 2, 2),
    ]


def test_first_and_last_seen() -> None:
    smith = next(
        e for e in tally_rows(ROWS).ranked(EntityType.PERSON, query="q") if e.name == "John Smith"
    )
    assert smith.first_seen == T1
    assert smith.last_seen == T2


def test_ranking_is_deterministic_regardless_of_input_order() -> None:
    forward = tally_rows(ROWS).ranked(EntityType.ORGANIZATION, query="q")
    backward = tally_rows(ROWS[::-1]).ranked(EntityType.ORGANIZATION, query="q")
    assert forward == backward
    assert [o.name for o in forward] == ["Company X", "Company Y", "Ministry Z"]


def test_top_limits_each_category() -> None:
    assert len(tally_rows(ROWS).ranked(EntityType.ORGANIZATION, query="q", top=1)) == 1


def test_most_common_spelling_wins_ties_alphabetically() -> None:
    counts = EntityTally()
    counts.add_article([(EntityType.PERSON, "Smith")], domain="a", seen_at=None)
    counts.add_article([(EntityType.PERSON, "SMITH")], domain="b", seen_at=None)
    assert counts.ranked(EntityType.PERSON, query="q")[0].name == "SMITH"


# ---- table layout ---------------------------------------------------------------


def entity(name: str, count: int, kind: EntityType = EntityType.PERSON) -> Entity:
    return Entity(name=name, type=kind, count=count, sources=1, query="q")


def test_sections_layout() -> None:
    stream = io.StringIO()
    write_sections(
        [("PEOPLE", [entity("John Smith", 183), entity("Jane Doe", 91)]), ("THEMES", [])],
        stream,
        footer="Counts are articles.",
    )
    lines = stream.getvalue().splitlines()
    assert lines[0] == "PEOPLE"
    assert set(lines[1]) == {"─"}
    assert lines[2].startswith("John Smith") and lines[2].endswith("183")
    assert len(lines[2]) == len(lines[3])
    assert "THEMES" not in stream.getvalue()
    assert lines[-1] == "Counts are articles."


def test_long_names_are_truncated() -> None:
    stream = io.StringIO()
    write_sections([("PEOPLE", [entity("x" * 80, 1)])], stream)
    assert "…" in stream.getvalue()


def test_empty_result_says_so() -> None:
    stream = io.StringIO()
    assert write_sections([("PEOPLE", [])], stream, footer="unused") == 0
    assert stream.getvalue().strip() == "No entities found."


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
    return runner.invoke(app, ["entities", "Company X", "--since", "1h", *args])


@respx.mock
def test_cli_table_groups_by_category() -> None:
    mock_published(Dataset.GKG, {0: ROWS[:1], 2: ROWS[1:]})
    result = invoke()
    assert result.exit_code == 0, result.output
    out = result.stdout
    for title in ("PEOPLE", "ORGANIZATIONS", "LOCATIONS", "COUNTRIES", "THEMES"):
        assert title in out
    assert out.index("PEOPLE") < out.index("ORGANIZATIONS") < out.index("THEMES")
    assert "Company Y" not in out
    assert "from 2 matching GKG records in 4 files" in out


@respx.mock
def test_cli_table_is_identical_across_runs() -> None:
    mock_published(Dataset.GKG, {0: ROWS[:1], 1: ROWS[1:]})
    assert invoke().stdout == invoke().stdout


@respx.mock
def test_cli_json_is_aggregated_with_reproducible_parameters() -> None:
    mock_published(Dataset.GKG, {0: ROWS})
    result = invoke("--json", "--type", "person", "--type", "organization")
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["endpoint"] == "gkg"
    assert document["parameters"]["files"] == 4
    assert document["parameters"]["types"] == ["organization", "person"]
    assert {r["type"] for r in document["results"]} == {"person", "organization"}
    company = next(r for r in document["results"] if r["name"] == "Company X")
    assert company["count"] == 2 and company["sources"] == 2


@respx.mock
def test_cli_mentions_keep_source_level_detail() -> None:
    mock_published(Dataset.GKG, {0: ROWS})
    result = invoke("--mentions", "--jsonl", "--raw", "--type", "person")
    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert {(r["name"], r["url"]) for r in records} == {
        ("John Smith", "https://reuters.com/1"),
        ("Jane Doe", "https://reuters.com/1"),
        ("john smith", "https://bbc.com/2"),
    }
    assert records[0]["raw"]["V2SOURCECOMMONNAME"] == "reuters.com"


@respx.mock
def test_cli_csv() -> None:
    mock_published(Dataset.GKG, {0: ROWS})
    result = invoke("--csv", "--type", "theme")
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert [(r["name"], r["count"]) for r in rows] == [("SANCTIONS", "2"), ("ECON_TRADE", "1")]


def test_cli_rejects_query_syntax_before_any_request() -> None:
    with respx.mock(assert_all_called=False) as mock:
        result = runner.invoke(app, ["entities", "a OR b"])
        assert mock.calls.call_count == 0
    assert isinstance(result.exception, InputError)
    assert result.stdout == ""


@respx.mock
def test_cli_refuses_a_month_of_gkg() -> None:
    mock_published(Dataset.GKG, {})
    result = runner.invoke(app, ["entities", "Company X", "--since", "30d"])
    assert isinstance(result.exception, InputError)
    assert "--allow-large" in (result.exception.hint or "")
    assert result.stdout == ""
