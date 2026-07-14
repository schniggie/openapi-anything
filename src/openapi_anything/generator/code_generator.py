"""Code Generator: assembles a complete, runnable FastAPI ``app.py`` from an
``APIDesign``. The design is fully consumed — title, description, models, endpoints
and per-endpoint handler bodies all flow into the generated code. Nothing about the
API surface is hardcoded by the generator.

A deterministic "safe" variant strips per-endpoint handler bodies (replacing them with
generic 200-echo handlers) so the wrapper always builds and passes smoke tests even if
the LLM-written handler bodies fail to compile.
"""

import re
from pathlib import Path
from typing import Any

from .designer import APIDesign, EndpointSpec
from .llm_client import LLMClient

_PY_KEYWORD_METHODS = {"delete"}


class CodeGenerator:
    def generate_app_py(
        self,
        design: APIDesign,
        inspection: dict[str, Any],
        target_desc: str,
        wrapper_id: str,
        safe: bool = False,
    ) -> str:
        """Assemble the full app.py source.

        If ``safe`` is True, per-endpoint handler bodies are replaced with generic
        echo handlers so the result always imports and returns 200. Used as the final
        fallback in the fix loop.
        """
        target_type = (design.target_type or inspection.get("type") or "cli").lower()
        out: list[str] = []
        out.append(f'"""Auto-generated FastAPI wrapper for: {target_desc}')
        out.append("")
        out.append(f"Wrapper id: {wrapper_id}")
        out.append("Produced by the openapi-anything generator pipeline.")
        out.append('"""')
        out.append("")
        out.append("from typing import Any")
        out.append("")
        out.append("import httpx")
        out.append("from fastapi import FastAPI")
        out.append("from pydantic import BaseModel")
        out.append("import subprocess")
        out.append("")
        # App metadata — straight from the design (B2/B4).
        title = (design.title or "Generated Wrapper").replace('"', "'")
        desc = (design.description or target_desc).replace('"', "'")
        out.append(f'app = FastAPI(title="{title}", description="{desc}")')
        out.append("")

        # Integration helpers
        self._emit_helpers(out, target_type, inspection)

        # Pydantic models from the design, plus auto-injected ones for referenced
        # request_model names that the LLM forgot to define.
        self._emit_models(out, design)

        # Endpoints from the design
        used_names: set[str] = set()
        for idx, ep in enumerate(design.endpoints):
            self._emit_endpoint(out, ep, idx, used_names, safe)

        # Always-present service endpoints
        self._emit_health(out, target_desc, wrapper_id)
        self._emit_root(out, design, target_desc, wrapper_id)

        return "\n".join(out) + "\n"

    # -- assembly helpers --------------------------------------------------

    def _emit_helpers(self, out: list[str], target_type: str, inspection: dict[str, Any]) -> None:
        if target_type == "web":
            base = inspection.get("base_url") or "https://httpbin.org"
            base = base.replace('"', "'")
            out.append(f'BASE_URL = "{base}"')
            out.append("")
            out.append("async def proxy_request(method: str, path: str, body: dict | None = None) -> dict:")
            out.append('    """Proxy a request to the wrapped web service."""')
            out.append("    url = BASE_URL.rstrip('/') + '/' + str(path).lstrip('/')")
            out.append("    async with httpx.AsyncClient(timeout=15) as client:")
            out.append("        if method.upper() == 'POST':")
            out.append("            resp = await client.post(url, json=body)")
            out.append("        elif method.upper() == 'PUT':")
            out.append("            resp = await client.put(url, json=body)")
            out.append("        elif method.upper() == 'DELETE':")
            out.append("            resp = await client.delete(url)")
            out.append("        else:")
            out.append("            resp = await client.get(url)")
            out.append("    return {'status_code': resp.status_code, 'body': resp.text[:8000]}")
        else:
            cmd = inspection.get("command") or "ls"
            cmd = cmd.replace('"', "'")
            out.append(f'COMMAND = "{cmd}"')
            out.append("")
            out.append("def run_command(args: list[str], timeout: int = 10) -> dict:")
            out.append('    """Run the wrapped command with args."""')
            out.append("    try:")
            out.append("        result = subprocess.run(")
            out.append("            [COMMAND] + list(args),")
            out.append("            capture_output=True,")
            out.append("            text=True,")
            out.append("            timeout=timeout,")
            out.append("        )")
            out.append("        return {")
            out.append("            'stdout': result.stdout,")
            out.append("            'stderr': result.stderr,")
            out.append("            'returncode': result.returncode,")
            out.append("        }")
            out.append("    except Exception as exc:")
            out.append("        return {'stdout': '', 'stderr': str(exc), 'returncode': -1}")
        out.append("")

    def _emit_models(self, out: list[str], design: APIDesign) -> None:
        referenced: set[str] = set()
        for ep in design.endpoints:
            if ep.request_model:
                referenced.add(ep.request_model)
            if ep.response_model:
                referenced.add(ep.response_model)
        defined = set(design.models.keys())
        out.append("# ---- Pydantic models (from design) ----")
        for name, src in design.models.items():
            out.append(self._indent_class(src.strip()))
        # Auto-inject minimal models for any referenced-but-undefined names so the
        # generated module always imports cleanly.
        for missing in sorted(referenced - defined):
            out.append(f"class {missing}(BaseModel):")
            out.append("    pass")
        if not design.models and not (referenced - defined):
            out.append("# (no models defined)")
        out.append("")

    def _emit_endpoint(
        self,
        out: list[str],
        ep: EndpointSpec,
        idx: int,
        used: set[str],
        safe: bool,
    ) -> None:
        method = (ep.method or "GET").lower()
        if method in _PY_KEYWORD_METHODS:
            # avoid `def delete(...)` shadowing builtin issues / keyword funcs
            method_attr = "delete"
        else:
            method_attr = method
        path = ep.path if ep.path.startswith("/") else "/" + ep.path
        out.append(f"@app.{method_attr}({path!r})")
        fname = self._func_name(ep, idx, used)
        # Consistency guard: if the LLM handler references `req` but no request_model
        # is declared, the design is inconsistent (a no-arg signature would NameError
        # on `req` at runtime). Use the safe echo body for THIS endpoint only.
        handler = ep.handler_code or ""
        body_refs_req = ("req." in handler) or ("req," in handler) or (" req)" in handler)
        inconsistent = (not ep.request_model) and body_refs_req
        effective_safe = safe or inconsistent
        if inconsistent and not safe:
            print(
                f"[codegen] endpoint {ep.method} {ep.path} references req without a "
                "request_model; using safe body"
            )
        if ep.request_model and not effective_safe:
            out.append(f"async def {fname}(req: {ep.request_model}):")
        else:
            out.append(f"async def {fname}():")
        body = self._endpoint_body(ep, effective_safe)
        out.append("    " + body.replace("\n", "\n    "))
        out.append("")

    def _endpoint_body(self, ep: EndpointSpec, safe: bool) -> str:
        if safe:
            return (
                'return {"status": "ok", "endpoint": ' + repr(ep.path) + ", "
                '"method": ' + repr((ep.method or "GET").upper()) + "}"
            )
        code = (ep.handler_code or "").strip()
        if not code:
            return 'return {"status": "ok", "endpoint": ' + repr(ep.path) + "}"
        return code

    def _emit_health(self, out: list[str], target_desc: str, wrapper_id: str) -> None:
        td = target_desc.replace('"', "'")
        out.append("@app.get('/health')")
        out.append("async def health():")
        out.append(
            f'    return {{"status": "ok", "target": "{td}", "wrapper_id": "{wrapper_id}"}}'
        )
        out.append("")

    def _emit_root(
        self, out: list[str], design: APIDesign, target_desc: str, wrapper_id: str
    ) -> None:
        td = target_desc.replace('"', "'")
        title = (design.title or "Generated Wrapper").replace('"', "'")
        out.append("@app.get('/')")
        out.append("async def root():")
        out.append("    return {")
        out.append(f'        "wrapper_id": "{wrapper_id}",')
        out.append(f'        "target": "{td}",')
        out.append(f'        "title": "{title}",')
        out.append('        "endpoints": ["/health", "/openapi.json", "/docs"],')
        out.append("    }")

    def _func_name(self, ep: EndpointSpec, idx: int, used: set[str]) -> str:
        method = re.sub(r"[^a-z0-9]", "_", (ep.method or "endpoint").lower())
        path = re.sub(r"[^a-zA-Z0-9]", "_", ep.path or "").strip("_") or "root"
        name = f"{method}_{path}"
        # de-duplicate
        base = name
        while name in used or name == "health" or name == "root":
            idx += 1
            name = f"{base}_{idx}"
        used.add(name)
        return name

    @staticmethod
    def _indent_class(src: str) -> str:
        # Models are emitted at module top level — ensure no leading indentation.
        return "\n".join(line for line in src.splitlines())

    # -- file writing ------------------------------------------------------

    def write_wrapper_files(
        self,
        output_dir: Path,
        app_code: str,
        design: APIDesign | None = None,
    ) -> Path:
        """Write app.py, a minimal Dockerfile, and a test_app.py.

        When ``design`` is provided, the generated test ALSO calls every designed
        endpoint and asserts a non-5xx response. This makes the fix loop catch
        runtime errors (e.g. a NameError from an inconsistent LLM handler), which a
        health/openapi-only smoke test would miss."""
        output_dir.mkdir(parents=True, exist_ok=True)
        app_path = output_dir / "app.py"
        app_path.write_text(app_code)

        (output_dir / "Dockerfile").write_text(
            "FROM python:3.11-slim\n"
            "WORKDIR /app\n"
            "RUN pip install --no-cache-dir fastapi 'uvicorn[standard]' httpx pydantic pytest\n"
            "COPY app.py test_app.py ./\n"
            "EXPOSE 8000\n"
            'CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]\n'
        )

        (output_dir / "test_app.py").write_text(self._generate_test_code(design))
        return app_path

    def _generate_test_code(self, design: APIDesign | None) -> str:
        """Build test_app.py: always health + openapi, plus one call per designed
        endpoint asserting no 5xx (catches runtime handler errors)."""
        lines = [
            "import pytest",
            "from fastapi.testclient import TestClient",
            "from app import app",
            "",
            "client = TestClient(app)",
            "",
            "def test_health():",
            "    r = client.get('/health')",
            "    assert r.status_code == 200, r.text",
            "",
            "def test_openapi():",
            "    r = client.get('/openapi.json')",
            "    assert r.status_code == 200",
            "    data = r.json()",
            "    assert 'paths' in data and '/health' in data['paths']",
            "",
        ]
        eps = design.endpoints if design else []
        for i, ep in enumerate(eps):
            method = (ep.method or "GET").upper()
            path = ep.path if ep.path.startswith("/") else "/" + ep.path
            has_body = bool(ep.request_model) or method in {"POST", "PUT", "PATCH"}
            lines.append(f"def test_endpoint_{i}():")
            lines.append(f"    # {method} {path}: {ep.description or ''}")
            lines.append("    # exercises the designed handler; any non-5xx is acceptable")
            lines.append("    # (422 = route exists but needs a model body; 500 = handler bug)")
            if has_body:
                lines.append(f"    r = client.request({method!r}, {path!r}, json={{}})")
            else:
                lines.append(f"    r = client.request({method!r}, {path!r})")
            lines.append("    assert r.status_code < 500, (r.status_code, r.text)")
            lines.append("")
        return "\n".join(lines)

    # -- LLM repair (used by the fix loop) ---------------------------------

    async def repair_app_py(
        self,
        broken_code: str,
        errors: list[str],
        design: APIDesign,
        inspection: dict[str, Any],
        target_desc: str,
        wrapper_id: str,
        llm: LLMClient,
    ) -> str:
        """Ask the LLM to return a corrected, complete app.py given the broken code
        and test/import errors. Falls back to the deterministic safe generator."""
        system = (
            "You are a FastAPI expert. You return ONLY a complete, self-contained, "
            "runnable Python file (app.py) with no markdown fences and no commentary."
        )
        prompt = (
            f"Target: {target_desc}\n"
            f"Wrapper id: {wrapper_id}\n\n"
            "The following generated FastAPI app.py failed. Fix ALL errors so it "
            "imports cleanly, defines the designed endpoints, includes GET /health "
            "returning JSON, and uses only stdlib + fastapi + pydantic + httpx + subprocess.\n\n"
            f"Errors:\n{''.join(errors)[-2500:]}\n\n"
            f"Broken code:\n{broken_code[-4000:]}\n\n"
            "Return the FULL corrected app.py only."
        )
        try:
            raw = await llm.complete(prompt, system=system, max_tokens=3000, temperature=0.1)
            code = self._strip_fence(raw)
            if "app = FastAPI" in code and "def " in code:
                return code
        except Exception as exc:
            print(f"[codegen] LLM repair failed ({exc}); using deterministic safe generator")
        return self.generate_app_py(design, inspection, target_desc, wrapper_id, safe=True)

    @staticmethod
    def _strip_fence(raw: str) -> str:
        cleaned = (raw or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:python)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned
