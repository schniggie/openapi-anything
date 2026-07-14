"""Tests for wrapper regeneration: re-run the pipeline for an existing id."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openapi_anything.gateway.jobs import JobStore
from openapi_anything.gateway.main import create_app
from openapi_anything.gateway.registry import Registry, WrapperEntry
from openapi_anything.service import DeployResult


def _entry(wrapper_id: str = "github") -> WrapperEntry:
    return WrapperEntry(
        id=wrapper_id,
        target_description="wrap Github trending in to REST API",
        openapi_url="http://x:1/openapi.json",
        service_url="http://x:1",
        container_name=f"wrapper-{wrapper_id}",
        status="degraded",
        created_at="2026-01-01T00:00:00",
        verification={"overall": False, "endpoints": {"GET /trending": "500"}},
    )


def _seed(tmp_path):
    import openapi_anything.gateway.jobs as jobs_mod
    import openapi_anything.gateway.registry as registry_mod

    originals = (registry_mod._registry_singleton, jobs_mod._job_store_singleton)
    reg = Registry(path=tmp_path / "registry.json")
    reg.register(_entry())
    store = JobStore()
    registry_mod._registry_singleton = reg
    jobs_mod._job_store_singleton = store
    return originals, store


def _restore(originals):
    import openapi_anything.gateway.jobs as jobs_mod
    import openapi_anything.gateway.registry as registry_mod

    registry_mod._registry_singleton, jobs_mod._job_store_singleton = originals


def _deployed() -> DeployResult:
    return DeployResult(wrapper_id="github", status="deployed", service_url="http://x:1")


# ---------------------------------------------------------------- JobStore


@pytest.mark.asyncio
async def test_jobstore_active_for():
    import asyncio

    store = JobStore()
    gate = asyncio.Event()

    async def runner(report):
        await gate.wait()
        return _deployed()

    job = store.submit("d", "github", runner)
    assert store.active_for("github") is True
    assert store.active_for("other") is False
    gate.set()
    await store.wait(job.id)
    assert store.active_for("github") is False


# ---------------------------------------------------------------- endpoint


@pytest.mark.asyncio
async def test_regenerate_defaults_to_original_description(tmp_path):
    originals, store = _seed(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            mock_gen = AsyncMock(return_value=_deployed())
            with patch("openapi_anything.gateway.main.generate_and_deploy", new=mock_gen):
                resp = await client.post("/services/github/_regenerate")
                assert resp.status_code == 202
                data = resp.json()
                assert data["wrapper_id"] == "github"
                assert data["poll"] == f"/jobs/{data['job_id']}"
                await store.wait(data["job_id"])

            call = mock_gen.await_args
            assert call.args[0] == "wrap Github trending in to REST API"
            # prior context carries the old verification report to the designer
            assert call.kwargs["prior"]["verification"]["overall"] is False
    finally:
        _restore(originals)


@pytest.mark.asyncio
async def test_regenerate_with_refined_description(tmp_path):
    originals, store = _seed(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            mock_gen = AsyncMock(return_value=_deployed())
            with patch("openapi_anything.gateway.main.generate_and_deploy", new=mock_gen):
                resp = await client.post(
                    "/services/github/_regenerate",
                    json={"description": "github trending with language filter"},
                )
                assert resp.status_code == 202
                await store.wait(resp.json()["job_id"])
            assert mock_gen.await_args.args[0] == "github trending with language filter"
            # refined description becomes the job's description
            assert store.list_all()[0].description == "github trending with language filter"
    finally:
        _restore(originals)


@pytest.mark.asyncio
async def test_regenerate_unknown_wrapper_404(tmp_path):
    originals, _ = _seed(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/services/nope/_regenerate")
            assert resp.status_code == 404
    finally:
        _restore(originals)


@pytest.mark.asyncio
async def test_regenerate_conflicts_with_active_job(tmp_path):
    import asyncio

    originals, store = _seed(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            gate = asyncio.Event()

            async def slow(*args, **kwargs):
                await gate.wait()
                return _deployed()

            with patch("openapi_anything.gateway.main.generate_and_deploy", new=slow):
                first = await client.post("/services/github/_regenerate")
                assert first.status_code == 202
                second = await client.post("/services/github/_regenerate")
                assert second.status_code == 409
                gate.set()
                await store.wait(first.json()["job_id"])
    finally:
        _restore(originals)


@pytest.mark.asyncio
async def test_hub_regenerate_form_redirects(tmp_path):
    originals, store = _seed(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "openapi_anything.gateway.main.generate_and_deploy",
                new=AsyncMock(return_value=_deployed()),
            ):
                resp = await client.post("/services/github/_regenerate/form")
                assert resp.status_code == 303
                assert resp.headers["location"].startswith("/")
                await store.wait(store.list_all()[0].id)
    finally:
        _restore(originals)


# ---------------------------------------------------------------- designer


@pytest.mark.asyncio
async def test_designer_prompt_includes_prior_context():
    from openapi_anything.generator.designer import Designer

    llm = MagicMock()
    llm.complete_json = AsyncMock(
        return_value={
            "title": "T",
            "description": "d",
            "target_type": "web",
            "endpoints": [
                {"method": "GET", "path": "/x", "handler_code": "return {}"}
            ],
            "models": {},
        }
    )
    designer = Designer(llm)
    await designer.design(
        {"type": "web", "base_url": "http://x"},
        "improve it",
        prior={"verification": {"overall": False, "endpoints": {"GET /trending": "500"}}},
    )
    prompt = llm.complete_json.await_args.args[0]
    assert "previous version" in prompt.lower()
    assert "GET /trending" in prompt


# ---------------------------------------------------------------- MCP meta


@pytest.mark.asyncio
async def test_mcp_regenerate_tool(tmp_path):
    from openapi_anything.gateway.mcp import MCPGateway

    reg = Registry(path=tmp_path / "registry.json")
    reg.register(_entry())
    store = JobStore()
    mcp = MCPGateway(registry=reg, jobs=store, client_factory=lambda: httpx.AsyncClient())

    listed = await mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "regenerate_api" in [t["name"] for t in listed["result"]["tools"]]

    with patch(
        "openapi_anything.gateway.mcp.generate_and_deploy",
        new=AsyncMock(return_value=_deployed()),
    ):
        resp = await mcp.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "regenerate_api", "arguments": {"wrapper_id": "github"}}}
        )
        import json as _json

        payload = _json.loads(resp["result"]["content"][0]["text"])
        assert payload["job_id"].startswith("job-")
        await store.wait(payload["job_id"])
    assert store.get(payload["job_id"]).status == "completed"


@pytest.mark.asyncio
async def test_mcp_regenerate_unknown_wrapper_errors(tmp_path):
    from openapi_anything.gateway.mcp import MCPGateway

    reg = Registry(path=tmp_path / "registry.json")
    mcp = MCPGateway(registry=reg, jobs=JobStore(), client_factory=lambda: httpx.AsyncClient())
    resp = await mcp.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "regenerate_api", "arguments": {"wrapper_id": "nope"}}}
    )
    assert resp["result"]["isError"] is True
