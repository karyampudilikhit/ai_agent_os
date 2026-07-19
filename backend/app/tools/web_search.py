"""Tavily web-search connector — the first Vision AI tool that gives
employees access to the real world instead of the model's frozen
training data.

Every specialist DynamicEmployee whose sub-task looks like it needs
current information gets a top-N-results snippet block injected into
its prompt before it runs. No tool-use loop, no explicit LLM decision
to search — a cheap heuristic decides based on the sub-task's wording.
This keeps the wall-clock cost low while making the "our employees
actually do research" claim genuinely true.

Failure modes are deliberately quiet: no API key configured, network
error, malformed response — the employee just gets no web_context and
behaves the way it did before. Never breaks the run.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
DEFAULT_MAX_RESULTS = 5
DEFAULT_TIMEOUT = 12.0

# Heuristic triggers — words in a sub-task that suggest current, real,
# researchable content is needed. Intentionally broad; false positives
# just mean a wasted search call, but false negatives mean an employee
# hallucinates something that could have been checked.
_TRIGGER_WORDS = (
    "research", "analyze", "find", "look up", "find out",
    "current", "latest", "recent", "today", "this week", "this month",
    "market", "competitor", "trend", "trends", "landscape",
    "who is", "what is", "how many", "how much", "how does",
    "study", "report", "statistics", "data on",
    "news", "product hunt", "reviews",
    "case study", "case studies",
    "compare", "vs.", "versus",
)


def should_search(text: str) -> bool:
    """True when the sub-task's wording suggests real-world data would
    help. Deliberately over-triggers rather than under-triggers."""
    if not text:
        return False
    lowered = text.lower()
    return any(t in lowered for t in _TRIGGER_WORDS)


class TavilySearchTool:
    def __init__(self, api_key: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY")
        self.timeout = timeout
        self.enabled = bool(self.api_key)
        if not self.enabled:
            logger.info("Tavily: no API key configured — web search disabled, employees will run without real-time data.")

    def search(self, query: str, max_results: int = DEFAULT_MAX_RESULTS) -> List[Dict[str, str]]:
        """Return top-N results as [{title, url, snippet}]. Empty list on
        any failure — callers already handle 'no results' gracefully."""
        if not self.enabled or not query:
            return []
        payload = {
            "api_key": self.api_key,
            "query": query.strip()[:400],
            "search_depth": "basic",  # basic is ~1s; advanced is ~4s
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            resp = httpx.post(TAVILY_URL, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Tavily search failed for %r: %s", query[:60], exc)
            return []

        out: List[Dict[str, str]] = []
        for item in (data.get("results") or [])[:max_results]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            snippet = str(item.get("content", "")).strip()
            if title or url or snippet:
                out.append({"title": title, "url": url, "snippet": snippet})
        return out

    def format_for_prompt(self, query: str, results: List[Dict[str, str]]) -> str:
        """Turn results into a compact block an employee can read. Empty
        string if no results — caller just skips the injection."""
        if not results:
            return ""
        lines = [
            f"RECENT WEB RESULTS FOR: \"{query}\"",
            "(Real search results — use them for facts, cite by URL if you quote specific numbers.)",
            "",
        ]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.get('title') or '(untitled)'}")
            if r.get("url"):
                lines.append(f"   {r['url']}")
            snippet = (r.get("snippet") or "").replace("\n", " ").strip()
            if snippet:
                lines.append(f"   {snippet[:400]}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def search_and_format(self, query: str, max_results: int = DEFAULT_MAX_RESULTS) -> str:
        """One-shot: search + format. Convenience for the employee-side call."""
        return self.format_for_prompt(query, self.search(query, max_results=max_results))
