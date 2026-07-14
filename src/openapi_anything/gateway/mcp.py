"""MCP export: serve deployed wrappers as MCP tools over Streamable HTTP.

One gateway-wide endpoint (``POST /mcp``) exposes every reachable wrapper's
endpoints as tools named ``{wrapper_id}__{method}_{path}`` plus meta tools
(``list_apis``, ``generate_api``, ``job_status``) so an agent can request a new
API and poll until its tools appear. ``POST /services/{id}/mcp`` serves the
same tools filtered to one wrapper (no meta tools).

Deliberately minimal and stateless: each POST gets a plain JSON response (the
Streamable HTTP spec permits this — no SSE stream, no sessions), so no MCP SDK
dependency is needed. Tool schemas are derived live from each wrapper's
``openapi.json`` (cached ``MCP_SPEC_TTL`` seconds, default 30).
"""

import json
import os
import re
import time
import uuid
from typing import Any, Callable

import httpx

from openapi_anything.service import generate_and_deploy

from .jobs import JobStore, get_job_store
from .registry import Registry, WrapperEntry, get_registry

PROTOCOL_VERSION = "2025-06-18"
_SKIP_PATHS = {"/", "/health"}
_METHODS = ("get", "post", "put", "delete", "patch")


def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-zA-Z0-9_]", "_", text)).strip("_")


def _resolve_refs(schema: Any, components: dict) -> Any:
    """Shallow-resolve local $refs so tool inputSchemas are self-contained."""
    if isinstance(schema, dict):
        ref = schema.get("$ref", "")
        if ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[-1]
            return _resolve_refs(components.get(name, {}), components)
        return {k: _resolve_refs(v, components) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_resolve_refs(v, components) for v in schema]
    return schema


def _operations(spec: dict) -> list[dict]:
    """Flatten an OpenAPI spec into callable operations (skipping meta paths)."""
    components = spec.get("components", {}).get("schemas", {})
    ops = []
    for path, methods in spec.get("paths", {}).items():
        if path in _SKIP_PATHS:
            continue
        for method in _METHODS:
            op = methods.get(method)
            if not isinstance(op, dict):
                continue
            properties: dict[str, Any] = {}
            required: list[str] = []
            query_params: list[str] = []
            path_params: list[str] = []
            for param in op.get("parameters", []):
                pname = param.get("name")
                if not pname:
                    continue
                properties[pname] = _resolve_refs(param.get("schema", {}), components)
                if param.get("description"):
                    properties[pname]["description"] = param["description"]
                if param.get("in") == "path":
                    path_params.append(pname)
                    required.append(pname)
                else:
                    query_params.append(pname)
                    if param.get("required"):
                        required.append(pname)
            body = (
                op.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            if body:
                body = _resolve_refs(body, components)
                if body.get("type") == "object":
                    properties.update(body.get("properties", {}))
                    required.extend(body.get("required", []))
                else:
                    properties["body"] = body
            ops.append(
                {
                    "slug": _slug(f"{method}_{path}"),
                    "method": method.upper(),
                    "path": path,
                    "summary": op.get("summary") or op.get("description") or f"{method.upper()} {path}",
                    "properties": properties,
                    "required": required,
                    "query_params": query_params,
                    "path_params": path_params,
                }
            )
    return ops


def openapi_to_tools(wrapper_id: str, target_desc: str, spec: dict) -> list[dict]:
    """MCP tool definitions for one wrapper's OpenAPI spec."""
    tools = []
    for op in _operations(spec):
        tools.append(
            {
                "name": f"{wrapper_id}__{op['slug']}",
                "description": f"{op['summary']} — endpoint of the '{wrapper_id}' API ({target_desc})",
                "inputSchema": {
                    "type": "object",
                    "properties": op["properties"],
                    "required": sorted(set(op["required"])),
                },
            }
        )
    return tools


_META_TOOLS = [
    {
        "name": "list_apis",
        "description": "List all deployed API wrappers (id, target, status).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "generate_api",
        "description": (
            "Generate and deploy a new REST API wrapper from a natural-language "
            "description. Returns a job_id immediately; poll with job_status. Once "
            "completed, the new API's endpoints appear as tools on this server."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What to wrap"},
                "wrapper_id": {"type": "string", "description": "Optional id (auto if omitted)"},
            },
            "required": ["description"],
        },
    },
    {
        "name": "regenerate_api",
        "description": (
            "Re-run the generation pipeline for an existing API wrapper (optionally "
            "with a refined description). The previous verification report is fed to "
            "the designer so it iterates instead of starting blind. Returns a job_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wrapper_id": {"type": "string"},
                "description": {"type": "string", "description": "Optional refined description"},
            },
            "required": ["wrapper_id"],
        },
    },
    {
        "name": "job_status",
        "description": "Status of a generation job (status, live phase, result or error).",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
]


