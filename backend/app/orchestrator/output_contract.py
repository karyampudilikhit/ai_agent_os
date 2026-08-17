"""What a finished turn actually has to CONTAIN.

The agentic loop already refuses DONE when a task asked for a computed
figure and no compute tool succeeded. That check works, and it is the
reason a Data Engineer and a Quant Analyst both ended up running
run_python on 2026-08-16 instead of quitting after one fetch. But it
knows exactly one requirement, and it derives that requirement from the
TASK TEXT: "does the founder's wording mention Sharpe or CAGR".

That leaves two gaps:

  - An employee whose job is ALWAYS to compute (or always to fetch a
    live page, or always to produce a file) has no way to say so. Every
    run has to re-earn the requirement through the founder's phrasing.
  - Two other failure shapes seen the same week had no check at all: a
    deliverable citing a URL nothing had fetched, and a report claiming
    figures were "saved at D:\\...\\SPY_SMA_Crossover_Backtest_Report.pdf"
    into a directory that did not exist.

So requirements become named KINDS that either a task or an employee's
config can demand, and the loop refuses DONE while any demanded kind is
unsatisfied.

Every kind is answered from a RECORDED FACT -- a tool that really
succeeded, a file that really exists on disk -- never from the model's
own account of what it did. That is the one rule in this codebase that
has held every time it was applied and failed every time it was not.

Kept separate from critique/compute_gate.py on purpose: that module
answers "does this TASK need computation and did the DELIVERABLE report
it", which is a question about text. This one answers "did this TURN do
the thing", which is a question about the ledger.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterable, List, Sequence, Set

logger = logging.getLogger(__name__)

EXECUTED_CODE = "executed_code"
FETCHED_URL = "fetched_url"
FILE_WRITTEN = "file_written"
URL_VERIFIED = "url_verified"
RANKED_RESULT = "ranked_result"

# Words that mean "the ORDER of the results is the answer". A task asking
# for the top 5 gainers is not asking for five stocks -- it is asking for
# the five with the largest change, which is a claim about sorting.
_RANKING_WORDS = (
    "top ", "best ", "worst ", "highest", "lowest", "biggest", "largest",
    "smallest", "gainers", "losers", "movers", "leaders", "laggards",
    "ranked", "rank ", "sorted", "sort by", "most ", "least ",
)

# Tools that CHANGE what a page is showing, as opposed to reading it.
# Sorting a table, applying a filter, choosing a period -- all of these
# go through one of these.
_INTERACTION_TOOLS = (
    "browser_click_element", "browser_click", "browser_select",
    "browser_type", "browser_press", "browser_submit",
)
_READ_TOOLS = ("browser_extract", "browser_extract_table", "browser_observe")


def task_wants_ranking(task: str) -> bool:
    return any(w in (task or "").lower() for w in _RANKING_WORDS)

# Tool name fragments that satisfy each kind. Matched against the
# loop's qualified names ("action.run_python"), so a bare name matches
# any namespace.
_FETCH_TOOLS = (
    "fetch_market_data", "web_fetch", "browser_extract_table",
    "browser_navigate", "browser_task_async", "deep_research",
)
_WRITE_TOOLS = ("write_file", "create_pdf", "create_docx", "create_pptx", "create_xlsx")

# Argument keys a write tool might carry its destination path under.
_PATH_KEYS = ("path", "file_path", "filename", "output_path", "destination", "dest")


CONTRACT_KINDS: Dict[str, str] = {
    EXECUTED_CODE: "must actually execute code and print a result",
    FETCHED_URL: "must actually fetch a live page or dataset",
    FILE_WRITTEN: "must actually produce a file that exists on disk",
    URL_VERIFIED: "must end at a URL that was opened and confirmed to respond",
    RANKED_RESULT: "must actually sort or filter the page, not read the default view",
}

# What the loop tells the model when it refuses DONE for each kind.
#
# The executed_code wording is UNCHANGED from the version that shipped
# and worked -- both specialists complied with it on the first refusal.
# It is tuned; do not tidy it.
REFUSAL_TEXT: Dict[str, str] = {
    EXECUTED_CODE: (
        "(REFUSED. This task asks for a figure that can only come from "
        "executing code -- a CAGR, Sharpe ratio, drawdown, win rate or "
        "similar -- and you have not successfully called action.run_python "
        "even once. Fetching the data is not computing the answer, and "
        "describing the formula is not computing it either. Call "
        "action.run_python NOW on the data you already have, print the "
        "numbers, and report exactly what it printed. You cannot finish "
        "this task without it.)"
    ),
    FETCHED_URL: (
        "(REFUSED. This task requires information you actually retrieved, "
        "and you have not successfully fetched a single page or dataset. "
        "Recalling something from memory is not retrieving it, and naming a "
        "source you did not open is worse than saying you could not find "
        "one. Fetch the real source NOW and report what it actually said.)"
    ),
    FILE_WRITTEN: (
        "(REFUSED. This task requires a file, and no file you wrote exists "
        "on disk. Note that describing a file, or naming a path you intend "
        "to save to, does not create it -- a previous run reported figures "
        "'saved at' a path that was never written, which is worse than "
        "producing nothing. Actually write the file NOW, then report its "
        "real path.)"
    ),
    RANKED_RESULT: (
        "(REFUSED. This task asks for a RANKED result -- top movers, best, "
        "worst, sorted -- and you have read the page without ever changing "
        "what it shows. A table's default view is not a ranking: the rows "
        "you are looking at are whatever the site chose to display, not the "
        "ones the founder asked for. A previous run reported the screener's "
        "default market-cap list as 'top gainers'; every number in it was "
        "real and every row was wrong. Sort or filter the page first. The "
        "cheapest way is the URL: many sites encode the sort as a "
        "parameter, and navigating there directly cannot mis-click. "
        "Otherwise use browser_find to locate the control by description "
        "-- browser_observe lists only the first elements it finds, so a "
        "control further down the page never appears in it -- then "
        "browser_click_element or browser_select to use it, and observe "
        "again to confirm the order really changed. Only then read the rows.)"
    ),
    URL_VERIFIED: (
        "(REFUSED. This task is only finished when the resulting URL has "
        "been opened and confirmed to load. Clicking a Deploy button is not "
        "the same as the deployment working -- builds fail, and a button "
        "click tells you nothing about what the visitor sees. Take the URL "
        "the page gave you, open it with action.browser_navigate, confirm "
        "it responds with the expected page, and report that.)"
    ),
}


def _tool_matches(qualified: str, fragments: Sequence[str]) -> bool:
    name = str(qualified or "").lower()
    return any(f in name for f in fragments)


# The same three predicates the end-of-run check uses, exported so the
# LOOP can apply them live.
#
# This matters more than it looks. The comparison below could tell a
# click that sorted a table from a click the page ignored -- but it only
# ran once the turn was over, so a model that clicked nothing useful got
# no signal at the moment it could still act on one. It made the same
# ineffective click four times and was told at the end. Same fact, same
# function, four steps too late.

def is_interaction_tool(qualified: str) -> bool:
    """True for tools that CHANGE what a page shows."""
    return _tool_matches(qualified, _INTERACTION_TOOLS)


def is_read_tool(qualified: str) -> bool:
    """True for tools that only READ a page."""
    return _tool_matches(qualified, _READ_TOOLS)


def is_browser_tool(qualified: str) -> bool:
    """True for anything that drives or reads a browser page.

    Used by the loop to notice it is doing web work at all -- which is
    what triggers the larger step budget and the browser rule block.
    """
    return _tool_matches(qualified, _INTERACTION_TOOLS + _READ_TOOLS + (
        "browser_navigate", "browser_find", "browser_scroll", "browser_wait",
        "browser_back", "browser_download", "browser_upload",
    ))


def is_page_view_tool(qualified: str) -> bool:
    """True for tools whose output is a RENDERING OF THE WHOLE PAGE, and
    is therefore comparable with the last one.

    browser_find is deliberately excluded even though it is a read. It
    returns matched controls in its own format and scans far deeper than
    an observation, so its output differs wildly from a page render --
    comparing a click against it would report "the page changed" every
    single time and quietly disable the check this exists for.
    """
    return _tool_matches(qualified, _INTERACTION_TOOLS + _READ_TOOLS + (
        "browser_navigate", "browser_scroll", "browser_wait", "browser_back",
    ))


def page_view_changed(before: str, after: str) -> bool:
    """True when acting actually moved the page.

    Public alias for the comparison the ranked_result contract is built
    on, so the loop and the gate can never disagree about what "the page
    changed" means.
    """
    return _materially_different(after, before)


def _claimed_paths(ledger_calls: Iterable[Dict[str, Any]]) -> List[str]:
    """Destination paths from successful write-tool calls."""
    paths: List[str] = []
    for call in ledger_calls or ():
        if not call.get("ok") or not _tool_matches(call.get("tool"), _WRITE_TOOLS):
            continue
        try:
            args = json.loads(call.get("args_text") or "{}")
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(args, dict):
            continue
        for key in _PATH_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value.strip())
    return paths


def satisfied_kinds(
    succeeded_tools: Set[str],
    computed: bool,
    ledger_calls: Iterable[Dict[str, Any]] = (),
) -> Set[str]:
    """Which contract kinds this turn has genuinely met.

    `computed` is the loop's own flag, already stricter than "run_python
    succeeded": it is False for a crashed script and False for one that
    ran and printed nothing. Reusing it keeps one definition of "really
    computed something" rather than growing a second.
    """
    done: Set[str] = set()
    if computed:
        done.add(EXECUTED_CODE)
    if any(_tool_matches(t, _FETCH_TOOLS) for t in succeeded_tools or ()):
        done.add(FETCHED_URL)

    # Deliberately stricter than "a write tool succeeded": the path has
    # to be there. This is the only kind that checks the world rather
    # than the call log, because it is the only one where the call log
    # provably was not enough.
    for path in _claimed_paths(ledger_calls):
        try:
            if os.path.exists(path):
                done.add(FILE_WRITTEN)
                break
        except (OSError, ValueError):  # noqa: PERF203
            continue

    # A URL counts as verified only when the run OPENED it after the work
    # that produced it. Two navigations minimum, and the last one is the
    # check: navigate → do the thing → navigate to the result.
    #
    # "I clicked Deploy" is the claim this refuses to accept. Builds fail
    # after the button goes green, and a click tells you nothing about
    # what a visitor actually sees.
    if _verified_a_result_url(ledger_calls):
        done.add(URL_VERIFIED)

    if _produced_a_ranking(ledger_calls):
        done.add(RANKED_RESULT)
    return done


def _browser_views(ledger_calls: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Successful browser results in time order, with their page text."""
    out = []
    for call in ledger_calls or ():
        if not call.get("ok"):
            continue
        view = str(call.get("output") or call.get("result_preview") or "")
        if not view:
            continue
        out.append({"tool": str(call.get("tool") or ""),
                    "at": float(call.get("at") or 0), "view": view})
    out.sort(key=lambda c: c["at"])
    return out


