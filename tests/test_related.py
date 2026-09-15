"""Co-occurrence scoring and the `related` command."""

import csv
import io
import json
import re

import pytest
import respx
from typer.testing import CliRunner

from builders import gkg_line, mock_published
from gdeltx.analysis.cooccurrence import RELATION, Cooccurrence
from gdeltx.cli import app
from gdeltx.commands.related import DISCLAIMER, write_tree
from gdeltx.errors import InputError
from gdeltx.models import EntityType
from gdeltx.parsers.gkg import parse_gkg, published_at, row_entities
from gdeltx.sources.files import Dataset

US = "1#United States#US###39#-98#US#5"


def article(url: str, *, persons: str = "", orgs: str = "", locations: str = "") -> str:
    return gkg_line(
        GKGRECORDID=url,
        V21DATE="20260912100000",
        V2SOURCECOMMONNAME=url.split("/")[2],
        V2DOCUMENTIDENTIFIER=url,
        V2ENHANCEDPERSONS=persons,
        V2ENHANCEDORGANIZATIONS=orgs,
        V2ENHANCEDLOCATIONS=locations,
        V21ALLNAMES=";".join(filter(None, [persons, orgs])),
    )


MATCHING = [
    article("https://a.com/1", persons="John Smith,1", orgs="Company X,2", locations=US),
    article("https://b.com/2", persons="John Smith,1", orgs="Company X Inc,2", locations=US),
    article("https://c.com/3", persons="Jane Doe,1", orgs="company x,2", locations=US),
]
BACKGROUND = [
    *(article(f"https://n{i}.com/{i}", persons="Other Person,1", locations=US) for i in range(20)),
    article("https://d.com/4", persons="Jane Doe,1"),
]


def scored(matching: list[str], background: list[str]) -> Cooccurrence:
    counts = Cooccurrence("Company X")
    rows = [parse_gkg(line) for line in matching]
    for row in rows:
        counts.add_match(
            row_entities(row), domain=row["V2SOURCECOMMONNAME"], seen_at=published_at(row)
        )
    for row in [*rows, *map(parse_gkg, background)]:
        counts.add_article(row_entities(row))
    return counts


# ---- scoring ------------------------------------------------------------------


def test_ubiquitous_entities_rank_below_distinctive_ones() -> None:
    counts = scored(MATCHING, BACKGROUND)
    people = counts.ranked(EntityType.PERSON)
    assert [(p.name, p.together, p.entity_articles, p.score) for p in people] == [
        ("John Smith", 2, 2, 0.666667),
        ("Jane Doe", 1, 2, 0.25),
    ]
    (us,) = counts.ranked(EntityType.COUNTRY)
    assert (us.together, us.entity_articles, us.score) == (3, 23, 0.130435)
    assert us.together > people[0].together and us.score < people[0].score


def test_score_is_reproducible_from_exported_counts() -> None:
    for kind in EntityType:
        for r in scored(MATCHING, BACKGROUND).ranked(kind):
            union = r.query_articles + r.entity_articles - r.together
            assert r.score == round(r.together / union, 6)
            assert (r.query_articles, r.total_articles) == (3, 24)


def test_the_query_itself_is_not_its_own_relation() -> None:
    names = [r.name for r in scored(MATCHING, BACKGROUND).ranked(EntityType.ORGANIZATION)]
    assert names == []


def test_min_count_drops_one_off_coincidences() -> None:
    people = scored(MATCHING, BACKGROUND).ranked(EntityType.PERSON, min_count=2)
    assert [p.name for p in people] == ["John Smith"]


def test_ranking_is_deterministic_regardless_of_input_order() -> None:
    forward = scored(MATCHING, BACKGROUND)
    backward = scored(MATCHING[::-1], BACKGROUND[::-1])
    for kind in EntityType:
        assert forward.ranked(kind) == backward.ranked(kind)


def test_ties_break_by_shared_articles_then_name() -> None:
    counts = Cooccurrence("q")
    for entities in ([("person", "B")], [("person", "A")], [("person", "C"), ("person", "D")]):
        typed = [(EntityType(kind), name) for kind, name in entities]
        counts.add_match(typed, domain=None, seen_at=None)
        counts.add_article(typed)
    assert [r.name for r in counts.ranked(EntityType.PERSON)] == ["A", "B", "C", "D"]


# ---- tree layout ----------------------------------------------------------------


