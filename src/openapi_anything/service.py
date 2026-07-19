"""Shared generate-and-deploy orchestration used by CLI, API, and hub UI."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openapi_anything.gateway.registry import Registry, WrapperEntry


@dataclass
class DeployResult:
    wrapper_id: str
    status: str
    service_url: str | None = None
    openapi_url: str | None = None
    errors: list[str] | None = None
    pipeline: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None


async def generate_and_deploy(
    description: str,
    registry: Registry,
    wrapper_id: str | None = None,
    output_base: Path | None = None,
    on_phase=None,
    prior: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
) -> DeployResult:
    """Run the full generator pipeline, deploy the resulting wrapper, and run
    post-deploy verification. Persists the verification report into the registry.

    ``on_phase(phase: str)`` surfaces live progress (pipeline phases plus the
    deploy/verify-live phases owned by this function)."""
    from openapi_anything.docker.manager import DockerManager
    from openapi_anything.generator.llm_client import LLMClient
    from openapi_anything.generator.pipeline import PipelineOrchestrator, default_output_base

    if output_base is None:
        output_base = default_output_base()
    wrapper_id = wrapper_id or f"wrapper-{uuid.uuid4().hex[:8]}"
    llm = LLMClient()
    orchestrator = PipelineOrchestrator(llm, output_base=output_base)
    credential_env = sorted(secrets.keys()) if secrets else []
    state = await orchestrator.run(
        description, wrapper_id, on_phase=on_phase, prior=prior, credential_env=credential_env
    )

    if state.status != "completed":
        return DeployResult(
            wrapper_id=wrapper_id,
            status="failed",
            errors=state.errors,
            pipeline={"phase": state.current_phase, "retries": state.retries},
        )

    if on_phase is not None:
        on_phase("deploy")
    docker_mgr = DockerManager(registry)
    wrapper_dir = output_base / wrapper_id
    service_url, _port = await docker_mgr.deploy_wrapper(
        wrapper_id, wrapper_dir, description, environment=secrets or None
    )

    # Phase 7: post-deploy verification against the live service.
    if on_phase is not None:
        on_phase("verify-live")
    verification: dict[str, Any] | None = None
    if state.design is not None:
        try:
            verification = await orchestrator.verify_deployed(service_url, state.design)
        except Exception as exc:  # verification must never fail the deployment
            verification = {"overall": False, "error": str(exc)}

    status = "healthy"
    if verification and verification.get("overall") is False:
        status = "degraded"

    entry = WrapperEntry(
        id=wrapper_id,
        target_description=description,
        openapi_url=f"{service_url}/openapi.json",
        service_url=service_url,
        container_name=f"wrapper-{wrapper_id}",
        status=status,
        created_at=datetime.now(UTC).isoformat(),
        verification=verification,
        wrapper_dir=str(wrapper_dir),
        secret_names=credential_env or None,
    )
    registry.register(entry)

    return DeployResult(
        wrapper_id=wrapper_id,
        status="deployed",
        service_url=service_url,
        openapi_url=entry.openapi_url,
        verification=verification,
        pipeline={
            "phase": state.current_phase,
            "retries": state.retries,
            "test_results": {"passed": state.test_results.get("passed")},
            "verification_overall": verification.get("overall") if verification else None,
        },
    )


async def undeploy(wrapper_id: str, registry: Registry) -> dict[str, Any]:
    """Stop + remove a wrapper's container/image and drop its registry entry.

    Returns a summary describing what was removed."""
    from openapi_anything.docker.manager import DockerManager

    entry = registry.get(wrapper_id)
    if not entry:
        return {"wrapper_id": wrapper_id, "removed": False, "reason": "not found in registry"}

    docker_mgr = DockerManager(registry)
    removed = docker_mgr.stop_and_remove_wrapper(wrapper_id)
    registry.remove(wrapper_id)
    return {"wrapper_id": wrapper_id, "removed": True, "lifecycle": removed}
