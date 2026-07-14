"""In-memory job store for asynchronous wrapper generation.

``POST /api/generate`` submits a job and returns immediately; the pipeline runs
as an asyncio task and clients poll ``GET /jobs/{id}``. In-memory by design:
jobs are ephemeral progress records (the durable outcome lives in the registry),
and the gateway is a single process.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from openapi_anything.service import DeployResult


@dataclass
class Job:
    id: str
    description: str
    wrapper_id: str
    status: str = "queued"  # queued | running | completed | failed | cancelled
    phase: str | None = None  # live pipeline phase while running
    created_at: str = ""
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_public(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def submit(
        self,
        description: str,
        wrapper_id: str,
        runner: Callable[[Callable[[str], None]], Awaitable[DeployResult]],
    ) -> Job:
        """Register a job and start the pipeline as a background asyncio task.

        ``runner`` receives a report callback; each ``report(phase)`` call is
        reflected live on the job record for pollers."""
        job = Job(
            id=f"job-{uuid.uuid4().hex[:12]}",
            description=description,
            wrapper_id=wrapper_id,
            created_at=_now(),
        )
        self._jobs[job.id] = job
        self._tasks[job.id] = asyncio.create_task(self._run(job, runner))
        return job

    async def _run(
        self, job: Job, runner: Callable[[Callable[[str], None]], Awaitable[DeployResult]]
    ) -> None:
        job.status = "running"

        def report(phase: str) -> None:
            job.phase = phase

        try:
            result = await runner(report)
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.error = "cancelled by user"
            job.finished_at = _now()
            return
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
        else:
            if result.status == "deployed":
                job.status = "completed"
                job.result = {
                    "wrapper_id": result.wrapper_id,
                    "status": "deployed",
                    "service_url": result.service_url,
                    "openapi_url": result.openapi_url,
                    "openapi_path": f"/services/{result.wrapper_id}/openapi.json",
                    "verification": result.verification,
                }
            else:
                job.status = "failed"
                job.error = "; ".join(result.errors or ["unknown error"])
        job.finished_at = _now()

    def cancel(self, job_id: str) -> bool:
        """Cancel an active job. Returns False for unknown or already-finished jobs."""
        job = self._jobs.get(job_id)
        task = self._tasks.get(job_id)
        if job is None or task is None or job.status not in ("queued", "running"):
            return False
        task.cancel()
        return True

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_all(self) -> list[Job]:
        """All jobs, newest first (dict preserves submission order)."""
        return list(reversed(self._jobs.values()))

    def has_active(self) -> bool:
        return any(j.status in ("queued", "running") for j in self._jobs.values())

    async def wait(self, job_id: str) -> None:
        """Await a job's background task (tests / graceful shutdown)."""
        task = self._tasks.get(job_id)
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass  # cancelled jobs settle their own status in _run


_job_store_singleton: JobStore | None = None


def get_job_store() -> JobStore:
    global _job_store_singleton
    if _job_store_singleton is None:
        _job_store_singleton = JobStore()
    return _job_store_singleton