def test_tree_layout() -> None:
    counts = scored(MATCHING, BACKGROUND)
    groups = [
        ("PEOPLE", counts.ranked(EntityType.PERSON)),
        ("ORGANIZATIONS", counts.ranked(EntityType.ORGANIZATION)),
        ("COUNTRIES", counts.ranked(EntityType.COUNTRY)),
    ]
    stream = io.StringIO()
    assert write_tree("Company X", groups, stream, footer=DISCLAIMER) == 3
    lines = stream.getvalue().splitlines()
    assert lines[0] == f"Company X  {RELATION}, in GDELT coverage"
    assert "├── PEOPLE" in lines
    assert "└── COUNTRIES" in lines
    assert "ORGANIZATIONS" not in stream.getvalue()
    smith = next(line for line in lines if "John Smith" in line)
    assert smith.startswith("│   ├── ") and smith.endswith("2   0.667")
    assert next(line for line in lines if "United States" in line).startswith("    └── ")
    assert lines[-1] == DISCLAIMER


def test_empty_tree_says_so() -> None:
    stream = io.StringIO()
    assert write_tree("q", [("PEOPLE", [])], stream, footer=DISCLAIMER) == 0
    assert stream.getvalue().strip() == "No co-occurring entities found."


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
    return runner.invoke(app, ["related", "Company X", "--since", "1h", "--min-count", "1", *args])


# Wording PLAN.md §14 rules out: each would assert a relationship the data cannot support.
FORBIDDEN = re.compile(r"linked to|connected to|associated with|relationship between", re.I)


@respx.mock
@pytest.mark.parametrize("flag", [None, "--json", "--jsonl", "--csv"])
def test_every_format_frames_results_as_cooccurrence(flag: str | None) -> None:
    mock_published(Dataset.GKG, {0: MATCHING, 1: BACKGROUND})
    result = invoke(*([flag] if flag else []))
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert not FORBIDDEN.search(out)

    if flag is None:
        assert RELATION in out and DISCLAIMER in " ".join(out.split())
        return
    if flag == "--json":
        document = json.loads(out)
        assert document["parameters"]["relation"] == RELATION
        assert document["parameters"]["note"] == DISCLAIMER
        records = document["results"]
    elif flag == "--jsonl":
        records = [json.loads(line) for line in out.splitlines()]
    else:
        records = list(csv.DictReader(io.StringIO(out)))
    assert records
    assert all(r["relation"] == RELATION for r in records)


def test_help_uses_the_same_framing() -> None:
    result = runner.invoke(app, ["related", "--help"])
    assert "co-occur" in result.stdout and not FORBIDDEN.search(result.stdout)


@respx.mock
def test_cli_json_carries_counts_and_score() -> None:
    mock_published(Dataset.GKG, {0: MATCHING, 1: BACKGROUND})
    result = invoke("--json", "--type", "person")
    document = json.loads(result.stdout)
    assert document["parameters"]["types"] == ["person"]
    smith = document["results"][0]
    assert {k: smith[k] for k in ("name", "together", "entity_articles", "score")} == {
        "name": "John Smith",
        "together": 2,
        "entity_articles": 2,
        "score": 0.666667,
    }
    assert (smith["query_articles"], smith["total_articles"]) == (3, 24)


@respx.mock
def test_second_pass_reads_from_the_cache() -> None:
    routes = mock_published(Dataset.GKG, {0: MATCHING, 1: BACKGROUND})
    assert invoke().exit_code == 0
    assert [route.call_count for route in routes.values()] == [1, 1]


@respx.mock
def test_no_cache_warns_about_the_double_download() -> None:
    mock_published(Dataset.GKG, {0: MATCHING})
    result = invoke("--no-cache")
    assert result.exit_code == 0, result.output
    assert "downloaded twice" in result.stderr
    assert "downloaded twice" not in result.stdout


@respx.mock
def test_no_matches_skips_the_second_pass() -> None:
    routes = mock_published(Dataset.GKG, {0: BACKGROUND})
    result = invoke()
    assert result.stdout.strip() == "No co-occurring entities found."
    assert routes[0].call_count == 1


def test_cli_rejects_query_syntax_before_any_request() -> None:
    with respx.mock(assert_all_called=False) as mock:
        result = runner.invoke(app, ["related", "a OR b"])
        assert mock.calls.call_count == 0
    assert isinstance(result.exception, InputError)
    assert result.stdout == ""
