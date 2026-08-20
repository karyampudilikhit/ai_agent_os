"""Which of the items asked for have actually been done.

THE FAILURE THIS ANSWERS. Phase 2 gave the loop a verdict -- "ten items
each opened individually needs about twenty-five steps and you have
twenty-two, so do eight PROPERLY and say so". It was handed to the model
as advice, and the live re-run ignored it: seventeen of twenty-two steps
went on results pages before the first job page opened. Three items, not
eight.

That is the pattern this codebase keeps rediscovering. A note in a prompt
is not enforcement. The verdict was computed deterministically and then
left to a model to remember fifteen steps later, which is exactly the
shape of every guard that has had to be rewritten here.

So the loop now KNOWS, at every step, how many of the asked-for items
have been opened and read -- and that knowledge is derived from the tool
ledger, never from the model's account of itself.

WHAT COUNTS AS EVIDENCE
  discovered  a link appeared in a records/table read
  opened      a navigation actually LANDED on that link
  read        a page-reading tool ran while that page was open
Nothing here asks whether the model believes an item is done. "Opened"
means the browser reported arriving; "read" means an extraction tool ran
against that arrival.

DELIBERATELY QUIET WHEN THE GOAL HAS NO ITEMS. A task that names no item
count, or wants no per-item work, produces an empty ledger and no note.
Everything that worked before this module keeps working unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# Links harvested from a records read or a table. These are the candidate
# items -- the things a per-item task must then open one by one.
_LINK_RE = re.compile(r"^link:\s*(\S+)", re.MULTILINE)
_HREF_RE = re.compile(r"https?://[^\s\"'<>\)\]]{12,300}")

_URL_LINE_RE = re.compile(r"^URL:\s*(\S+)", re.MULTILINE)
_NOW_ON_RE = re.compile(r"Now on:.*?\((\S+?)\)")
# THE LINE THE EXTRACTORS ACTUALLY EMIT.
#
# browser_extract_records wraps its output with wrap_untrusted, whose
# header carries "Source: <url>" -- not "URL:" and not "Now on:". Reading
# only the other two meant the page a records read happened on was never
# recorded, listing_urls stayed empty, and every candidate link was
# rejected as "not below a known listing". The nudge therefore never
# fired on the live run it was built for, while passing every synthetic
# test, because the fixtures used the URL: shape.
_SOURCE_RE = re.compile(r"^Source:\s*(\S+)", re.MULTILINE)

# Tools that READ whatever page is currently open.
_READ_TOOLS = ("browser_extract", "browser_extract_section",
               "browser_extract_table", "browser_extract_records",
               "browser_observe", "web_read")
# Tools that GATHER candidates rather than advancing per-item work.
_LIST_TOOLS = ("browser_extract_records", "browser_extract_table")

DISCOVERED = "discovered"
OPENED = "opened"
READ = "read"


def _norm(url: str) -> str:
    u = (url or "").strip().rstrip(".,);]'\"").lower()
    u = re.sub(r"^https?://", "", u)
    u = u.split("#", 1)[0]
    if u.startswith("www."):
        u = u[4:]
    return u.rstrip("/")


def _looks_like_an_item(url: str, listing_urls: Set[str]) -> bool:
    """An item link is deeper than the page that listed it.

    A results page links to its own filters, its logo and its next page as
    well as to items. The distinguishing property is not a word in the
    path -- that would be a per-site rule -- but depth: an item lives
    BELOW the listing that pointed at it.
    """
    n = _norm(url)
    if not n or n.count("/") < 2:
        return False
    for base in listing_urls:
        if n != base and n.startswith(base.split("?", 1)[0]) and len(n) > len(base) + 3:
            return True
    # A deep path on a host we have actually visited. Kept as a fallback
    # so a site whose listing URL never got recorded still yields items:
    # the previous version required a known listing and so returned
    # nothing at all when that lookup failed.
    host = n.split("/", 1)[0]
    return (any(host == b.split("/", 1)[0] for b in listing_urls)
            and n.count("/") >= 3)


@dataclass
class ItemProgress:
    discovered: List[str] = field(default_factory=list)
    opened: Set[str] = field(default_factory=set)
    read: Set[str] = field(default_factory=set)
    wanted: int = 0

    @property
    def done(self) -> int:
        """An item counts as done only when it was opened AND read."""
        return len(self.opened & self.read)

    def summary(self) -> str:
        return (f"{self.done}/{self.wanted} item(s) opened and read; "
                f"{len(self.discovered)} candidate(s) found")


def derive(ledger_calls: Iterable[Dict[str, Any]], wanted: int) -> ItemProgress:
    """Per-item progress, read entirely from what the tools returned."""
    prog = ItemProgress(wanted=max(0, int(wanted or 0)))
    listing_urls: Set[str] = set()
    seen: Set[str] = set()
    current = ""

    for call in sorted(ledger_calls or (), key=lambda c: float(c.get("at") or 0)):
        if not call.get("ok"):
            continue
        tool = str(call.get("tool") or "")
        out = str(call.get("output") or call.get("result_preview") or "")

        m = (_URL_LINE_RE.search(out) or _NOW_ON_RE.search(out)
             or _SOURCE_RE.search(out))
        landed = _norm(m.group(1)) if m else ""
        if landed:
            current = landed

        if any(t in tool for t in _LIST_TOOLS) and current:
            listing_urls.add(current)

        # Candidate links come out of records reads and page text.
        if any(t in tool for t in ("browser_extract_records", "browser_extract_table",
                                   "browser_extract")):
            for raw in _LINK_RE.findall(out) + _HREF_RE.findall(out):
                n = _norm(raw)
                if n and n not in seen and _looks_like_an_item(raw, listing_urls or {current}):
                    seen.add(n)
                    prog.discovered.append(n)

        # Landing on a discovered candidate is what "opened" means.
        if landed and landed in seen and landed not in listing_urls:
            prog.opened.add(landed)

        # A read tool run while that candidate is open is "read".
        if any(t in tool for t in _READ_TOOLS) and current in prog.opened:
            prog.read.add(current)

    return prog


def progress_note(prog: ItemProgress, action: str,
                  budget_left: int) -> Optional[str]:
    """What to tell the loop BEFORE it gathers yet another list.

    Returns None unless the run is genuinely stalling on discovery: items
    have been found, none of them opened, and the tool about to run
    gathers more candidates instead of advancing one.

    A NOTE, NOT A BLOCK -- for the same reason instrument_memory nudges
    rather than refuses. A run may legitimately need a second page of
    candidates, and a guard that cannot be overridden turns one wrong
    judgement into a dead run.
    """
    if prog.wanted <= 1 or not prog.discovered:
        return None
    if not any(t in (action or "") for t in _LIST_TOOLS):
        return None
    if prog.done >= min(prog.wanted, len(prog.discovered)):
        return None
    # Only once there are plainly enough candidates to be getting on with.
    if len(prog.discovered) < min(prog.wanted, 3):
        return None
    if prog.opened:
        return None

    return (
        f"(NOTE BEFORE THIS CALL: you have already found "
        f"{len(prog.discovered)} candidate(s) and opened NONE of them. The "
        f"task needs each item opened and read on its own page, and you "
        f"have about {budget_left} step(s) left — roughly "
        f"{max(0, budget_left // 2)} item(s) worth. Gathering another list "
        f"does not advance it. Open one of the candidates you already have.)"
    )


def shortfall_note(prog: ItemProgress) -> Optional[str]:
    """What the run must say if it finishes short. Facts, not an apology."""
    if prog.wanted <= 1 or prog.done >= prog.wanted:
        return None
    return (
        f"(ITEMS ACTUALLY VERIFIED: {prog.done} of {prog.wanted}. You opened "
        f"and read {prog.done} item page(s). Report those {prog.done} and say "
        f"plainly that {prog.done} of {prog.wanted} were verified and why. Do "
        f"NOT fill the remainder from the results list — an item you did not "
        f"open is not one of the {prog.wanted} that were asked for.)"
    )
