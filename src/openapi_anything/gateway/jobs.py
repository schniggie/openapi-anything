"""Job store for asynchronous wrapper generation, with optional redis persistence.

``POST /api/generate`` submits a job and returns immediately; the pipeline runs
as an asyncio task and clients poll ``GET /jobs/{id}``. Job *records* are
write-through persisted to redis when ``REDIS_URL`` is set (the compose stack
points at its redis service), so history survives gateway restarts. Running
asyncio tasks cannot survive a restart, so jobs loaded in a non-terminal state
are marked failed. Without ``REDIS_URL`` (tests, bare dev) the store is purely
in-memory, as before; a redis outage degrades to in-memory too.
"""

import asyncio
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from openapi_anything.service import DeployResult

_REDIS_KEY = "openapi-anything:jobs"


def _connect_redis():
    url = os.getenv("REDIS_URL", "")
    if not url:
        return None
    try:
        import redis

        client = redis.Redis.from_url(url, socket_timeout=2, decode_responses=True)
        client.ping()
        return client
    except Exception as exc:
        print(f"[jobs] redis unavailable ({exc}); falling back to in-memory jobs")
        return None


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
    def __init__(self, redis_client=None) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_requested: set[str] = set()
        self._redis = redis_client if redis_client is not None else _connect_redis()
        self._load()

    # ---- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._redis is None:
            return
        try:
            raw = self._redis.hgetall(_REDIS_KEY)
        except Exception as exc:
            print(f"[jobs] redis load failed ({exc}); continuing in-memory")
            self._redis = None
            return
        loaded: list[Job] = []
        for value in raw.values():
            try:
                loaded.append(Job(**json.loads(value)))
            except (ValueError, TypeError) as exc:
                print(f"[jobs] skipping corrupt job record: {exc}")
        for job in sorted(loaded, key=lambda j: j.created_at):
            if job.status in ("queued", "running"):
                # The task died with the previous gateway process.
                job.status = "failed"
                job.error = "interrupted by gateway restart"
                job.finished_at = _now()
                self._persist(job)
            self._jobs[job.id] = job

    def _persist(self, job: Job) -> None:
        if self._redis is None:
            return
        try:
            self._redis.hset(_REDIS_KEY, job.id, json.dumps(asdict(job)))
        except Exception as exc:
            print(f"[jobs] redis persist failed for {job.id}: {exc}")

    def _prune(self) -> None:
        """Cap history: drop oldest terminal jobs beyond JOBS_HISTORY_MAX."""
        max_jobs = int(os.getenv("JOBS_HISTORY_MAX", "200"))
        excess = len(self._jobs) - max_jobs
        if excess <= 0:
            return
        for job in list(self._jobs.values()):
            if excess <= 0:
                break
            if job.status in ("queued", "running"):
                continue
            del self._jobs[job.id]
            self._tasks.pop(job.id, None)
            excess -= 1
            if self._redis is not None:
                try:
                    self._redis.hdel(_REDIS_KEY, job.id)
                except Exception:
                    pass

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
        self._prune()
        self._persist(job)
        self._tasks[job.id] = asyncio.create_task(self._run(job, runner))
        return job

    async def _run(
        self, job: Job, runner: Callable[[Callable[[str], None]], Awaitable[DeployResult]]
    ) -> None:
        job.status = "running"
        self._persist(job)

        def report(phase: str) -> None:
            job.phase = phase
            self._persist(job)

        try:
            result = await runner(report)
        except asyncio.CancelledError:
            if job.id in self._cancel_requested:
                job.status = "cancelled"
                job.error = "cancelled by user"
            else:
                # Task torn down without a cancel request: gateway shutdown.
                job.status = "failed"
                job.error = "interrupted by gateway shutdown"
            job.finished_at = _now()
            self._persist(job)
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
        self._persist(job)

    def cancel(self, job_id: str) -> bool:
        """Cancel an active job. Returns False for unknown or already-finished jobs."""
        job = self._jobs.get(job_id)
        task = self._tasks.get(job_id)
        if job is None or task is None or job.status not in ("queued", "running"):
            return False
        self._cancel_requested.add(job_id)
        task.cancel()
        return True

    def active_for(self, wrapper_id: str) -> bool:
        """True when a queued/running job already targets this wrapper."""
        return any(
            j.wrapper_id == wrapper_id and j.status in ("queued", "running")
            for j in self._jobs.values()
        )

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
