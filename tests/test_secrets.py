"""Tests for target credentials: secrets flow into wrapper env, never into
generated code, job records, registry, or LLM prompts (names only)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from docker.errors import NotFound

from openapi_anything.docker.manager import DockerManager
from openapi_anything.gateway.jobs import JobStore
from openapi_anything.gateway.main import create_app
from openapi_anything.gateway.registry import Registry, WrapperEntry
from openapi_anything.gateway.secrets import SecretStore
from openapi_anything.service import DeployResult, generate_and_deploy

SECRETS = {"API_KEY": "sekret-value-123", "API_TOKEN": "tok-456"}


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}

    def ping(self):
        return True

    def hset(self, key, field=None, value=None, mapping=None):
        h = self.hashes.setdefault(key, {})
        if mapping:
            h.update(mapping)
        else:
            h[field] = value

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def delete(self, key):
        self.hashes.pop(key, None)


# ---------------------------------------------------------------- SecretStore


def test_secret_store_roundtrip_and_delete():
    store = SecretStore(redis_client=FakeRedis())
    store.set("w1", SECRETS)
    assert store.get("w1") == SECRETS
    assert store.names("w1") == ["API_KEY", "API_TOKEN"]
    store.delete("w1")
    assert store.get("w1") == {}


def test_secret_store_memory_fallback(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    store = SecretStore()
    store.set("w1", SECRETS)
    assert store.get("w1") == SECRETS


# ---------------------------------------------------------------- deploy env


@pytest.mark.asyncio
async def test_deploy_passes_environment(tmp_path):
    reg = Registry(path=tmp_path / "registry.json")
    with patch.object(DockerManager, "__init__", lambda self, r: None):
        mgr = DockerManager(reg)
    mgr.registry = reg
    mgr.client = MagicMock()
    mgr.client.containers.get.side_effect = NotFound("none")

    with patch.object(DockerManager, "_wait_for_health", new=AsyncMock(return_value=True)):
        await mgr.deploy_wrapper("w1", tmp_path, "desc", environment=SECRETS)

    assert mgr.client.containers.run.call_args.kwargs["environment"] == SECRETS


# ------------------------------------------------------- service + pipeline


@pytest.mark.asyncio
async def test_generate_and_deploy_threads_secrets(tmp_path):
    registry = Registry(path=tmp_path / "registry.json")
    with (
        patch("openapi_anything.generator.pipeline.PipelineOrchestrator") as MockPipe,
        patch("openapi_anything.docker.manager.DockerManager") as MockDM,
    ):
        state = MagicMock()
        state.status = "completed"
        state.design = None
        state.test_results = {"passed": True}
        state.current_phase = "verify"
        state.retries = 0
        orch = MagicMock()
        orch.run = AsyncMock(return_value=state)
        MockPipe.return_value = orch

        dm = MagicMock()
        dm.deploy_wrapper = AsyncMock(return_value=("http://x:1", 1))
        MockDM.return_value = dm

        await generate_and_deploy(
            "wrap X", registry, "w1", output_base=tmp_path, secrets=SECRETS
        )

    # pipeline sees NAMES only
    assert orch.run.await_args.kwargs["credential_env"] == ["API_KEY", "API_TOKEN"]
    # docker deploy sees the VALUES
    assert dm.deploy_wrapper.await_args.kwargs["environment"] == SECRETS
    # registry records names, never values
    entry = registry.get("w1")
    assert entry.secret_names == ["API_KEY", "API_TOKEN"]
    assert "sekret-value-123" not in json.dumps(
        {k: v for k, v in entry.__dict__.items()}
    )


@pytest.mark.asyncio
async def test_pipeline_injects_credential_names_into_inspection(tmp_path):
    from openapi_anything.generator.designer import APIDesign, EndpointSpec
    from openapi_anything.generator.pipeline import PipelineOrchestrator

    design = APIDesign(
        title="T",
        description="d",
        target_type="cli",
        endpoints=[
            EndpointSpec(
                method="POST",
                path="/execute",
                request_model="ExecuteRequest",
                handler_code="return run_command(req.args)",
            )
        ],
        models={"ExecuteRequest": "class ExecuteRequest(BaseModel):\n    args: list[str] = []"},
    )
    inspector = MagicMock()
    inspector.inspect = AsyncMock(return_value={"type": "cli", "command": "ls"})
    designer = MagicMock()
    designer.design = AsyncMock(return_value=design)

    orch = PipelineOrchestrator(MagicMock(), output_base=tmp_path / "w")
    orch.inspector = inspector
    orch.designer = designer

    state = await orch.run("wrap ls", "w", credential_env=["API_KEY"])
    assert state.status == "completed"
    inspection_passed = designer.design.await_args.args[0]
    assert inspection_passed["credential_env_vars"] == ["API_KEY"]


@pytest.mark.asyncio
async def test_designer_prompt_mentions_env_credentials():
    from openapi_anything.generator.designer import Designer

    llm = MagicMock()
    llm.complete_json = AsyncMock(
        return_value={
            "title": "T", "description": "d", "target_type": "web",
            "endpoints": [{"method": "GET", "path": "/x", "handler_code": "return {}"}],
            "models": {},
        }
    )
    designer = Designer(llm)
    await designer.design(
        {"type": "web", "base_url": "http://x", "credential_env_vars": ["API_KEY"]}, "d"
    )
    prompt = llm.complete_json.await_args.args[0]
    assert "os.getenv" in prompt
    assert "API_KEY" in prompt


def test_wrapper_template_imports_os():
    from pathlib import Path

    template = Path("src/openapi_anything/generator/code_templates/wrapper_template.py.j2")
    assert "import os" in template.read_text()


# ---------------------------------------------------------------- API layer


def _seed(tmp_path):
    import openapi_anything.gateway.jobs as jobs_mod
    import openapi_anything.gateway.registry as registry_mod
    import openapi_anything.gateway.secrets as secrets_mod

    originals = (
        registry_mod._registry_singleton,
        jobs_mod._job_store_singleton,
        secrets_mod._secret_store_singleton,
    )
    registry_mod._registry_singleton = Registry(path=tmp_path / "registry.json")
    store = JobStore()
    jobs_mod._job_store_singleton = store
    secret_store = SecretStore(redis_client=FakeRedis())
    secrets_mod._secret_store_singleton = secret_store
    return originals, store, secret_store


def _restore(originals):
    import openapi_anything.gateway.jobs as jobs_mod
    import openapi_anything.gateway.registry as registry_mod
    import openapi_anything.gateway.secrets as secrets_mod

    (
        registry_mod._registry_singleton,
        jobs_mod._job_store_singleton,
        secrets_mod._secret_store_singleton,
    ) = originals


@pytest.mark.asyncio
async def test_api_generate_accepts_secrets_and_stores_them(tmp_path):
    originals, store, secret_store = _seed(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            mock_gen = AsyncMock(return_value=DeployResult(wrapper_id="w", status="deployed"))
            with patch("openapi_anything.gateway.main.generate_and_deploy", new=mock_gen):
                resp = await client.post(
                    "/api/generate",
                    json={"description": "wrap X", "wrapper_id": "w", "secrets": SECRETS},
                )
                assert resp.status_code == 202
                job_id = resp.json()["job_id"]
                await store.wait(job_id)

            assert mock_gen.await_args.kwargs["secrets"] == SECRETS
            # stored for later regenerate
            assert secret_store.get("w") == SECRETS
            # job record never contains values
            job_json = json.dumps((await client.get(f"/jobs/{job_id}")).json())
            assert "sekret-value-123" not in job_json
    finally:
        _restore(originals)


@pytest.mark.asyncio
async def test_hub_form_parses_secrets_lines(tmp_path):
    """Hub generate form accepts KEY=value lines in a secrets field."""
    originals, store, secret_store = _seed(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            mock_gen = AsyncMock(return_value=DeployResult(wrapper_id="w", status="deployed"))
            with patch("openapi_anything.gateway.hub_ui.generate_and_deploy", new=mock_gen):
                resp = await client.post(
                    "/generate",
                    data={
                        "description": "wrap X",
                        "wrapper_id": "w",
                        "secrets": "API_KEY=sekret-value-123\n\nAPI_TOKEN = tok-456\n",
                    },
                )
                assert resp.status_code == 303
                await store.wait(store.list_all()[0].id)
            assert mock_gen.await_args.kwargs["secrets"] == SECRETS
            assert secret_store.get("w") == SECRETS
    finally:
        _restore(originals)


@pytest.mark.asyncio
async def test_regenerate_reuses_stored_secrets(tmp_path):
    originals, store, secret_store = _seed(tmp_path)
    try:
        import openapi_anything.gateway.registry as registry_mod

        registry_mod._registry_singleton.register(
            WrapperEntry(
                id="w",
                target_description="wrap X",
                openapi_url="http://x:1/openapi.json",
                service_url="http://x:1",
                container_name="wrapper-w",
                status="healthy",
                created_at="2026-01-01T00:00:00",
            )
        )
        secret_store.set("w", SECRETS)

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            mock_gen = AsyncMock(return_value=DeployResult(wrapper_id="w", status="deployed"))
            with patch("openapi_anything.gateway.main.generate_and_deploy", new=mock_gen):
                resp = await client.post("/services/w/_regenerate")
                assert resp.status_code == 202
                await store.wait(resp.json()["job_id"])
            assert mock_gen.await_args.kwargs["secrets"] == SECRETS
    finally:
        _restore(originals)
