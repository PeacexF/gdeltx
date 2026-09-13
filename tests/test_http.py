from pathlib import Path

import httpx
import pytest
import respx

from gdeltx.cache import CacheStore
from gdeltx.console import Reporter
from gdeltx.errors import APIError, ParseError, RateLimitError
from gdeltx.sources import HttpClient, RateLimiter
from gdeltx.sources.http import USER_AGENT

URL = "https://api.gdeltproject.org/api/v2/doc/doc"


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr("gdeltx.sources.http.time.sleep", delays.append)
    return delays


@pytest.fixture
def client() -> HttpClient:
    return HttpClient(retries=3, reporter=Reporter(quiet=True))


@respx.mock
def test_success_returns_body(client: HttpClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"articles": []}))
    fetched = client.get(URL, params={"query": "OpenAI"})
    assert fetched.json() == {"articles": []}
    assert fetched.from_cache is False


@respx.mock
def test_sends_identifying_user_agent(client: HttpClient) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    client.get(URL)
    assert route.calls.last.request.headers["user-agent"] == USER_AGENT
    assert "gdeltx" in USER_AGENT


@respx.mock
def test_empty_response_is_not_an_error(client: HttpClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"articles": []}))
    assert client.get(URL).json()["articles"] == []


@respx.mock
def test_malformed_json_raises_parse_error(client: HttpClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"<html>nope</html>"))
    with pytest.raises(ParseError, match="not valid JSON"):
        client.get(URL).json()


@respx.mock
def test_retries_then_succeeds(client: HttpClient, no_sleeping: list[float]) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert client.get(URL).json() == {"ok": True}
    assert route.call_count == 3
    assert len(no_sleeping) == 2


@respx.mock
def test_429_exhausted_raises_rate_limit_error(client: HttpClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitError) as excinfo:
        client.get(URL)
    assert "429" in str(excinfo.value)
    assert excinfo.value.exit_code == 75


@respx.mock
def test_429_honours_retry_after(client: HttpClient, no_sleeping: list[float]) -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={}),
        ]
    )
    client.get(URL)
    assert no_sleeping == [7.0]


@respx.mock
def test_500_exhausted_raises_api_error(client: HttpClient) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(APIError, match="after 4 attempts"):
        client.get(URL)
    assert route.call_count == 4


@respx.mock
def test_timeout_is_retried(client: HttpClient) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.TimeoutException("slow"), httpx.Response(200, json={})]
    )
    client.get(URL)
    assert route.call_count == 2


@respx.mock
def test_connection_error_is_retried(client: HttpClient) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.ConnectError("dns"), httpx.Response(200, json={})]
    )
    client.get(URL)
    assert route.call_count == 2


@respx.mock
def test_404_is_not_retried(client: HttpClient) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(APIError, match="HTTP 404"):
        client.get(URL)
    assert route.call_count == 1


@respx.mock
def test_failure_never_becomes_an_empty_result(client: HttpClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(APIError):
        client.get(URL)


@respx.mock
def test_zero_retries_fails_immediately() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(APIError, match="after 1 attempts"):
        HttpClient(retries=0, reporter=Reporter(quiet=True)).get(URL)
    assert route.call_count == 1


@respx.mock
def test_cache_hit_skips_the_network(tmp_path: Path) -> None:
    cache = CacheStore(tmp_path, ttl=3600)
    client = HttpClient(cache=cache, reporter=Reporter(quiet=True))
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"n": 1}))

    first = client.get(URL, params={"query": "x"})
    second = client.get(URL, params={"query": "x"})

    assert route.call_count == 1
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.json() == {"n": 1}


@respx.mock
def test_different_params_are_cached_separately(tmp_path: Path) -> None:
    client = HttpClient(cache=CacheStore(tmp_path), reporter=Reporter(quiet=True))
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    client.get(URL, params={"query": "a"})
    client.get(URL, params={"query": "b"})
    assert route.call_count == 2


@respx.mock
def test_failures_are_not_cached(tmp_path: Path) -> None:
    cache = CacheStore(tmp_path)
    client = HttpClient(cache=cache, retries=0, reporter=Reporter(quiet=True))
    respx.get(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(APIError):
        client.get(URL)
    assert cache.total_bytes() == 0


def test_rate_limiter_spaces_requests() -> None:
    limiter = RateLimiter(0.05)
    assert limiter.acquire() == 0.0
    assert limiter.acquire() > 0.0


def test_rate_limiter_disabled_at_zero() -> None:
    limiter = RateLimiter(0)
    assert limiter.acquire() == 0.0
    assert limiter.acquire() == 0.0


@respx.mock
def test_client_closes_cleanly() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    with HttpClient(reporter=Reporter(quiet=True)) as client:
        client.get(URL)
