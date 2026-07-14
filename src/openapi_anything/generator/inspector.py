"""Target Inspector: given NL description, inspects the target (CLI, GitHub, web service) using available tools + LLM analysis."""

import subprocess
import httpx
import re
from bs4 import BeautifulSoup
from pathlib import Path
from typing import Any, Dict
from .llm_client import LLMClient
from .websearch import SearxNGClient


class TargetInspector:
    def __init__(self, llm: LLMClient, search: SearxNGClient | None = None):
        self.llm = llm
        self.search = search if search is not None else SearxNGClient()
        self.temp_dir = Path("/tmp/openapi-anything-inspect")
        self.temp_dir.mkdir(exist_ok=True)

    async def _research(self, description: str) -> list[dict[str, str]]:
        """Web-search the target description; never raises (empty on failure)."""
        try:
            return await self.search.search(description)
        except Exception as exc:
            print(f"[inspector] web research failed ({exc}); continuing without it")
            return []

    async def inspect(self, description: str) -> Dict[str, Any]:
        """Main entry: determine target type from desc, inspect, return structured info for designer."""
        research = await self._research(description)
        research_block = SearxNGClient.format_results(research)

        # Simple heuristic for target type (LLM could improve)
        desc_lower = description.lower()
        if "cli" in desc_lower or "command" in desc_lower or re.search(r"\b(ls|cat|grep|docker)\b", desc_lower):
            result = await self._inspect_cli(description, research_block)
        elif "github.com" in desc_lower or "repo" in desc_lower:
            result = await self._inspect_github(description, research_block)
        else:
            # default to web service
            result = await self._inspect_web(description, research_block, research)

        result["web_research"] = research
        return result

    async def _inspect_cli(self, description: str, research_block: str = "") -> Dict[str, Any]:
        """Inspect CLI target: extract command, run --help, capture usage."""
        # Extract possible command name
        match = re.search(r"wrap (?:the )?([a-z0-9_-]+) (?:command|cli)", description, re.I)
        cmd = match.group(1) if match else "ls"  # default sample

        try:
            result = subprocess.run([cmd, "--help"], capture_output=True, text=True, timeout=5)
            help_text = result.stdout + result.stderr
        except Exception as e:
            help_text = f"Could not run {cmd} --help: {e}"

        try:
            research_section = (
                f"\n\nWeb research about the target:\n{research_block}" if research_block else ""
            )
            analysis = await self.llm.complete(
                f"Analyze this CLI help output and describe its main functionality, arguments, and output format for REST wrapping:\n{help_text[:2000]}{research_section}",
                system="You are an expert at turning CLIs into REST APIs. Extract key subcommands, options, input/output.",
            )
        except Exception as e:
            analysis = f"LLM unavailable ({e}); using heuristic CLI inspection for {cmd}"

        return {
            "type": "cli",
            "command": cmd,
            "help_text": help_text[:1500],
            "llm_analysis": analysis,
            "suggested_endpoints": ["POST /execute"],
        }

    async def _inspect_github(self, description: str, research_block: str = "") -> Dict[str, Any]:
        """Inspect GitHub repo: clone shallow, inspect README, entrypoints."""
        # Extract repo url
        match = re.search(r"(https?://github.com/[\w/-]+)", description)
        if not match:
            return {"type": "github", "error": "no repo url found"}

        repo_url = match.group(1)
        clone_dir = self.temp_dir / "repo"
        if clone_dir.exists():
            import shutil
            shutil.rmtree(clone_dir)

        try:
            subprocess.run(["git", "clone", "--depth", "1", repo_url, str(clone_dir)], check=True, timeout=30)
            readme = (clone_dir / "README.md").read_text()[:3000] if (clone_dir / "README.md").exists() else ""
        except Exception as e:
            return {"type": "github", "error": str(e)}

        try:
            research_section = (
                f"\n\nWeb research about the target:\n{research_block}" if research_block else ""
            )
            analysis = await self.llm.complete(
                f"Summarize what this GitHub repo does, its main CLI or entry point, and how to wrap its functionality as REST API:\n{readme}{research_section}",
                system="Expert at wrapping GitHub CLIs/tools into REST services.",
            )
        except Exception as e:
            analysis = f"LLM unavailable ({e}); using README excerpt only"

        return {
            "type": "github",
            "repo_url": repo_url,
            "readme_summary": readme[:500],
            "llm_analysis": analysis,
        }

    async def _inspect_web(
        self,
        description: str,
        research_block: str = "",
        research: list | None = None,
    ) -> Dict[str, Any]:
        """Inspect web target: fetch URL, parse with bs4 for forms/endpoints.

        Target resolution order: explicit URL in the description, else the top
        web-research hit, else the legacy httpbin default. Wrapping the wrong
        target silently (the old httpbin-always fallback) produced useless
        wrappers for requests like 'wrap Github trending'."""
        match = re.search(r"(https?://[\w\.-]+(?::\d+)?(?:/[\w/.-]*)?)", description)
        if match:
            url = match.group(1)
        else:
            url = next(
                (r["url"] for r in (research or []) if r.get("url")),
                "https://httpbin.org",
            )

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=10, follow_redirects=True)
                html = resp.text[:5000]
        except Exception as e:
            html = f"fetch error: {e}"

        soup = BeautifulSoup(html, "html.parser")
        forms = [str(f)[:300] for f in soup.find_all("form")][:3]
        links = [a.get("href") for a in soup.find_all("a", href=True)][:10]

        try:
            research_section = (
                f"\n\nWeb research about the target:\n{research_block}" if research_block else ""
            )
            analysis = await self.llm.complete(
                f"Analyze this web page HTML and suggest RESTful endpoints to expose its functionality (forms, main actions):\n{html[:1500]}{research_section}",
                system="You design clean REST APIs for web services. Focus on CRUD or action endpoints.",
            )
        except Exception as e:
            analysis = f"LLM unavailable ({e}); using HTML structure heuristics"

        return {
            "type": "web",
            "base_url": url,
            "forms_sample": forms,
            "links_sample": links,
            "llm_analysis": analysis,
            "suggested_endpoints": ["GET /scrape", "POST /submit-form"],
        }
