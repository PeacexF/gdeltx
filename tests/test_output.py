import csv
import io
import json

import pytest
from pydantic import Field

from gdeltx.models import Record, RequestMeta
from gdeltx.output import Column, Format, TableSpec, write


class Article(Record):
    title: str
    domain: str
    tone: float | None = None
    tags: list[str] = Field(default_factory=list)


META = RequestMeta(query="OpenAI", endpoint="doc", parameters={"maxrecords": 2})

SPEC = TableSpec(
    columns=[
        Column("Domain", "domain"),
        Column("Title", "title"),
    ]
)


def records() -> list[Article]:
    return [
        Article(title="First", domain="reuters.com", tone=-1.5, tags=["a", "b"]),
        Article(title="Second", domain="bbc.com", tone=0.5, raw={"orig": 1}),
    ]


def render(fmt: str, **kwargs: object) -> str:
    stream = io.StringIO()
    write(records(), fmt=fmt, meta=META, spec=SPEC, stream=stream, **kwargs)  # type: ignore[arg-type]
    return stream.getvalue()


def test_json_is_valid_and_carries_metadata() -> None:
    payload = json.loads(render("json"))
    assert payload["query"] == "OpenAI"
    assert payload["endpoint"] == "doc"
    assert payload["parameters"] == {"maxrecords": 2}
    assert "retrieved_at" in payload
    assert len(payload["results"]) == 2
    assert payload["results"][0]["title"] == "First"


def test_jsonl_is_one_record_per_line() -> None:
    lines = render("jsonl").strip().split("\n")
    assert len(lines) == 2
    assert [json.loads(line)["title"] for line in lines] == ["First", "Second"]


def test_jsonl_omits_metadata_by_default() -> None:
    for line in render("jsonl").strip().split("\n"):
        assert "_meta" not in json.loads(line)


def test_jsonl_header_record_is_opt_in() -> None:
    lines = render("jsonl", include_meta=True).strip().split("\n")
    assert len(lines) == 3
    assert json.loads(lines[0])["_meta"]["query"] == "OpenAI"


def test_raw_is_excluded_unless_requested() -> None:
    assert "orig" not in render("jsonl")
    assert "orig" in render("jsonl", include_raw=True)


def test_csv_has_header_and_rows() -> None:
    rows = list(csv.reader(io.StringIO(render("csv"))))
    assert rows[0] == ["domain", "title"]
    assert rows[1] == ["reuters.com", "First"]
    assert len(rows) == 3


def test_csv_flattens_lists_without_a_spec() -> None:
    stream = io.StringIO()
    write(records(), fmt="csv", meta=META, stream=stream)
    rows = list(csv.reader(io.StringIO(stream.getvalue())))
    assert "tags" in rows[0]
    assert "a; b" in rows[1]


def test_table_renders_uppercase_headers() -> None:
    output = render("table")
    assert "DOMAIN" in output
    assert "TITLE" in output
    assert "reuters.com" in output


def test_table_reports_emptiness() -> None:
    stream = io.StringIO()
    spec = TableSpec(columns=SPEC.columns, empty_message="No results.")
    assert write([], fmt="table", meta=META, spec=spec, stream=stream) == 0
    assert "No results." in stream.getvalue()


@pytest.mark.parametrize("fmt", ["json", "jsonl", "csv"])
def test_empty_machine_output_is_still_valid(fmt: str) -> None:
    stream = io.StringIO()
    assert write([], fmt=fmt, meta=META, stream=stream) == 0
    if fmt == "json":
        assert json.loads(stream.getvalue())["results"] == []
    else:
        assert stream.getvalue() == ""


def test_write_returns_record_count() -> None:
    stream = io.StringIO()
    assert write(records(), fmt="jsonl", meta=META, stream=stream) == 2


def test_jsonl_consumes_input_lazily() -> None:
    consumed = []

    def generate():
        for record in records():
            consumed.append(record.title)
            yield record

    stream = io.StringIO()
    write(generate(), fmt="jsonl", meta=META, stream=stream)
    assert consumed == ["First", "Second"]


def test_unknown_format_rejected() -> None:
    with pytest.raises(ValueError, match="yaml"):
        write([], fmt="yaml", meta=META, stream=io.StringIO())


def test_format_enum_values() -> None:
    assert [f.value for f in Format] == ["table", "json", "jsonl", "csv"]


def test_unicode_is_not_escaped() -> None:
    stream = io.StringIO()
    write(
        [Article(title="Санкции", domain="example.ru")],
        fmt="jsonl",
        meta=META,
        stream=stream,
    )
    assert "Санкции" in stream.getvalue()
