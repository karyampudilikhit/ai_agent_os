"""Deep browser research — multi-page, JS-rendered site crawling for
competitor/company analysis tasks that a flat single-page fetch can't
serve well.

`WebFetchTool` (web_fetch.py) is httpx + BeautifulSoup: fast, but reads
the raw pre-hydration HTML. Most modern product/pricing pages are
React/Next.js SPAs — httpx sees an near-empty shell, not the rendered
content. This tool launches real headless Chromium so what gets read is
what a human visitor actually sees, and it follows a handful of the
site's own links (pricing, product, about, blog) instead of reading
just the homepage.

Trust tier: same as WebFetchTool. Read-only — no login, no forms, no
clicks that mutate anything on the target site. No approval gate
needed; this is a research connector, not an action.

Design mirrors the rest of this tool tree (WebFetchTool, TavilySearchTool):
  - Bounded cost. MAX_PAGES_PER_SITE, MAX_SITE_SECONDS, and
    MAX_TEXT_CHARS_PER_PAGE all cap how much one crawl can cost in time
    and prompt tokens.
  - Quiet failure. A dead site, a timeout, a blocked crawl — the caller
    gets back whatever pages succeeded (possibly none) and the run
    continues exactly as if this tool didn't exist.
  - No API key / account needed — it's just a local headless browser.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

MAX_PAGES_PER_SITE = 7          # homepage + up to 6 followed links
MAX_SITE_SECONDS = 75.0         # wall-clock budget per site crawl
MAX_TEXT_CHARS_PER_PAGE = 3000  # keep prompt injection bounded
PAGE_NAV_TIMEOUT_MS = 15000     # per-page navigation timeout
MAX_SITES_PER_TASK = 3          # cap on how many competitors we'll crawl at once

USER_AGENT = (
    "Mozilla/5.0 (VisionAI-Employee/1.0; +https://vision-ai.local) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Link text/href keywords worth following, in priority order — a link
# matching an earlier keyword wins if the page budget forces a choice.
# Deliberately the sections a human researching a competitor would open
# first: pricing, then product, then who-they-are, then recent signal.
_LINK_PRIORITY = (
    "pricing", "plans",
    "product", "features",
    "about", "team", "company",
    "blog", "news",
    "careers", "jobs",
    "customers", "case-studies", "case studies",
)

_JUNK_TAGS = ("script", "style", "nav", "footer", "aside", "form",
              "noscript", "iframe", "svg", "button", "header")


def _root_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _same_domain(url: str, root_netloc: str) -> bool:
    return _root_domain(url) == root_netloc.replace("www.", "").lower()


def _priority_score(href: str, text: str) -> int:
    """Lower = higher priority. Links matching nothing still get a
    (low-priority) score rather than being excluded outright."""
    hay = (href + " " + text).lower()
    for i, kw in enumerate(_LINK_PRIORITY):
        if kw in hay:
            return i
    return len(_LINK_PRIORITY) + 1


def distinct_domain_urls(urls: List[str], limit: int = MAX_SITES_PER_TASK) -> List[str]:
    """Dedupe a list of URLs down to one per root domain (first-seen
    wins), capped at `limit`. Turns a flat list of search-result URLs
    into a short list of distinct competitor sites worth deep-crawling
    — without this, 3 Tavily hits on the same domain would look like 3
    different competitors."""
    seen: Set[str] = set()
    out: List[str] = []
    for u in urls:
        dom = _root_domain(u)
        if not dom or dom in seen:
            continue
        seen.add(dom)
        out.append(u)
        if len(out) >= limit:
            break
    return out


class DeepResearchTool:
    """Multi-page JS-rendered crawl of one company site at a time.
    `research_multiple` fans out across several sites in parallel via a
    thread pool — each thread opens its own Playwright browser instance,
    so this is safe across threads despite Playwright's sync API having
    no cross-thread state to share."""

    def __init__(self) -> None:
        try:
            import playwright.sync_api  # noqa: F401
            self.enabled = True
        except ImportError:
            self.enabled = False
            logger.info(
                "Playwright not installed — deep browser research disabled. "
                "Run: pip install playwright && playwright install chromium"
            )

    def research(self, start_url: str) -> List[Dict[str, str]]:
        """Crawl one site starting at start_url. Returns a list of
        {url, title, content} for every page successfully read, bounded
        by MAX_PAGES_PER_SITE. Empty list on any failure or if disabled
        — callers treat that as 'no deep research for this site', the
        same quiet-failure contract as every other tool in this tree."""
        if not self.enabled or not start_url:
            return []
        if not start_url.startswith(("http://", "https://")):
            start_url = "https://" + start_url

        deadline = time.monotonic() + MAX_SITE_SECONDS
        pages: List[Dict[str, str]] = []
        visited: Set[str] = set()

        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    ctx = browser.new_context(user_agent=USER_AGENT)
                    page = ctx.new_page()
                    page.set_default_navigation_timeout(PAGE_NAV_TIMEOUT_MS)

                    root_netloc = urlparse(start_url).netloc
                    to_visit = [start_url]

                    while to_visit and len(pages) < MAX_PAGES_PER_SITE and time.monotonic() < deadline:
                        url = to_visit.pop(0)
                        if url in visited:
                            continue
                        visited.add(url)

                        result = self._read_page(page, url)
                        if result is not None:
                            pages.append(result)

                        # Only the homepage (first page) contributes new
                        # links to follow — this stays "read the site's
                        # main sections," not a general-purpose spider.
                        if len(pages) == 1 and result is not None:
                            links = self._extract_links(page, url, root_netloc)
                            to_visit.extend(links[: MAX_PAGES_PER_SITE - 1])
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Deep research failed for %s: %s", start_url[:80], exc)
            return pages  # whatever succeeded before the failure

        logger.info("Deep research: %d page(s) read from %s", len(pages), start_url[:80])
        return pages

    def _read_page(self, page, url: str) -> Optional[Dict[str, str]]:
        try:
            resp = page.goto(url, wait_until="domcontentloaded")
            if resp is not None and resp.status >= 400:
                return None
            # Brief, bounded wait for lazy-rendered content — long
            # enough for most React hydration, short enough that one
            # slow page can't eat the whole site's time budget.
            page.wait_for_timeout(400)
            title = page.title() or ""
            text = self._extract_text(page)
        except Exception as exc:  # noqa: BLE001
            logger.info("Deep research page failed %s: %s", url[:80], exc)
            return None
        if not text:
            return None
        if len(text) > MAX_TEXT_CHARS_PER_PAGE:
            text = text[:MAX_TEXT_CHARS_PER_PAGE].rstrip() + "\n[…content truncated…]"
        return {"url": url, "title": title.strip(), "content": text}

    def _extract_text(self, page) -> str:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(page.content(), "html.parser")
        for tag in soup(list(_JUNK_TAGS)):
            tag.decompose()
        root = soup.find("main") or soup.find("article") or soup.body or soup
        text = "\n".join(root.stripped_strings)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def _extract_links(self, page, base_url: str, root_netloc: str) -> List[str]:
        try:
            hrefs = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => ({href: e.getAttribute('href'), text: e.innerText || ''}))",
            )
        except Exception:  # noqa: BLE001
            return []
        candidates: List[Tuple[int, str]] = []
        seen: Set[str] = set()
        for item in hrefs:
            href = (item.get("href") or "").strip()
            text = (item.get("text") or "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            full = urljoin(base_url, href).split("#")[0]
            if full in seen or not _same_domain(full, root_netloc):
                continue
            seen.add(full)
            candidates.append((_priority_score(href, text), full))
        candidates.sort(key=lambda t: t[0])
        return [url for _, url in candidates]

    def research_multiple(self, urls: List[str]) -> Dict[str, List[Dict[str, str]]]:
        """Crawl up to MAX_SITES_PER_TASK sites in parallel. Returns
        {url: [pages...]} — a site that failed entirely maps to []."""
        urls = urls[:MAX_SITES_PER_TASK]
        if not urls:
            return {}
        out: Dict[str, List[Dict[str, str]]] = {}
        with ThreadPoolExecutor(max_workers=min(3, len(urls))) as executor:
            futures = {executor.submit(self.research, u): u for u in urls}
            for fut in as_completed(futures):
                u = futures[fut]
                try:
                    out[u] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Deep research thread failed for %s: %s", u[:80], exc)
                    out[u] = []
        return out

    def format_for_prompt(self, site_pages: Dict[str, List[Dict[str, str]]]) -> str:
        """Turn {url: [pages]} into a prompt-injectable block, grouped
        by site so the model can tell competitors apart and attribute
        claims to the right one."""
        if not site_pages:
            return ""
        lines = ["DEEP SITE RESEARCH (multiple real pages read per company — not just search snippets)"]
        any_content = False
        for site_url, pages in site_pages.items():
            if not pages:
                continue
            any_content = True
            lines.append("")
            lines.append(f"=== {site_url} — {len(pages)} page(s) read ===")
            for p in pages:
                lines.append("")
                lines.append(f"--- {p['title'] or '(untitled)'} · {p['url']} ---")
                lines.append(p["content"])
        if not any_content:
            return ""
        return "\n".join(lines) + "\n"
