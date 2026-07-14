"""Proxy logic: route /services/{id}/* to the correct backend container via registry lookup."""

import os

import httpx
from fastapi import Request, HTTPException
from starlette.responses import StreamingResponse
from .registry import get_registry


class GatewayProxy:
    def __init__(self, timeout: float | None = None):
        if timeout is None:
            timeout = float(os.getenv("PROXY_TIMEOUT", "30"))
        self.client = httpx.AsyncClient(timeout=timeout)

    async def proxy_request(self, wrapper_id: str, path: str, request: Request):
        reg = get_registry()
        entry = reg.get(wrapper_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Wrapper {wrapper_id} not found")
        if entry.status not in ("healthy", "starting"):
            raise HTTPException(status_code=503, detail=f"Wrapper {wrapper_id} is {entry.status}")

        target_url = entry.service_url.rstrip("/") + "/" + path.lstrip("/")
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length")
        }
        body = await request.body()
        try:
            resp = await self.client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
                params=request.query_params,
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Proxy error: {exc}") from exc

        return StreamingResponse(
            resp.aiter_bytes(),
            status_code=resp.status_code,
            headers=dict(resp.headers),
            media_type=resp.headers.get("content-type"),
        )