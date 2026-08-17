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
    from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter
    # Same defaults main._build_adapter uses, so this exercises the model
    # the server actually runs rather than a lookalike.
    return OllamaAdapter(
        base_url=os.environ.get("OLLAMA_HOST", "").strip() or "http://localhost:11434",
        model=os.environ.get("PIPELINE_MODEL", "").strip() or "gpt-oss:120b-cloud",
        api_key=os.environ.get("OLLAMA_API_KEY", "").strip() or None,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="loop model override")
    ap.add_argument("--task", default=TASK)
    ap.add_argument("--role", default="Market Screener Analyst")
    args = ap.parse_args()

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

    # Written BEFORE anything else is printed. The dump is the artefact
    # worth keeping, and it must survive whatever the console does next.
    with open("browser_trace.json", "w", encoding="utf-8") as fh:
        json.dump({"elapsed": elapsed, "calls": calls, "transcript": transcript,
                   "budget_widened": ex._budget_widened}, fh, indent=1, default=str)

    print("\n" + "=" * 78)
    print(f"TRANSCRIPT  ({elapsed:.0f}s, {len(transcript)} chars)")
    print("=" * 78)
    print(transcript or "(the loop returned nothing — no tool was ever called)")
    print("\nfull trace written to browser_trace.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
