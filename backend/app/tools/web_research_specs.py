"""Search and read the open web, as tools the loop can CHOOSE.

WHY THIS EXISTS. Tavily has been in this codebase for a while, but not as
a tool -- it ran as prompt enrichment before the loop started, fired by
keyword match in dynamic_employee, stuffing snippets into a prompt
nobody asked. The agentic loop could never select it, so every task that
needed a page went to the browser whether the browser was the right
instrument or not.

That cost real runs. Of five live tests today, THREE failed on bot
detection rather than on anything the loop did wrong: Finviz's Cloudflare
challenge turned away roughly two runs in three, Reddit refused every
route outright with "You've been blocked by network security", and Indeed
served a security check. None of those tasks needed a browser. They
needed the contents of a page that a search API can fetch without ever
looking like a robot.

THE ROUTING RULE, and it is not "which one works":

    Does the task need a page state that exists at no URL?

  - News headlines, forum posts, job listings, an article's text: the
    content already exists at a URL. Search wins -- no bot wall, one API
    call instead of twenty browser steps, and no visible window.
  - A screener filtered and sorted by weekly change: on Finviz that state
    IS the URL, so either tool can reach it. On TradingView it is not --
    the sort only exists after a click. Browser only.
  - Filling a form, signing in, sending a message: the browser is the
    only thing that can ACT. Search can only read.

WHY THIS FILE, AND NOT A PROMPT INSTRUCTION. The guards in this codebase
check what a run RETRIEVED, not what it says it retrieved -- and
_gate_deliverable reads the output of every successful tool call, not
only browser ones. So a search tool that returns its real snippets is
covered by the fabrication check for free: a deliverable that reports
rows appearing in no tool output still fails, whichever tool fetched
them. A prompt instruction telling the model to "use search" would have
bought none of that.

What is deliberately NOT claimed: search returns prose. It does not
return table cells, and it will not give you a post's score and comment
count as separate fields the way the browser's record extractor does.
For "read this page's data" the browser is still the better instrument.
For "find me the pages" it is not close.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from backend.app.actions.action_registry import ActionSpec

logger = logging.getLogger(__name__)

TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
DEFAULT_TIMEOUT = 20.0
# Roughly the cap browser_extract uses for a page. Enough for an article
# or a listing page, short enough not to crowd the step transcript.
MAX_PAGE_CHARS = 12000
MAX_RESULTS = 10


def _api_key() -> Optional[str]:
    return (os.environ.get("TAVILY_API_KEY") or "").strip() or None


def _no_key(tool: str) -> str:
    return (
        f"({tool} is not configured — TAVILY_API_KEY is not set. This is a "
        f"missing setting, NOT a fact about the page: do not report the "
        f"content as unavailable. Use the browser tools instead "
        f"(action.browser_navigate then action.browser_extract), or say "
        f"plainly that web search is unconfigured.)"
    )


# ------------------------------------------------------------ search

def _search_impl(args: Dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "(web_search failed: no query given)"
    if not _api_key():
        return _no_key("web_search")

    try:
        count = max(1, min(MAX_RESULTS, int(float(str(args.get("max_results") or 5)))))
    except (TypeError, ValueError):
        count = 5

    from backend.app.tools.web_search import TavilySearchTool

    results = TavilySearchTool().search(query, max_results=count)
    if not results:
        return (
            f"(web_search returned nothing for {query!r}. Either the query is "
            f"too narrow, or the search backend failed. Try different wording, "
            f"or open a likely site directly with action.browser_navigate.)"
        )

    from backend.app.browser.policy import wrap_untrusted

    lines = [f"[web search — {query!r}]",
             f"{len(results)} result(s). These are SNIPPETS a search engine "
             f"returned, not full pages. To quote anything beyond a headline, "
             f"open the URL with action.web_read first.", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"--- result {i} ---")
        lines.append(f"title: {r.get('title') or '(untitled)'}")
        if r.get("url"):
            lines.append(f"url: {r['url']}")
        snippet = (r.get("snippet") or "").replace("\n", " ").strip()
        if snippet:
            lines.append(f"snippet: {snippet[:600]}")
        lines.append("")
    return wrap_untrusted("\n".join(lines), query)


# -------------------------------------------------------------- read

def _read_impl(args: Dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "(web_read failed: no url given)"
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url

    key = _api_key()
    if not key:
        return _no_key("web_read")

    # The same policy the browser obeys. A tool that reads a URL is a
    # tool that can reach an internal address, and routing around the
    # browser must not route around that.
    try:
        from backend.app.browser.policy import PolicyViolation, active_policy
        active_policy().check_url(url)
    except PolicyViolation as exc:
        return str(exc)
    except Exception:  # noqa: BLE001
        pass

    try:
        resp = httpx.post(
            TAVILY_EXTRACT_URL,
            json={"urls": [url]},
            headers={"Authorization": f"Bearer {key}"},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return (
            f"(web_read could not fetch {url}: {exc}. This is a retrieval "
            f"failure, not a statement about the page's contents. Try "
            f"action.browser_navigate on the same URL, or a different source.)"
        )

    results = data.get("results") or []
    failed = data.get("failed_results") or []
    if not results:
        why = ""
        if failed and isinstance(failed[0], dict):
            why = f" ({failed[0].get('error') or ''})".rstrip(" ()")
        return (
            f"(web_read got no content from {url}{why}. The page may be "
            f"login-walled, rendered entirely by JavaScript, or refusing "
            f"automated access. Report it as UNREACHABLE rather than empty, "
            f"or try action.browser_navigate with interactive true.)"
        )

    first = results[0] if isinstance(results[0], dict) else {}
    content = str(first.get("raw_content") or first.get("content") or "").strip()
    final_url = str(first.get("url") or url)

    # Reading a page IS retrieving it — recorded so a later citation of
    # this URL can be verified against the network rather than against
    # the finished text. See source_ledger.py.
    try:
        from backend.app.tools.source_ledger import get_ledger
        get_ledger().record_fetched(final_url)
    except Exception:  # noqa: BLE001
        pass

    if not content:
        return (
            f"(web_read reached {final_url} but it carried no readable text. "
            f"Treat it as UNREACHABLE, not as empty.)"
        )

    from backend.app.browser.policy import wrap_untrusted

    truncated = ""
    if len(content) > MAX_PAGE_CHARS:
        truncated = (
            f"\n[…TEXT TRUNCATED at {MAX_PAGE_CHARS} of {len(content)} chars. "
            f"You are NOT seeing the whole page. Do not present what you can "
            f"see as a complete list.]"
        )
    return wrap_untrusted(
        f"[page text — {final_url}]\n{content[:MAX_PAGE_CHARS]}{truncated}",
        final_url,
    )


# ------------------------------------------------------------- specs

WEB_SEARCH_SPEC = ActionSpec(
    name="web_search",
    description=(
        "Search the open web and get back the best matching pages as title + "
        "URL + snippet. USE THIS FIRST when you need to FIND pages — news, "
        "articles, forum posts, listings, documentation, company information "
        "— because it reaches sites that block automated browsers (Reddit, "
        "Indeed and Finviz all refused a browser today) and costs one call "
        "instead of twenty browser steps. It returns SNIPPETS, not full "
        "pages: call action.web_read on a URL before quoting anything longer "
        "than a headline. It CANNOT click, sort, filter, fill a form or sign "
        "in — for any of those, use the browser tools."
    ),
    parameters=[
        {"name": "query", "type": "string", "required": True,
         "description": "What to search for, in plain words."},
        {"name": "max_results", "type": "number", "required": False,
         "description": "How many results to return (1-10, default 5)."},
    ],
    handler=_search_impl,
    preview=lambda a: f"Search the web for {str(a.get('query'))[:60]!r}",
    mutating=False,
    capability="web.search",
)

WEB_READ_SPEC = ActionSpec(
    name="web_read",
    description=(
        "Read the full text of one web page by URL, without opening a "
        "browser. USE THIS when the content you need exists at a URL and you "
        "only need to READ it — an article, a post, a listing page. It gets "
        "through most bot walls that stop the browser. It returns flowed "
        "TEXT: it does not preserve table cells, and it will not give you a "
        "post's score or a row's columns as separate fields — for structured "
        "data use action.browser_extract_table or "
        "action.browser_extract_records. It cannot act on the page at all."
    ),
    parameters=[
        {"name": "url", "type": "string", "required": True,
         "description": "The full URL of the page to read."},
    ],
    handler=_read_impl,
    preview=lambda a: f"Read {str(a.get('url'))[:70]}",
    mutating=False,
    capability="web.page.read",
)

ALL_SPECS = [WEB_SEARCH_SPEC, WEB_READ_SPEC]
