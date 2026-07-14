"""Main FastAPI gateway app: registry, proxy routes under /services/{id}, hub UI,
JSON + form generate endpoints, and wrapper lifecycle (undeploy)."""

import asyncio
import contextlib
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from openapi_anything.service import generate_and_deploy, undeploy

from .health import run_sweeper
from .hub_ui import router as hub_router
from .jobs import JobStore, get_job_store
from .mcp import get_mcp
from .proxy import GatewayProxy
from .registry import Registry, get_registry


def revive_wrappers(registry: Registry) -> dict:
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
    async def lifespan(app: FastAPI):
        # Revive exited wrapper containers, then reconcile statuses continuously.
        outcome = await asyncio.to_thread(revive_wrappers, get_registry())
        if outcome:
            print(f"[gateway] wrapper revive: {outcome}")
        sweeper = asyncio.create_task(run_sweeper(get_registry()))
        yield
        sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweeper

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

    # ---- JSON API for agent clients (B1: distinct from the hub's form /generate) ----
    # Async by design: generation takes minutes, so the endpoint returns a job id
    # immediately and clients poll GET /jobs/{id} for the outcome.
    @app.post("/api/generate", status_code=202)
    async def generate_wrapper_api(
        req: GenerateRequest,
        reg: Registry = Depends(get_registry),
        jobs: JobStore = Depends(get_job_store),
    ):
        wrapper_id = req.wrapper_id or f"wrapper-{uuid.uuid4().hex[:8]}"
        job = jobs.submit(
            req.description,
            wrapper_id,
            lambda report: generate_and_deploy(
                req.description, reg, wrapper_id, on_phase=report
            ),
        )
        return {
            "job_id": job.id,
            "status": "queued",
            "wrapper_id": wrapper_id,
            "poll": f"/jobs/{job.id}",
        }

    @app.get("/jobs")
    async def list_jobs(jobs: JobStore = Depends(get_job_store)):
        return {"jobs": [j.to_public() for j in jobs.list_all()]}

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, jobs: JobStore = Depends(get_job_store)):
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
    ):
        _cancel_or_raise(job_id, jobs, reg)
        return {"job_id": job_id, "cancelled": True}

    @app.post("/jobs/{job_id}/cancel/form")
    async def cancel_job_form(
        job_id: str,
        jobs: JobStore = Depends(get_job_store),
        reg: Registry = Depends(get_registry),
    ):
        """Hub-friendly cancel (browser form), mirroring /services/{id}/delete."""
        _cancel_or_raise(job_id, jobs, reg)
        return RedirectResponse(url="/", status_code=303)

    @app.get("/services/{wrapper_id}")
    @app.get("/services/{wrapper_id}/")
    async def wrapper_index(wrapper_id: str, reg: Registry = Depends(get_registry)):
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
    async def delete_wrapper(wrapper_id: str, reg: Registry = Depends(get_registry)):
        summary = await undeploy(wrapper_id, reg)
        if not summary.get("removed"):
            raise HTTPException(404, summary.get("reason", "Wrapper not found"))
        return {"wrapper_id": wrapper_id, "removed": True, "lifecycle": summary["lifecycle"]}

    @app.post("/services/{wrapper_id}/delete")
    async def delete_wrapper_form(wrapper_id: str, reg: Registry = Depends(get_registry)):
        """Hub-friendly undeploy (browsers can't send DELETE from a form)."""
        await undeploy(wrapper_id, reg)
        return RedirectResponse(url="/", status_code=303)

    # ---- Regenerate: re-run the pipeline for an existing wrapper ----
    class RegenerateRequest(BaseModel):
        description: str | None = None

    def _submit_regenerate(
        wrapper_id: str, description: str | None, reg: Registry, jobs: JobStore
    ):
        entry = reg.get(wrapper_id)
        if not entry:
            raise HTTPException(404, "Wrapper not found")
        if jobs.active_for(wrapper_id):
            raise HTTPException(409, f"A job for {wrapper_id} is already running")
        desc = description or entry.target_description
        prior = {
            "previous_description": entry.target_description,
            "verification": entry.verification,
        }
        return jobs.submit(
            desc,
            wrapper_id,
            lambda report: generate_and_deploy(
                desc, reg, wrapper_id, on_phase=report, prior=prior
            ),
        )

    @app.post("/services/{wrapper_id}/_regenerate", status_code=202)
    async def regenerate_wrapper(
        wrapper_id: str,
        req: RegenerateRequest | None = None,
        reg: Registry = Depends(get_registry),
        jobs: JobStore = Depends(get_job_store),
    ):
        job = _submit_regenerate(wrapper_id, req.description if req else None, reg, jobs)
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
    ):
        """Hub-friendly regenerate (browser form), redirects back to the hub."""
        job = _submit_regenerate(wrapper_id, None, reg, jobs)
        message = f"Regeneration started as job {job.id} (wrapper {wrapper_id})."
        import urllib.parse

        return RedirectResponse(
            url=f"/?{urllib.parse.urlencode({'message': message})}", status_code=303
        )

    # ---- MCP export: wrappers as MCP tools (Streamable HTTP, stateless) ----
    async def _mcp_response(request: Request, wrapper_filter: str | None = None):
        body = await request.json()
        resp = await get_mcp().handle(body, wrapper_filter=wrapper_filter)
        if resp is None:  # notification
            return Response(status_code=202)
        return JSONResponse(resp)

    @app.post("/mcp")
    async def mcp_gateway(request: Request):
        """All wrappers as tools + meta tools (list_apis, generate_api, job_status)."""
        return await _mcp_response(request)

    @app.post("/services/{wrapper_id}/mcp")
    async def mcp_wrapper(wrapper_id: str, request: Request):
        """Single wrapper's endpoints as MCP tools (registered before the catch-all)."""
        return await _mcp_response(request, wrapper_filter=wrapper_id)

    # ---- Introspection: gateway-owned meta routes, registered BEFORE the ----
    # ---- catch-all so they are not proxied; the _ prefix keeps a wrapper's ----
    # ---- own /logs or /source endpoints reachable through the proxy. ----
    @app.get("/services/{wrapper_id}/_logs", response_class=PlainTextResponse)
    async def wrapper_logs(
        wrapper_id: str, tail: int = 100, reg: Registry = Depends(get_registry)
    ):
        if not reg.get(wrapper_id):
            raise HTTPException(404, "Wrapper not found")
        from openapi_anything.docker.manager import DockerManager

        try:
            return DockerManager(reg).get_logs(wrapper_id, tail=tail)
        except KeyError:
            raise HTTPException(404, "Container not found (wrapper may run locally)")

    @app.get("/services/{wrapper_id}/_source", response_class=PlainTextResponse)
    async def wrapper_source(wrapper_id: str, reg: Registry = Depends(get_registry)):
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
    ):
        return await proxy.proxy_request(wrapper_id, path, request)

    @app.get("/health")
    async def health():
        return {"status": "ok", "wrappers": len(registry.list_all())}

    @app.get("/registry")
    async def list_registry(reg: Registry = Depends(get_registry)):
        return {"wrappers": [e.__dict__ for e in reg.list_all()]}

    return app
