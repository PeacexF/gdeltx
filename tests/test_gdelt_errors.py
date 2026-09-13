"""Behaviour pinned to real GDELT responses captured in tests/fixtures."""

import pytest

from gdeltx.errors import APIError, ParseError
from gdeltx.sources.http import Fetched, gdelt_error_message


def load(fixtures_dir, name: str) -> bytes:
    return (fixtures_dir / name).read_bytes()


def test_maxrecords_error_arrives_as_http_200(fixtures_dir) -> None:
    """GDELT reports this failure with a 200 status and a plain-text body."""
    body = load(fixtures_dir, "doc_error_maxrecords.txt")
    assert gdelt_error_message(body) == "A maximum of 250 records can be returned."


def test_plain_text_error_becomes_an_api_error(fixtures_dir) -> None:
    body = load(fixtures_dir, "doc_error_maxrecords.txt")
    with pytest.raises(APIError, match="maximum of 250 records"):
        Fetched(body=body, url="doc").json()


def test_rate_limit_body_is_extracted(fixtures_dir) -> None:
    message = gdelt_error_message(load(fixtures_dir, "doc_rate_limited.txt"))
    assert message is not None
    assert "one every 5 seconds" in message


def test_real_timeline_response_parses(fixtures_dir) -> None:
    data = Fetched(body=load(fixtures_dir, "doc_timeline_2017.json"), url="doc").json()
    assert data["query_details"]["title"] == "OpenAI"
    series = data["timeline"][0]
    assert series["series"] == "Article Count"
    assert series["data"][0]["date"] == "20170101T000000Z"


def test_empty_context_response_is_not_an_error(fixtures_dir) -> None:
    data = Fetched(body=load(fixtures_dir, "context_empty.json"), url="context").json()
    assert data["articles"] == []


def test_json_body_is_never_mistaken_for_an_error(fixtures_dir) -> None:
    assert gdelt_error_message(load(fixtures_dir, "doc_timeline_2017.json")) is None
    assert gdelt_error_message(b'{"articles": []}') is None
    assert gdelt_error_message(b"[]") is None


def test_html_body_is_not_treated_as_a_gdelt_message() -> None:
    assert gdelt_error_message(b"<!DOCTYPE HTML><html><body>404</body></html>") is None


def test_long_non_json_body_falls_back_to_parse_error() -> None:
    with pytest.raises(ParseError):
        Fetched(body=b"x" * 900, url="doc").json()


def test_empty_body_is_a_parse_error() -> None:
    with pytest.raises(ParseError):
        Fetched(body=b"", url="doc").json()
