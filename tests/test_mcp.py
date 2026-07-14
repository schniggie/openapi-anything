"""Tests for the MCP export: OpenAPI->tool derivation and the JSON-RPC endpoint."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from openapi_anything.gateway.jobs import JobStore
from openapi_anything.gateway.main import create_app
from openapi_anything.gateway.mcp import MCPGateway, openapi_to_tools
from openapi_anything.gateway.registry import Registry, WrapperEntry
from openapi_anything.service import DeployResult

TRENDING_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "GitHub Trending API", "version": "0.1.0"},
    "paths": {
        "/": {"get": {"summary": "Index"}},
        "/health": {"get": {"summary": "Health"}},
        "/trending": {
            "get": {
                "summary": "Trending repositories",
                "parameters": [
                    {
                        "name": "language",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                    }
                ],
            }
        },
        "/execute": {
            "post": {
                "summary": "Execute",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ExecuteRequest"}
                        }
                    }
                },
            }
        },
    },
    "components": {
        "schemas": {
            "ExecuteRequest": {
                "type": "object",
                "properties": {"args": {"type": "array", "items": {"type": "string"}}},
                "required": ["args"],
            }
        }
    },
}


# ------------------------------------------------------------ tool derivation


def test_openapi_to_tools_skips_meta_paths():
    tools = openapi_to_tools("github", "wrap github trending", TRENDING_SPEC)
    names = [t["name"] for t in tools]
    assert "github__get_trending" in names
    assert "github__post_execute" in names
    assert not any("health" in n for n in names)
    assert len(tools) == 2


def test_openapi_to_tools_query_params_in_schema():
    tools = openapi_to_tools("github", "d", TRENDING_SPEC)
    trending = next(t for t in tools if t["name"] == "github__get_trending")
    assert trending["inputSchema"]["type"] == "object"
    assert "language" in trending["inputSchema"]["properties"]
    assert trending["description"].startswith("Trending repositories")


def test_openapi_to_tools_resolves_body_refs():
    tools = openapi_to_tools("github", "d", TRENDING_SPEC)
    execute = next(t for t in tools if t["name"] == "github__post_execute")
    props = execute["inputSchema"]["properties"]
    assert props["args"]["type"] == "array"
    assert execute["inputSchema"]["required"] == ["args"]


# ------------------------------------------------------------ gateway plumbing


def _entry(wrapper_id: str = "github", status: str = "healthy") -> WrapperEntry:
    return WrapperEntry(
        id=wrapper_id,
        target_description="wrap github trending",
        openapi_url="http://backend:1/openapi.json",
        service_url="http://backend:1",
        container_name=f"wrapper-{wrapper_id}",
        status=status,
        created_at="2026-01-01T00:00:00",
    )


def _backend_transport():
    """Fake wrapper backend: serves openapi.json and echoes API calls."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=TRENDING_SPEC)
        if request.url.path == "/trending":
            return httpx.Response(
                200, json={"items": [{"name": "repo1", "lang": request.url.params.get("language")}]}
            )
        if request.url.path == "/execute" and request.method == "POST":
            return httpx.Response(200, json={"echo": json.loads(request.content)})
        return httpx.Response(404, json={"detail": "nope"})

    return httpx.MockTransport(handler)


def _mcp(reg: Registry, jobs: JobStore | None = None) -> MCPGateway:
    return MCPGateway(
        registry=reg,
        jobs=jobs or JobStore(),
        client_factory=lambda: httpx.AsyncClient(transport=_backend_transport()),
    )


@pytest.fixture
def reg(tmp_path):
    r = Registry(path=tmp_path / "registry.json")
    r.register(_entry())
    return r


@pytest.mark.asyncio
async def test_initialize_handshake(reg):
    mcp = _mcp(reg)
    resp = await mcp.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}
    )
    assert resp["result"]["protocolVersion"]
    assert "tools" in resp["result"]["capabilities"]
    assert resp["result"]["serverInfo"]["name"]