def run_saw_a_data_table(ledger_calls: Iterable[Dict[str, Any]]) -> bool:
    """Did this run ever look at a page with a data table on it?

    Guards against demanding a ranking of work that has no rows to rank.
    `task_wants_ranking` matches "top " and "best ", so "write the best
    headline" and "summarise the top findings" are ranking-shaped tasks
    about no table at all -- and a gate that fails honest work is worse
    than the bug it prevents.
    """
    try:
        from backend.app.browser.observation import parse_data_keys
    except Exception:  # noqa: BLE001
        return False
    for call in _browser_views(ledger_calls):
        if parse_data_keys(call["view"]):
            return True
    return False


def _produced_a_ranking(ledger_calls: Iterable[Dict[str, Any]]) -> bool:
    """True when THIS RUN changed how the data is ordered, then read it.

    NOT "is some column sorted". That question was measured against the
    live TradingView screener and answers YES ON THE DEFAULT VIEW -- it
    arrives sorted by market cap descending. Requiring only "something is
    sorted" would pass the exact run this contract exists to reject, where
    the agent read the default market-cap list and reported it as the top
    weekly gainers.

    A page arriving sorted is not the agent's doing. What proves a ranking
    was produced is that the ordering being read DIFFERS from the one the
    page handed over. Verified end to end: "Mkt cap descending" -> "Chg %
    descending", rows NVDA/AAPL/GOOG -> TREVQ/ETBI/IOBTQ.

    Falls back to the older page-comparison check when the run never
    recorded a sort measurement at all -- an older transcript, or a
    ranking produced by filtering rather than sorting -- so this is
    strictly additive and cannot make a previously-passing honest run
    start failing for want of a field that did not exist.
    """
    try:
        from backend.app.browser.observation import parse_sorted_columns
    except Exception:  # noqa: BLE001
        return _changed_the_page_before_reading(ledger_calls)

    first_sig = None
    changed_at = 0.0
    measured = False
    for call in _browser_views(ledger_calls):
        cols = parse_sorted_columns(call["view"])
        if cols is None:
            continue
        measured = True
        sig = frozenset((c["column"], c["direction"]) for c in cols)
        if first_sig is None:
            first_sig = sig
        elif sig != first_sig and not changed_at:
            # The FIRST moment the order differed, not the last. Taking
            # the last meant the very call that revealed the new order
            # also moved the goalpost past itself, so the read could never
            # be "after" the change and a genuine sort scored zero.
            changed_at = call["at"]

    if not measured:
        return _changed_the_page_before_reading(ledger_calls)
    if not changed_at:
        return False

    # The rows have to be READ at or after the moment the order changed.
    # Reading first and sorting afterwards answers a question nobody
    # asked -- the failed run's reported figures came from an extract
    # taken before anything was touched.
    #
    # ">=" rather than ">": every browser primitive re-observes as part
    # of acting, so the call that changes the order also returns the
    # reordered page. Requiring a strictly later read would refuse a run
    # that sorted and read in one step.
    for call in _browser_views(ledger_calls):
        if call["at"] >= changed_at and _tool_matches(call["tool"], _READ_TOOLS):
            return True
    return False


