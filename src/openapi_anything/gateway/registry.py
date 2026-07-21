"""Registry for deployed wrapper services. Persists to registry.json.

The registry is shared across processes (the gateway container and a host-side CLI
both read/write the same file), so it is mtime-aware: reads reload from disk when the
file has been changed externally, and writes are atomic (temp + replace) to avoid
partial reads by the other process.
"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class WrapperEntry:
    id: str
    target_description: str
    openapi_url: str
    service_url: str
    container_name: str
    status: str  # 'healthy', 'starting', 'error', 'degraded', 'unreachable'
    created_at: str
    verification: dict[str, Any] | None = None  # post-deploy verification report (phase 7)
    wrapper_dir: str | None = None  # generated-code dir (serves /_source)
    secret_names: list[str] | None = None  # credential env var NAMES (values in SecretStore)


class Registry:
    def __init__(self, path: Path = Path("registry.json")):
        self.path = Path(path)
        self._entries: Dict[str, WrapperEntry] = {}
        self._mtime: Optional[float] = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._entries = {}
            self._mtime = None
            return
        try:
            self._mtime = self.path.stat().st_mtime
            data = json.loads(self.path.read_text())
            entries: Dict[str, WrapperEntry] = {}
            for entry_data in data.get("wrappers", []):
                # tolerate old entries that predate newer optional fields
                entry_data.setdefault("verification", None)
                entry_data.setdefault("wrapper_dir", None)
                entry_data.setdefault("secret_names", None)
                entry = WrapperEntry(**entry_data)
                entries[entry.id] = entry
            self._entries = entries
        except (OSError, ValueError, TypeError):
            self._entries = {}

    def _maybe_reload(self) -> None:
        """Reload from disk if an external process (e.g. the host CLI) changed it."""
        try:
            if not self.path.exists():
                if self._entries or self._mtime is not None:
                    self._entries = {}
                    self._mtime = None
                return
            m = self.path.stat().st_mtime
            if self._mtime is None or m != self._mtime:
                self._load()
        except OSError:
            pass

    def _save(self) -> None:
        data = {"wrappers": [asdict(e) for e in self._entries.values()]}
        blob = json.dumps(data, indent=2)
        # Atomic write: temp file in same dir, then os.replace.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".registry-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(blob)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        self._mtime = self.path.stat().st_mtime

    def register(self, entry: WrapperEntry) -> None:
        self._maybe_reload()
        self._entries[entry.id] = entry
        self._save()

    def get(self, wrapper_id: str) -> Optional[WrapperEntry]:
        self._maybe_reload()
        return self._entries.get(wrapper_id)

    def list_all(self) -> List[WrapperEntry]:
        self._maybe_reload()
        return list(self._entries.values())

    def update_status(self, wrapper_id: str, status: str) -> None:
        self._maybe_reload()
        if wrapper_id in self._entries:
            self._entries[wrapper_id].status = status
            self._save()

    def remove(self, wrapper_id: str) -> None:
        self._maybe_reload()
        if wrapper_id in self._entries:
            del self._entries[wrapper_id]
            self._save()


# Module-level singleton — the single source of truth for the running application
_registry_singleton: Optional[Registry] = None


def get_registry(path: Path = Path("registry.json")) -> Registry:
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = Registry(path)
    return _registry_singleton
