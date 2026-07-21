"""Per-wrapper traffic metrics collected at the gateway.

The proxy route and MCP tool calls record request count, 5xx/error count,
latency, and last-used per wrapper. Counters live in memory and are flushed to
redis (``METRICS_FLUSH_INTERVAL`` s, default 30) when ``REDIS_URL`` is set, so
totals survive gateway restarts. Not a monitoring system — a hub-friendly
"is anything using this wrapper?" signal.
"""

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Dict, cast

import redis

_KEY_PREFIX = "openapi-anything:metrics:"


def metrics_flush_interval() -> float:
    return float(os.getenv("METRICS_FLUSH_INTERVAL", "30"))


def _connect_redis() -> "redis.Redis | None":
    url = os.getenv("REDIS_URL", "")
    if not url:
        return None
    try:
        client = redis.Redis.from_url(url, socket_timeout=2, decode_responses=True)
        client.ping()
        return client
    except Exception as exc:
        print(f"[metrics] redis unavailable ({exc}); metrics are in-memory only")
        return None


@dataclass
class _Record:
    requests: int = 0
    errors: int = 0
    latency_ms_total: float = 0.0
    last_used: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "errors": self.errors,
            "avg_latency_ms": (
                round(self.latency_ms_total / self.requests, 1) if self.requests else 0.0
            ),
            "last_used": self.last_used,
        }


class MetricsStore:
    def __init__(self, redis_client: "redis.Redis | None" = None):
        self._redis = redis_client if redis_client is not None else _connect_redis()
        self._records: Dict[str, _Record] = {}
        self._load()

    def _load(self) -> None:
        if self._redis is None:
            return
        try:
            # redis-py's command stubs return a pipeline-compatible Awaitable|T
            # union even on a plain sync client; this store only uses sync redis.
            keys = cast(list[str], self._redis.keys(_KEY_PREFIX + "*"))
            for key in keys:
                raw = cast(Dict[str, str], self._redis.hgetall(key))
                wrapper_id = key[len(_KEY_PREFIX):]
                self._records[wrapper_id] = _Record(
                    requests=int(raw.get("requests", 0)),
                    errors=int(raw.get("errors", 0)),
                    latency_ms_total=float(raw.get("latency_ms_total", 0)),
                    last_used=raw.get("last_used") or None,
                )
        except Exception as exc:
            print(f"[metrics] redis load failed ({exc}); starting empty")

    def record(self, wrapper_id: str, status_code: int, latency_ms: float) -> None:
        rec = self._records.setdefault(wrapper_id, _Record())
        rec.requests += 1
        if status_code >= 500:
            rec.errors += 1
        rec.latency_ms_total += latency_ms
        rec.last_used = datetime.now(UTC).isoformat()

    def get(self, wrapper_id: str) -> dict[str, Any]:
        return self._records.get(wrapper_id, _Record()).public()

    def all(self) -> dict[str, dict[str, Any]]:
        return {wid: rec.public() for wid, rec in self._records.items()}

    def flush(self) -> None:
        if self._redis is None:
            return
        for wid, rec in self._records.items():
            try:
                self._redis.hset(
                    _KEY_PREFIX + wid,
                    mapping={
                        "requests": rec.requests,
                        "errors": rec.errors,
                        "latency_ms_total": rec.latency_ms_total,
                        "last_used": rec.last_used or "",
                    },
                )
            except Exception as exc:
                print(f"[metrics] redis flush failed for {wid}: {exc}")
                return

    def remove(self, wrapper_id: str) -> None:
        self._records.pop(wrapper_id, None)
        if self._redis is not None:
            try:
                self._redis.delete(_KEY_PREFIX + wrapper_id)
            except Exception:
                pass


_metrics_singleton: MetricsStore | None = None


def get_metrics_store() -> MetricsStore:
    global _metrics_singleton
    if _metrics_singleton is None:
        _metrics_singleton = MetricsStore()
    return _metrics_singleton
