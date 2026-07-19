"""Read-only web fetch — the general-purpose "AI can read any URL"
capability. Complements Tavily search: Tavily tells the employee what
exists, this tool lets them actually read it.

Two ways this fires inside DynamicEmployee.run_task:

  1. AUTO-DETECT — any http(s) URL mentioned in the task text or the
     Supervisor's sub-task gets fetched, its main content extracted
     and injected into the prompt. Covers: "look at competitor.com",
     "check reddit.com/r/marketing", "read this pricing page".

  2. DEEP READ — after a Tavily search fires, the top 1-2 results' full
     page bodies get pulled in addition to the snippet. Snippets alone
     are often too shallow for real research; a real page gives an
     employee enough to write substantive analysis instead of "based
     on limited information…" hedges.

Design deliberately restrictive:
  - Read-only. No POST, no forms, no auth. Nothing that could act on
    the user's behalf. That's a v2 tool.
  - Bounded content size. Full webpages can be 100k+ tokens; we cap
    each fetch at MAX_TEXT_CHARS to keep prompts manageable.
  - Quiet failures. Bad URL, timeout, blocked, 4xx/5xx → we skip the
    injection and the employee behaves the way it did before. Never
    breaks a run because a page didn't load.
  - Realistic User-Agent + short timeout. Enough to look like a
    real browser without any anti-bot cat-and-mouse.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 12.0
MAX_TEXT_CHARS = 4000     # per page, so a 3-page fetch is ~12k chars
MAX_PAGES_PER_TASK = 4    # hard cap so we don't blow the context window

USER_AGENT = (
    "Mozilla/5.0 (VisionAI-Employee/1.0; +https://vision-ai.local) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Safari/537.36"
)

# Loose URL matcher — deliberately over-captures then we validate/dedupe
URL_RE = re.compile(r"https?://[^\s\"'>()\[\]]+", re.IGNORECASE)

# Tags whose text is almost never the article; strip them entirely.
_JUNK_TAGS = ("script", "style", "nav", "footer", "aside", "form",
              "noscript", "iframe", "svg", "button", "header")


def extract_urls(text: str) -> List[str]:
    """Return unique http(s) URLs mentioned in a text blob, preserving
    order of first appearance. Trailing punctuation stripped."""
    if not text:
        return []
    seen = []
    for raw in URL_RE.findall(text):
        # Common trailing junk (., ,, ), ], .) etc.)
        url = raw.rstrip(".,);:]!?").rstrip("'\"")
        if url and url not in seen:
            seen.append(url)
    return seen


class WebFetchTool:
    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.enabled = True  # no API key needed — read-only web

    def fetch(self, url: str) -> Optional[str]:
        """Return cleaned page text (bounded to MAX_TEXT_CHARS) or None
        on any failure. Callers should treat None as "no fetch"."""
        if not url or not url.startswith(("http://", "https://")):
            return None
        try:
            resp = httpx.get(
                url,
                timeout=self.timeout,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.info("Fetch failed for %s: %s", url[:80], exc)
            return None

        ct = resp.headers.get("content-type", "").lower()
        # Only handle HTML/text — skip PDFs/images/etc. cleanly
        if "html" not in ct and "text" not in ct:
            logger.info("Fetch skipped (content-type=%s): %s", ct, url[:80])
            return None

        try:
            text = self._html_to_text(resp.text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("HTML parse failed for %s: %s", url[:80], exc)
            return None

        text = text.strip()
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS].rstrip() + "\n[…content truncated…]"
        return text

    def _html_to_text(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(list(_JUNK_TAGS)):
            tag.decompose()
        # Prefer main/article content if the page marks it up
        root = soup.find("main") or soup.find("article") or soup.body or soup
        # Collapse extra whitespace; each paragraph on its own line
        parts = []
        for chunk in root.stripped_strings:
            parts.append(chunk)
        text = "\n".join(parts)
        # Collapse 3+ blank lines to 2
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text

    def fetch_multiple(self, urls: List[str]) -> List[dict]:
        """Fetch up to MAX_PAGES_PER_TASK URLs and return list of
        {url, content} for the ones that succeeded."""
        out = []
        for url in urls[:MAX_PAGES_PER_TASK]:
            text = self.fetch(url)
            if text:
                out.append({"url": url, "content": text})
        return out

    def format_for_prompt(self, pages: List[dict]) -> str:
        """Turn fetched pages into a prompt-injectable block. Empty
        string if none."""
        if not pages:
            return ""
        lines = [
            "FULL WEB PAGES YOU CAN READ",
            "(These are the actual pages, not snippets. Cite by URL if you quote specific numbers.)",
        ]
        for i, p in enumerate(pages, 1):
            lines.append("")
            lines.append(f"=== Page {i}: {p['url']} ===")
            lines.append(p["content"])
        return "\n".join(lines) + "\n"
