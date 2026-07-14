"""Tests for generator pipeline, gateway, and deployment."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from openapi_anything.gateway.main import create_app
from openapi_anything.gateway.registry import Registry, WrapperEntry
from openapi_anything.generator.code_generator import CodeGenerator
from openapi_anything.generator.designer import APIDesign, EndpointSpec
from openapi_anything.generator.pipeline import PipelineOrchestrator
from openapi_anything.service import generate_and_deploy, undeploy


@pytest.fixture
def tmp_registry(tmp_path):
    return Registry(path=tmp_path / "registry.json")


@pytest.fixture
def cli_inspection():
    return {
        "type": "cli",
        "command": "ls",
        "help_text": "usage: ls [OPTION]... [FILE]...",
        "llm_analysis": "List directory contents",
        "suggested_endpoints": ["POST /execute"],
    }


@pytest.fixture
def cli_design():
    """Design that the LLM could plausibly return for an `ls` wrapper. Note the
    title, models and handler body are all consumed by the generator (B2/B4)."""
    return APIDesign(
        title="LS Wrapper",
        description="wrap the ls command as a REST API",
        target_type="cli",
        endpoints=[
            EndpointSpec(
                method="POST",
                path="/execute",
                description="Execute ls",
                request_model="ExecuteRequest",
                handler_code="return run_command(req.args)",
            )
        ],
        models={
            "ExecuteRequest": (
                "class ExecuteRequest(BaseModel):\n"
                "    args: list[str] = []"
            )
        },
        integration_notes="subprocess",
    )


def _import_app(wrapper_dir: Path):
    import importlib
    import sys

    sys.path.insert(0, str(wrapper_dir))
    try:
        if "app" in sys.modules:
            del sys.modules["app"]
        return importlib.import_module("app")
    finally:
        if str(wrapper_dir) in sys.path:
            sys.path.remove(str(wrapper_dir))
        if "app" in sys.modules:
            del sys.modules["app"]


def test_registry_persistence(tmp_path):
    reg_path = tmp_path / "registry.json"
    reg = Registry(path=reg_path)
    entry = WrapperEntry(
        id="test-wrapper",
        target_description="test",
        openapi_url="http://127.0.0.1:9001/openapi.json",
        service_url="http://127.0.0.1:9001",
        container_name="wrapper-test-wrapper",
        status="healthy",
        created_at="2026-01-01T00:00:00",
    )
    reg.register(entry)

    reg2 = Registry(path=reg_path)
    loaded = reg2.get("test-wrapper")
    assert loaded is not None
    assert loaded.service_url == "http://127.0.0.1:9001"
    # verification field defaults to None for legacy entries
    assert loaded.verification is None


def test_code_generator_consumes_design(tmp_path, cli_design, cli_inspection):
    """B2/B4: title/description/models/endpoints all come from the design."""
    gen = CodeGenerator()
    app_code = gen.generate_app_py(cli_design, cli_inspection, "wrap the ls command as a REST API", "wrapper-test")
    wrapper_dir = tmp_path / "wrapper-test"
    app_path = gen.write_wrapper_files(wrapper_dir, app_code)
    assert app_path.exists()
    assert (wrapper_dir / "Dockerfile").exists()
    assert (wrapper_dir / "test_app.py").exists()

    app_module = _import_app(wrapper_dir)
    client = TestClient(app_module.app)
    assert client.get("/health").status_code == 200
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    spec = openapi.json()
    # Title flows straight from the design (not hardcoded "Generated Wrapper")
    assert spec["info"]["title"] == "LS Wrapper"
    assert "/execute" in spec["paths"]
    # And the endpoint actually runs ls
    r = client.post("/execute", json={"args": ["-la", str(wrapper_dir)]})
    assert r.status_code == 200
    assert "app.py" in r.json()["stdout"]


def test_code_generator_safe_fallback(tmp_path, cli_design, cli_inspection):
    """Safe mode strips handler bodies but keeps the design's surface."""
    gen = CodeGenerator()
    code = gen.generate_app_py(cli_design, cli_inspection, "desc", "w", safe=True)
    wrapper_dir = tmp_path / "w"
    gen.write_wrapper_files(wrapper_dir, code)
    app_module = _import_app(wrapper_dir)
    client = TestClient(app_module.app)
    assert client.get("/health").status_code == 200
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"] == "LS Wrapper"
    assert "/execute" in spec["paths"]


