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
# AND THE FOURTH SHAPE, which is the one that was still missing.
#
# Found by replaying the real 21-call internship trace, not by guessing:
# browser_extract heads its output "[page text — <url>]", and
# browser_extract_section, _records, _toc and outline all use the same
# bracketed form. None of them matches URL:, Now on: or Source:, so a
# browser_extract run on an item page did not register as READING that
# item -- which is precisely "opened but never read", the state the
# nudge was built to notice and never saw.
#
# The previous session's handoff guessed the cause was a browser_click
# route with no landing URL. It was not; it was this.
_BRACKET_RE = re.compile(r"^\[[^\]\n]*?(https?://[^\s\]]+)\]", re.MULTILINE)

# Tools that READ whatever page is currently open.
_READ_TOOLS = ("browser_extract", "browser_extract_section",
               "browser_extract_table", "browser_extract_records",
               "browser_observe", "web_read")
# Tools that GATHER candidates rather than advancing per-item work.
_LIST_TOOLS = ("browser_extract_records", "browser_extract_table")

DISCOVERED = "discovered"
OPENED = "opened"
READ = "read"

# Per-item evidence kept for the relevance check, bounded so a run that
# opens twenty pages does not carry twenty full page dumps in memory.
MAX_ITEM_EVIDENCE = 8000


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
    # WHAT THE DERIVATION SAW, kept for diagnosis rather than for any
    # decision. Nothing below reads these; they exist because four live
    # runs produced no nudge and no way to tell which step of this
    # derivation came up empty.
    examined: int = 0
    listings: List[str] = field(default_factory=list)
    # Browser calls whose output carried no page address at all. The
    # leading suspect for the second cause of that silence: a landing
    # this module cannot see is an item it cannot count as opened.
    unlocated: List[str] = field(default_factory=list)
    # WHAT WAS ACTUALLY READ FROM EACH ITEM'S OWN PAGE, keyed by item.
    #
    # Not for progress -- `read` above already answers that. This is the
    # evidence a later check needs to ask whether an item is the thing
    # that was ASKED FOR, and it has to be scoped per item: the whole
    # run's text would let a listing page vouch for every candidate on
    # it, which is exactly how "Fashion Merchandising Intern" was once
    # accepted as a Product Manager internship.
    evidence: Dict[str, str] = field(default_factory=dict)
    # Candidates a call actually tried to open and failed on. "The site
    # refused three of them" and "I ran out of steps before reaching
    # them" are different answers, and the founder deserves the right one.
    blocked: Set[str] = field(default_factory=set)

    @property
    def done(self) -> int:
        """An item counts as done only when it was opened AND read."""
        return len(self.opened & self.read)

    def summary(self) -> str:
        return (f"{self.done}/{self.wanted} item(s) opened and read; "
                f"{len(self.discovered)} candidate(s) found")


def diagnose(prog: "ItemProgress") -> str:
    """Everything the derivation saw, on one line a person can read.

    THIS EXISTS BECAUSE THE GUARD IT DESCRIBES HAS NEVER FIRED. Nineteen
    unit tests pass; four live runs produced no nudge. The first cause
    was found by replaying a trace and is fixed; the second is still
    open, and the reason it is still open is that a run reports only
    whether the note appeared, never which step of the derivation ran
    dry. This line is what makes the next run answer that.
    """
    bits = [f"examined={prog.examined}",
            f"listings={len(prog.listings)}",
            f"discovered={len(prog.discovered)}",
            f"opened={len(prog.opened)}",
            f"read={len(prog.read)}",
            f"done={prog.done}/{prog.wanted}"]
    if prog.unlocated:
        bits.append("no-address=" + ",".join(prog.unlocated[:6]))
    if prog.discovered and not prog.opened:
        bits.append("NOTHING OPENED")
    if prog.opened and not prog.read:
        bits.append("OPENED BUT NEVER READ")
    if not prog.discovered:
        bits.append("NO CANDIDATES HARVESTED")
    return " ".join(bits)


