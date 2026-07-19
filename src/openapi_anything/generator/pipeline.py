"""Pipeline Orchestrator: phased generation with an iterative fix loop.

Phases implemented:
1. Inspect target (tools + LLM)
2. Design REST API (LLM) -> APIDesign consumed end-to-end by the code generator
3. Generate code (deterministic assembly of the design)
4. Test generation + execution (pytest smoke)
5. Fix loop: on failure, feed errors back to the LLM and regenerate
   (up to ``max_retries``); final deterministic safe fallback guarantees success
6. (build + deploy handled by DockerManager in service.generate_and_deploy)
7. Pre-deploy verification via FastAPI TestClient (health + openapi paths).
   Post-deploy verification is run by the Verifier in service.generate_and_deploy.
"""

import importlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .code_generator import CodeGenerator
from .designer import APIDesign, EndpointSpec
from .inspector import TargetInspector
from .designer import Designer
from .llm_client import LLMClient
from .verifier import Verifier


def default_output_base() -> Path:
    return Path(os.getenv("WRAPPER_OUTPUT_BASE", "/tmp/openapi-anything-wrappers"))


@dataclass
class PipelineState:
    wrapper_id: str
    target_description: str
    current_phase: str = "init"
    inspection: dict[str, Any] = field(default_factory=dict)
    design: APIDesign | None = None
    generated_code_path: Path | None = None
    test_results: dict[str, Any] = field(default_factory=dict)
    docker_info: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    retries: int = 0
    max_retries: int = field(
        default_factory=lambda: int(os.getenv("PIPELINE_MAX_RETRIES", "5"))
    )
    status: str = "running"


