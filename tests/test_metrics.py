"""Tests for per-wrapper traffic metrics collected at the gateway."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from openapi_anything.gateway.main import create_app
from openapi_anything.gateway.metrics import MetricsStore
from openapi_anything.gateway.registry import Registry, WrapperEntry


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}

    def ping(self):
        return True

    def hset(self, key, field=None, value=None, mapping=None):
        h = self.hashes.setdefault(key, {})
        if mapping:
            h.update({k: str(v) for k, v in mapping.items()})
        else:
            h[field] = str(value)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in self.hashes if k.startswith(prefix)]

    def delete(self, key):
        self.hashes.pop(key, None)


def _entry(wrapper_id: str = "w1") -> WrapperEntry:
    return WrapperEntry(
        id=wrapper_id,
        target_description="t",
        openapi_url="http://x:1/openapi.json",
        service_url="http://x:1",
        container_name=f"wrapper-{wrapper_id}",
        status="healthy",
        created_at="2026-01-01T00:00:00",
    )


# ---------------------------------------------------------------- store unit


def test_metrics_record_and_aggregate():
    store = MetricsStore(redis_client=None)
    store.record("w1", 200, 100.0)
    store.record("w1", 200, 300.0)
    store.record("w1", 502, 50.0)
    m = store.get("w1")
    assert m["requests"] == 3
    assert m["errors"] == 1
    assert m["avg_latency_ms"] == pytest.approx(150.0)
    assert m["last_used"] is not None


def test_metrics_unknown_wrapper_zeroes():
    store = MetricsStore(redis_client=None)
    m = store.get("nope")
    assert m["requests"] == 0
    assert m["errors"] == 0


def test_metrics_flush_and_reload_via_redis():
    fake = FakeRedis()
    store = MetricsStore(redis_client=fake)
    store.record("w1", 200, 120.0)
    store.record("w1", 500, 80.0)
    store.flush()

    reborn = MetricsStore(redis_client=fake)
    m = reborn.get("w1")
    assert m["requests"] == 2
    assert m["errors"] == 1
    assert m["avg_latency_ms"] == pytest.approx(100.0)


def test_metrics_remove():
    fake = FakeRedis()
    store = MetricsStore(redis_client=fake)
    store.record("w1", 200, 10.0)
    store.flush()
    store.remove("w1")
    assert store.get("w1")["requests"] == 0
    assert MetricsStore(redis_client=fake).get("w1")["requests"] == 0


# ---------------------------------------------------------------- gateway


def _seed(tmp_path):
    import openapi_anything.gateway.metrics as metrics_mod
    import openapi_anything.gateway.registry as registry_mod

    originals = (registry_mod._registry_singleton, metrics_mod._metrics_singleton)
    reg = Registry(path=tmp_path / "registry.json")
    reg.register(_entry())
    registry_mod._registry_singleton = reg
    store = MetricsStore(redis_client=None)
    metrics_mod._metrics_singleton = store
    return originals, store


def _restore(originals):
    import openapi_anything.gateway.metrics as metrics_mod
    import openapi_anything.gateway.registry as registry_mod

    registry_mod._registry_singleton, metrics_mod._metrics_singleton = originals


def test_proxy_route_records_metrics(tmp_path):
    from starlette.responses import JSONResponse

    from openapi_anything.gateway.proxy import GatewayProxy

    originals, store = _seed(tmp_path)
    try:
        app = create_app()
        client = TestClient(app)
        with patch.object(
            GatewayProxy,
            "proxy_request",
            new=AsyncMock(return_value=JSONResponse({"ok": True}, status_code=200)),
        ):
            assert client.get("/services/w1/foo").status_code == 200
            assert client.get("/services/w1/foo").status_code == 200
        m = store.get("w1")
        assert m["requests"] == 2
        assert m["errors"] == 0
        assert m["avg_latency_ms"] >= 0
    finally:
        _restore(originals)


def test_metrics_endpoint(tmp_path):
    originals, store = _seed(tmp_path)
    try:
        store.record("w1", 200, 42.0)
        app = create_app()
        client = TestClient(app)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert resp.json()["wrappers"]["w1"]["requests"] == 1
    finally:
        _restore(originals)


def test_hub_shows_traffic(tmp_path):
    originals, store = _seed(tmp_path)
    try:
        store.record("w1", 200, 42.0)
        app = create_app()
        client = TestClient(app)
        html = client.get("/").text
        assert "Traffic" in html
        assert "1 req" in html
    finally:
        _restore(originals)


@pytest.mark.asyncio
async def test_undeploy_clears_metrics(tmp_path):
    originals, store = _seed(tmp_path)
    try:
        store.record("w1", 200, 42.0)
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "openapi_anything.gateway.main.undeploy",
                new=AsyncMock(return_value={"wrapper_id": "w1", "removed": True, "lifecycle": {}}),
            ):
                resp = await client.delete("/services/w1")
                assert resp.status_code == 200
        assert store.get("w1")["requests"] == 0
    finally:
        _restore(originals)
