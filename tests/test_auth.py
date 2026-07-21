"""Tests for opt-in gateway auth: GATEWAY_API_KEY protects admin routes only.

Unset -> fully open (today's behavior). Set -> X-API-Key header or HTTP Basic
(browser-native, no custom login page) required on admin routes. Wrapper
traffic (/services/{id}/* proxy, its per-wrapper /mcp) and /health always
stay open, by explicit product decision (agents/other systems should be able
to use a deployed wrapper without holding the gateway's admin key).
"""

import base64

import httpx
import pytest

from openapi_anything.gateway.auth import require_auth


def _basic_header(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# ---------------------------------------------------------------- unit-level


@pytest.mark.asyncio
async def test_open_when_key_unset(monkeypatch):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    await require_auth(x_api_key=None, credentials=None)  # must not raise


@pytest.mark.asyncio
async def test_rejects_when_key_set_and_no_credentials(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("GATEWAY_API_KEY", "s3cret")
    with pytest.raises(HTTPException) as exc_info:
        await require_auth(x_api_key=None, credentials=None)
    assert exc_info.value.status_code == 401
    assert exc_info.value.headers is not None
    assert exc_info.value.headers.get("WWW-Authenticate") == "Basic"


@pytest.mark.asyncio
async def test_accepts_correct_api_key_header(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "s3cret")
    await require_auth(x_api_key="s3cret", credentials=None)  # must not raise


@pytest.mark.asyncio
async def test_rejects_wrong_api_key_header(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("GATEWAY_API_KEY", "s3cret")
    with pytest.raises(HTTPException) as exc_info:
        await require_auth(x_api_key="wrong", credentials=None)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_accepts_correct_basic_auth_password(monkeypatch):
    from fastapi.security import HTTPBasicCredentials

    monkeypatch.setenv("GATEWAY_API_KEY", "s3cret")
    creds = HTTPBasicCredentials(username="anything", password="s3cret")
    await require_auth(x_api_key=None, credentials=creds)  # must not raise


@pytest.mark.asyncio
async def test_rejects_wrong_basic_auth_password(monkeypatch):
    from fastapi import HTTPException
    from fastapi.security import HTTPBasicCredentials

    monkeypatch.setenv("GATEWAY_API_KEY", "s3cret")
    creds = HTTPBasicCredentials(username="anything", password="nope")
    with pytest.raises(HTTPException) as exc_info:
        await require_auth(x_api_key=None, credentials=creds)
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------- integration


def _seed_registry(tmp_path):
    import openapi_anything.gateway.registry as registry_mod
    from openapi_anything.gateway.registry import Registry

    original = registry_mod._registry_singleton
    registry_mod._registry_singleton = Registry(path=tmp_path / "registry.json")
    return original


def _restore_registry(original):
    import openapi_anything.gateway.registry as registry_mod

    registry_mod._registry_singleton = original


@pytest.mark.asyncio
async def test_admin_route_open_when_key_unset(tmp_path, monkeypatch):
    from openapi_anything.gateway.main import create_app

    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    original = _seed_registry(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/registry")
            assert resp.status_code == 200
    finally:
        _restore_registry(original)


@pytest.mark.asyncio
async def test_admin_route_requires_key_when_set(tmp_path, monkeypatch):
    from openapi_anything.gateway.main import create_app

    monkeypatch.setenv("GATEWAY_API_KEY", "s3cret")
    original = _seed_registry(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unauth = await client.get("/registry")
            assert unauth.status_code == 401

            via_header = await client.get("/registry", headers={"X-API-Key": "s3cret"})
            assert via_header.status_code == 200

            via_basic = await client.get("/registry", headers=_basic_header("x", "s3cret"))
            assert via_basic.status_code == 200

            wrong = await client.get("/registry", headers={"X-API-Key": "wrong"})
            assert wrong.status_code == 401
    finally:
        _restore_registry(original)


@pytest.mark.asyncio
async def test_hub_requires_key_when_set(tmp_path, monkeypatch):
    from openapi_anything.gateway.main import create_app

    monkeypatch.setenv("GATEWAY_API_KEY", "s3cret")
    original = _seed_registry(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/")).status_code == 401
            ok = await client.get("/", headers={"X-API-Key": "s3cret"})
            assert ok.status_code == 200
    finally:
        _restore_registry(original)


@pytest.mark.asyncio
async def test_gateway_mcp_requires_key_when_set(tmp_path, monkeypatch):
    from openapi_anything.gateway.main import create_app

    monkeypatch.setenv("GATEWAY_API_KEY", "s3cret")
    original = _seed_registry(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
            assert resp.status_code == 401
    finally:
        _restore_registry(original)


@pytest.mark.asyncio
async def test_health_always_open(tmp_path, monkeypatch):
    from openapi_anything.gateway.main import create_app

    monkeypatch.setenv("GATEWAY_API_KEY", "s3cret")
    original = _seed_registry(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200
    finally:
        _restore_registry(original)


@pytest.mark.asyncio
async def test_wrapper_proxy_and_wrapper_mcp_stay_open(tmp_path, monkeypatch):
    """Product decision: deployed-wrapper traffic never requires the admin key,
    even when GATEWAY_API_KEY is set — only gateway-owned admin routes do."""
    from openapi_anything.gateway.main import create_app
    from openapi_anything.gateway.registry import WrapperEntry

    monkeypatch.setenv("GATEWAY_API_KEY", "s3cret")
    original = _seed_registry(tmp_path)
    try:
        import openapi_anything.gateway.registry as registry_mod

        registry_mod._registry_singleton.register(
            WrapperEntry(
                id="w1",
                target_description="t",
                openapi_url="http://127.0.0.1:1/openapi.json",
                service_url="http://127.0.0.1:1",
                container_name="wrapper-w1",
                status="healthy",
                created_at="2026-01-01T00:00:00",
            )
        )
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # per-wrapper MCP: no auth required (no key -> 401 would mean it's
            # wrongly protected; a working JSON-RPC response proves it's open)
            mcp_resp = await client.post(
                "/services/w1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
            assert mcp_resp.status_code != 401

            # catch-all proxy: unreachable backend still proves auth wasn't the
            # blocker (502/503, never 401)
            proxy_resp = await client.get("/services/w1/anything")
            assert proxy_resp.status_code != 401
    finally:
        _restore_registry(original)
