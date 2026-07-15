"""Tests for redis-backed job persistence (survives gateway restarts)."""

import json

import pytest

from openapi_anything.gateway.jobs import JobStore
from openapi_anything.service import DeployResult


class FakeRedis:
    """Minimal stand-in for redis.Redis (decode_responses=True)."""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}

    def ping(self):
        return True

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hdel(self, key, *fields):
        for f in fields:
            self.hashes.get(key, {}).pop(f, None)


def _deployed() -> DeployResult:
    return DeployResult(
        wrapper_id="w",
        status="deployed",
        service_url="http://x:1",
        openapi_url="http://x:1/openapi.json",
    )


def _records(fake: FakeRedis) -> dict[str, dict]:
    key = next(iter(fake.hashes), None)
    return {k: json.loads(v) for k, v in fake.hashes.get(key, {}).items()} if key else {}


@pytest.mark.asyncio
async def test_submit_persists_job_record():
    fake = FakeRedis()
    store = JobStore(redis_client=fake)

    async def runner(report):
        report("inspect")
        return _deployed()

    job = store.submit("wrap ls", "w", runner)
    await store.wait(job.id)

    records = _records(fake)
    assert job.id in records
    assert records[job.id]["status"] == "completed"
    assert records[job.id]["result"]["openapi_path"] == "/services/w/openapi.json"


@pytest.mark.asyncio
async def test_history_survives_restart():
    fake = FakeRedis()
    store = JobStore(redis_client=fake)

    async def runner(report):
        return _deployed()

    job = store.submit("wrap ls", "w", runner)
    await store.wait(job.id)

    reborn = JobStore(redis_client=fake)  # simulates gateway restart
    loaded = reborn.get(job.id)
    assert loaded is not None
    assert loaded.status == "completed"
    assert loaded.description == "wrap ls"


@pytest.mark.asyncio
async def test_interrupted_jobs_marked_failed_on_restart():
    fake = FakeRedis()
    store = JobStore(redis_client=fake)

    import asyncio

    gate = asyncio.Event()

    async def runner(report):
        await gate.wait()
        return _deployed()

    job = store.submit("wrap ls", "w", runner)
    assert store.get(job.id).status in ("queued", "running")

    reborn = JobStore(redis_client=fake)  # restart while job was active
    loaded = reborn.get(job.id)
    assert loaded.status == "failed"
    assert "interrupted" in loaded.error
    assert loaded.finished_at is not None
    # and the failure is persisted back
    assert _records(fake)[job.id]["status"] == "failed"

    gate.set()
    await store.wait(job.id)


@pytest.mark.asyncio
async def test_history_pruned_to_max(monkeypatch):
    monkeypatch.setenv("JOBS_HISTORY_MAX", "3")
    fake = FakeRedis()
    store = JobStore(redis_client=fake)

    async def runner(report):
        return _deployed()

    jobs = [store.submit(f"d{i}", f"w{i}", runner) for i in range(5)]
    for j in jobs:
        await store.wait(j.id)
    # trigger prune with one more submit
    last = store.submit("d5", "w5", runner)
    await store.wait(last.id)

    assert len(store.list_all()) <= 3
    assert store.get(jobs[0].id) is None  # oldest gone
    assert store.get(last.id) is not None  # newest kept


def test_corrupt_record_skipped():
    fake = FakeRedis()
    fake.hset("openapi-anything:jobs", "job-bad", "{not json")
    store = JobStore(redis_client=fake)
    assert store.list_all() == []


@pytest.mark.asyncio
async def test_shutdown_cancellation_is_not_user_cancel():
    """Task cancellation without JobStore.cancel() (graceful gateway shutdown)
    must be recorded as an interruption, not as a user cancel."""
    import asyncio

    fake = FakeRedis()
    store = JobStore(redis_client=fake)
    gate = asyncio.Event()

    async def runner(report):
        await gate.wait()
        return _deployed()

    job = store.submit("wrap ls", "w", runner)
    await asyncio.sleep(0)  # let the task start
    store._tasks[job.id].cancel()  # shutdown-style cancel, not store.cancel()
    await store.wait(job.id)

    loaded = store.get(job.id)
    assert loaded.status == "failed"
    assert "interrupted" in loaded.error
    assert _records(fake)[job.id]["status"] == "failed"


@pytest.mark.asyncio
async def test_user_cancel_still_reports_cancelled():
    import asyncio

    store = JobStore(redis_client=FakeRedis())
    gate = asyncio.Event()

    async def runner(report):
        await gate.wait()
        return _deployed()

    job = store.submit("wrap ls", "w", runner)
    await asyncio.sleep(0)
    assert store.cancel(job.id) is True
    await store.wait(job.id)
    assert store.get(job.id).status == "cancelled"
    assert store.get(job.id).error == "cancelled by user"


def test_no_redis_env_means_memory_only(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    store = JobStore()
    assert store._redis is None
