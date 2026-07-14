"""Tests for async generation jobs: JobStore, /api/generate 202 flow, /jobs endpoints."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from openapi_anything.gateway.jobs import JobStore
from openapi_anything.gateway.main import create_app
from openapi_anything.gateway.registry import Registry
from openapi_anything.service import DeployResult


def _deployed(wrapper_id: str = "w") -> DeployResult:
    return DeployResult(
        wrapper_id=wrapper_id,
        status="deployed",
        service_url="http://x:1",
        openapi_url="http://x:1/openapi.json",
        verification={"overall": True},
    )


def _failed(wrapper_id: str = "w") -> DeployResult:
    return DeployResult(wrapper_id=wrapper_id, status="failed", errors=["pipeline exploded"])


# ---------------------------------------------------------------- JobStore unit


@pytest.mark.asyncio
async def test_jobstore_completes_successful_run():
    store = JobStore()

    async def runner(report):
        return _deployed()

    job = store.submit("wrap ls", "w", runner)
    assert job.status == "queued"
    await store.wait(job.id)
    job = store.get(job.id)
    assert job.status == "completed"
    assert job.result["status"] == "deployed"
    assert job.result["openapi_path"] == "/services/w/openapi.json"
    assert job.finished_at is not None


@pytest.mark.asyncio
async def test_jobstore_records_failed_result():
    store = JobStore()

    async def runner(report):
        return _failed()

    job = store.submit("wrap ls", "w", runner)
    await store.wait(job.id)
    job = store.get(job.id)
    assert job.status == "failed"
    assert "pipeline exploded" in job.error


@pytest.mark.asyncio
async def test_jobstore_records_exception():
    store = JobStore()

    async def runner(report):
        raise RuntimeError("docker daemon gone")

    job = store.submit("wrap ls", "w", runner)
    await store.wait(job.id)
    job = store.get(job.id)
    assert job.status == "failed"
    assert "docker daemon gone" in job.error


def test_jobstore_get_unknown_returns_none():
    assert JobStore().get("nope") is None


@pytest.mark.asyncio
async def test_jobstore_list_newest_first():
    store = JobStore()

    async def runner(report):
        return _deployed()

    j1 = store.submit("a", "w1", runner)
    j2 = store.submit("b", "w2", runner)
    await store.wait(j1.id)
    await store.wait(j2.id)
    ids = [j.id for j in store.list_all()]
    assert ids == [j2.id, j1.id]


# ------------------------------------------------------------- phases + cancel


@pytest.mark.asyncio
async def test_jobstore_reports_phases():
    """The runner receives a report callback; each call updates job.phase."""
    store = JobStore()

    async def runner(report):
        report("inspect")
        report("design")
        return _deployed()

    job = store.submit("wrap ls", "w", runner)
    assert job.phase is None
    await store.wait(job.id)
    job = store.get(job.id)
    assert job.phase == "design"
    assert job.status == "completed"


@pytest.mark.asyncio
async def test_jobstore_cancel_running_job():
    import asyncio

    store = JobStore()
    started = asyncio.Event()

    async def runner(report):
        report("inspect")
        started.set()
        await asyncio.sleep(60)  # simulates a long pipeline
        return _deployed()

    job = store.submit("wrap ls", "w", runner)
    await started.wait()
    assert store.cancel(job.id) is True
    await store.wait(job.id)
    job = store.get(job.id)
    assert job.status == "cancelled"
    assert "cancelled" in job.error
    assert job.finished_at is not None


def test_jobstore_cancel_unknown_returns_false():
    assert JobStore().cancel("nope") is False


@pytest.mark.asyncio
async def test_jobstore_cancel_terminal_returns_false():
    store = JobStore()

    async def runner(report):
        return _deployed()

    job = store.submit("wrap ls", "w", runner)
    await store.wait(job.id)
    assert store.cancel(job.id) is False  # already completed


@pytest.mark.asyncio
async def test_pipeline_reports_phases(tmp_path):
    """PipelineOrchestrator.run(on_phase=...) reports each phase transition."""
    from unittest.mock import MagicMock

    from openapi_anything.generator.pipeline import PipelineOrchestrator

    cli_inspection = {"type": "cli", "command": "ls", "suggested_endpoints": ["POST /execute"]}
    from openapi_anything.generator.designer import APIDesign, EndpointSpec

    design = APIDesign(
        title="LS",
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
    mock_inspector = MagicMock()
    mock_inspector.inspect = AsyncMock(return_value=cli_inspection)
    mock_designer = MagicMock()
    mock_designer.design = AsyncMock(return_value=design)

    orchestrator = PipelineOrchestrator(MagicMock(), output_base=tmp_path / "wrappers")
    orchestrator.inspector = mock_inspector
    orchestrator.designer = mock_designer

    phases: list[str] = []
    state = await orchestrator.run("wrap ls", "w", on_phase=phases.append)
    assert state.status == "completed"
    assert phases == ["inspect", "design", "generate", "verify"]


# ---------------------------------------------------------------- API endpoints


def _seed(tmp_path):
    """Fresh registry + job store singletons; returns (originals, store)."""
    import openapi_anything.gateway.jobs as jobs_mod
    import openapi_anything.gateway.registry as registry_mod

    orig_reg = registry_mod._registry_singleton
    orig_jobs = jobs_mod._job_store_singleton
    registry_mod._registry_singleton = Registry(path=tmp_path / "registry.json")
    store = JobStore()
    jobs_mod._job_store_singleton = store
    return (orig_reg, orig_jobs), store


def _restore(originals):
    import openapi_anything.gateway.jobs as jobs_mod
    import openapi_anything.gateway.registry as registry_mod

    registry_mod._registry_singleton, jobs_mod._job_store_singleton = originals


@pytest.mark.asyncio
async def test_api_generate_returns_job_and_completes(tmp_path):
    originals, store = _seed(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "openapi_anything.gateway.main.generate_and_deploy",
                new=AsyncMock(return_value=_deployed()),
            ):
                resp = await client.post(
                    "/api/generate", json={"description": "wrap ls", "wrapper_id": "w"}
                )
                assert resp.status_code == 202
                data = resp.json()
                assert data["status"] == "queued"
                assert data["wrapper_id"] == "w"
                assert data["poll"] == f"/jobs/{data['job_id']}"

                await store.wait(data["job_id"])

            resp = await client.get(f"/jobs/{data['job_id']}")
            assert resp.status_code == 200
            job = resp.json()
            assert job["status"] == "completed"
            assert job["result"]["openapi_path"] == "/services/w/openapi.json"
    finally:
        _restore(originals)


@pytest.mark.asyncio
async def test_api_generate_failure_visible_in_job(tmp_path):
    originals, store = _seed(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "openapi_anything.gateway.main.generate_and_deploy",
                new=AsyncMock(return_value=_failed()),
            ):
                resp = await client.post("/api/generate", json={"description": "wrap ls"})
                job_id = resp.json()["job_id"]
                await store.wait(job_id)

            resp = await client.get(f"/jobs/{job_id}")
            job = resp.json()
            assert job["status"] == "failed"
            assert "pipeline exploded" in job["error"]
    finally:
        _restore(originals)


@pytest.mark.asyncio
async def test_get_unknown_job_404(tmp_path):
    originals, _ = _seed(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/jobs/nope")
            assert resp.status_code == 404
    finally:
        _restore(originals)


@pytest.mark.asyncio
async def test_jobs_list_endpoint(tmp_path):
    originals, store = _seed(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "openapi_anything.gateway.main.generate_and_deploy",
                new=AsyncMock(return_value=_deployed()),
            ):
                resp = await client.post("/api/generate", json={"description": "wrap ls"})
                await store.wait(resp.json()["job_id"])

            resp = await client.get("/jobs")
            assert resp.status_code == 200
            jobs = resp.json()["jobs"]
            assert len(jobs) == 1
            assert jobs[0]["status"] == "completed"
    finally:
        _restore(originals)


@pytest.mark.asyncio
async def test_cancel_job_endpoint(tmp_path):
    import asyncio

    originals, store = _seed(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            slow = asyncio.Event()

            async def never_finishes(*args, **kwargs):
                slow.set()
                await asyncio.sleep(60)

            with patch(
                "openapi_anything.gateway.main.generate_and_deploy", new=never_finishes
            ):
                resp = await client.post("/api/generate", json={"description": "wrap ls"})
                job_id = resp.json()["job_id"]
                await slow.wait()

                resp = await client.post(f"/jobs/{job_id}/cancel")
                assert resp.status_code == 200
                assert resp.json()["cancelled"] is True
                await store.wait(job_id)

            job = (await client.get(f"/jobs/{job_id}")).json()
            assert job["status"] == "cancelled"

            # terminal job → 409; unknown job → 404
            resp = await client.post(f"/jobs/{job_id}/cancel")
            assert resp.status_code == 409
            resp = await client.post("/jobs/nope/cancel")
            assert resp.status_code == 404
    finally:
        _restore(originals)


@pytest.mark.asyncio
async def test_job_phase_exposed_in_api(tmp_path):
    originals, store = _seed(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

            async def with_phases(*args, on_phase=None, **kwargs):
                if on_phase:
                    on_phase("inspect")
                    on_phase("deploy")
                return _deployed()

            with patch(
                "openapi_anything.gateway.main.generate_and_deploy", new=with_phases
            ):
                resp = await client.post("/api/generate", json={"description": "wrap ls"})
                job_id = resp.json()["job_id"]
                await store.wait(job_id)

            job = (await client.get(f"/jobs/{job_id}")).json()
            assert job["phase"] == "deploy"
    finally:
        _restore(originals)


@pytest.mark.asyncio
async def test_hub_form_starts_job_async(tmp_path):
    """The hub form must return immediately (job started), not block on generation.

    Must redirect (Post/Redirect/Get): rendering HTML directly at /generate breaks
    the hub's meta-refresh, which re-requests the current URL as GET /generate → 405.
    """
    originals, store = _seed(tmp_path)
    try:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "openapi_anything.gateway.hub_ui.generate_and_deploy",
                new=AsyncMock(return_value=_deployed()),
            ):
                resp = await client.post("/generate", data={"description": "wrap ls"})
                assert resp.status_code == 303
                location = resp.headers["location"]
                assert location.startswith("/")
                follow = await client.get(location)
                assert follow.status_code == 200
                assert "job" in follow.text.lower()
                assert len(store.list_all()) == 1
                await store.wait(store.list_all()[0].id)
            assert store.list_all()[0].status == "completed"
    finally:
        _restore(originals)
