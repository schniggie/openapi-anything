"""Docker Manager: dynamic per-wrapper container lifecycle using docker SDK.

- Builds from generated Dockerfile + app.py
- Allocates free host port
- Starts container, waits for healthy (via /health) using async polling
- Tears down containers + images on undeploy
- Falls back to local uvicorn subprocess when Docker/Podman is unavailable
"""

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

from ..gateway.registry import Registry

# Track local uvicorn subprocesses for cleanup
_local_processes: Dict[str, subprocess.Popen] = {}


class DockerManager:
    def __init__(self, registry: Registry):
        self.registry = registry
        self.client = None
        try:
            self.client = docker.from_env()
            self.client.ping()
        except (DockerException, OSError):
            self.client = self._try_podman_socket()

    @staticmethod
    def _try_podman_socket():
        """Attempt to connect to a rootless podman socket if DOCKER_HOST points at one
        (or the default XDG runtime path exists)."""
        try:
            sock = os.environ.get(
                "DOCKER_HOST",
                f"unix://{os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')}/podman/podman.sock",
            )
            if sock.startswith("unix://"):
                client = docker.DockerClient(base_url=sock)
                client.ping()
                return client
        except (DockerException, OSError):
            return None
        return None

    def _find_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    def _proxy_host(self) -> str:
        return os.environ.get("GATEWAY_PROXY_HOST", "127.0.0.1")

    async def _wait_for_health(self, service_url: str, attempts: int = 40, delay: float = 0.5) -> bool:
        import httpx

        for _ in range(attempts):
            try:
                async with httpx.AsyncClient() as client:
                    r = await client.get(f"{service_url}/health", timeout=2)
                    if r.status_code == 200:
                        return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(delay)
        return False

    async def _deploy_local(
        self,
        wrapper_id: str,
        wrapper_dir: Path,
        proxy_host: str,
        environment: Dict[str, str] | None = None,
    ) -> Tuple[str, int]:
        port = self._find_free_port()
        service_url = f"http://{proxy_host}:{port}"
        print(f"[local] Starting uvicorn for {wrapper_id} on port {port}")

        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", str(port)],
            cwd=str(wrapper_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **(environment or {})},
        )
        _local_processes[wrapper_id] = proc

        if not await self._wait_for_health(service_url):
            proc.terminate()
            try:
                _out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                _out, err = b"", b""
            raise RuntimeError(
                f"Wrapper {wrapper_id} failed health check on port {port}: "
                f"{(err or b'').decode(errors='ignore')[-500:]}"
            )

        return service_url, port

    async def deploy_wrapper(
        self,
        wrapper_id: str,
        wrapper_dir: Path,
        target_desc: str,
        environment: Dict[str, str] | None = None,
    ) -> Tuple[str, int]:
        """Build and run wrapper container, or fall back to local uvicorn subprocess.

        ``environment`` carries target credentials into the container — values
        exist only in the container config, never in the generated code."""
        proxy_host = self._proxy_host()

        if self.client is None:
            return await self._deploy_local(wrapper_id, wrapper_dir, proxy_host, environment)

        image_tag = f"openapi-wrapper-{wrapper_id}:latest"
        container_name = f"wrapper-{wrapper_id}"
        print(f"[docker] Building {image_tag} from {wrapper_dir}")

        # Remove existing container with same name if present
        try:
            existing = self.client.containers.get(container_name)
            existing.remove(force=True)
        except NotFound:
            pass
        except (APIError, DockerException):
            pass

        self.client.images.build(path=str(wrapper_dir), tag=image_tag, rm=True)
        port = self._find_free_port()
        self.client.containers.run(
            image_tag,
            detach=True,
            name=container_name,
            ports={"8000/tcp": port},
            remove=False,
            environment=environment or {},
            # Survive daemon restarts; rootless-podman reboots are additionally
            # covered by revive_registered() at gateway startup.
            restart_policy={"Name": "unless-stopped"},
        )

        service_url = f"http://{proxy_host}:{port}"
        if not await self._wait_for_health(service_url):
            raise RuntimeError(f"Wrapper {wrapper_id} container failed health check on port {port}")

        return service_url, port

    def revive_registered(self) -> Dict[str, str]:
        """Start registered wrappers whose containers exist but are not running
        (e.g. after a host reboot — rootless podman does not honor restart
        policies across boots without a systemd unit). Returns id -> outcome."""
        if self.client is None:
            return {}
        outcome: Dict[str, str] = {}
        for entry in self.registry.list_all():
            try:
                container = self.client.containers.get(f"wrapper-{entry.id}")
            except NotFound:
                outcome[entry.id] = "container-missing"
                continue
            except (APIError, DockerException) as exc:
                outcome[entry.id] = f"error: {exc}"
                continue
            if container.status == "running":
                outcome[entry.id] = "running"
                continue
            try:
                container.start()
                outcome[entry.id] = "started"
                print(f"[docker] revived wrapper container wrapper-{entry.id}")
            except (APIError, DockerException) as exc:
                outcome[entry.id] = f"error: {exc}"
        return outcome

    def get_logs(self, wrapper_id: str, tail: int = 100) -> str:
        """Container logs for a wrapper. Raises KeyError when the container (or a
        container runtime) is unavailable."""
        if self.client is None:
            raise KeyError("no container runtime available")
        try:
            container = self.client.containers.get(f"wrapper-{wrapper_id}")
        except NotFound:
            raise KeyError(wrapper_id) from None
        return container.logs(tail=tail).decode(errors="replace")

    def stop_and_remove_wrapper(self, wrapper_id: str, remove_image: bool = True) -> dict:
        """Stop + remove a wrapper's container (and image). Works for both the Docker
        path and the local-subprocess fallback. Returns a summary of what happened."""
        container_name = f"wrapper-{wrapper_id}"
        image_tag = f"openapi-wrapper-{wrapper_id}:latest"
        summary: dict = {"container": None, "image": None}

        # Local subprocess path
        proc = _local_processes.pop(wrapper_id, None)
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
                summary["container"] = "local-terminated"
            except Exception:
                try:
                    proc.kill()
                    summary["container"] = "local-killed"
                except Exception:
                    summary["container"] = "local-error"

        if self.client is not None:
            try:
                cont = self.client.containers.get(container_name)
                cont.remove(force=True)
                summary["container"] = "removed"
            except NotFound:
                summary["container"] = "not-found"
            except (APIError, DockerException) as exc:
                summary["container"] = f"error: {exc}"

            if remove_image:
                try:
                    self.client.images.remove(image_tag, force=True)
                    summary["image"] = "removed"
                except (ImageNotFound, NotFound):
                    summary["image"] = "not-found"
                except (APIError, DockerException) as exc:
                    summary["image"] = f"error: {exc}"
        elif summary["container"] is None:
            summary["container"] = "no-runtime"

        return summary