def derive(ledger_calls: Iterable[Dict[str, Any]], wanted: int) -> ItemProgress:
    """Per-item progress, read entirely from what the tools returned."""
    prog = ItemProgress(wanted=max(0, int(wanted or 0)))
    listing_urls: Set[str] = set()
    seen: Set[str] = set()
    current = ""
    # Arguments of calls that FAILED. Resolved against the candidate list
    # once, at the end, because a call can fail on an item that has not
    # been discovered yet -- the run may click a card before the records
    # read that harvests its link.
    failed_args: List[str] = []

    for call in sorted(ledger_calls or (), key=lambda c: float(c.get("at") or 0)):
        if not call.get("ok"):
            if "browser" in str(call.get("tool") or ""):
                failed_args.append(str(call.get("args_text") or ""))
            continue
        tool = str(call.get("tool") or "")
        out = str(call.get("output") or call.get("result_preview") or "")
        prog.examined += 1

        m = (_URL_LINE_RE.search(out) or _NOW_ON_RE.search(out)
             or _SOURCE_RE.search(out) or _BRACKET_RE.search(out))
        landed = _norm(m.group(1)) if m else ""
        if landed:
            current = landed
        elif "browser" in tool:
            # A browser call that reported no address. Recorded, not
            # acted on: this is the shape the open cause is expected to
            # take, and a guess dressed as a fix is what this run is
            # meant to replace.
            prog.unlocated.append(tool.rsplit(".", 1)[-1])

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
            # Keep what that read returned, against this item and no
            # other. Bounded per item: a run opening twenty pages must
            # not carry twenty full page dumps, and the first few
            # thousand characters of a job page are where the role,
            # company and location live.
            if len(prog.evidence.get(current, "")) < MAX_ITEM_EVIDENCE:
                prog.evidence[current] = (
                    prog.evidence.get(current, "") + "\n" + out
                )[:MAX_ITEM_EVIDENCE]

    prog.listings = sorted(listing_urls)

    # A candidate this run TRIED to open and could not. Distinct from
    # one it simply never got to, and the difference matters to the
    # founder: "the site refused three of them" is a fact about the
    # world, "I ran out of steps" is a fact about the budget.
    for args in failed_args:
        low = args.lower()
        for cand in seen:
            if cand in low and cand not in prog.opened:
                prog.blocked.add(cand)

    logger.info("[item-state] derive: %s", diagnose(prog))
    return prog


# WHY AN EVALUATION PRODUCED NO NOTE. One of these is logged every time
# progress_note runs, so a run that never nudges says WHICH clause
# stopped it instead of leaving the next session to guess again.
#
# The clauses are unchanged -- this is the same ladder of conditions the
# note has always used, named. Splitting the decision out from the text
# also means the reason can be asserted in a test, which "returns None"
# never could: every one of the six ways to stay silent looked identical
# from outside.
NO_ITEM_GOAL = "silent: the goal names no items"
NOTHING_DISCOVERED = "silent: no candidates have been harvested yet"
NOT_A_LIST_CALL = "silent: this call is not gathering another list"
ENOUGH_DONE = "silent: enough candidates are already opened and read"
TOO_FEW_CANDIDATES = "silent: too few candidates to be worth redirecting to"
ALREADY_OPENED = "silent: at least one candidate has been opened"
FIRED = "NUDGED"


def verdict(prog: ItemProgress, action: str) -> str:
    """Which clause decides this evaluation. Pure, and the only copy.

    progress_note reads its answer from here rather than repeating the
    conditions, so the reason logged can never drift from the reason
    acted on.
    """
    if prog.wanted <= 1:
        return NO_ITEM_GOAL
    if not prog.discovered:
        return NOTHING_DISCOVERED
    if not any(t in (action or "") for t in _LIST_TOOLS):
        return NOT_A_LIST_CALL
    if prog.done >= min(prog.wanted, len(prog.discovered)):
        return ENOUGH_DONE
    # Only once there are plainly enough candidates to be getting on with.
    if len(prog.discovered) < min(prog.wanted, 3):
        return TOO_FEW_CANDIDATES
    if prog.opened:
        return ALREADY_OPENED
    return FIRED


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
    reason = verdict(prog, action)
    logger.info("[item-state] progress_note: %s | action=%s budget_left=%s | %s",
                reason, (action or "?").rsplit(".", 1)[-1], budget_left,
                diagnose(prog))
    if reason != FIRED:
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
