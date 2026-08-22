"""Which way of looking for things is still producing any.

THE FAILURE THIS ANSWERS. Asked for twenty internships, a run spent all
twenty-one of its calls on one host. It searched Indeed eight different
ways, harvested forty-nine candidates, opened two of them, verified
none, and stopped because the budget ran out. Nothing was broken: the
browser worked, the extractor worked, the site answered every time.

Six guards were watching and every one of them was blind, for the same
reason. The repeat guard fingerprints (tool, arguments), and eight
Indeed URLs are eight fingerprints. strategy_memory keys on the page,
and each search URL is a different page. instrument_memory keys on
host x instrument and only fires on FAILURE -- Indeed was succeeding.
The item nudge went permanently silent the moment one candidate was
opened.

What no object in the system could answer was: which source am I on,
which way am I searching it, and has that produced anything new lately.

THE HIERARCHY MATTERS. A query going dry is not a source going dry:

    SOURCE (in.indeed.com)
      └── STRATEGY  ("product manager intern, India, last 7 days")
            └── PAGE (start=0, start=10, start=20 ...)

Page two of a productive query is exactly what SHOULD happen next, and a
check that could not tell it apart from re-asking a dead question would
block the useful case to stop the useless one. So barrenness is measured
per STRATEGY, and a source is only spent once several of its strategies
are.

EXHAUSTION IS COUNTED, NOT JUDGED. A strategy is exhausted when
consecutive reads under it yield no candidate this run has not already
seen. That is arithmetic over the tool ledger. Nothing here asks the
model whether a source is worth continuing with, because "Indeed is
exhausted" is exactly the kind of claim this codebase does not accept
from a model -- and in the run above the model believed the opposite
right up to the last call.

WHAT THIS DOES NOT DO. It never picks the next source. Deciding that a
new way in is needed is arithmetic and belongs to code; deciding which
way in to try is language and world knowledge, and belongs to the model.
No list of sites appears anywhere in this module, and adding one would
make it a job-board tool instead of an orchestrator.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlparse

logger = logging.getLogger(__name__)

UNTRIED = "UNTRIED"
ACTIVE = "ACTIVE"
EXHAUSTED = "EXHAUSTED"
BLOCKED = "BLOCKED"

# Consecutive reads under one strategy that bring back nothing new
# before that strategy is called spent. Two, not one: a single barren
# read is routine -- a slow page, a first page already seen -- and
# refusing on it would stop honest pagination dead.
BARREN_READS_TO_EXHAUST = 2

# How many of a source's strategies must be spent before the SOURCE is.
# A site usually has more than one useful way in, and declaring the
# whole of it dead because one query went dry is the mistake this
# hierarchy exists to avoid.
STRATEGIES_TO_EXHAUST_SOURCE = 2

# Reads in a row anywhere on one source that bring back nothing new
# before the SOURCE is spent, however many different queries were used.
# Three, because a site is entitled to one dry query and a second is not
# yet a pattern.
SOURCE_BARREN_READS_TO_EXHAUST = 3

# ITEM PAGES REFUSED BEFORE A SOURCE IS CALLED UNVERIFIABLE.
#
# Measured on a live run: a site answered every search with fifteen
# records and refused every single job page behind them. Seven attempts
# went into learning that, because nothing distinguished "this source
# has no more candidates" from "this source has candidates it will not
# let you read". They are different facts and they need different
# answers -- the first says search differently, the second says the
# source cannot satisfy the task at all, however good its search is.
#
# Two, because one refusal is a page and two is a policy.
ITEM_FAILURES_TO_EXHAUST_VERIFICATION = 2

# A source that can be searched but whose items cannot be opened or read.
# Discovery still works; verification never will.
VERIFICATION_EXHAUSTED = "VERIFICATION_EXHAUSTED"

# Query parameters that change the PAGE rather than the QUESTION. Asking
# for page two is the same strategy continuing, and it is the case that
# must never be mistaken for a repeat. Generic across the web; no site
# is named.
_PAGINATION_PARAMS = {
    "start", "page", "offset", "from", "p", "pn", "pagenum", "page_num",
    "pageno", "page_number", "skip", "index", "first", "begin", "cursor",
}

# Parameters that carry no meaning at all -- tracking, session, and
# analytics. Two URLs differing only in these are the same request.
_NOISE_PARAM_PREFIXES = ("utm_", "itm_", "gclid", "fbclid", "msclkid",
                         "ref", "referer", "referrer", "src", "source",
                         "tk", "bb", "vjk", "trk", "sid", "session",
                         "campaign", "medium", "cid", "aid", "eid")

# Tools that GATHER candidates. The same set item_state calls a list
# read; kept in sync deliberately, since a discovery call and a
# candidate-harvesting call are the same event seen twice.
_DISCOVERY_TOOLS = ("browser_extract_records", "browser_extract_table",
                    "web_search")
# Tools that move to a new question or a new page of one.
_NAVIGATION_TOOLS = ("browser_navigate", "web_read")


def _host_of(url: str) -> str:
    try:
        host = (urlparse(url if "://" in url else "http://" + url).hostname
                or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    # in.indeed.com and www.indeed.com are one source, not two.
    parts = [p for p in host.split(".") if p]
    if len(parts) > 2:
        return ".".join(parts[-2:])
    return host


def _meaningful_params(url: str) -> Tuple[Tuple[str, str], ...]:
    """The parameters that say WHAT is being asked for.

    Pagination and tracking come out, so "page 2 of this search" and
    "this search again with a different tracking id" both collapse onto
    the strategy they belong to.
    """
    try:
        q = parse_qsl(urlparse(url if "://" in url else "http://" + url).query,
                      keep_blank_values=False)
    except Exception:  # noqa: BLE001
        return ()
    out = []
    for k, v in q:
        kl = k.lower()
        if kl in _PAGINATION_PARAMS:
            continue
        if any(kl.startswith(p) for p in _NOISE_PARAM_PREFIXES):
            continue
        if not v or len(v) > 120:
            continue
        out.append((kl, v.strip().lower()))
    return tuple(sorted(out))


def strategy_key(url: str = "", query: str = "") -> str:
    """One way of looking, independent of which page of it you are on.

    A free-text query (web_search) is its own strategy. A URL is its
    host, its path, and the parameters that carry the question.
    """
    if query and not url:
        words = sorted({w for w in re.split(r"[^a-z0-9]+", query.lower())
                        if len(w) > 2})
        return "q:" + " ".join(words)
    if not url:
        return ""
    try:
        p = urlparse(url if "://" in url else "http://" + url)
    except Exception:  # noqa: BLE001
        return ""
    path = (p.path or "/").rstrip("/") or "/"
    params = "&".join(f"{k}={v}" for k, v in _meaningful_params(url))
    return f"{_host_of(url)}{path}" + (f"?{params}" if params else "")


@dataclass
class Strategy:
    """One way of searching one source."""

    source: str
    key: str
    calls: int = 0
    new_candidates: int = 0
    duplicates: int = 0
    barren_streak: int = 0
    reads: int = 0
    status: str = UNTRIED

    @property
    def exhausted(self) -> bool:
        return self.status == EXHAUSTED

    def record_read(self, new: int, dup: int) -> None:
        self.reads += 1
        self.new_candidates += new
        self.duplicates += dup
        if new > 0:
            self.barren_streak = 0
            self.status = ACTIVE
        else:
            self.barren_streak += 1
            if self.barren_streak >= BARREN_READS_TO_EXHAUST:
                self.status = EXHAUSTED
            elif self.status == UNTRIED:
                self.status = ACTIVE

    def line(self) -> str:
        return (f"{self.key[:58]:60} calls={self.calls} new={self.new_candidates} "
                f"dup={self.duplicates} {self.status}")


@dataclass
class Source:
    host: str
    strategies: Dict[str, Strategy] = field(default_factory=dict)
    blocked: bool = False
    # Consecutive reads ANYWHERE on this source that brought back nothing
    # new. Needed alongside per-strategy barrenness because a run can
    # keep inventing fresh queries against a site it has already emptied
    # -- which is what happened above: six strategies, each read once, so
    # no single one ever looked barren while thirty of forty-nine
    # candidates were repeats.
    barren_streak: int = 0
    # ITEM PAGES, counted apart from search pages. A source can be
    # excellent at one and useless at the other: the run this came from
    # answered every search with fifteen records and refused every job
    # page behind them. Nothing distinguished "no more candidates" from
    # "candidates it will not let you read", so seven calls went into
    # learning it the hard way.
    item_opens: int = 0
    item_failures: int = 0
    item_reads: int = 0

    @property
    def new_candidates(self) -> int:
        return sum(s.new_candidates for s in self.strategies.values())

    @property
    def calls(self) -> int:
        return sum(s.calls for s in self.strategies.values())

    @property
    def can_verify(self) -> bool:
        """False once this source has refused enough item pages.

        Counted, not judged: a source is unverifiable when the pages
        BEHIND its results repeatedly will not open or read, whatever
        its search does.
        """
        return self.item_failures < ITEM_FAILURES_TO_EXHAUST_VERIFICATION

    @property
    def status(self) -> str:
        if self.blocked:
            return BLOCKED
        # Checked BEFORE barrenness: a source still yielding candidates
        # is not exhausted in the discovery sense, and saying so would
        # send the run back to search it again. What it cannot do is the
        # only thing the task actually needs.
        if not self.can_verify:
            return VERIFICATION_EXHAUSTED
        if not self.strategies:
            return UNTRIED
        if self.barren_streak >= SOURCE_BARREN_READS_TO_EXHAUST:
            return EXHAUSTED
        spent = [s for s in self.strategies.values() if s.exhausted]
        if (len(spent) >= STRATEGIES_TO_EXHAUST_SOURCE
                and len(spent) == len(self.strategies)):
            return EXHAUSTED
        return ACTIVE

    def line(self) -> str:
        return (f"{self.host:26} {self.status:10} calls={self.calls:3} "
                f"new={self.new_candidates:3} "
                f"strategies={len(self.strategies)}")


@dataclass
class DiscoveryState:
    sources: Dict[str, Source] = field(default_factory=dict)
    # Every candidate identity seen anywhere, so the SECOND source to
    # offer the same job gets no credit for it.
    seen_ids: Set[str] = field(default_factory=set)
    seen_titles: Set[str] = field(default_factory=set)
    total_new: int = 0
    total_duplicates: int = 0
    current_source: str = ""
    current_strategy: str = ""

    def source_of(self, host: str) -> Optional[Source]:
        return self.sources.get(host)

    def strategy_of(self, host: str, key: str) -> Optional[Strategy]:
        src = self.sources.get(host)
        return src.strategies.get(key) if src else None

    @property
    def tried_sources(self) -> List[str]:
        return sorted(self.sources)

    @property
    def exhausted_sources(self) -> List[str]:
        return sorted(h for h, s in self.sources.items()
                      if s.status in (EXHAUSTED, BLOCKED, VERIFICATION_EXHAUSTED))

    @property
    def unverifiable_sources(self) -> List[str]:
        """Sources that search fine and will not let their items be read."""
        return sorted(h for h, s in self.sources.items() if not s.can_verify)

    @property
    def productive_sources(self) -> List[str]:
        return sorted(h for h, s in self.sources.items()
                      if s.status == ACTIVE and s.new_candidates > 0)

    def describe(self) -> str:
        if not self.sources:
            return "(no discovery yet)"
        lines = [f"sources={len(self.sources)} new={self.total_new} "
                 f"duplicates={self.total_duplicates}"]
        for host in sorted(self.sources):
            src = self.sources[host]
            lines.append("  " + src.line())
            for key in sorted(src.strategies):
                lines.append("      " + src.strategies[key].line())
        return "\n".join(lines)


# --------------------------------------------------- candidate identity

_GENERIC_TITLE_WORDS = {
    "the", "a", "an", "at", "in", "for", "of", "and", "or", "to", "with",
    "job", "jobs", "role", "position", "posting", "vacancy", "opening",
    "opportunity", "hiring", "apply", "new", "urgent", "immediate",
    "remote", "onsite", "hybrid", "fulltime", "parttime",
}


# A TITLE ALONE IS NOT AN IDENTITY.
#
# "Product Manager Intern" names a ROLE, and every job board is full of
# them. Treating that as an identity merged fifteen distinct postings
# into one in a regression fixture -- silently, and in the direction
# that UNDER-reports, which the founder would never have seen.
#
# So a title only identifies a posting once it carries enough words to
# be about a particular one; in practice that means it has picked up the
# employer. Below this it is ignored entirely and identity falls back to
# the URL token, which is never ambiguous.
MIN_TITLE_WORDS_TO_IDENTIFY = 4


def title_signature(title: str) -> str:
    """A title reduced to the words that distinguish it.

    Used to notice that two SOURCES are offering the same item. Matching
    is on the EXACT resulting word set, not on overlap: a fuzzy matcher
    here would merge two different internships at the same company and
    silently under-report, which is worse than missing a duplicate.
    Missing one costs a wasted open; merging two wrong ones corrupts the
    count the founder is given.
    """
    from backend.app.orchestrator.relevance import _stem
    words = {_stem(w) for w in re.split(r"[^A-Za-z0-9]+", (title or "").lower())
             if w and w not in _GENERIC_TITLE_WORDS and len(w) > 1}
    if len(words) < MIN_TITLE_WORDS_TO_IDENTIFY:
        return ""
    return " ".join(sorted(words))


_RECORD_BLOCK = re.compile(r"---\s*record\s+\d+\s*---(.*?)(?=---\s*record|\Z)",
                           re.IGNORECASE | re.DOTALL)
_TITLE_LINE = re.compile(r"^title:\s*(.+)$", re.MULTILINE)
_LINK_LINE = re.compile(r"^link:\s*(\S+)$", re.MULTILINE)


def candidates_in(output: str) -> List[Tuple[str, str]]:
    """(link, title) for each record block in a discovery read."""
    out: List[Tuple[str, str]] = []
    for block in _RECORD_BLOCK.findall(output or ""):
        link = _LINK_LINE.search(block)
        title = _TITLE_LINE.search(block)
        if link:
            out.append((link.group(1), title.group(1).strip() if title else ""))
    return out


def _identities(link: str, title: str) -> Tuple[Set[str], str]:
    from backend.app.orchestrator.item_state import _id_tokens
    return _id_tokens(link), title_signature(title)


# ------------------------------------------------------------- derive

def _url_arg(call: Dict[str, Any]) -> str:
    m = re.search(r'"url":\s*"([^"]+)"', str(call.get("args_text") or ""))
    return m.group(1) if m else ""


def _query_arg(call: Dict[str, Any]) -> str:
    m = re.search(r'"query":\s*"([^"]+)"', str(call.get("args_text") or ""))
    return m.group(1) if m else ""


def _blocked_looking(output: str) -> bool:
    from backend.app.orchestrator.instrument_memory import looks_unreachable
    return looks_unreachable(output or "")


_READING_TOOLS = ("browser_extract", "web_read", "browser_extract_section",
                  "browser_observe")


def _norm_key(url: str) -> str:
    from backend.app.orchestrator.item_state import _id_tokens, _norm
    ids = _id_tokens(url)
    return sorted(ids)[0] if ids else _norm(url)


def _empty_looking(out: str) -> bool:
    return "THIS PAGE RETURNED ALMOST NO TEXT" in (out or "")


def derive(ledger_calls: Iterable[Dict[str, Any]]) -> DiscoveryState:
    """Everything above, read from what the tools actually returned."""
    st = DiscoveryState()
    current_url = ""
    # Identities of every candidate harvested so far, so a later
    # navigation can be recognised as opening an ITEM rather than
    # searching. Grows as records come in.
    candidate_keys: Set[str] = set()

    for call in sorted(ledger_calls or (), key=lambda c: float(c.get("at") or 0)):
        tool = str(call.get("tool") or "")
        out = str(call.get("output") or call.get("result_preview") or "")
        url = _url_arg(call)
        query = _query_arg(call)

        # BLOCKED IS ABOUT THE BROWSER, NOT ABOUT ANY TOOL THAT FAILED.
        #
        # A web_read refused by a host whose pages the browser is reading
        # perfectly says nothing about the source -- that is a fact about
        # one instrument, and instrument_memory already owns it. Marking
        # the source blocked on it declared Indeed dead in the run above
        # while the browser was still returning fifteen records a call.
        if not call.get("ok"):
            host = _host_of(url)
            if host:
                if url and _norm_key(url) in candidate_keys:
                    st.sources.setdefault(host, Source(host=host)).item_failures += 1
                elif "browser" in tool and _blocked_looking(out):
                    st.sources.setdefault(host, Source(host=host)).blocked = True
            continue

        # AN ITEM PAGE THAT WOULD NOT OPEN OR READ.
        #
        # Distinguished from a search page by where its address came
        # from: a candidate this run harvested is an item, and anything
        # else on the host is a way of finding one. That keeps the test
        # free of URL-shape guessing, which has broken three times.
        if url and _norm_key(url) in candidate_keys:
            host = _host_of(url)
            if host:
                src = st.sources.setdefault(host, Source(host=host))
                if _blocked_looking(out) or _empty_looking(out):
                    src.item_failures += 1
                else:
                    src.item_opens += 1
                    if any(t in tool for t in _READING_TOOLS):
                        src.item_reads += 1

        # A navigation sets which source and question are in play. It does
        # NOT create a strategy: opening an individual item is a
        # navigation too, and counting those as ways-of-searching filled
        # the run above with ten "strategies", six of which were job
        # pages. A strategy earns its record when something is actually
        # HARVESTED under it, below.
        if any(t in tool for t in _NAVIGATION_TOOLS) and url:
            current_url = url
            host = _host_of(url)
            if host:
                st.current_source = host
                st.current_strategy = strategy_key(url=url)
                if "browser" in tool and _blocked_looking(out):
                    st.sources.setdefault(host, Source(host=host)).blocked = True
            continue

        if not any(t in tool for t in _DISCOVERY_TOOLS):
            continue

        # A discovery read belongs to the question currently in play. A
        # web_search carries its own question and needs no page.
        if "web_search" in tool and query:
            host = "(web search)"
            key = strategy_key(query=query)
        else:
            host = st.current_source or _host_of(current_url)
            key = st.current_strategy or strategy_key(url=current_url)
        if not host:
            continue

        src = st.sources.setdefault(host, Source(host=host))
        strat = src.strategies.setdefault(key, Strategy(source=host, key=key))
        strat.calls += 1

        new = dup = 0
        for link, title in candidates_in(out):
            ids, sig = _identities(link, title)
            known = bool(ids & st.seen_ids) or (bool(sig) and sig in st.seen_titles)
            if known:
                dup += 1
                candidate_keys.add(_norm_key(link))
                continue
            new += 1
            st.seen_ids |= ids
            candidate_keys.add(_norm_key(link))
            if sig:
                st.seen_titles.add(sig)
        strat.record_read(new, dup)
        src.barren_streak = 0 if new > 0 else src.barren_streak + 1
        st.total_new += new
        st.total_duplicates += dup
        st.current_source, st.current_strategy = host, key

    return st
