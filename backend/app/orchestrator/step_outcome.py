"""Did the step reach the state it was aiming for?

WHY THIS EXISTS — the arithmetic. Per-step accuracy measured on live
sites is roughly 79%. That is survivable over three steps and fatal over
twenty:

      6 steps -> 25%      (a screener sort; matches what we measured)
     20 steps -> 0.8%     (a message flow, an ad wizard)

Failures MULTIPLY, and nothing in the loop stopped that. When a step went
wrong the agent wandered off to a different approach instead of redoing
the step that failed, so one bad decision at minute fifteen wasted the
whole run. Recoverable failure turns that multiplication into addition:
if a step can be detected as failed and retried, twenty steps at 79% with
two retries each is a task that finishes.

WHAT IS AND IS NOT JUDGED HERE. The model declares an `expect` -- plain
words for the state it is trying to reach. This module only ever answers
from OBSERVABLE FACTS: did the tool fail, did the page move, did the rows
move, did the URL change. It never asks the model whether its own step
worked, because a model that just acted is the least reliable witness to
whether the action landed, and every guard in this codebase that asked
the model something has had to be rewritten as a guard that reads a
record.

So the verdict is deliberately three-valued. ACHIEVED and MISSED are
claims from evidence. UNKNOWN means the expectation is not mechanically
checkable -- "the page shows job listings" is a judgement about content,
not a state transition -- and UNKNOWN never blocks and never retries. A
check that guessed here would stall honest runs on every page it could
not read, which is a worse failure than the one it set out to fix.
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

ACHIEVED = "achieved"
MISSED = "missed"
UNKNOWN = "unknown"

# How many times one step may be retried before the loop moves on. Two is
# enough to survive a mis-click and a stale element id, and short of the
# eight-guess sweeps this codebase has had to guard against elsewhere.
MAX_STEP_RETRIES = 2

# Expectation wording that names a change to the DATA. These are the ones
# worth checking hardest, because "I sorted it" is exactly the claim that
# shipped a default view as a ranking.
_DATA_WORDS = (
    "sort", "sorted", "order", "ordered", "rank", "ranked", "filter",
    "filtered", "top ", "highest", "lowest", "reorder", "descending",
    "ascending",
)
# Expectation wording that names a move to somewhere else. "open" is
# deliberately NOT here: "open the dropdown" and "open the results page"
# are different claims, and the generic verb matched the wrong one.
_NAV_WORDS = ("navigate", "go to", "load the", "visit", "arrive",
              "redirect", "lands on", "takes me to")
# Expectation wording about revealing something without changing data.
# Matched on the specific NOUN rather than the verb, so it does not
# collide with navigation. The page should move; the rows need not --
# opening a filter panel that left the data alone did exactly what it
# meant to, and judging it on the data would fail a correct step.
_REVEAL_WORDS = ("dropdown", "menu", "dialog", "modal", "panel", "expand",
                 "reveal", "popup", "pop-up", "tooltip", "accordion")


def _mentions(text: str, words) -> bool:
    t = (text or "").lower()
    return any(w in t for w in words)


def judge_step(
    expect: str,
    result: str,
    before_view: str,
    after_view: str,
    call_failed: bool,
    tool: str = "",
) -> Tuple[str, Optional[str]]:
    """Return (verdict, reason). Reason is set only when MISSED.

    `before_view` / `after_view` are the recorded tool outputs either side
    of the action -- the same text the ledger keeps and the end-of-run
    gate reads, so the loop and the gate can never disagree about what
    happened.
    """
    if call_failed:
        return MISSED, "the call itself failed"

    if not (expect or "").strip():
        return UNKNOWN, None

    # Everything below needs two comparable page views. Without them --
    # a compute tool, a file write, the first browser call of a run --
    # there is nothing to compare and the honest answer is UNKNOWN.
    if not before_view or not after_view:
        return UNKNOWN, None

    try:
        from backend.app.browser.observation import rows_changed, sort_changed
        from backend.app.orchestrator.output_contract import (
            is_read_tool, page_view_changed,
        )
    except Exception:  # noqa: BLE001
        return UNKNOWN, None

    # A READ DOES NOT MOVE THE PAGE, AND MUST NOT BE JUDGED AS IF IT
    # SHOULD.
    #
    # browser_extract_table, browser_extract and browser_extract_records
    # read what is already there. Measured on a live screener run: the
    # sort was applied correctly at step 3, and at step 8 the agent
    # extracted the rows and declared "the table shows the 5 stocks with
    # the highest weekly percentage change". Both views read "SORTED:
    # Perf Week descending" -- identical, because the sort had already
    # happened and reading it again changes nothing -- so this reported
    # "the rows are in exactly the same order as before" and told the
    # run not to move on.
    #
    # A guard that fails a step for correctly reading an already-correct
    # page is worse than no guard: it spends the budget arguing with a
    # run that has the answer in hand. What a read is FOR is returning
    # data, and whether it did that is the extraction's own business.
    if is_read_tool(tool):
        return UNKNOWN, None

    page_moved = page_view_changed(before_view, after_view)
    data_moved = rows_changed(before_view, after_view)
    order_moved = sort_changed(before_view, after_view)

    # An expectation about ORDER is answered by the sort measurement, and
    # only falls back to the rows when the page carries no sort reading.
    if _mentions(expect, _DATA_WORDS):
        if order_moved is True or data_moved is True:
            return ACHIEVED, None
        if order_moved is False and data_moved is False:
            return MISSED, (
                "the rows are in exactly the same order as before — whatever "
                "that control does, it did not reorder the data"
            )
        if data_moved is False:
            return MISSED, "the rows did not change"
        return UNKNOWN, None

    # An expectation about REVEALING something is answered by the page,
    # and is checked BEFORE navigation because its words are the more
    # specific of the two.
    if _mentions(expect, _REVEAL_WORDS):
        if page_moved:
            return ACHIEVED, None
        return MISSED, "the page is unchanged — nothing opened or expanded"

    # An expectation about GOING somewhere is answered by the URL.
    if _mentions(expect, _NAV_WORDS):
        before_url = _url_of(before_view)
        after_url = _url_of(after_view)
        if before_url and after_url:
            if before_url != after_url:
                return ACHIEVED, None
            return MISSED, (
                f"still on {after_url[:80]} — the address did not change, so "
                f"that did not take you anywhere new"
            )
        return UNKNOWN, None

    # Anything else is a judgement about CONTENT ("the results show
    # Product Manager roles"), which no mechanical check can settle.
    # UNKNOWN, and the loop carries on.
    return UNKNOWN, None


_URL_LINE = re.compile(r"^URL:\s*(\S+)", re.MULTILINE)
_NOW_ON = re.compile(r"Now on:.*?\((\S+?)\)")


def _url_of(view: str) -> str:
    m = _URL_LINE.search(view or "")
    if m:
        return m.group(1)
    m = _NOW_ON.search(view or "")
    return m.group(1) if m else ""


def retry_note(expect: str, reason: str, attempt: int) -> str:
    """What the model is told when its own expectation did not arrive.

    Written to send it back to THIS step rather than onward. The failure
    mode being designed against is the agent treating a missed step as a
    reason to try something else entirely, two screens further on, with
    the original goal quietly abandoned.
    """
    return (
        f"(THAT STEP DID NOT DO WHAT YOU EXPECTED. You said this should "
        f"happen: \"{expect}\". It did not — {reason}. "
        f"Attempt {attempt} of {MAX_STEP_RETRIES}. Do NOT move on to a "
        f"different part of the task: the state you were trying to reach "
        f"is still not reached, and everything after it depends on it. "
        f"Try THIS step again a different way — a different element, a "
        f"different control, or action.browser_find to locate the right "
        f"one by description.)"
    )