@pytest.mark.asyncio
async def test_initialized_notification_returns_none(reg):
    mcp = _mcp(reg)
    assert await mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


@pytest.mark.asyncio
async def test_tools_list_includes_wrapper_and_meta_tools(reg):
    mcp = _mcp(reg)
    resp = await mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in resp["result"]["tools"]]
    assert "github__get_trending" in names
    assert "list_apis" in names
    assert "generate_api" in names
    assert "job_status" in names


@pytest.mark.asyncio
async def test_tools_list_skips_unreachable_wrappers(tmp_path):
    reg = Registry(path=tmp_path / "registry.json")
    reg.register(_entry("dead", status="unreachable"))
    mcp = _mcp(reg)
    resp = await mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    names = [t["name"] for t in resp["result"]["tools"]]
    assert not any(n.startswith("dead__") for n in names)


@pytest.mark.asyncio
async def test_tools_call_get_with_query_param(reg):
    mcp = _mcp(reg)
    resp = await mcp.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "github__get_trending", "arguments": {"language": "python"}}}
    )
    assert resp["result"]["isError"] is False
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["items"][0]["lang"] == "python"


@pytest.mark.asyncio
async def test_tools_call_post_sends_body(reg):
    mcp = _mcp(reg)
    resp = await mcp.handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "github__post_execute", "arguments": {"args": ["-la"]}}}
    )
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["echo"] == {"args": ["-la"]}


@pytest.mark.asyncio
async def test_tools_call_unknown_tool_is_error(reg):
    mcp = _mcp(reg)
    resp = await mcp.handle(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "nope__get_x", "arguments": {}}}
    )
    assert resp["result"]["isError"] is True


@pytest.mark.asyncio
async def test_unknown_method_returns_jsonrpc_error(reg):
    mcp = _mcp(reg)
    resp = await mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "resources/list"})
    assert resp["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_meta_generate_api_submits_job(reg):
    jobs = JobStore()
    mcp = _mcp(reg, jobs)
    with patch(
        "openapi_anything.gateway.mcp.generate_and_deploy",
        new=AsyncMock(return_value=DeployResult(wrapper_id="w", status="deployed")),
    ):
        resp = await mcp.handle(
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
             "params": {"name": "generate_api",
                        "arguments": {"description": "wrap the date command"}}}
        )
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["job_id"].startswith("job-")
        await jobs.wait(payload["job_id"])

    status = await mcp.handle(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
         "params": {"name": "job_status", "arguments": {"job_id": payload["job_id"]}}}
    )
    job = json.loads(status["result"]["content"][0]["text"])
    assert job["status"] == "completed"


# ------------------------------------------------------------ HTTP endpoints


@pytest.mark.asyncio
async def test_mcp_http_endpoint(tmp_path):
    import openapi_anything.gateway.mcp as mcp_mod
    import openapi_anything.gateway.registry as registry_mod

    reg = Registry(path=tmp_path / "registry.json")
    reg.register(_entry())
    orig_reg = registry_mod._registry_singleton
    orig_mcp = mcp_mod._mcp_singleton
    registry_mod._registry_singleton = reg
    mcp_mod._mcp_singleton = None  # rebuild against seeded registry
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18"}},
            )
            assert resp.status_code == 200
            assert resp.json()["result"]["serverInfo"]["name"]

            # notification -> 202, empty body
            resp = await client.post(
                "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            assert resp.status_code == 202

            # per-wrapper endpoint filters to that wrapper + no meta tools
            resp = await client.post(
                "/services/github/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            )
            names = [t["name"] for t in resp.json()["result"]["tools"]]
            assert all(n.startswith("github__") for n in names)

            # GET not supported (no SSE stream)
            assert (await client.get("/mcp")).status_code == 405
    finally:
        registry_mod._registry_singleton = orig_reg
        mcp_mod._mcp_singleton = orig_mcp