class PipelineOrchestrator:
    def __init__(self, llm: LLMClient, output_base: Path | None = None):
        self.llm = llm
        output_base = output_base if output_base is not None else default_output_base()
        self.inspector = TargetInspector(llm)
        self.designer = Designer(llm)
        self.generator = CodeGenerator()
        self.verifier = Verifier()
        self.output_base = output_base
        self.output_base.mkdir(parents=True, exist_ok=True)

    def _run_pytest(self, wrapper_dir: Path) -> dict[str, Any]:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "test_app.py", "-q", "--tb=short"],
            cwd=str(wrapper_dir),
            capture_output=True,
            text=True,
            timeout=90,
        )
        return {
            "passed": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:],
        }

    def _import_app(self, wrapper_dir: Path) -> dict[str, Any]:
        """Pre-deploy check: import the generated app and probe health + openapi via
        a TestClient without touching Docker."""
        info: dict[str, Any] = {}
        from fastapi.testclient import TestClient

        sys.path.insert(0, str(wrapper_dir))
        try:
            if "app" in sys.modules:
                del sys.modules["app"]
            importlib.import_module("app")
            from app import app as wrapper_app  # type: ignore[import-untyped]

            client = TestClient(wrapper_app)
            health = client.get("/health")
            openapi = client.get("/openapi.json")
            info = {
                "health": health.status_code,
                "openapi": openapi.status_code,
                "paths": list(openapi.json().get("paths", {}).keys())
                if openapi.status_code == 200
                else [],
            }
        finally:
            if str(wrapper_dir) in sys.path:
                sys.path.remove(str(wrapper_dir))
            if "app" in sys.modules:
                del sys.modules["app"]
        return info

    async def _generate_and_test(
        self,
        wrapper_dir: Path,
        design: APIDesign,
        inspection: dict[str, Any],
        description: str,
        wrapper_id: str,
        state: PipelineState,
        on_phase=None,
    ) -> tuple[str, dict[str, Any]]:
        """Fix loop: deterministic generate -> test -> LLM repair -> ... -> safe fallback.

        Returns the final app code and the last test result. Updates ``state.retries``
        and ``state.errors`` as a side effect.
        """
        last_code = ""
        errors: list[str] = []
        test: dict[str, Any] = {"passed": False, "stdout": "", "stderr": ""}

        for attempt in range(state.max_retries + 1):
            if attempt == 0:
                code = self.generator.generate_app_py(design, inspection, description, wrapper_id)
            else:
                print(f"[pipeline] fix-loop attempt {attempt}/{state.max_retries}")
                if on_phase is not None:
                    on_phase(f"generate: fix attempt {attempt}/{state.max_retries}")
                code = await self.generator.repair_app_py(
                    last_code, errors, design, inspection, description, wrapper_id, self.llm
                )
            last_code = code
            self.generator.write_wrapper_files(wrapper_dir, code, design)
            test = self._run_pytest(wrapper_dir)
            if test["passed"]:
                state.retries = attempt
                print(f"[pipeline] tests passed on attempt {attempt}")
                return code, test
            errors = [test.get("stdout", ""), test.get("stderr", "")]
            state.errors.append(f"[attempt {attempt}] tests failed")

        # Final deterministic safe fallback — guarantees a working wrapper.
        print("[pipeline] fix loop exhausted; using deterministic safe generator")
        code = self.generator.generate_app_py(design, inspection, description, wrapper_id, safe=True)
        self.generator.write_wrapper_files(wrapper_dir, code, design)
        test = self._run_pytest(wrapper_dir)
        state.retries = state.max_retries + 1
        return code, test

    async def run(
        self,
        description: str,
        wrapper_id: str | None = None,
        on_phase=None,
        prior: dict[str, Any] | None = None,
        credential_env: list[str] | None = None,
    ) -> PipelineState:
        """Execute full pipeline. Returns final state (success or failed).

        ``on_phase(phase: str)`` is called at each phase transition so callers
        (the gateway job store) can surface live progress to pollers."""
        if not wrapper_id:
            wrapper_id = f"wrapper-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        state = PipelineState(wrapper_id=wrapper_id, target_description=description)

        def report(phase: str) -> None:
            state.current_phase = phase
            if on_phase is not None:
                on_phase(phase)

        try:
            report("inspect")
            state.inspection = await self.inspector.inspect(description)
            if credential_env:
                # Secret NAMES only — the designer writes os.getenv(...) calls;
                # values go straight into the container env at deploy.
                state.inspection["credential_env_vars"] = credential_env
            print(f"[pipeline] Inspect complete for {wrapper_id}")

            report("design")
            state.design = await self.designer.design(state.inspection, description, prior=prior)
            print(
                f"[pipeline] Design complete: {len(state.design.endpoints)} endpoints, "
                f"{len(state.design.models)} models"
            )

            report("generate")
            wrapper_dir = self.output_base / wrapper_id
            code, test = await self._generate_and_test(
                wrapper_dir, state.design, state.inspection, description, wrapper_id, state,
                on_phase=on_phase,
            )
            state.test_results = test
            if not test["passed"]:
                raise RuntimeError(f"Generated tests failed:\n{test.get('stderr', '')}")
            state.generated_code_path = wrapper_dir / "app.py"
            print(f"[pipeline] Code generated at {state.generated_code_path}")

            report("verify")
            state.docker_info["pre_deploy"] = self._import_app(wrapper_dir)
            if state.docker_info["pre_deploy"].get("health") != 200:
                raise RuntimeError(
                    f"Pre-deploy health check failed: {state.docker_info['pre_deploy']}"
                )
            print("[pipeline] Pre-deploy verification passed")

            state.status = "completed"
            print(f"[pipeline] Pipeline completed for {wrapper_id}")

        except Exception as exc:
            state.errors.append(str(exc))
            state.status = "failed"
            state.current_phase = "error"
            print(f"[pipeline] Failed at {state.current_phase}: {exc}")

        return state

    async def verify_deployed(self, service_url: str, design: APIDesign) -> dict[str, Any]:
        """Post-deploy verification against a live service URL."""
        expected = [f"{ep.method} {ep.path}" for ep in design.endpoints]
        return await self.verifier.verify_wrapper(service_url, expected)


# re-export for backwards-compatible imports
__all__ = ["PipelineOrchestrator", "PipelineState", "APIDesign", "EndpointSpec"]
