"""Secret store for wrapped-target credentials.

Secrets are injected into wrapper containers as environment variables at deploy
time. Values live only here (redis when ``REDIS_URL`` is set, else process
memory) and in the container env — never in generated code, job records, the
registry (names only), or LLM prompts (names only). Local-trust design: values
are stored unencrypted in redis; keep the stack on a trusted network.
"""

import os
from typing import Dict

_KEY_PREFIX = "openapi-anything:secrets:"


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
        print(f"[secrets] redis unavailable ({exc}); using in-memory secret store")
        return None


class SecretStore:
    def __init__(self, redis_client=None):
        self._redis = redis_client if redis_client is not None else _connect_redis()
        self._memory: Dict[str, Dict[str, str]] = {}

    def set(self, wrapper_id: str, secrets: Dict[str, str]) -> None:
        if not secrets:
            return
        if self._redis is not None:
            try:
                self._redis.hset(_KEY_PREFIX + wrapper_id, mapping=secrets)
                return
            except Exception as exc:
                print(f"[secrets] redis write failed ({exc}); keeping in memory")
        self._memory[wrapper_id] = dict(secrets)

    def get(self, wrapper_id: str) -> Dict[str, str]:
        if self._redis is not None:
            try:
                return self._redis.hgetall(_KEY_PREFIX + wrapper_id)
            except Exception as exc:
                print(f"[secrets] redis read failed ({exc})")
        return dict(self._memory.get(wrapper_id, {}))

    def names(self, wrapper_id: str) -> list[str]:
        return sorted(self.get(wrapper_id).keys())

    def delete(self, wrapper_id: str) -> None:
        if self._redis is not None:
            try:
                self._redis.delete(_KEY_PREFIX + wrapper_id)
            except Exception:
                pass
        self._memory.pop(wrapper_id, None)


_secret_store_singleton: SecretStore | None = None


def get_secret_store() -> SecretStore:
    global _secret_store_singleton
    if _secret_store_singleton is None:
        _secret_store_singleton = SecretStore()
    return _secret_store_singleton
