"""Tests for wrapper container resilience: restart policy + boot-time revive."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from docker.errors import NotFound

from openapi_anything.docker.manager import DockerManager
from openapi_anything.gateway.registry import Registry, WrapperEntry


def _entry(wrapper_id: str, status: str = "healthy") -> WrapperEntry:
    return WrapperEntry(
        id=wrapper_id,
        target_description="t",
        openapi_url="http://x:1/openapi.json",
        service_url="http://x:1",
        container_name=f"wrapper-{wrapper_id}",
        status=status,
        created_at="2026-01-01T00:00:00",
    )


def _manager_with_mock_client(registry) -> tuple[DockerManager, MagicMock]:
    with patch.object(DockerManager, "__init__", lambda self, reg: None):
        mgr = DockerManager(registry)
    mgr.registry = registry
    mgr.client = MagicMock()
    return mgr, mgr.client


@pytest.mark.asyncio
async def test_deploy_sets_restart_policy(tmp_path):
    reg = Registry(path=tmp_path / "registry.json")
    mgr, client = _manager_with_mock_client(reg)
    client.containers.get.side_effect = NotFound("none")

    with patch.object(DockerManager, "_wait_for_health", new=AsyncMock(return_value=True)):
        await mgr.deploy_wrapper("w1", tmp_path, "desc")

    run_kwargs = client.containers.run.call_args.kwargs
    assert run_kwargs["restart_policy"] == {"Name": "unless-stopped"}


def test_revive_starts_exited_registered_containers(tmp_path):
    reg = Registry(path=tmp_path / "registry.json")
    reg.register(_entry("w1"))
    reg.register(_entry("w2"))
    mgr, client = _manager_with_mock_client(reg)

    exited = MagicMock()
    exited.status = "exited"
    running = MagicMock()
    running.status = "running"
    client.containers.get.side_effect = lambda name: {
        "wrapper-w1": exited,
        "wrapper-w2": running,
    }[name]

    result = mgr.revive_registered()

    exited.start.assert_called_once()
    running.start.assert_not_called()
    assert result == {"w1": "started", "w2": "running"}


def test_revive_handles_missing_container_and_no_runtime(tmp_path):
    reg = Registry(path=tmp_path / "registry.json")
    reg.register(_entry("gone"))
    mgr, client = _manager_with_mock_client(reg)
    client.containers.get.side_effect = NotFound("gone")
    assert mgr.revive_registered() == {"gone": "container-missing"}

    mgr.client = None  # no runtime: no-op, no crash
    assert mgr.revive_registered() == {}


@pytest.mark.asyncio
async def test_gateway_lifespan_revives_wrappers(tmp_path):
    """Gateway startup must attempt to revive registered wrappers."""
    import openapi_anything.gateway.registry as registry_mod

    from openapi_anything.gateway.main import create_app

    reg = Registry(path=tmp_path / "registry.json")
    original = registry_mod._registry_singleton
    registry_mod._registry_singleton = reg
    try:
        with patch(
            "openapi_anything.gateway.main.revive_wrappers", new=MagicMock(return_value={})
        ) as mock_revive:
            app = create_app()
            async with app.router.lifespan_context(app):
                pass
        mock_revive.assert_called_once()
    finally:
        registry_mod._registry_singleton = original
