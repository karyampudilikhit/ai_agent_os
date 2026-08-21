"""What happened to each item, and whether the answer may claim them.

THE FAILURE THIS ANSWERS -- the one the audit called the largest hole in
the system. Every per-item guard built so far lives inside the execution
loop. The check that decides whether a founder is told "done" lives in
_gate_deliverable, and nothing connects them: `item_state` is imported by
exactly one file outside its own tests.

So a run could list ten job titles off a single results page, open none
of them, write ten plausible rows, and pass. Not by defeating a guard --
by never meeting one. The row-backing check asks whether a reported row
appears in text the run read, and the LISTING PAGE is text the run read.
Ten rows, ten titles, all present, all "backed".

WHAT MAKES A CLAIM REFUSABLE HERE IS A COUNT, NOT A JUDGEMENT. To report
ten items each verified on its own page, a run must have visited at
least ten distinct pages. That is arithmetic against the ledger, and it
is the only thing this module will BLOCK on. It cannot be argued with
and it cannot be wrong: a run that landed on three pages did not read
ten.

RELEVANCE INFORMS, IT DOES NOT BLOCK -- not in V1. `relevance.judge`
answers a harder question, and a wrong answer there would fail honest
work, which this codebase holds to be the worse error. Its verdicts
shape the REPORT the founder reads, so a rejected item is visible and
counted; they do not by themselves stop a run being called done. When
relevance has live evidence behind it, that decision can be revisited.

DELIBERATELY BLIND TO ITS OWN BLIND SPOT. `item_state.derive` can miss
an arrival -- a click route that reported no address, an SPA that never
changed URL. If this module blocked on derive's opened-count, every one
of those misses would become a false accusation against a run that did
the work. The distinct-pages count is measured independently of
candidate harvesting for exactly that reason, so derive being wrong can
make this quiet but never make it wrong.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# The life of one item, in the order it can only ever go.
DISCOVERED = "DISCOVERED"   # a link was harvested
OPENED = "OPENED"           # the run landed on it
READ = "READ"               # a read tool ran while it was open
VERIFIED = "VERIFIED"       # read AND it is what was asked for
REJECTED = "REJECTED"       # read AND it is not
BLOCKED = "BLOCKED"         # tried to open, could not

_ORDER = (DISCOVERED, OPENED, READ, VERIFIED, REJECTED, BLOCKED)

# Rows in a rendered answer. A deliverable presents its items as a
# markdown table, a numbered list, or repeated headings; whichever it
# used, the COUNT of them is what it is claiming to have delivered.
_MD_ROW = re.compile(r"^\s*\|(?!\s*[-: ]+\|)(?P<first>[^|]+)\|", re.MULTILINE)
_MD_SEP = re.compile(r"^[\s|:-]+$")
_NUMBERED = re.compile(r"^\s{0,3}(\d{1,3})[.)]\s+\S", re.MULTILINE)
_HEADING = re.compile(r"^\s{0,3}#{2,4}\s+\S", re.MULTILINE)

# Header words that mean a table row is the header, not an item.
_HEADER_WORDS = {"company", "role", "title", "name", "ticker", "item",
                 "location", "date", "url", "link", "#", "no", "s.no",
                 "rank", "position", "stipend", "salary"}


@dataclass
class ItemVerdict:
    url: str
    state: str
    reason: str = ""

    def line(self) -> str:
        tail = f" — {self.reason}" if self.reason else ""
        return f"[{self.state}] {self.url}{tail}"


@dataclass
class Tally:
    verified: int = 0
    rejected: int = 0
    blocked: int = 0
    read: int = 0
    opened: int = 0
    discovered: int = 0
    wanted: int = 0

    def shortfall(self) -> int:
        return max(0, self.wanted - self.verified)

    def line(self) -> str:
        bits = [f"{self.verified} verified"]
        if self.rejected:
            bits.append(f"{self.rejected} rejected")
        if self.blocked:
            bits.append(f"{self.blocked} blocked")
        pending = self.discovered - (self.verified + self.rejected
                                     + self.blocked)
        if pending > 0:
            bits.append(f"{pending} never opened")
        return f"{', '.join(bits)} of {self.wanted} asked for"


def verify(prog: Any, subj: Any = None) -> List[ItemVerdict]:
    """One verdict per candidate, from the ledger-derived progress.

    `subj` is a relevance.Subject or None. None -- or a subject too
    generic to enforce -- means every item that was read counts as
    verified, which is exactly the behaviour before relevance existed.
    """
    out: List[ItemVerdict] = []
    try:
        discovered = list(getattr(prog, "discovered", ()) or ())
        opened = set(getattr(prog, "opened", ()) or ())
        read = set(getattr(prog, "read", ()) or ())
        blocked = set(getattr(prog, "blocked", ()) or ())
        evidence = dict(getattr(prog, "evidence", {}) or {})
    except Exception:  # noqa: BLE001
        return out

    for url in discovered:
        if url in blocked and url not in opened:
            out.append(ItemVerdict(url, BLOCKED,
                                   "a call tried to open it and failed"))
            continue
        if url not in opened:
            out.append(ItemVerdict(url, DISCOVERED,
                                   "found, but never opened"))
            continue
        if url not in read:
            out.append(ItemVerdict(url, OPENED,
                                   "opened, but nothing was read from it"))
            continue

        if subj is None:
            out.append(ItemVerdict(url, VERIFIED))
            continue
        try:
            from backend.app.orchestrator.relevance import judge
            ok, why = judge(evidence.get(url, ""), subj)
        except Exception:  # noqa: BLE001
            ok, why = True, ""
        out.append(ItemVerdict(url, VERIFIED if ok else REJECTED, "" if ok else why))
    return out


def tally(verdicts: Sequence[ItemVerdict], wanted: int = 0) -> Tally:
    t = Tally(wanted=max(0, int(wanted or 0)), discovered=len(verdicts))
    for v in verdicts:
        if v.state == VERIFIED:
            t.verified += 1
            t.read += 1
            t.opened += 1
        elif v.state == REJECTED:
            t.rejected += 1
            t.read += 1
            t.opened += 1
        elif v.state == BLOCKED:
            t.blocked += 1
        elif v.state == READ:
            t.read += 1
            t.opened += 1
        elif v.state == OPENED:
            t.opened += 1
    return t


# ------------------------------------------------------ the gate check

def claimed_items(text: str) -> int:
    """How many items this answer PRESENTS itself as delivering.

    The largest of the three ways an answer enumerates things, because a
    deliverable that uses a table AND numbers its sections is claiming
    the rows, not twice the rows.
    """
    body = text or ""

    rows = 0
    for m in _MD_ROW.finditer(body):
        cell = m.group("first").strip().strip("*`_ ").strip()
        if not cell or _MD_SEP.match(cell):
            continue
        if cell.lower().strip(":# ") in _HEADER_WORDS:
            continue
        rows += 1

    numbered = len(_NUMBERED.findall(body))
    headings = len(_HEADING.findall(body))
    return max(rows, numbered, headings)


# The bracketed header every extractor writes: "[page text — <url>]",
# "[records — <url>]", "[section — Heading (h2) — <url>]". Counted here
# as well as by goal_state's own reader, because UNDERCOUNTING pages is
# the one way the refusal below could accuse an honest run -- and a
# reader that misses a shape undercounts. goal_state itself is left
# alone: its navigation stop is live-proven, and widening what it
# accepts as an arrival is a change that deserves its own validation
# rather than riding along with this one.
_BRACKET_URL = re.compile(r"^\[[^\]\n]*?(https?://[^\s\]]+)\]", re.MULTILINE)


def distinct_pages_visited(ledger_calls: Iterable[Dict[str, Any]]) -> int:
    """How many DIFFERENT pages this run actually landed on.

    Measured from page views directly, with no reference to candidate
    harvesting, so `item_state` failing to recognise an arrival cannot
    make this undercount. That independence is what lets the check below
    be a hard refusal rather than a suggestion.
    """
    return len(_pages_visited(ledger_calls))


def _pages_visited(ledger_calls: Iterable[Dict[str, Any]]) -> set:
    calls = list(ledger_calls or ())
    pages = set()
    try:
        from backend.app.orchestrator.goal_state import _norm_url, visited_urls
        pages.update(visited_urls(calls))
    except Exception:  # noqa: BLE001
        def _norm_url(u):  # noqa: E306
            return (u or "").strip().lower().rstrip("/")
    for call in calls:
        if not call.get("ok"):
            continue
        text = str(call.get("output") or call.get("result_preview") or "")
        for m in _BRACKET_URL.finditer(text):
            pages.add(_norm_url(m.group(1)))
    pages.discard("")
    return pages


def item_pages_read(ledger_calls: Iterable[Dict[str, Any]]) -> Optional[int]:
    """Distinct pages this run reached that sit BELOW a page it read as
    a list. None when it never read a list, so there is no basis here.

    WHY NOT JUST COUNT PAGES. Replaying the real internship trace, the
    run reached thirteen distinct pages -- and they were Indeed, Naukri,
    LinkedIn, Internshala and a handful of search URLs, not thirteen job
    postings. "Thirteen pages, so eleven items is plausible" was the
    wrong reading of a true number: a search page is not an item, and a
    check that cannot tell them apart lets exactly the failure it exists
    for walk through.

    Depth is the distinction, and it is the same one item_state uses: an
    item lives BELOW the listing that pointed at it. Unlike item_state,
    this needs no link harvesting -- only which pages were read as
    lists, which the tool used says directly.
    """
    calls = list(ledger_calls or ())
    try:
        from backend.app.orchestrator.item_state import (
            _looks_like_an_item, derive,
        )
        listings = set(derive(calls, 0).listings)
    except Exception:  # noqa: BLE001
        return None
    if not listings:
        # It never read a list. Every page it reached may legitimately
        # be an item, and this measure would read zero -- which would
        # accuse an honest run of everything. Say "no basis" instead.
        return None
    return sum(1 for p in _pages_visited(calls)
               if _looks_like_an_item(p, listings))


def overclaim(text: str, task: str, spec: Any,
              ledger_calls: Iterable[Dict[str, Any]]) -> Optional[str]:
    """The refusal, or None. Arithmetic only -- see the module docstring.

    Fires only for a task that asked for several items EACH VISITED
    INDIVIDUALLY, and only when the number of pages this run reached
    makes the number of items it reports impossible. Every other case,
    including every case this cannot be certain about, returns None.
    """
    try:
        if spec is None or not getattr(spec, "confident", False):
            return None
        if not getattr(spec, "per_item_work", False):
            return None
        wanted = int(getattr(spec, "item_count", 0) or 0)
    except (TypeError, ValueError):
        return None
    if wanted < 2:
        return None

    calls = list(ledger_calls or ())
    if not calls:
        return None

    claimed = claimed_items(text)
    if claimed < 2:
        return None

    # Item pages when the run read a list and there is a basis to tell
    # items from search pages; the raw page count when there is not.
    # Whichever applies, it is counted from page views, never from the
    # candidate list -- so item_state missing an arrival can make this
    # quiet but never make it accuse.
    item_pages = item_pages_read(calls)
    if item_pages is None:
        pages, what = distinct_pages_visited(calls), "distinct page(s) in total"
    else:
        pages, what = item_pages, "item page(s) below the list it read"

    if pages >= claimed:
        # It reached at least as many pages as it reports items. Whether
        # it read the RIGHT ones is a question this check cannot answer
        # honestly, so it does not pretend to.
        return None

    logger.info("[item-verify] overclaim: claims %d item(s), reached %d %s",
                claimed, pages, what)
    return (
        f"this task asked for {wanted} item(s) each verified on its own page, "
        f"and the answer presents {claimed} of them — but this run reached "
        f"only {pages} {what}. It cannot have read {claimed} item pages, so "
        f"most of what is reported was taken from a results list rather than "
        f"verified. Report the ones actually opened and say plainly how many "
        f"of {wanted} that is"
    )
