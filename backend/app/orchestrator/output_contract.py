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
}


def _tool_matches(qualified: str, fragments: Sequence[str]) -> bool:
    name = str(qualified or "").lower()
    return any(f in name for f in fragments)


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
    return done


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