class MCPGateway:
    def __init__(
        self,
        registry: Registry,
        jobs: JobStore,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self.registry = registry
        self.jobs = jobs
        self.client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=float(os.getenv("PROXY_TIMEOUT", "30")))
        )
        self._spec_ttl = float(os.getenv("MCP_SPEC_TTL", "30"))
        self._spec_cache: dict[str, tuple[float, dict]] = {}

    # ---- spec access -------------------------------------------------------

    async def _spec(self, entry: WrapperEntry) -> dict | None:
        cached = self._spec_cache.get(entry.id)
        if cached and time.monotonic() - cached[0] < self._spec_ttl:
            return cached[1]
        try:
            async with self.client_factory() as client:
                resp = await client.get(f"{entry.service_url}/openapi.json")
                resp.raise_for_status()
                spec = resp.json()
        except Exception as exc:
            print(f"[mcp] failed to fetch openapi.json for {entry.id}: {exc}")
            return None
        self._spec_cache[entry.id] = (time.monotonic(), spec)
        return spec

    def _entries(self, wrapper_filter: str | None) -> list[WrapperEntry]:
        return [
            e
            for e in self.registry.list_all()
            if e.status in ("healthy", "degraded")
            and (wrapper_filter is None or e.id == wrapper_filter)
        ]

    # ---- JSON-RPC dispatch --------------------------------------------------

    async def handle(self, body: dict, wrapper_filter: str | None = None) -> dict | None:
        """Handle one JSON-RPC message. Returns None for notifications."""
        method = body.get("method", "")
        msg_id = body.get("id")
        if method.startswith("notifications/"):
            return None

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": body.get("params", {}).get("protocolVersion")
                    or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "openapi-anything", "version": "0.1.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": await self._list_tools(wrapper_filter)}
            elif method == "tools/call":
                params = body.get("params", {})
                result = await self._call_tool(
                    params.get("name", ""), params.get("arguments") or {}, wrapper_filter
                )
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    async def _list_tools(self, wrapper_filter: str | None) -> list[dict]:
        tools: list[dict] = []
        for entry in self._entries(wrapper_filter):
            spec = await self._spec(entry)
            if spec:
                tools.extend(openapi_to_tools(entry.id, entry.target_description, spec))
        if wrapper_filter is None:
            tools.extend(_META_TOOLS)
        return tools

    # ---- tool execution ------------------------------------------------------

    @staticmethod
    def _tool_result(payload: Any, is_error: bool = False) -> dict:
        text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        return {"content": [{"type": "text", "text": text}], "isError": is_error}

    async def _call_tool(self, name: str, args: dict, wrapper_filter: str | None) -> dict:
        if wrapper_filter is None:
            meta = await self._call_meta(name, args)
            if meta is not None:
                return meta

        wrapper_id, sep, slug = name.partition("__")
        if not sep:
            return self._tool_result(f"Unknown tool: {name}", is_error=True)
        entry = self.registry.get(wrapper_id)
        if not entry or (wrapper_filter is not None and wrapper_id != wrapper_filter):
            return self._tool_result(f"Unknown API wrapper: {wrapper_id}", is_error=True)
        spec = await self._spec(entry)
        if not spec:
            return self._tool_result(f"Wrapper {wrapper_id} spec unavailable", is_error=True)
        op = next((o for o in _operations(spec) if o["slug"] == slug), None)
        if not op:
            return self._tool_result(f"Unknown operation: {slug}", is_error=True)

        path = op["path"]
        remaining = dict(args)
        for pname in op["path_params"]:
            path = path.replace("{" + pname + "}", str(remaining.pop(pname, "")))
        if op["method"] in ("GET", "DELETE"):
            query, body = remaining, None
        else:
            query = {k: remaining.pop(k) for k in op["query_params"] if k in remaining}
            body = remaining or None

        try:
            async with self.client_factory() as client:
                resp = await client.request(
                    op["method"],
                    f"{entry.service_url}{path}",
                    params=query or None,
                    json=body,
                )
        except Exception as exc:
            return self._tool_result(f"Request to {wrapper_id} failed: {exc}", is_error=True)

        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text
        return self._tool_result(payload, is_error=resp.status_code >= 400)

    async def _call_meta(self, name: str, args: dict) -> dict | None:
        """Meta tools on the gateway-wide endpoint. None = not a meta tool."""
        if name == "list_apis":
            return self._tool_result(
                [
                    {"id": e.id, "target": e.target_description, "status": e.status}
                    for e in self.registry.list_all()
                ]
            )
        if name == "generate_api":
            description = args.get("description", "")
            if not description:
                return self._tool_result("'description' is required", is_error=True)
            wid = args.get("wrapper_id") or f"wrapper-{uuid.uuid4().hex[:8]}"
            job = self.jobs.submit(
                description,
                wid,
                lambda report: generate_and_deploy(
                    description, self.registry, wid, on_phase=report
                ),
            )
            return self._tool_result(
                {"job_id": job.id, "wrapper_id": wid, "status": "queued",
                 "hint": "poll with job_status; tools appear here once completed"}
            )
        if name == "regenerate_api":
            wid = args.get("wrapper_id", "")
            entry = self.registry.get(wid)
            if not entry:
                return self._tool_result(f"Unknown API wrapper: {wid}", is_error=True)
            if self.jobs.active_for(wid):
                return self._tool_result(f"A job for {wid} is already running", is_error=True)
            description = args.get("description") or entry.target_description
            prior = {
                "previous_description": entry.target_description,
                "verification": entry.verification,
            }
            job = self.jobs.submit(
                description,
                wid,
                lambda report: generate_and_deploy(
                    description, self.registry, wid, on_phase=report, prior=prior
                ),
            )
            return self._tool_result(
                {"job_id": job.id, "wrapper_id": wid, "status": "queued",
                 "hint": "poll with job_status"}
            )
        if name == "job_status":
            job = self.jobs.get(args.get("job_id", ""))
            if not job:
                return self._tool_result("Job not found", is_error=True)
            return self._tool_result(job.to_public())
        return None


_mcp_singleton: MCPGateway | None = None


def get_mcp() -> MCPGateway:
    global _mcp_singleton
    if _mcp_singleton is None:
        _mcp_singleton = MCPGateway(registry=get_registry(), jobs=get_job_store())
    return _mcp_singleton
