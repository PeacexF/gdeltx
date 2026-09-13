"""Source aggregation and the `sources` command, over DOC and over GKG."""

import csv
import io
import json
from collections import Counter
from datetime import UTC, datetime

import httpx
import pytest
import respx
from typer.testing import CliRunner

from builders import mock_published
from gdeltx.analysis.aggregate import SourceTally
from gdeltx.cli import app
from gdeltx.errors import InputError, RateLimitError
from gdeltx.sources.doc import ENDPOINT
from gdeltx.sources.files import Dataset

T1 = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


# ---- aggregation --------------------------------------------------------------


def test_counts_first_and_last_seen() -> None:
    tally = SourceTally()
    tally.add(domain="reuters.com", seen_at=T2)
    tally.add(domain="reuters.com", seen_at=T1)
    tally.add(domain="bbc.com", seen_at=None)
    reuters, bbc = tally.ranked(query="q")
    assert (reuters.domain, reuters.articles, reuters.first_seen, reuters.last_seen) == (
        "reuters.com",
        2,
        T1,
        T2,
    )
    assert (bbc.articles, bbc.first_seen) == (1, None)


def test_country_and_language_are_the_most_common_with_stable_ties() -> None:
    tally = SourceTally()
    for country in ("France", "Belgium", "France", "Belgium"):
        tally.add(domain="x.fr", seen_at=None, country=country, language="French")
    source = tally.ranked(query="q")[0]
    assert (source.country, source.language) == ("Belgium", "French")


def test_tone_is_a_rounded_mean_independent_of_order() -> None:
    tones = [0.1, 0.2, 0.3, -1.7, 2.9999]
    forward, backward = SourceTally(), SourceTally()
    for tone in tones:
        forward.add(domain="a.com", seen_at=None, tone=tone)
    for tone in reversed(tones):
        backward.add(domain="a.com", seen_at=None, tone=tone)
    assert forward.ranked(query="q") == backward.ranked(query="q")
    assert forward.ranked(query="q")[0].average_tone == round(sum(tones) / 5, 4)


def test_missing_tone_and_themes_stay_none() -> None:
    tally = SourceTally()
    tally.add(domain="a.com", seen_at=None)
    source = tally.ranked(query="q")[0]
    assert source.average_tone is None
    assert source.topics is None


def test_topics_count_articles_and_keep_the_top_five() -> None:
    tally = SourceTally()
    tally.add(domain="a.com", seen_at=None, themes=["TAX", "TAX", "ECON", "A", "B", "C", "D"])
    tally.add(domain="a.com", seen_at=None, themes=["ECON", "ZZZ"])
    assert tally.ranked(query="q")[0].topics == {"ECON": 2, "A": 1, "B": 1, "C": 1, "D": 1}


def test_ranking_breaks_ties_by_domain_and_respects_top() -> None:
    tally = SourceTally()
    for domain in ("c.com", "a.com", "b.com", "a.com"):
        tally.add(domain=domain, seen_at=None)
    assert [s.domain for s in tally.ranked(query="q")] == ["a.com", "b.com", "c.com"]
    assert [s.domain for s in tally.ranked(query="q", top=2)] == ["a.com", "b.com"]


def test_domains_are_not_rewritten_and_blank_domains_are_unattributed() -> None:
    tally = SourceTally()
    tally.add(domain="www.bbc.com", seen_at=None)
    tally.add(domain="bbc.com", seen_at=None)
    tally.add(domain=None, seen_at=None)
    tally.add(domain="  ", seen_at=None)
    assert sorted(s.domain for s in tally.ranked(query="q")) == ["bbc.com", "www.bbc.com"]
    assert (tally.articles, tally.unattributed) == (4, 2)


# ---- CLI over DOC -------------------------------------------------------------

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("gdeltx.sources.http.time.sleep", lambda _: None)
    for name in ("FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(name, raising=False)


def doc_payload() -> dict:
    rows = [
        ("reuters.com", "United States", "English", "20260912100000"),
        ("bbc.co.uk", "United Kingdom", "English", "20260912083000"),
        ("reuters.com", "United States", "English", "20260911121500"),
        ("lemonde.fr", "France", "French", "20260912070000"),
        ("reuters.com", "United States", "English", "20260912110000"),
        ("", "", "", "20260912110000"),
    ]
    return {
        "articles": [
            {
                "url": f"https://{domain or 'unknown'}/{i}",
                "title": f"article {i}",
                "seendate": f"{stamp[:8]}T{stamp[8:]}Z",
                "domain": domain,
                "language": language,
                "sourcecountry": country,
            }
            for i, (domain, country, language, stamp) in enumerate(rows)
        ]
    }


@respx.mock
def test_doc_counts_reconcile_with_search() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=doc_payload()))
    args = ["OpenAI", "--since", "7d", "--max", "250", "--json"]

    searched = json.loads(runner.invoke(app, ["search", *args]).stdout)["results"]
    grouped = runner.invoke(app, ["sources", *args])
    assert grouped.exit_code == 0, grouped.output
    sources = json.loads(grouped.stdout)["results"]

    expected = Counter(article["domain"] for article in searched if article["domain"])
    assert {s["domain"]: s["articles"] for s in sources} == dict(expected)
    assert sum(s["articles"] for s in sources) + 1 == len(searched)
    assert "1 articles had no domain" in grouped.stderr


@respx.mock
def test_doc_json_fields_and_parameters() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=doc_payload()))
    result = runner.invoke(app, ["sources", '"OpenAI" (regulation OR lawsuit)', "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["endpoint"] == "doc"
    assert document["parameters"]["group_by"] == "domain"
    assert document["parameters"]["mode"] == "artlist"
    first = document["results"][0]
    assert first["domain"] == "reuters.com"
    assert first["articles"] == 3
    assert first["country"] == "United States"
    assert first["language"] == "English"
    assert first["first_seen"] == "2026-09-11T12:15:00Z"
    assert first["last_seen"] == "2026-09-12T11:00:00Z"
    assert first["average_tone"] is None


@respx.mock
def test_doc_table() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=doc_payload()))
    result = runner.invoke(app, ["sources", "OpenAI", "--top", "2"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["SOURCE", "ARTICLES", "COUNTRY", "LANGUAGE", "LAST", "SEEN"]
    assert lines[1].split()[:2] == ["reuters.com", "3"]
    assert "lemonde.fr" not in result.stdout
    assert "5 articles from 3 sources" not in result.stdout
    assert "6 articles from 3 sources" in result.stdout


@respx.mock
def test_doc_rate_limit_is_an_error_not_an_empty_table(fixtures_dir) -> None:
    body = (fixtures_dir / "doc_rate_limited.txt").read_bytes()
    respx.get(ENDPOINT).mock(return_value=httpx.Response(429, content=body))
    result = runner.invoke(app, ["sources", "OpenAI"])
    assert isinstance(result.exception, RateLimitError)
    assert result.stdout == ""


def test_options_that_do_not_apply_are_refused() -> None:
    with respx.mock(assert_all_called=False) as mock:
        allow = runner.invoke(app, ["sources", "OpenAI", "--allow-large"])
        domain = runner.invoke(app, ["sources", "OpenAI", "--from", "gkg", "--domain", "bbc.com"])
        syntax = runner.invoke(app, ["sources", "a OR b", "--from", "gkg"])
        assert mock.calls.call_count == 0
    assert isinstance(allow.exception, InputError)
    assert isinstance(domain.exception, InputError)
    assert isinstance(syntax.exception, InputError)


# ---- CLI over GKG, using captured rows -----------------------------------------


def captured_gkg(fixtures_dir) -> list[str]:
    return [
        line + "\n" for line in (fixtures_dir / "gkg.sample.tsv").read_text("utf-8").splitlines()
    ]


@respx.mock
def test_gkg_json_has_tone_and_topics(fixtures_dir) -> None:
    mock_published(Dataset.GKG, {0: captured_gkg(fixtures_dir)})
    result = runner.invoke(app, ["sources", "Trump", "--from", "gkg", "--since", "1h", "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["endpoint"] == "gkg"
    assert document["parameters"]["files"] == 4
    by_domain = {s["domain"]: s for s in document["results"]}
    assert set(by_domain) == {"mynspr.org", "nbcdfw.com"}
    nbc = by_domain["nbcdfw.com"]
    assert nbc["articles"] == 1
    assert nbc["average_tone"] == 2.1333
    assert "ECON_HOUSING_PRICES" in nbc["topics"]
    assert nbc["country"] is None and nbc["language"] is None


@respx.mock
def test_gkg_table_and_csv(fixtures_dir) -> None:
    mock_published(Dataset.GKG, {0: captured_gkg(fixtures_dir)})
    table = runner.invoke(app, ["sources", "AI", "--from", "gkg", "--since", "1h"])
    assert table.exit_code == 0, table.output
    assert table.stdout.splitlines()[0].split() == [
        "SOURCE",
        "ARTICLES",
        "TONE",
        "TOP",
        "THEME",
        "LAST",
        "SEEN",
    ]
    footer = table.stdout.splitlines()[-1]
    assert footer.startswith("4 matching GKG records") and footer.endswith("document tone.")

    as_csv = runner.invoke(app, ["sources", "Trump", "--from", "gkg", "--since", "1h", "--csv"])
    rows = list(csv.DictReader(io.StringIO(as_csv.stdout)))
    assert json.loads(rows[0]["topics"])


@pytest.mark.parametrize("origin", ["doc", "gkg"])
def test_every_column_survives_an_80_column_terminal(origin: str, monkeypatch) -> None:
    from gdeltx.commands.sources import TABLES, Origin
    from gdeltx.models import RequestMeta
    from gdeltx.output import write

    monkeypatch.setenv("COLUMNS", "80")
    tally = SourceTally()
    tally.add(
        domain="northerndailyleader.com.au",
        seen_at=T1,
        country="United Kingdom of Great Britain",
        language="Portuguese",
        tone=-12.5,
        themes=["WB_1921_PRIVATE_SECTOR_DEVELOPMENT"],
    )
    stream = io.StringIO()
    spec = TABLES[Origin(origin)]
    write(
        tally.ranked(query="q"),
        fmt="table",
        meta=RequestMeta(query="q", endpoint=origin),
        spec=spec,
        stream=stream,
    )
    header = stream.getvalue().splitlines()[0]
    assert all(column.header.upper() in header for column in spec.columns)
    assert max(len(line) for line in stream.getvalue().splitlines()) <= 80