def test_code_generator_req_without_model_guard(tmp_path, cli_inspection):
    """A handler that references `req` but declares no request_model must NOT
    emit a NameError-prone handler. The codegen swaps in the safe echo body so the
    endpoint returns 200 instead of 500 (regression for the degraded-wrapper bug)."""
    gen = CodeGenerator()
    broken = APIDesign(
        title="X",
        description="d",
        target_type="cli",
        endpoints=[
            EndpointSpec(
                method="GET",
                path="/listings",
                request_model=None,  # inconsistent: handler uses req
                handler_code='return {"e": req.path}',
            )
        ],
        models={},
    )
    code = gen.generate_app_py(broken, cli_inspection, "d", "w")
    assert "req.path" not in code  # safe body replaced the inconsistent handler
    wrapper_dir = tmp_path / "w"
    gen.write_wrapper_files(wrapper_dir, code, broken)
    app_module = _import_app(wrapper_dir)
    client = TestClient(app_module.app)
    r = client.get("/listings")
    assert r.status_code == 200, r.text  # not 500


def test_generated_test_exercises_endpoints(tmp_path, cli_design, cli_inspection):
    """The generated test_app.py calls each designed endpoint (so the fix loop can
    catch runtime handler errors, not just import/health issues)."""
    gen = CodeGenerator()
    code = gen.generate_app_py(cli_design, cli_inspection, "d", "w")
    wrapper_dir = tmp_path / "w"
    gen.write_wrapper_files(wrapper_dir, code, cli_design)
    test_code = (wrapper_dir / "test_app.py").read_text()
    assert "def test_endpoint_0" in test_code
    assert "/execute" in test_code
    assert "status_code < 500" in test_code


@pytest.mark.asyncio
async def test_pipeline_without_llm(tmp_path, cli_inspection, cli_design):
    mock_llm = MagicMock()
    mock_inspector = MagicMock()
    mock_inspector.inspect = AsyncMock(return_value=cli_inspection)
    mock_designer = MagicMock()
    mock_designer.design = AsyncMock(return_value=cli_design)

    orchestrator = PipelineOrchestrator(mock_llm, output_base=tmp_path / "wrappers")
    orchestrator.inspector = mock_inspector
    orchestrator.designer = mock_designer

    state = await orchestrator.run("wrap the ls command as a REST API", "wrapper-ls")
    assert state.status == "completed"
    assert state.generated_code_path is not None
    assert state.test_results["passed"] is True
    assert state.retries == 0  # deterministic design passed first try
    # pre-deploy verification captured the designed paths
    paths = state.docker_info["pre_deploy"]["paths"]
    assert "/execute" in paths


@pytest.mark.asyncio
async def test_pipeline_fix_loop_falls_back_to_safe(tmp_path, cli_inspection):
    """A design whose handler body is a syntax error must still complete via the
    fix loop -> deterministic safe fallback."""
    broken_design = APIDesign(
        title="Broken",
        description="d",
        target_type="cli",
        endpoints=[
            EndpointSpec(
                method="POST",
                path="/execute",
                request_model="ExecuteRequest",
                handler_code="this is not valid python !!!",
            )
        ],
        models={"ExecuteRequest": "class ExecuteRequest(BaseModel):\n    args: list[str] = []"},
    )
    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(side_effect=RuntimeError("no llm"))  # repair() falls back
    mock_inspector = MagicMock()
    mock_inspector.inspect = AsyncMock(return_value=cli_inspection)
    mock_designer = MagicMock()
    mock_designer.design = AsyncMock(return_value=broken_design)

    orchestrator = PipelineOrchestrator(mock_llm, output_base=tmp_path / "wrappers")
    orchestrator.inspector = mock_inspector
    orchestrator.designer = mock_designer

    state = await orchestrator.run("wrap the ls command as a REST API", "wrapper-broken")
    assert state.status == "completed"
    assert state.test_results["passed"] is True
    # Attempt 0 (deterministic, broken handler) failed; the fix loop recovered via the
    # safe fallback, so at least one retry was needed.
    assert state.retries >= 1


def test_gateway_health_and_registry():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_gateway_proxy_404():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/services/nonexistent/openapi.json")
    assert resp.status_code == 404


def _seed_singleton(reg: Registry):
    import openapi_anything.gateway.registry as registry_mod

    original = registry_mod._registry_singleton
    registry_mod._registry_singleton = reg
    return original


