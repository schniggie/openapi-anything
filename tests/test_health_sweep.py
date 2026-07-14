"""Tests for the registry health sweep and the _logs/_source gateway endpoints."""

from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from openapi_anything.gateway.health import sweep_once
from openapi_anything.gateway.main import create_app
from openapi_anything.gateway.registry import Registry, WrapperEntry


def _entry(wrapper_id: str, status: str = "healthy", **kwargs) -> WrapperEntry:
    return WrapperEntry(
        id=wrapper_id,
        target_description="test",
        openapi_url="http://127.0.0.1:9001/openapi.json",
        service_url="http://127.0.0.1:9001",
        container_name=f"wrapper-{wrapper_id}",
        status=status,
        created_at="2026-01-01T00:00:00",
        **kwargs,
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------- sweep


@pytest.mark.asyncio
async def test_sweep_marks_dead_wrapper_unreachable(tmp_path):
    reg = Registry(path=tmp_path / "registry.json")
    reg.register(_entry("w1", status="healthy"))

    def handler(request):
        raise httpx.ConnectError("connection refused")

    async with _client(handler) as client:
        result = await sweep_once(reg, client)

    assert result == {"w1": "unreachable"}
    assert reg.get("w1").status == "unreachable"


@pytest.mark.asyncio
async def test_sweep_recovers_wrapper_to_healthy(tmp_path):
    reg = Registry(path=tmp_path / "registry.json")
    reg.register(_entry("w1", status="unreachable"))

    def handler(request):
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok"})

    async with _client(handler) as client:
        result = await sweep_once(reg, client)

    assert result == {"w1": "healthy"}
    assert reg.get("w1").status == "healthy"


@pytest.mark.asyncio
async def test_sweep_preserves_degraded_verdict(tmp_path):
    """A responding wrapper that failed post-deploy verification stays degraded."""
    reg = Registry(path=tmp_path / "registry.json")
    reg.register(_entry("w1", status="degraded"))

    def handler(request):
        return httpx.Response(200, json={"status": "ok"})

    async with _client(handler) as client:
        result = await sweep_once(reg, client)

    assert result == {"w1": "degraded"}
    assert reg.get("w1").status == "degraded"


# ------------------------------------------------------------ _logs / _source


def _seed_registry(reg: Registry):
    import openapi_anything.gateway.registry as registry_mod

    original = registry_mod._registry_singleton
    registry_mod._registry_singleton = reg
    return original


def _restore_registry(original):
    import openapi_anything.gateway.registry as registry_mod

    registry_mod._registry_singleton = original


def test_logs_endpoint_returns_container_logs(tmp_path):
    reg = Registry(path=tmp_path / "registry.json")
    reg.register(_entry("w1"))
    original = _seed_registry(reg)
    try:
        app = create_app()
        client = TestClient(app)
        with patch("openapi_anything.docker.manager.DockerManager") as MockDM:
            MockDM.return_value.get_logs = MagicMock(return_value="INFO: uvicorn running\n")
            resp = client.get("/services/w1/_logs?tail=50")
        assert resp.status_code == 200
        assert "uvicorn running" in resp.text
        MockDM.return_value.get_logs.assert_called_once_with("w1", tail=50)
    finally:
        _restore_registry(original)


def test_logs_endpoint_404s(tmp_path):
    reg = Registry(path=tmp_path / "registry.json")
    reg.register(_entry("w1"))
    original = _seed_registry(reg)
    try:
        app = create_app()
        client = TestClient(app)
        # unknown wrapper
        assert client.get("/services/nope/_logs").status_code == 404
        # known wrapper, container gone
        with patch("openapi_anything.docker.manager.DockerManager") as MockDM:
            MockDM.return_value.get_logs = MagicMock(side_effect=KeyError("w1"))
            assert client.get("/services/w1/_logs").status_code == 404
    finally:
        _restore_registry(original)


def test_source_endpoint_serves_generated_app(tmp_path):
    wrapper_dir = tmp_path / "w1"
    wrapper_dir.mkdir()
    (wrapper_dir / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")

    reg = Registry(path=tmp_path / "registry.json")
    reg.register(_entry("w1", wrapper_dir=str(wrapper_dir)))
    original = _seed_registry(reg)
    try:
        app = create_app()
        client = TestClient(app)
        resp = client.get("/services/w1/_source")
        assert resp.status_code == 200
        assert "FastAPI()" in resp.text
    finally:
        _restore_registry(original)


def test_source_endpoint_404_when_dir_missing(tmp_path):
    reg = Registry(path=tmp_path / "registry.json")
    reg.register(_entry("w1"))  # legacy entry: no wrapper_dir
    original = _seed_registry(reg)
    try:
        app = create_app()
        client = TestClient(app)
        resp = client.get("/services/w1/_source")
        assert resp.status_code == 404
    finally:
        _restore_registry(original)


def test_registry_loads_legacy_entries_without_wrapper_dir(tmp_path):
    """registry.json written before the wrapper_dir field must still load."""
    path = tmp_path / "registry.json"
    path.write_text(
        '{"wrappers": [{"id": "old", "target_description": "x", '
        '"openapi_url": "http://x/openapi.json", "service_url": "http://x", '
        '"container_name": "wrapper-old", "status": "healthy", '
        '"created_at": "2026-01-01T00:00:00"}]}'
    )
    reg = Registry(path=path)
    entry = reg.get("old")
    assert entry is not None
    assert entry.wrapper_dir is None
