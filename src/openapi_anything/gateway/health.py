"""Registry health sweep: reconcile recorded wrapper status with reality.

Wrapper status is otherwise written once at deploy time; a container that dies
later would show healthy forever. The gateway runs ``run_sweeper`` in the
background (FastAPI lifespan) to probe each wrapper's /health periodically.
"""

import asyncio
import os

import httpx

from .registry import Registry

def sweep_interval() -> float:
    return float(os.getenv("HEALTH_SWEEP_INTERVAL", "30"))


def probe_timeout() -> float:
    return float(os.getenv("HEALTH_PROBE_TIMEOUT", "2"))


async def sweep_once(registry: Registry, client: httpx.AsyncClient) -> dict[str, str]:
    """Probe every registered wrapper's /health; update registry statuses.

    A responding wrapper becomes ``healthy`` — unless it is ``degraded``, which is
    a post-deploy verification verdict the sweep must not overwrite. A failed
    probe becomes ``unreachable``. Returns the id -> status map of this sweep.
    """
    results: dict[str, str] = {}
    timeout = probe_timeout()
    for entry in registry.list_all():
        try:
            resp = await client.get(f"{entry.service_url}/health", timeout=timeout)
            status = "healthy" if resp.status_code == 200 else "unreachable"
        except Exception:
            status = "unreachable"
        if status == "healthy" and entry.status == "degraded":
            status = "degraded"
        if entry.status != status:
            registry.update_status(entry.id, status)
        results[entry.id] = status
    return results


async def run_sweeper(registry: Registry, interval: float | None = None) -> None:
    """Sweep forever; one failing sweep never kills the loop."""
    if interval is None:
        interval = sweep_interval()
    while True:
        try:
            async with httpx.AsyncClient() as client:
                await sweep_once(registry, client)
        except Exception as exc:
            print(f"[health] sweep failed: {exc}")
        await asyncio.sleep(interval)
