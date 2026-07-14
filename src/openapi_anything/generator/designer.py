"""API Designer: LLM autonomously designs clean REST API (endpoints, models, handlers)
from target inspection. The produced APIDesign is fully consumed by the code generator
(title, description, models, endpoints and per-endpoint handler bodies all flow into
the generated wrapper) — nothing is hardcoded downstream.
"""

import json
from typing import Any

from pydantic import BaseModel, Field

from .llm_client import LLMClient


class EndpointSpec(BaseModel):
    """A single REST endpoint. ``handler_code`` is the body of an async function
    (no signature). The code generator wraps it with a decorator + signature derived
    from method/path/request_model."""

    model_config = {"extra": "ignore"}
    method: str
    path: str
    description: str = ""
    request_model: str | None = None  # Pydantic class name expected on `req`
    response_model: str | None = None
    handler_code: str = ""  # python source: async function BODY only


class APIDesign(BaseModel):
    """Full REST design produced by the LLM. Every field is consumed by the
    CodeGenerator when assembling the wrapper's app.py."""

    model_config = {"extra": "ignore"}
    title: str = "Generated Wrapper"
    description: str = ""
    target_type: str = "cli"  # 'cli' | 'web' | 'github'
    endpoints: list[EndpointSpec] = Field(default_factory=list)
    models: dict[str, str] = Field(default_factory=dict)  # name -> python source
    integration_notes: str = ""


def _default_design(target_desc: str, target_type: str) -> APIDesign:
    """Deterministic fallback design used when the LLM is unavailable or unparseable."""
    if target_type == "web":
        return APIDesign(
            title="Web Wrapper",
            description=target_desc,
            target_type="web",
            endpoints=[
                EndpointSpec(
                    method="GET",
                    path="/fetch",
                    description="Fetch from the wrapped web service",
                    request_model=None,
                    handler_code=(
                        'return await proxy_request("GET", "/get")'
                    ),
                ),
                EndpointSpec(
                    method="POST",
                    path="/proxy",
                    description="Proxy a request to the wrapped web service",
                    request_model="ProxyRequest",
                    handler_code=(
                        'return await proxy_request(req.method.upper(), req.path, req.body)'
                    ),
                ),
            ],
            models={
                "ProxyRequest": (
                    "class ProxyRequest(BaseModel):\n"
                    "    path: str = '/get'\n"
                    "    method: str = 'GET'\n"
                    "    body: dict | None = None"
                )
            },
            integration_notes="httpx-based proxy",
        )
    # cli / github default
    return APIDesign(
        title="CLI Wrapper",
        description=target_desc,
        target_type="cli",
        endpoints=[
            EndpointSpec(
                method="POST",
                path="/execute",
                description="Execute the wrapped command",
                request_model="ExecuteRequest",
                handler_code="return run_command(req.args)",
            )
        ],
        models={
            "ExecuteRequest": (
                "class ExecuteRequest(BaseModel):\n"
                "    args: list[str] = []"
            )
        },
        integration_notes="subprocess-based execution",
    )


class Designer:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def design(
        self,
        inspection: dict[str, Any],
        target_desc: str,
        prior: dict[str, Any] | None = None,
    ) -> APIDesign:
        """Use the LLM to produce a full APIDesign (endpoints + models + handler bodies).

        ``prior`` carries context from a previous version when regenerating
        (old description + verification report) so the design iterates instead
        of starting blind."""
        target_type = inspection.get("type", "cli")

        prior_section = ""
        if prior:
            prior_section = (
                "\nA previous version of this wrapper exists. Improve on it: keep "
                "endpoints that verified ok, fix or replace the ones that failed.\n"
                f"Previous version context:\n{json.dumps(prior, default=str)[:1200]}\n"
            )

        system_prompt = (
            "You are an expert at wrapping arbitrary targets into clean, agent-usable "
            "REST APIs with FastAPI. You design RESTful endpoints using standard verbs "
            "and Pydantic v2 models so the auto-generated OpenAPI is complete and correct. "
            "Output ONLY valid JSON matching the requested schema. No prose, no markdown."
        )

        helpers = (
            "Available helper functions inside the generated app (call them from handler_code):\n"
            "- CLI targets: run_command(args: list[str]) -> dict  # returns "
            "{'stdout','stderr','returncode'}\n"
            "- Web targets: await proxy_request(method: str, path: str, body: dict|None=None) -> dict  "
            "# returns {'status_code','body'}; BASE_URL is also available."
        )

        prompt = f"""
Target description: {target_desc}

Inspection results:
{json.dumps(inspection, default=str)[:2500]}
{prior_section}
{helpers}

Design a complete REST API and respond with JSON only using exactly this schema:
{{
  "title": "short title",
  "description": "one-line description",
  "target_type": "{target_type}",
  "endpoints": [
    {{
      "method": "GET|POST|PUT|DELETE|PATCH",
      "path": "/something",
      "description": "what it does",
      "request_model": "PydanticClassName or null",
      "response_model": "PydanticClassName or null",
      "handler_code": "the BODY ONLY of an async function (no 'async def', no signature). "
                      "If request_model is set, the parameter 'req' is available. Return a dict."
    }}
  ],
  "models": {{
    "ClassName": "full python source of the class (e.g. 'class Foo(BaseModel):\\n    bar: str')"
  }},
  "integration_notes": "brief notes"
}}

Rules:
- Always include enough endpoints to fully expose the target's core functionality.
- handler_code must be valid Python that only uses the helpers above, `req`, and the models.
- Every request_model referenced in endpoints MUST be defined in "models".
- Do NOT include /health or / (the generator adds those automatically).
"""
        # Use GLM-5.1 JSON mode (verified supported by this LiteLLM proxy) for reliably
        # parseable designs. Retry a few times: json_object mode occasionally returns
        # empty content transiently (finish_reason="length"), which clears on retry.
        data = None
        last_err = None
        for attempt in range(3):
            try:
                data = await self.llm.complete_json(prompt, system=system_prompt)
                break
            except Exception as exc:  # network / auth / empty-content / bad-json
                last_err = exc
                print(f"[designer] complete_json attempt {attempt + 1}/3 failed ({exc})")

        if data is None:
            print(f"[designer] LLM design unavailable ({last_err}); using default design")
            return _default_design(target_desc, target_type)

        try:
            return self._parse(data, target_desc, target_type)
        except Exception as exc:
            print(f"[designer] Failed to validate LLM design ({exc}); using default design")
            return _default_design(target_desc, target_type)

    def _parse(self, data: Any, target_desc: str, target_type: str) -> APIDesign:
        if "target_type" not in data:
            data["target_type"] = target_type
        # Coerce models: accept dict name->source; if list of objects, normalize.
        models = data.get("models", {})
        if isinstance(models, list):
            norm: dict[str, str] = {}
            for item in models:
                if isinstance(item, dict) and "name" in item:
                    src = item.get("source") or item.get("code") or ""
                    norm[str(item["name"])] = src
            models = norm
        elif not isinstance(models, dict):
            models = {}
        data["models"] = {str(k): str(v) for k, v in models.items()}
        design = APIDesign.model_validate(data)
        # Guarantee the design is internally consistent: every request_model must
        # have a model definition; drop the annotation otherwise so generated code imports.
        defined = set(design.models.keys())
        for ep in design.endpoints:
            if ep.request_model and ep.request_model not in defined:
                ep.request_model = None
        if not design.endpoints:
            return _default_design(target_desc, target_type)
        return design