def test_gateway_wrapper_index(tmp_path):
    reg = Registry(path=tmp_path / "registry.json")
    entry = WrapperEntry(
        id="test-wrapper",
        target_description="test target",
        openapi_url="http://127.0.0.1:9001/openapi.json",
        service_url="http://127.0.0.1:9001",
        container_name="wrapper-test-wrapper",
        status="healthy",
        created_at="2026-01-01T00:00:00",
    )
    reg.register(entry)

    original = _seed_singleton(reg)
    try:
        app = create_app()
        client = TestClient(app)
        resp = client.get("/services/test-wrapper/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["wrapper_id"] == "test-wrapper"
        assert "openapi" in data["links"]
    finally:
        import openapi_anything.gateway.registry as registry_mod

        registry_mod._registry_singleton = original


@pytest.mark.asyncio
async def test_generate_and_deploy_local(tmp_path, cli_inspection, cli_design):
    registry = Registry(path=tmp_path / "registry.json")
    output_base = tmp_path / "wrappers"

    mock_llm = MagicMock()
    with (
        patch("openapi_anything.generator.llm_client.LLMClient", return_value=mock_llm),
        patch("openapi_anything.generator.pipeline.PipelineOrchestrator") as mock_pipeline_cls,
        patch("openapi_anything.docker.manager.DockerManager") as mock_docker_cls,
    ):
        mock_orchestrator = MagicMock()
        mock_state = MagicMock()
        mock_state.status = "completed"
        mock_state.current_phase = "verify"
        mock_state.errors = []
        mock_state.test_results = {"passed": True}
        mock_state.docker_info = {}
        mock_state.design = cli_design
        mock_orchestrator.run = AsyncMock(return_value=mock_state)
        mock_orchestrator.verify_deployed = AsyncMock(
            return_value={"overall": True, "openapi_title": "LS Wrapper"}
        )
        mock_pipeline_cls.return_value = mock_orchestrator

        mock_docker = MagicMock()
        mock_docker.deploy_wrapper = AsyncMock(return_value=("http://127.0.0.1:8123", 8123))
        mock_docker_cls.return_value = mock_docker

        result = await generate_and_deploy(
            "wrap the ls command as a REST API",
            registry,
            "wrapper-ls",
            output_base=output_base,
        )

    assert result.status == "deployed"
    assert result.service_url == "http://127.0.0.1:8123"
    entry = registry.get("wrapper-ls")
    assert entry is not None
    assert entry.status == "healthy"
    # Phase 7 verification persisted into the registry entry
    assert entry.verification == {"overall": True, "openapi_title": "LS Wrapper"}


@pytest.mark.asyncio
async def test_undeploy_removes_registry_entry(tmp_path):
    registry = Registry(path=tmp_path / "registry.json")
    entry = WrapperEntry(
        id="to-remove",
        target_description="x",
        openapi_url="http://127.0.0.1:9/openapi.json",
        service_url="http://127.0.0.1:9",
        container_name="wrapper-to-remove",
        status="healthy",
        created_at="2026-01-01T00:00:00",
    )
    registry.register(entry)

    with patch("openapi_anything.docker.manager.DockerManager") as MockDM:
        MockDM.return_value.stop_and_remove_wrapper = MagicMock(
            return_value={"container": "removed", "image": "removed"}
        )
        summary = await undeploy("to-remove", registry)

    assert summary["removed"] is True
    assert registry.get("to-remove") is None


@pytest.mark.asyncio
async def test_api_generate_json_endpoint(tmp_path):
    """B1: JSON POST /api/generate works for agent clients (async job contract)."""
    import httpx

    import openapi_anything.gateway.jobs as jobs_mod
    from openapi_anything.gateway.jobs import JobStore
    from openapi_anything.service import DeployResult

    reg = Registry(path=tmp_path / "registry.json")
    original = _seed_singleton(reg)
    original_jobs = jobs_mod._job_store_singleton
    store = JobStore()
    jobs_mod._job_store_singleton = store
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "openapi_anything.gateway.main.generate_and_deploy",
                new=AsyncMock(
                    return_value=DeployResult(
                        wrapper_id="w",
                        status="deployed",
                        service_url="http://x:1",
                        openapi_url="http://x:1/openapi.json",
                        verification={"overall": True},
                    )
                ),
            ):
                resp = await client.post(
                    "/api/generate",
                    json={"description": "wrap the ls command as a REST API", "wrapper_id": "w"},
                )
                assert resp.status_code == 202
                data = resp.json()
                assert data["status"] == "queued"
                assert data["wrapper_id"] == "w"
                await store.wait(data["job_id"])

            job = (await client.get(data["poll"])).json()
        assert job["status"] == "completed"
        assert job["result"]["openapi_path"] == "/services/w/openapi.json"
    finally:
        import openapi_anything.gateway.registry as registry_mod

        registry_mod._registry_singleton = original
        jobs_mod._job_store_singleton = original_jobs


def test_api_generate_json_rejects_form():
    """B1 regression: the hub form route must not swallow the JSON route."""
    app = create_app()
    client = TestClient(app)
    # JSON body to /api/generate parses; a non-JSON content type to /api/generate 422s
    resp = client.post("/api/generate", data={"description": "x"})
    assert resp.status_code == 422


def test_delete_wrapper_endpoint(tmp_path):
    """DELETE /services/{id} undeploys (routed before the catch-all proxy)."""
    reg = Registry(path=tmp_path / "registry.json")
    entry = WrapperEntry(
        id="del-me",
        target_description="x",
        openapi_url="http://127.0.0.1:9/openapi.json",
        service_url="http://127.0.0.1:9",
        container_name="wrapper-del-me",
        status="healthy",
        created_at="2026-01-01T00:00:00",
    )
    reg.register(entry)
    original = _seed_singleton(reg)
    try:
        app = create_app()
        client = TestClient(app)
        with patch("openapi_anything.gateway.main.undeploy", new=AsyncMock(
            return_value={"wrapper_id": "del-me", "removed": True, "lifecycle": {"container": "removed"}}
        )):
            resp = client.delete("/services/del-me")
        assert resp.status_code == 200
        assert resp.json()["removed"] is True
    finally:
        import openapi_anything.gateway.registry as registry_mod

        registry_mod._registry_singleton = original


def test_hub_delete_form_route_not_proxied(tmp_path):
    """POST /services/{id}/delete is handled by the gateway, not proxied."""
    reg = Registry(path=tmp_path / "registry.json")
    original = _seed_singleton(reg)
    try:
        app = create_app()
        client = TestClient(app)
        with patch("openapi_anything.gateway.main.undeploy", new=AsyncMock(
            return_value={"wrapper_id": "x", "removed": True, "lifecycle": {}}
        )) as mock_undeploy:
            resp = client.post("/services/x/delete", follow_redirects=False)
        assert resp.status_code in (303, 302, 307)
        mock_undeploy.assert_awaited_once()
    finally:
        import openapi_anything.gateway.registry as registry_mod

        registry_mod._registry_singleton = original


@pytest.mark.asyncio
async def test_designer_uses_json_mode_and_retries_on_empty(tmp_path):
    """The Designer must call complete_json (json_object mode), and must retry when
    the model returns empty content (the GLM-5.1 transient finish_reason=length quirk)
    before falling back to the default design."""
    from openapi_anything.generator.designer import Designer

    llm = MagicMock()
    # First two calls: empty (raises ValueError, mimicking the transient quirk).
    # Third call: a valid unique design.
    good = {
        "title": "Unique LS API",
        "description": "d",
        "target_type": "cli",
        "endpoints": [
            {"method": "POST", "path": "/execute", "request_model": "X",
             "handler_code": "return run_command(req.args)"}
        ],
        "models": {"X": "class X(BaseModel):\n    args: list[str] = []"},
    }
    llm.complete_json = AsyncMock(
        side_effect=[ValueError("empty"), ValueError("empty"), good]
    )
    llm.complete = AsyncMock(return_value="should-not-be-called")

    designer = Designer(llm)
    design = await designer.design(
        {"type": "cli", "command": "ls"}, "wrap the ls command as a REST API"
    )

    assert llm.complete_json.await_count == 3  # retried twice, succeeded on 3rd
    llm.complete.assert_not_awaited()  # must use json mode, not plain completion
    assert design.title == "Unique LS API"  # the LLM design, not the default fallback
    assert len(design.endpoints) == 1


@pytest.mark.asyncio
async def test_designer_falls_back_when_llm_unavailable():
    """When every JSON attempt fails, the Designer must return a usable default design."""
    from openapi_anything.generator.designer import Designer

    llm = MagicMock()
    llm.complete_json = AsyncMock(side_effect=RuntimeError("network down"))
    designer = Designer(llm)
    design = await designer.design(
        {"type": "web", "base_url": "https://httpbin.org"}, "wrap httpbin"
    )
    # default design for a web target
    assert design.title == "Web Wrapper"
    assert design.endpoints  # non-empty, usable
