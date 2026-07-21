"""LiteLLM client: OpenAI-compatible interface to GLM-5.x via LiteLLM.

Provides text completion, JSON-mode completion, structured (Pydantic) output, and
tool calling for the agentic pipeline. Requires LITELLM_API_KEY (no default —
never hardcode a real key here; this file is public).

Note on JSON mode: GLM-5.1 via this LiteLLM proxy accepts
``response_format={"type": "json_object"}`` and returns well-formed JSON *provided*
``max_tokens`` is large enough — with too small a budget the model returns empty
content with ``finish_reason="length"``. ``complete_json`` therefore uses a generous
budget (3000) and treats empty content as a parse failure so callers can retry/fallback.
"""

import json
import os
from typing import Any, Type, TypeVar, cast

from openai import AsyncOpenAI
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# LiteLLM config from plan
DEFAULT_MODEL = "GLM-5.2"  # exact match from proxy /v1/models list; override: LLM_MODEL

# GLM-5.x are reasoning models: hidden reasoning_content counts against max_tokens,
# and complex prompts burn >3000 tokens on reasoning alone — yielding empty content
# with finish_reason="length" *deterministically* (verified 2026-07-04 with the real
# designer prompt: 3000 failed 3/3, 8000 succeeded in ~60s, inside the 120s timeout).
# The Designer's retries cover residual length-failures. Override: LLM_JSON_MAX_TOKENS.
JSON_MODE_MAX_TOKENS = 8000


class LLMClient:
    """Env config (read at construction so the compose stack controls it):
    LITELLM_BASE_URL, LITELLM_API_KEY, LLM_MODEL, LLM_REQUEST_TIMEOUT,
    LLM_JSON_MAX_TOKENS."""

    def __init__(self, model: str | None = None):
        self.client = AsyncOpenAI(
            base_url=os.getenv("LITELLM_BASE_URL", "https://litellm.xn--8pr.xyz/v1"),
            # A placeholder (never a real default — this file is public): newer
            # openai SDK versions validate credentials eagerly and raise on a
            # falsy api_key, which would break construction-time robustness.
            # The real auth failure surfaces at the actual API call instead,
            # where every call site already handles it (try/except + fallback).
            api_key=os.getenv("LITELLM_API_KEY") or "not-configured",
            # A full json-mode design normally completes in ~20-60s; generous headroom
            # while still failing fast rather than hanging indefinitely.
            timeout=float(os.getenv("LLM_REQUEST_TIMEOUT", "120")),
        )
        self.model = model or os.getenv("LLM_MODEL", DEFAULT_MODEL)

    async def complete(
        self, prompt: str, system: str = "", max_tokens: int = 2000, temperature: float = 0.2
    ) -> str:
        """Simple text completion."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # model/messages are cast: this talks to a LiteLLM proxy that accepts
        # arbitrary model names (GLM-5.x, Kimi, etc.), not just the openai SDK's
        # narrow Literal union of OpenAI's own model names.
        resp = await self.client.chat.completions.create(
            model=cast(Any, self.model),
            messages=cast(Any, messages),
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""

    async def complete_json(
        self, prompt: str, system: str = "", max_tokens: int | None = None, temperature: float = 0.1
    ) -> dict[str, Any]:
        """JSON-mode completion. Returns a parsed dict.

        Raises ``ValueError`` if the model returned empty/non-JSON content (callers
        should retry or fall back). Needs an adequate token budget for reasoning
        models — see module docstring."""
        if max_tokens is None:
            max_tokens = int(os.getenv("LLM_JSON_MAX_TOKENS", str(JSON_MODE_MAX_TOKENS)))
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        resp = await self.client.chat.completions.create(
            model=cast(Any, self.model),
            messages=cast(Any, messages),
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=temperature,
        )
        content = resp.choices[0].message.content or ""
        content = content.strip()
        if not content:
            raise ValueError(
                f"json_object mode returned empty content (finish_reason="
                f"{resp.choices[0].finish_reason}); max_tokens too small?"
            )
        try:
            return cast(dict[str, Any], json.loads(content))
        except json.JSONDecodeError as exc:
            raise ValueError(f"json_object content not valid JSON: {exc}") from exc

    async def complete_structured(self, prompt: str, response_model: Type[T], system: str = "") -> T:
        """Structured output via JSON mode + Pydantic parse."""
        data = await self.complete_json(prompt, system=system)
        return response_model.model_validate(data)

    async def complete_with_tools(
        self, prompt: str, tools: list[dict[str, Any]], system: str = ""
    ) -> dict[str, Any]:
        """Tool calling support for inspection / code execution phases."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        resp = await self.client.chat.completions.create(
            model=cast(Any, self.model),
            messages=cast(Any, messages),
            tools=cast(Any, tools),
            tool_choice="auto",
            max_tokens=2000,
        )
        return resp.choices[0].message.model_dump() if resp.choices else {}
