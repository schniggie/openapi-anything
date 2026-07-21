"""Main FastAPI gateway app: registry, proxy routes under /services/{id}, hub UI,
JSON + form generate endpoints, and wrapper lifecycle (undeploy)."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from openapi_anything.service import generate_and_deploy, undeploy

from .health import run_sweeper
from .hub_ui import router as hub_router
from .jobs import Job, JobStore, get_job_store
from .mcp import get_mcp
from .metrics import get_metrics_store, metrics_flush_interval
from .proxy import GatewayProxy
from .registry import Registry, get_registry
from .secrets import get_secret_store


def revive_wrappers(registry: Registry) -> dict[str, str]:
    """Best-effort start of registered wrapper containers that are not running
    (host reboots leave them exited under rootless podman)."""
    from openapi_anything.docker.manager import DockerManager

    try:
        return DockerManager(registry).revive_registered()
    except Exception as exc:
        print(f"[gateway] wrapper revive failed: {exc}")
        return {}


def create_app() -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Revive exited wrapper containers, then reconcile statuses continuously.
        outcome = await asyncio.to_thread(revive_wrappers, get_registry())
        if outcome:
            print(f"[gateway] wrapper revive: {outcome}")
        sweeper = asyncio.create_task(run_sweeper(get_registry()))

        async def _flush_metrics_forever() -> None:
            while True:
                await asyncio.sleep(metrics_flush_interval())
                get_metrics_store().flush()

        flusher = asyncio.create_task(_flush_metrics_forever())
        yield
        for task in (sweeper, flusher):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        get_metrics_store().flush()  # final write-behind on shutdown

    app = FastAPI(
        title="openapi-anything Gateway",
        description="Central hub and proxy for dynamically generated per-target REST wrappers",
        version="0.1.0",
        lifespan=lifespan,
    )

    registry = get_registry()
    proxy = GatewayProxy()

    # Hub UI (HTML). Registered first so `GET /` and the form `POST /generate` exist.
    app.include_router(hub_router, prefix="", tags=["hub"])

    class GenerateRequest(BaseModel):
        description: str
        wrapper_id: str | None = None
        # Target credentials -> wrapper container env vars. Values are kept in the
        # SecretStore + container env only; the LLM sees the names.
        secrets: dict[str, str] | None = None

    # ---- JSON API for agent clients (B1: distinct from the hub's form /generate) ----
    # Async by design: generation takes minutes, so the endpoint returns a job id
    # immediately and clients poll GET /jobs/{id} for the outcome.
    @app.post("/api/generate", status_code=202)
    async def generate_wrapper_api(
        req: GenerateRequest,
        reg: Registry = Depends(get_registry),
        jobs: JobStore = Depends(get_job_store),
    ) -> dict[str, Any]:
        wrapper_id = req.wrapper_id or f"wrapper-{uuid.uuid4().hex[:8]}"
        secrets = req.secrets or None
        if secrets:
            get_secret_store().set(wrapper_id, secrets)
        job = jobs.submit(
            req.description,
            wrapper_id,
            lambda report: generate_and_deploy(
                req.description, reg, wrapper_id, on_phase=report, secrets=secrets
            ),
        )
        return {
            "job_id": job.id,
            "status": "queued",
            "wrapper_id": wrapper_id,
            "poll": f"/jobs/{job.id}",
        }

    @app.get("/jobs")
    async def list_jobs(jobs: JobStore = Depends(get_job_store)) -> dict[str, Any]:
        return {"jobs": [j.to_public() for j in jobs.list_all()]}

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, jobs: JobStore = Depends(get_job_store)) -> dict[str, Any]:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job.to_public()

    def _cancel_or_raise(job_id: str, jobs: JobStore, reg: Registry) -> None:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if not jobs.cancel(job_id):
            raise HTTPException(409, f"Job already {job.status}")
        # Best-effort cleanup: a cancel mid-deploy can leave a container behind.
        try:
            from openapi_anything.docker.manager import DockerManager

            DockerManager(reg).stop_and_remove_wrapper(job.wrapper_id)
        except Exception:
            pass

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(
        job_id: str,
        jobs: JobStore = Depends(get_job_store),
        reg: Registry = Depends(get_registry),
    ) -> dict[str, Any]:
        _cancel_or_raise(job_id, jobs, reg)
        return {"job_id": job_id, "cancelled": True}

    @app.post("/jobs/{job_id}/cancel/form")
    async def cancel_job_form(
        job_id: str,
        jobs: JobStore = Depends(get_job_store),
        reg: Registry = Depends(get_registry),
    ) -> RedirectResponse:
        """Hub-friendly cancel (browser form), mirroring /services/{id}/delete."""
        _cancel_or_raise(job_id, jobs, reg)
        return RedirectResponse(url="/", status_code=303)

    @app.get("/services/{wrapper_id}")
    @app.get("/services/{wrapper_id}/")
    async def wrapper_index(wrapper_id: str, reg: Registry = Depends(get_registry)) -> dict[str, Any]:
        entry = reg.get(wrapper_id)
        if not entry:
            raise HTTPException(404, "Wrapper not found")
        return {
            "wrapper_id": wrapper_id,
            "target_description": entry.target_description,
            "status": entry.status,
            "verification": entry.verification,
            "links": {
                "openapi": f"/services/{wrapper_id}/openapi.json",
                "health": f"/services/{wrapper_id}/health",
                "docs": f"/services/{wrapper_id}/docs",
                "mcp": f"/services/{wrapper_id}/mcp (POST, JSON-RPC)",
                "undeploy": f"/services/{wrapper_id} (DELETE)",
            },
        }

    # ---- Lifecycle: must be registered BEFORE the catch-all proxy below ----
    @app.delete("/services/{wrapper_id}")
    async def delete_wrapper(
        wrapper_id: str, reg: Registry = Depends(get_registry)
    ) -> dict[str, Any]:
        summary = await undeploy(wrapper_id, reg)
        if not summary.get("removed"):
            raise HTTPException(404, summary.get("reason", "Wrapper not found"))
        get_secret_store().delete(wrapper_id)
        get_metrics_store().remove(wrapper_id)
        return {"wrapper_id": wrapper_id, "removed": True, "lifecycle": summary["lifecycle"]}

    @app.post("/services/{wrapper_id}/delete")
    async def delete_wrapper_form(
        wrapper_id: str, reg: Registry = Depends(get_registry)
    ) -> RedirectResponse:
        """Hub-friendly undeploy (browsers can't send DELETE from a form)."""
        await undeploy(wrapper_id, reg)
        return RedirectResponse(url="/", status_code=303)

    # ---- Regenerate: re-run the pipeline for an existing wrapper ----
    class RegenerateRequest(BaseModel):
        description: str | None = None
        secrets: dict[str, str] | None = None  # override; else stored secrets reused

    def _submit_regenerate(
        wrapper_id: str,
        description: str | None,
        reg: Registry,
        jobs: JobStore,
        secrets: dict[str, str] | None = None,
    ) -> Job:
        entry = reg.get(wrapper_id)
        if not entry:
            raise HTTPException(404, "Wrapper not found")
        if jobs.active_for(wrapper_id):
            raise HTTPException(409, f"A job for {wrapper_id} is already running")
        desc = description or entry.target_description
        store = get_secret_store()
        if secrets:
            store.set(wrapper_id, secrets)
        else:
            secrets = store.get(wrapper_id) or None
        prior = {
            "previous_description": entry.target_description,
            "verification": entry.verification,
        }
        return jobs.submit(
            desc,
            wrapper_id,
            lambda report: generate_and_deploy(
                desc, reg, wrapper_id, on_phase=report, prior=prior, secrets=secrets
            ),
        )

    @app.post("/services/{wrapper_id}/_regenerate", status_code=202)
    async def regenerate_wrapper(
        wrapper_id: str,
        req: RegenerateRequest | None = None,
        reg: Registry = Depends(get_registry),
        jobs: JobStore = Depends(get_job_store),
    ) -> dict[str, Any]:
        job = _submit_regenerate(
            wrapper_id,
            req.description if req else None,
            reg,
            jobs,
            secrets=req.secrets if req else None,
        )
        return {
            "job_id": job.id,
            "status": "queued",
            "wrapper_id": wrapper_id,
            "poll": f"/jobs/{job.id}",
        }

    @app.post("/services/{wrapper_id}/_regenerate/form")
    async def regenerate_wrapper_form(
        wrapper_id: str,
        reg: Registry = Depends(get_registry),
        jobs: JobStore = Depends(get_job_store),
    ) -> RedirectResponse:
        """Hub-friendly regenerate (browser form), redirects back to the hub."""
        job = _submit_regenerate(wrapper_id, None, reg, jobs)
        message = f"Regeneration started as job {job.id} (wrapper {wrapper_id})."
        import urllib.parse

        return RedirectResponse(
            url=f"/?{urllib.parse.urlencode({'message': message})}", status_code=303
        )

    # ---- MCP export: wrappers as MCP tools (Streamable HTTP, stateless) ----
    async def _mcp_response(request: Request, wrapper_filter: str | None = None) -> Response:
        body = await request.json()
        resp = await get_mcp().handle(body, wrapper_filter=wrapper_filter)
        if resp is None:  # notification
            return Response(status_code=202)
        return JSONResponse(resp)

    @app.post("/mcp")
    async def mcp_gateway(request: Request) -> Response:
        """All wrappers as tools + meta tools (list_apis, generate_api, job_status)."""
        return await _mcp_response(request)

    @app.post("/services/{wrapper_id}/mcp")
    async def mcp_wrapper(wrapper_id: str, request: Request) -> Response:
        """Single wrapper's endpoints as MCP tools (registered before the catch-all)."""
        return await _mcp_response(request, wrapper_filter=wrapper_id)

    # ---- Introspection: gateway-owned meta routes, registered BEFORE the ----
    # ---- catch-all so they are not proxied; the _ prefix keeps a wrapper's ----
    # ---- own /logs or /source endpoints reachable through the proxy. ----
    @app.get("/services/{wrapper_id}/_logs", response_class=PlainTextResponse)
    async def wrapper_logs(
        wrapper_id: str, tail: int = 100, reg: Registry = Depends(get_registry)
    ) -> str:
        if not reg.get(wrapper_id):
            raise HTTPException(404, "Wrapper not found")
        from openapi_anything.docker.manager import DockerManager

        try:
            return DockerManager(reg).get_logs(wrapper_id, tail=tail)
        except KeyError:
            raise HTTPException(404, "Container not found (wrapper may run locally)")

    @app.get("/services/{wrapper_id}/_source", response_class=PlainTextResponse)
    async def wrapper_source(wrapper_id: str, reg: Registry = Depends(get_registry)) -> str:
        entry = reg.get(wrapper_id)
        if not entry:
            raise HTTPException(404, "Wrapper not found")
        app_py = Path(entry.wrapper_dir) / "app.py" if entry.wrapper_dir else None
        if app_py is None or not app_py.exists():
            raise HTTPException(
                404,
                "Generated source not available (wrapper predates source tracking "
                "or the generation directory was cleaned up)",
            )
        return app_py.read_text()

    # ---- Catch-all proxy: any other /services/{id}/* -> backend container ----
    @app.api_route(
        "/services/{wrapper_id}/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    )
    async def proxy_to_wrapper(
        wrapper_id: str,
        path: str,
        request: Request,
    ) -> Response:
        import time

        start = time.monotonic()
        try:
            response = await proxy.proxy_request(wrapper_id, path, request)
        except HTTPException as exc:
            if exc.status_code != 404:  # unknown wrappers aren't traffic
                get_metrics_store().record(
                    wrapper_id, exc.status_code, (time.monotonic() - start) * 1000
                )
            raise
        get_metrics_store().record(
            wrapper_id, response.status_code, (time.monotonic() - start) * 1000
        )
        return response

    @app.get("/metrics")
    async def metrics() -> dict[str, Any]:
        return {"wrappers": get_metrics_store().all()}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "wrappers": len(registry.list_all())}

    @app.get("/registry")
    async def list_registry(reg: Registry = Depends(get_registry)) -> dict[str, Any]:
        return {"wrappers": [e.__dict__ for e in reg.list_all()]}

    return app
