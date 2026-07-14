"""Tests for the SearxNG web search client and its TargetInspector integration."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from openapi_anything.generator.inspector import TargetInspector
from openapi_anything.generator.websearch import SearxNGClient


def _searxng_transport(results: list[dict], status_code: int = 200) -> httpx.MockTransport:
    """Fake SearxNG server: answers /search with the given results payload."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.url.params["format"] == "json"
        return httpx.Response(status_code, json={"query": "x", "results": results})

    return httpx.MockTransport(handler)


SAMPLE_RESULTS = [
    {
        "url": "https://httpbin.org/",
        "title": "httpbin.org",
        "content": "HTTP request & response testing service.",
        "engine": "startpage",
        "score": 7.5,
    },
    {
        "url": "https://example.com/docs",
        "title": "Example Docs",
        "content": "API documentation for example.",
        "engine": "brave",
        "score": 3.0,
    },
    {
        "url": "https://example.com/blog",
        "title": "Example Blog",
        "content": "A blog post.",
        "engine": "brave",
        "score": 1.0,
    },
]


@pytest.mark.asyncio
async def test_search_parses_results():
    client = SearxNGClient(transport=_searxng_transport(SAMPLE_RESULTS))
    results = await client.search("httpbin api")
    assert len(results) == 3
    assert results[0] == {
        "title": "httpbin.org",
        "url": "https://httpbin.org/",
        "snippet": "HTTP request & response testing service.",
    }


@pytest.mark.asyncio
async def test_search_respects_max_results():
    client = SearxNGClient(transport=_searxng_transport(SAMPLE_RESULTS))
    results = await client.search("httpbin api", max_results=2)
    assert len(results) == 2


@pytest.mark.asyncio
async def test_search_returns_empty_on_http_error():
    client = SearxNGClient(transport=_searxng_transport([], status_code=500))
    assert await client.search("anything") == []


@pytest.mark.asyncio
async def test_search_returns_empty_on_network_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = SearxNGClient(transport=httpx.MockTransport(boom))
    assert await client.search("anything") == []


def test_base_url_from_env(monkeypatch):
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searx.local:8080/")
    client = SearxNGClient()
    assert client.base_url == "http://searx.local:8080"


def test_format_results_compact():
    formatted = SearxNGClient.format_results(
        [{"title": "T1", "url": "https://u1", "snippet": "S1"}]
    )
    assert "T1" in formatted
    assert "https://u1" in formatted
    assert "S1" in formatted


def test_format_results_empty():
    assert SearxNGClient.format_results([]) == ""


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.complete = AsyncMock(return_value="analysis")
    return llm


@pytest.fixture
def mock_search():
    search = MagicMock(spec=SearxNGClient)
    search.search = AsyncMock(
        return_value=[
            {"title": "ls manual", "url": "https://man7.org/ls", "snippet": "list directory"}
        ]
    )
    search.format_results = SearxNGClient.format_results
    return search


@pytest.mark.asyncio
async def test_inspector_includes_web_research(mock_llm, mock_search):
    inspector = TargetInspector(mock_llm, search=mock_search)
    result = await inspector.inspect("wrap the ls command as a REST API")
    assert result["type"] == "cli"
    assert result["web_research"] == [
        {"title": "ls manual", "url": "https://man7.org/ls", "snippet": "list directory"}
    ]
    # research snippets reach the LLM analysis prompt
    prompt = mock_llm.complete.await_args.args[0]
    assert "ls manual" in prompt


@pytest.mark.asyncio
async def test_inspector_web_target_includes_research(mock_llm, mock_search):
    inspector = TargetInspector(mock_llm, search=mock_search)
    # avoid a live fetch: point at an unroutable URL and let the fetch error path run
    result = await inspector.inspect("wrap http://127.0.0.1:1 as a REST API")
    assert result["type"] == "web"
    assert result["web_research"]


@pytest.mark.asyncio
async def test_inspector_web_resolves_base_url_from_research(mock_llm):
    """No URL in the description -> the top research hit becomes base_url
    (regression: 'wrap Github trending' used to silently wrap httpbin.org)."""
    search = MagicMock(spec=SearxNGClient)
    search.search = AsyncMock(
        return_value=[
            {"title": "Trending", "url": "http://127.0.0.1:1/trending", "snippet": "s"},
            {"title": "Other", "url": "http://127.0.0.1:1/other", "snippet": "s"},
        ]
    )
    inspector = TargetInspector(mock_llm, search=search)
    result = await inspector.inspect("wrap Github trending in to REST API")
    assert result["type"] == "web"
    assert result["base_url"] == "http://127.0.0.1:1/trending"


@pytest.mark.asyncio
async def test_inspector_web_explicit_url_beats_research(mock_llm, mock_search):
    """A URL in the description always wins over research hits."""
    inspector = TargetInspector(mock_llm, search=mock_search)
    result = await inspector.inspect("wrap http://127.0.0.1:1/explicit as a REST API")
    assert result["base_url"] == "http://127.0.0.1:1/explicit"


@pytest.mark.asyncio
async def test_inspector_web_httpbin_only_as_last_resort(mock_llm):
    """No URL anywhere (description + research empty) -> legacy httpbin default."""
    from unittest.mock import patch

    search = MagicMock(spec=SearxNGClient)
    search.search = AsyncMock(return_value=[])
    inspector = TargetInspector(mock_llm, search=search)
    with patch(
        "openapi_anything.generator.inspector.httpx.AsyncClient",
        side_effect=RuntimeError("no network in tests"),
    ):
        result = await inspector.inspect("wrap something nondescript")
    assert result["base_url"] == "https://httpbin.org"


@pytest.mark.asyncio
async def test_inspector_survives_search_failure(mock_llm):
    search = MagicMock(spec=SearxNGClient)
    search.search = AsyncMock(side_effect=RuntimeError("searx down"))
    inspector = TargetInspector(mock_llm, search=search)
    result = await inspector.inspect("wrap the ls command as a REST API")
    assert result["type"] == "cli"
    assert result["web_research"] == []


@pytest.mark.asyncio
async def test_inspector_default_search_client(mock_llm):
    """Constructing without an explicit client must not break existing callers."""
    inspector = TargetInspector(mock_llm)
    assert isinstance(inspector.search, SearxNGClient)
