"""SourceLedger — the record of what this process ACTUALLY retrieved,
so a citation can be checked against reality instead of taken on faith.

Why this exists, from a real run: asked for a report on stock gainers
plus each company's business model, an employee produced a clean table
where every business-model cell carried a Wikipedia citation. The server
log for that run shows ZERO requests to wikipedia.org — it never opened
a single one of those pages. The descriptions came from the model's own
memory and had plausible-looking source URLs attached afterwards. Two of
them were wrong on the facts (AXT described as a chemicals company; a
SanDisk/Western Digital relationship stated backwards).

That is the worst failure shape this product has: not "an uncited guess"
(which the evidence layer already flags as unsourced_claim and the UI
shows in red), but a guess wearing a citation. It reads as the most
trustworthy line in the report. The existing EvidenceExtractor could
never catch it, because it only ever sees the finished text — in which a
fabricated URL and a real one are indistinguishable.

The fix is to stop asking the text and start asking the network. Every
component that genuinely retrieves a page records it here; the evidence
pass then cross-checks each cited URL against this ledger. A citation to
a URL that was never retrieved is now provable, not a matter of opinion.

Two tiers, deliberately distinct:
  - FETCHED — we actually pulled this page's content (web_fetch, a
    browser navigation, a deep-research crawl). Citing it is fair.
  - SEEN    — a search engine surfaced this URL and we read its title/
    snippet, but never opened it (Tavily results). Citing it is weaker
    but not fabrication, so it counts as known.
Anything in neither tier was invented.

Scope note (single-user MVP, consistent with every other in-memory store
here — RunStore, ClarificationStore, BrowserSessionManager): this ledger
is process-wide rather than per-run, and never reset. The tradeoff is
deliberate and one-directional — a URL legitimately fetched by an
EARLIER run could mask a fabrication in a LATER one (a false negative),
but a URL that was never fetched at all can never be wrongly flagged (no
false positives). Under-reporting fabrication is acceptable; accusing a
correct citation of being fake is not. Making this per-run needs a run_id
threaded through every fetch site and is the obvious next step.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Dict, List, Set
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# Bound so a long-lived server can't grow this without limit. When full,
# the oldest entries are dropped — which can only cause a false negative
# (an old real citation looking unknown), never a false accusation of a
# URL that was never fetched.
MAX_ENTRIES = 4000

# Matches http(s) URLs inside prose, including the 【...】 citation
# brackets this system's models like to emit. Trailing punctuation is
# stripped separately since a sentence-ending period is not part of a URL.
_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]\}【】,;]+", re.IGNORECASE)
_TRAILING_JUNK = ".,;:!?'\")]}>*_"

# Bare DOIs as they appear in API payloads — Crossref returns
# "10.2139/ssrn.2622782", not a doi.org link. Without this, a DOI handed
# back by an authoritative lookup API would be treated as invented the
# moment an employee cited it as https://doi.org/..., which is exactly
# the false accusation this module is built to avoid.
# The backslash is not cosmetic: raw JSON from Crossref escapes the
# separator, arriving as "10.5772\/intechopen.70867". Matching only a
# bare slash silently found zero DOIs in exactly the payloads this
# exists to read.
_DOI_RE = re.compile(r"\b10\.\d{4,9}\\?/[^\s\"'<>,;\)\]\}\\]+", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """Reduce a URL to a comparison key: lowercase scheme+host, no
    fragment, no trailing slash, no 'www.'. Deliberately keeps the query
    string — '?page=2' is a genuinely different page — but drops the
    fragment, which never is."""
    raw = (url or "").strip().rstrip(_TRAILING_JUNK)
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if not parts.scheme or not parts.netloc:
            return raw.lower()
        host = parts.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = parts.path.rstrip("/")
        return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))
    except ValueError:
        return raw.lower()


def extract_urls(text: str) -> List[str]:
    """Every http(s) URL appearing in a block of text, de-duplicated,
    in order of first appearance."""
    out: List[str] = []
    seen: Set[str] = set()
    for match in _URL_RE.findall(text or ""):
        cleaned = match.rstrip(_TRAILING_JUNK)
        if not cleaned:
            continue
        key = normalize_url(cleaned)
        if key and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


class SourceLedger:
    def __init__(self) -> None:
        self._fetched: Dict[str, float] = {}
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    # ---- recording -------------------------------------------------

    def record_fetched(self, url: str) -> None:
        """Call ONLY after a page's content was genuinely retrieved."""
        self._record(self._fetched, url)

    def record_seen(self, url: str) -> None:
        """A search engine surfaced this URL (title/snippet read) but the
        page itself was never opened."""
        self._record(self._seen, url)

    def _record(self, store: Dict[str, float], url: str) -> None:
        key = normalize_url(url)
        if not key:
            return
        with self._lock:
            store[key] = time.time()
            if len(store) > MAX_ENTRIES:
                # Drop the oldest quarter in one pass — cheaper than
                # evicting one entry per insert once we're at the cap.
                cutoff = sorted(store.items(), key=lambda kv: kv[1])
                for old_key, _ in cutoff[: MAX_ENTRIES // 4]:
                    store.pop(old_key, None)

    # ---- querying --------------------------------------------------

    def was_fetched(self, url: str) -> bool:
        key = normalize_url(url)
        with self._lock:
            return key in self._fetched

    def is_known(self, url: str) -> bool:
        """Fetched, or at least surfaced by a search. Anything else was
        never in front of this system at all."""
        key = normalize_url(url)
        with self._lock:
            return key in self._fetched or key in self._seen

    def unretrieved_urls(self, text: str) -> List[str]:
        """URLs cited in `text` that this process never fetched OR saw.
        These are the provable fabrications."""
        return [u for u in extract_urls(text) if not self.is_known(u)]

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {"fetched": len(self._fetched), "seen": len(self._seen)}


_ledger: SourceLedger = SourceLedger()


def get_ledger() -> SourceLedger:
    return _ledger


def record_payload_sources(payload: str, limit: int = 200) -> int:
    """Record every URL and DOI appearing INSIDE a retrieved payload as
    'seen'. Returns how many were recorded.

    Rationale: a reference this system was actually shown is not
    fabricated, even though nobody opened it. When a lookup API answers
    "this paper's DOI is 10.1016/j.jfineco.2015.01.010", citing that DOI
    is sourced behaviour — flagging it would be a false accusation, and
    false positives are the one failure mode worse than the bug this
    ledger exists to catch.

    Note this keeps the detector honest rather than weakening it: a DOI
    or arXiv id that appeared in NO payload and NO page still gets
    flagged. The run that invented arxiv.org/abs/2005.12345 (really a
    computer-security paper) would still be caught, because that id was
    never in front of the system at all.

    Never raises — a bookkeeping failure must not break a tool call.
    """
    if not payload:
        return 0
    n = 0
    try:
        for url in extract_urls(payload)[:limit]:
            _ledger.record_seen(url)
            n += 1
        for doi in _DOI_RE.findall(payload)[:limit]:
            # Undo the JSON escaping before storing, so the key matches
            # the plain https://doi.org/10.x/y form an employee will cite.
            clean = doi.replace("\\/", "/").rstrip(_TRAILING_JUNK)
            _ledger.record_seen(f"https://doi.org/{clean}")
            n += 1
    except Exception:  # noqa: BLE001
        return n
    return n
