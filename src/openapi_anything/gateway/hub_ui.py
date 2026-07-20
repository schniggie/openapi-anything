"""Web hub UI using Jinja2: list wrappers, submit new NL description, trigger generate."""

from dataclasses import asdict
from pathlib import Path

import urllib.parse
import uuid

import jinja2
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from openapi_anything.service import generate_and_deploy

from .jobs import get_job_store
from .registry import get_registry
from .secrets import get_secret_store


def _parse_secret_lines(raw: str) -> dict[str, str]:
    """Parse KEY=value lines (hub form) into a secrets dict; blanks ignored."""
    secrets: dict[str, str] = {}
    for line in (raw or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            secrets[key.strip()] = value.strip()
    return secrets

router = APIRouter()
template_dir = str(Path(__file__).parent / "templates")
env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(template_dir),
    autoescape=jinja2.select_autoescape(["html", "htm"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
Path(template_dir).mkdir(exist_ok=True)


def _render_hub(request: Request, wrappers: list, message: str | None = None) -> HTMLResponse:
    from .metrics import get_metrics_store

    template = env.get_template("hub.html")
    jobs = get_job_store()
    metrics = get_metrics_store()
    for w in wrappers:
        w["metrics"] = metrics.get(w["id"])
    html = template.render(
        request=request,
        wrappers=wrappers,
        jobs=[j.to_public() for j in jobs.list_all()],
        jobs_active=jobs.has_active(),
        message=message,
        title="openapi-anything Hub",
    )
    return HTMLResponse(html)


@router.get("/", response_class=HTMLResponse)
async def hub_home(
    request: Request, registry=Depends(get_registry), message: str | None = None
):
    wrappers = [asdict(w) for w in registry.list_all()]
    return _render_hub(request, wrappers, message=message)


@router.post("/generate")
async def trigger_generate(
    registry=Depends(get_registry),
    description: str = Form(...),
    wrapper_id: str = Form(None),
    secrets: str = Form(""),
):
    """Start generation as a background job and redirect back to the hub
    (Post/Redirect/Get: the hub's meta-refresh re-requests the current URL as GET,
    so rendering HTML directly at /generate would produce 405s on refresh)."""
    wid = wrapper_id or f"wrapper-{uuid.uuid4().hex[:8]}"
    secret_dict = _parse_secret_lines(secrets) or None
    if secret_dict:
        get_secret_store().set(wid, secret_dict)
    job = get_job_store().submit(
        description,
        wid,
        lambda report: generate_and_deploy(
            description, registry, wid, on_phase=report, secrets=secret_dict
        ),
    )
    message = f"Generation started as job {job.id} (wrapper {wid}). This page auto-refreshes."
    query = urllib.parse.urlencode({"message": message})
    return RedirectResponse(url=f"/?{query}", status_code=303)