"""SearxNG web search client: research tool for the inspection phase.

Queries a SearxNG instance (JSON API) so the pipeline can gather real-world
context about a target (docs, API references) before designing the wrapper.
Configured via ``SEARXNG_BASE_URL``; failures degrade to empty results so the
pipeline never blocks on search availability.
"""

import os

import httpx

DEFAULT_SEARXNG_BASE_URL = "https://searxng.schnigg.ie"


class SearxNGClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = (
            base_url or os.getenv("SEARXNG_BASE_URL", DEFAULT_SEARXNG_BASE_URL)
        ).rstrip("/")
        self.timeout = timeout if timeout is not None else float(os.getenv("SEARXNG_TIMEOUT", "10"))
        self.default_max_results = int(os.getenv("SEARXNG_MAX_RESULTS", "5"))
        self._transport = transport  # injectable for tests

    async def search(self, query: str, max_results: int | None = None) -> list[dict[str, str]]:
        """Search SearxNG; returns [{'title','url','snippet'}], [] on any failure."""
        if max_results is None:
            max_results = self.default_max_results
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self.timeout
            ) as client:
                resp = await client.get(
                    f"{self.base_url}/search",
                    params={"q": query, "format": "json"},
                )
                resp.raise_for_status()
                raw = resp.json().get("results", [])
        except Exception as exc:
            print(f"[websearch] search failed ({exc}); continuing without results")
            return []

        return [
            {
                "title": str(item.get("title", "")),
                "url": str(item.get("url", "")),
                "snippet": str(item.get("content", "")),
            }
            for item in raw[:max_results]
        ]

    @staticmethod
    def format_results(results: list[dict[str, str]]) -> str:
        """Compact one-line-per-hit block for embedding in LLM prompts."""
        return "\n".join(
            f"- {r['title']} ({r['url']}): {r['snippet']}" for r in results
        )
