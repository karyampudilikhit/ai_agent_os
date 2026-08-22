"""Drive the browser loop in-process and print the whole trace.

WHY THIS EXISTS. Every autopsy in this project has been a
reconstruction. The tool-call ledger lives in memory with no endpoint and
no persistence, so once uvicorn has answered the request the only record
of what the agent did is the deliverable it wrote about itself -- which
is precisely the source this codebase has learned not to trust.

So this runs the SAME AgenticExecutor the server runs, against a real
model, and dumps what actually happened: every decision, every tool
result, the ledger, and whether each of the five browser fixes fired.

Isolated to the loop on purpose. The CEO, the Supervisor, synthesis and
critique all have their own failure modes and none of them are the thing
under test here.

    python trace_browser_run.py
    python trace_browser_run.py --model nemotron-3-super:cloud
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

TASK = (
    "Open https://www.tradingview.com/screener/ and use the stock screener to "
    "find the top 5 gainers and the top 5 losers by WEEKLY percentage change.\n\n"
    "Work the page step by step: observe it, change the change-% column or "
    "filter to the weekly period, sort by that column, and read the rows off "
    "the actual table.\n\n"
    "For each of the 10 stocks give me: ticker, company name, weekly % change, "
    "and last price. Report only what you actually read off the page - if you "
    "cannot change the period to weekly, say so plainly and tell me which "
    "period the numbers you found actually are."
)


def _adapter():
    # Routed exactly as the server routes it, rather than pinned to
    # Ollama. This built an OllamaAdapter directly and so quietly
    # ignored PIPELINE_MODEL whenever it named an OpenRouter model --
    # which made the fallback adapter a dead one, and a trace whose
    # fallback is dead reports a model problem that is really a
    # configuration problem.
    from backend.app.main import _build_adapter
    return _build_adapter(
        model=os.environ.get("PIPELINE_MODEL", "").strip() or "gpt-oss:120b-cloud",
        use_mock=False,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="loop model override")
    ap.add_argument("--task", default=TASK)
    ap.add_argument("--task-file", default=None,
                    help="read the task from a file (multi-line tasks do not "
                         "survive a Windows command line intact)")
    ap.add_argument("--role", default="Market Screener Analyst")
    args = ap.parse_args()
    if args.task_file:
        args.task = Path(args.task_file).read_text(encoding="utf-8").strip()

    # Page text carries en-dashes and minus signs; the Windows console is
    # cp1252 and raises on them. A trace that dies while PRINTING the
    # trace is a special kind of useless -- it cost one full 88-second
    # run of the thing it exists to observe.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Quieten the libraries that narrate every socket.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from backend.app.orchestrator.execution_loop import AgenticExecutor
    from backend.app.tools.tool_call_ledger import get_call_ledger

    started = time.time()
    ex = AgenticExecutor(_adapter(), loop_model=args.model)
    print("=" * 78)
    print(f"LOOP MODEL   : {args.model or os.environ.get('AGENT_LOOP_MODEL') or 'pipeline default'}")
    print(f"BUDGET       : {ex.max_steps} steps / {ex.deadline_seconds:.0f}s / "
          f"{ex.step_max_tokens} tokens per step  (before any widening)")
    print("=" * 78)

    out = ex.run(args.task, role=args.role)
    elapsed = time.time() - started

    calls = get_call_ledger().calls(since=started)
    print("\n" + "=" * 78)
    print("TOOL CALLS ACTUALLY MADE")
    print("=" * 78)
    if not calls:
        print("  (none — the loop never reached a tool)")
    for i, c in enumerate(calls, 1):
        status = "ok " if c.get("ok") else "FAIL"
        out_len = len(str(c.get("output") or c.get("result_preview") or ""))
        print(f"{i:3}. [{status}] {c.get('tool')}  args={str(c.get('args_text'))[:110]}")
        print(f"      -> {out_len} chars: {str(c.get('output') or c.get('result_preview') or '')[:200]!r}")

    print("\n" + "=" * 78)
    print("DID EACH FIX FIRE?")
    print("=" * 78)
    transcript = out or ""
    tools_used = [str(c.get("tool") or "") for c in calls]
    checks = [
        ("4  budget widened for browser work", ex._budget_widened),
        ("3  browser_find was called", any("browser_find" in t for t in tools_used)),
        ("1  a dead interaction was reported back", "byte-for-byte the same" in transcript
            or "THE PAGE IS STILL UNCHANGED" in transcript),
        ("1  escalated after repeats", "THE PAGE IS STILL UNCHANGED" in transcript),
        ("2  a URL carried the query (a second navigate)",
            sum(1 for t in tools_used if "browser_navigate" in t) > 1),
        ("-  DONE was refused", "REFUSED" in transcript),
    ]
    for label, fired in checks:
        print(f"  [{'YES' if fired else ' no'}]  {label}")

    # WHY THE PER-ITEM NUDGE DID OR DID NOT FIRE.
    #
    # It has never fired across four live runs while passing nineteen
    # unit tests, and every one of those runs reported the same thing:
    # no note. Silence has six causes, indistinguishable from the
    # transcript, which is how three fixes came to be proposed for the
    # wrong one. This reads the same ledger the guard reads and names
    # the clause that decided.
    item_state: dict = {}
    try:
        from backend.app.orchestrator.execution_loop import BROWSER_MAX_STEPS
        from backend.app.orchestrator.goal_spec import from_task as _spec_of
        from backend.app.orchestrator.item_state import (
            _LIST_TOOLS, derive, diagnose, verdict,
        )
        from backend.app.orchestrator.task_graph import build as _build_plan

        budget = BROWSER_MAX_STEPS if ex._budget_widened else ex.max_steps
        plan = _build_plan(args.task, budget, spec=_spec_of(args.task))
        prog = derive(calls, plan.feasible_items)
        # What the guard would say at the moment it matters: a call that
        # gathers yet another list rather than opening a candidate.
        would_say = verdict(prog, f"action.{_LIST_TOOLS[0]}")
        eligible = bool(plan.spec.per_item_work) and plan.feasible_items > 1
        item_state = {
            "eligible": eligible,
            "per_item_work": plan.spec.per_item_work,
            "item_count": plan.spec.item_count,
            "feasible_items": plan.feasible_items,
            "examined": prog.examined,
            "listings": prog.listings,
            "discovered": prog.discovered,
            "opened": sorted(prog.opened),
            "read": sorted(prog.read),
            "done": prog.done,
            "unlocated": prog.unlocated,
            "verdict_on_a_list_call": would_say,
            "note_appeared_in_transcript": "NOTE BEFORE THIS CALL" in transcript,
        }

        print("\n" + "=" * 78)
        print("PER-ITEM PROGRESS  (P0-1: why the nudge fires or stays silent)")
        print("=" * 78)
        print(f"  gate      : "
              f"{'EVALUATED every step' if eligible else 'NEVER EVALUATED'}"
              f"  (per_item_work={plan.spec.per_item_work}, "
              f"feasible_items={plan.feasible_items})")
        print(f"  ledger    : {diagnose(prog)}")
        print(f"  verdict   : {would_say}")
        print(f"  in output : "
              f"{'yes' if item_state['note_appeared_in_transcript'] else 'no'}")
        if prog.unlocated:
            print(f"  no address: {', '.join(prog.unlocated[:10])}")
            print("              ^ browser calls whose output carried no page "
                  "address. An arrival this cannot see is an item it cannot "
                  "count as opened.")
        for u in prog.listings:
            print(f"  listing   : {u}")
        for u in prog.discovered[:12]:
            mark = "read" if u in prog.read else ("open" if u in prog.opened else " -  ")
            print(f"    [{mark}] {u}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n(per-item diagnosis unavailable: {exc})")
        item_state = {"error": str(exc)}

    # Written BEFORE anything else is printed. The dump is the artefact
    # worth keeping, and it must survive whatever the console does next.
    with open("browser_trace.json", "w", encoding="utf-8") as fh:
        json.dump({"elapsed": elapsed, "calls": calls, "transcript": transcript,
                   "budget_widened": ex._budget_widened,
                   "item_state": item_state}, fh, indent=1, default=str)

    print("\n" + "=" * 78)
    print(f"TRANSCRIPT  ({elapsed:.0f}s, {len(transcript)} chars)")
    print("=" * 78)
    print(transcript or "(the loop returned nothing — no tool was ever called)")
    print("\nfull trace written to browser_trace.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
