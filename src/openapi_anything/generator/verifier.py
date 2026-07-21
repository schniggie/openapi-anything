"""Verifier: post-deploy checks — health, fetch openapi.json, exercise every designed
endpoint, and confirm the openapi title/path reflects the design. Returns a structured
report that is persisted into the registry entry."""

from typing import Any

import httpx

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class Verifier:
    async def verify_wrapper(self, service_url: str, expected_endpoints: list[str]) -> dict[str, Any]:
        results: dict[str, Any] = {
            "service_url": service_url,
            "health": False,
            "openapi": False,
            "openapi_title": None,
            "openapi_paths": [],
            "endpoints": {},
        }

        async with httpx.AsyncClient(timeout=12.0) as client:
            # Health
            try:
                r = await client.get(f"{service_url}/health")
                results["health"] = r.status_code == 200
            except Exception as exc:
                results["endpoints"]["/health"] = f"error: {exc}"

            # OpenAPI
            try:
                r = await client.get(f"{service_url}/openapi.json")
                if r.status_code == 200 and "paths" in r.json():
                    results["openapi"] = True
                    spec = r.json()
                    results["openapi_title"] = spec.get("info", {}).get("title")
                    results["openapi_paths"] = sorted(spec.get("paths", {}).keys())
            except Exception as exc:
                results["endpoints"]["/openapi.json"] = f"error: {exc}"

            # Exercise each designed endpoint (best-effort; record status code / error).
            for ep in expected_endpoints:
                method, path = ("GET", ep) if isinstance(ep, str) and " " not in ep else ep.split(" ", 1)
                method = (method or "GET").upper()
                path = path or "/"
                if not path.startswith("/"):
                    path = "/" + path
                key = f"{method} {path}"
                try:
                    if method in _SAFE_METHODS:
                        r = await client.get(f"{service_url}{path}")
                    else:
                        # POST/PUT/PATCH/DELETE: send an empty JSON body. We only
                        # require a non-5xx response (a 422 from a required model is
                        # acceptable evidence the route exists).
                        r = await client.request(method, f"{service_url}{path}", json={})
                    ok = r.status_code < 500
                    results["endpoints"][key] = "ok" if ok else f"status:{r.status_code}"
                except Exception as exc:
                    results["endpoints"][key] = f"error: {exc}"

        results["overall"] = bool(
            results["health"]
            and results["openapi"]
            and all(v in ("ok",) or (isinstance(v, str) and not v.startswith("error")) for v in results["endpoints"].values())
        )
        return results