def _normalise(text: str) -> str:
    return " ".join((text or "").split())[:6000]


def _materially_different(a: str, b: str) -> bool:
    """True when two page views are not effectively the same.

    A click that lands on nothing still returns a fresh observation --
    same URL, same elements, same text. Comparing the VIEWS is what
    separates "sorted the table" from "clicked something and the page
    ignored it", and no cheaper signal does that: both look identical in
    the call log.
    """
    na, nb = _normalise(a), _normalise(b)
    if not na or not nb:
        return False
    if na == nb:
        return False
    import difflib
    return difflib.SequenceMatcher(None, na, nb).ratio() < 0.98


def _changed_the_page_before_reading(ledger_calls: Iterable[Dict[str, Any]]) -> bool:
    """True when an interaction actually CHANGED what the page shows.

    Three conditions, and the run that motivated this failed all three
    in different ways:

      - the interaction must have SUCCEEDED. That run made three clicks
        and one failed; counting attempts would call a page sorted on
        the strength of a click that did nothing.
      - the page must have CHANGED. Its successful click at step 6 hit
        an element that was not the sort control -- the click worked,
        the page did not move. Requiring only "a click succeeded" passes
        that run, which is why comparing views is the load-bearing part.
      - a read must follow. The run's reported figures came from an
        extract taken at step 2, before any click at all. Reading first
        and fiddling afterwards is not sorting.

    Every browser tool re-observes after acting and that output is kept
    whole in the ledger, so the comparison is against recorded evidence
    rather than the model's account of what its click did.
    """
    calls = sorted(ledger_calls or (), key=lambda c: float(c.get("at") or 0))
    prev_view = ""
    changed_at = 0.0

    for call in calls:
        if not call.get("ok"):
            continue
        tool = str(call.get("tool") or "")
        at = float(call.get("at") or 0)
        view = str(call.get("output") or call.get("result_preview") or "")

        if _tool_matches(tool, _INTERACTION_TOOLS):
            if prev_view and _materially_different(view, prev_view):
                changed_at = max(changed_at, at)
            if view:
                prev_view = view
        elif _tool_matches(tool, _READ_TOOLS):
            if changed_at and at > changed_at:
                return True
            if view:
                prev_view = view
    return False


