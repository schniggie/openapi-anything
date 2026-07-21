"""Opt-in gateway auth: GATEWAY_API_KEY protects admin routes only.

Unset (default) -> fully open, matching the project's local-trust default.
Set -> admin routes (generate, jobs, delete, regenerate, logs/source,
registry, metrics, hub UI, the gateway-wide /mcp with its generate/regenerate
meta tools) require the key. Deployed-wrapper traffic (the /services/{id}/*
proxy and a wrapper's own /mcp) and /health are never gated — by design,
other systems should be able to use a deployed API without holding the
gateway's admin key.

Accepts either `X-API-Key: <key>` (API/MCP clients) or HTTP Basic auth with
the key as the password, any username (browsers prompt for this natively —
no custom login page needed for the hub).
"""

import os
import secrets

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_basic = HTTPBasic(auto_error=False)


async def require_auth(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> None:
    configured_key = os.getenv("GATEWAY_API_KEY", "")
    if not configured_key:
        return  # auth disabled

    provided = x_api_key or (credentials.password if credentials else None)
    if provided and secrets.compare_digest(provided, configured_key):
        return

    raise HTTPException(
        status_code=401,
        detail="Missing or invalid credentials (X-API-Key header or HTTP Basic password)",
        headers={"WWW-Authenticate": "Basic"},
    )