def _verified_a_result_url(ledger_calls: Iterable[Dict[str, Any]]) -> bool:
    """True when a navigation succeeded AFTER some page-changing action.

    Ordering is the whole test. Opening a URL before doing the work
    proves nothing about the work; opening one afterwards is the agent
    going back to look at what it made.
    """
    acted_at: float = 0.0
    for call in ledger_calls or ():
        if not call.get("ok"):
            continue
        tool = str(call.get("tool") or "")
        at = float(call.get("at") or 0)
        if _tool_matches(tool, ("browser_click", "browser_submit", "browser_press",
                               "deploy_vercel", "browser_type")):
            acted_at = max(acted_at, at)
        elif acted_at and _tool_matches(tool, ("browser_navigate",)) and at > acted_at:
            return True
    return False


def unsatisfied_kinds(
    required: Iterable[str],
    succeeded_tools: Set[str],
    computed: bool,
    ledger_calls: Iterable[Dict[str, Any]] = (),
) -> List[str]:
    """Required kinds this turn has not met, in CONTRACT_KINDS order so
    refusals are deterministic rather than set-ordered."""
    done = satisfied_kinds(succeeded_tools, computed, ledger_calls)
    wanted = {k for k in required or () if k in CONTRACT_KINDS}
    return [k for k in CONTRACT_KINDS if k in wanted and k not in done]
