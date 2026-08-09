"""run_test_model — answer one question: is the tool-selection failure
scaffolding, or the model?

Four scaffolding levers were tried (build the tool, lengthen its
description, rank it first, add prompt rules) and `run_python` was called
in 0 of 4 quant runs. `AGENT_LOOP_MODEL` swaps the model for the agentic
loop ONLY — synthesis, critique and evidence stay on the main adapter —
so any behaviour change is attributable to the loop's model rather than
to a different pipeline.

Watch ONE number: the `run_python` call count.

  >= 1  ->  model problem. Route tool-selection to the stronger model and
            most further scaffolding is unnecessary.
  == 0  ->  architecture problem. Build required-outputs.

WHY THIS IS A SCRIPT AND NOT AN HTTP CALL. The documented way to test
this was against a locally-run server. A stale server on :8000 serves the
WRONG code while looking perfectly healthy, which nearly produced a fake
benchmark result once already. Running the pipeline in-process removes
that failure mode: the code under test is the code on disk, necessarily.

WHY IT DRIVES A DynamicEmployee AND NOT Pipeline.run_objective. The first
version of this script called `Pipeline.run_objective` directly, which
looks like the obvious entry point and is the wrong one:
`AgenticExecutor` is constructed in exactly ONE place —
`dynamic_employee.py:575` — so the raw pipeline path can never call a
tool at all. That version would have reported `run_python: 0` with
total confidence, for a run in which no tool COULD have been called.
A measurement whose answer is fixed by construction is worse than no
measurement, so the diagnostic goes through the specialist, the same way
the app does.

THE TWO GUARDS BELOW EXIST BECAUSE A SILENT FALLBACK WOULD FAKE THE
ANSWER. `_build_loop_adapter` catches its own errors and falls back to
the pipeline adapter with a warning. If that happened here the run would
complete normally, report `run_python: 0`, and "confirm" a conclusion it
never tested. So: the model is pre-flighted before the run, and the
fallback warning is treated as a fatal error, not a log line.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
sys.path.insert(0, os.path.dirname(__file__))

# The original prompt from the 9 Aug live test was not preserved. This is
# a faithful reconstruction, kept here as the DEFAULT so every future run
# is comparable to every past one — a diagnostic whose input drifts
# between runs measures nothing. Change it only deliberately.
DEFAULT_PROMPT = (
    "Build a quantitative trading model end to end. Pick a liquid US "
    "equity ETF, define a specific rules-based strategy, fetch real "
    "historical daily price data, implement the strategy, and run a "
    "rigorous backtest on that data. Deliver the results with real "
    "computed numbers: annualised return (CAGR), Sharpe ratio, and "
    "maximum drawdown. Numbers must come from executing code on the "
    "actual data, not from estimation or recall."
)

CONFIG = {
    "limits": {
        "max_spawn_rate_per_minute": 60,
        "min_completeness_threshold": 0.7,
        "max_refinement_depth": 2,
    }
}

# "Sharpe" followed by a real number. Rejects "Sharpe: N/A", "Sharpe ratio
# could not be computed" — the exact shapes the failing runs shipped.
SHARPE_RE = re.compile(r"sharpe[^\n]{0,40}?(-?\d+\.\d+|-?\d+)", re.IGNORECASE)
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def sharpe_in_prose(text: str):
    """Look for a delivered Sharpe OUTSIDE fenced code.

    Caught this reporting itself as a pass: the deliverable contained
    `print(f"Sharpe Ratio: {sharpe:.4f}")` in a code block, and the
    regex matched the `4` in `.4f` as the number. It scored a run
    "real Sharpe delivered" on an unformatted f-string placeholder —
    the same fabrication-shaped failure this project exists to catch,
    committed by its own measuring instrument. Source code is a claim
    about what WOULD be computed; only prose reports what WAS.
    """
    return SHARPE_RE.search(FENCE_RE.sub("", text or ""))


class FallbackDetector(logging.Handler):
    """Turn the loop's silent model-fallback warning into a hard failure.

    `_build_loop_adapter` logs and continues when the override model
    cannot be built. Continuing means the diagnostic silently measures
    the OLD model and reports it as the new one's result.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fell_back = False
        self.override_confirmed = False
        self.step_failures: list = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "could not be built" in msg and "AGENT_LOOP_MODEL" in msg:
            self.fell_back = True
        if "agentic loop using override model" in msg:
            self.override_confirmed = True
        # A step that died on HTTP 5xx / a timeout ended the loop for
        # INFRASTRUCTURE reasons. The tool count from such a run is not a
        # measurement of anything — the model never got to choose. Four
        # replication runs reported a confident "run_python: 0" that was
        # really "Ollama cloud 500'd on step 2", which would have
        # overturned a correct finding with noise.
        if "agentic step call failed" in msg:
            self.step_failures.append(msg[:200])


def preflight(model: str) -> None:
    """Fail before the run, not during it, if the model is unusable."""
    from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter

    print(f"Pre-flighting {model} ...", flush=True)
    adapter = OllamaAdapter(
        base_url=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        model=model,
        api_key=os.environ.get("OLLAMA_API_KEY") or None,
    )
    # Deliberately exercises the SAME path the loop uses, including the
    # adapter's format="json" constrained decoding — a model that answers
    # free-text fine but breaks under JSON constraint would otherwise fail
    # only once the run was already underway.
    out = adapter.chat_completion(
        'Reply with exactly this JSON and nothing else: {"ok": true}',
        max_tokens=2000,
    )
    text = out if isinstance(out, str) else str(out)
    if not text.strip():
        raise SystemExit(f"FATAL: {model} returned nothing on pre-flight.")
    print(f"  ok -> {text.strip()[:80]}\n", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    ap.add_argument("--model", default=os.environ.get("AGENT_LOOP_MODEL", ""),
                    help="model for the agentic loop only (AGENT_LOOP_MODEL)")
    ap.add_argument("--pipeline-model", default="gpt-oss:120b-cloud",
                    help="model for synthesis/critique/evidence (held constant)")
    ap.add_argument("--label", default="", help="name for the result file")
    # Thinking models spend num_predict on `thinking` BEFORE `response`,
    # so the default 700 leaves a reasoning-heavy model with nothing left
    # to answer with. Held identical across compared runs — it is a
    # confound otherwise, and the baseline's 0/4 was measured at 700.
    ap.add_argument("--step-tokens", type=int, default=3000,
                    help="AGENT_STEP_MAX_TOKENS for the loop (default 700 is "
                         "too small for thinking models)")
    args = ap.parse_args()

    if not args.model:
        return _die("pass --model (or set AGENT_LOOP_MODEL). Without an "
                    "override this measures the baseline you already have.")

    # Must be set before the loop imports/builds — both are read at
    # module import and call time respectively.
    os.environ["AGENT_LOOP_MODEL"] = args.model
    os.environ["AGENT_STEP_MAX_TOKENS"] = str(args.step_tokens)

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    log_buf = io.StringIO()
    detector = FallbackDetector()
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.StreamHandler(log_buf)])
    logging.getLogger().addHandler(detector)

    preflight(args.model)

    from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter
    from backend.app.orchestrator.pipeline_controller import Pipeline
    from backend.app.orchestrator.synthesis import SynthesisEngine
    from backend.app.critique.critique_agent import CritiqueEngine
    from backend.app.employees.dynamic_employee import DynamicEmployee
    from backend.app.tools.tool_call_ledger import get_call_ledger

    ledger = get_call_ledger()
    ledger.reset()  # a per-run count is the whole measurement

    adapter = OllamaAdapter(model=args.pipeline_model)
    pipeline = Pipeline(
        model_adapter=adapter,
        config=CONFIG,
        synthesis_engine=SynthesisEngine(adapter, max_tokens=5000),
        critique_engine=CritiqueEngine(adapter, max_tokens=4000),
    )

    # ONE specialist, not a full supervisor-led team. The question here
    # is narrow — "does the loop reach for the compute tool?" — and a
    # team run answers it through a layer of delegation variance that
    # would make two runs incomparable. The loop is per-specialist, so
    # one specialist is a complete test of it.
    employee = DynamicEmployee(
        employee_id="diag_tool_selection",
        role="Quantitative Analyst",
        mandate=(
            "You build and backtest trading strategies. You never estimate "
            "or recall a performance number — you compute it from real data "
            "by executing code, and you report exactly what the execution "
            "printed."
        ),
        pipeline=pipeline,
    )

    print("=" * 68)
    print(f"loop model     : {args.model}")
    print(f"pipeline model : {args.pipeline_model}  (held constant)")
    print(f"step tokens    : {args.step_tokens}")
    print(f"path           : DynamicEmployee.run_task (the loop lives here)")
    print("=" * 68 + "\n")

    started = datetime.now()
    error = None
    output = ""
    try:
        result = employee.run_task(args.prompt)
        output = (result or {}).get("output") or ""
        critique = (result or {}).get("critique") or {}
        snap = {
            "status": "completed",
            "completeness": critique.get("completeness_score"),
            "fabricated_claims": critique.get("fabricated_claims"),
            "was_refined": (result or {}).get("was_refined"),
        }
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        snap = {"status": "crashed"}

    elapsed = (datetime.now() - started).total_seconds()

    # ---- the measurement -------------------------------------------
    py_calls = ledger.calls("run_python")
    data_calls = ledger.calls("fetch_market_data")
    histogram: dict = {}
    for c in ledger.calls():
        histogram[c["tool"]] = histogram.get(c["tool"], 0) + 1

    sharpe = sharpe_in_prose(output)

    if detector.fell_back or (not detector.override_confirmed and not error):
        print("\n" + "!" * 68)
        print("INVALID RUN — the loop did NOT use the override model.")
        print("A fallback happened, so this result describes the OLD model.")
        print("!" * 68)
        return 2

    if detector.step_failures:
        print("\n" + "!" * 68)
        print("INVALID RUN — the agentic loop was cut short by an "
              "infrastructure failure, not by the model's choice:")
        for f in detector.step_failures:
            print(f"  {f}")
        print(f"\n(Observed {len(py_calls)} run_python call(s) before it died "
              "— NOT a result. Re-run when the backend is healthy.)")
        print("!" * 68)
        return 3

    # NOT "model problem" on a nonzero count. This script originally said
    # exactly that, and was wrong the first time it mattered: nemotron
    # called run_python once, which looked like a clean win over the
    # baseline's 0-of-4 — until the gpt-oss CONTROL on this same harness
    # called it 5 times. The baseline's zero belonged to the old harness,
    # not to the model, so a single arm can only report what it observed.
    # One arm is an observation; attribution needs the control.
    verdict = (
        f"{len(py_calls)} run_python call(s) on THIS harness. A single arm "
        "attributes nothing — compare against a control arm that changes "
        "only the variable under test."
        if py_calls else
        "0 run_python calls — the tool was not selected on this harness."
    )

    print("\n" + "=" * 68)
    print("RESULT")
    print("=" * 68)
    print(f"run_python calls      : {len(py_calls)}   <-- the number that matters")
    print(f"fetch_market_data     : {len(data_calls)}")
    print(f"all tool calls        : {histogram or '{}'}")
    print(f"real Sharpe delivered : {sharpe.group(0) if sharpe else 'NO'}")
    print(f"status                : {snap.get('status')}")
    print(f"elapsed               : {elapsed:.0f}s")
    if error:
        print(f"error                 : {error}")
    print(f"\nVERDICT: {verdict}")

    label = args.label or args.model.replace(":", "_").replace("/", "_")
    stamp = started.strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(os.path.dirname(__file__), f"diag_{label}_{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({
            "loop_model": args.model,
            "pipeline_model": args.pipeline_model,
            "step_max_tokens": args.step_tokens,
            "prompt": args.prompt,
            "run_python_calls": len(py_calls),
            "fetch_market_data_calls": len(data_calls),
            "tool_histogram": histogram,
            "sharpe_in_deliverable": sharpe.group(0) if sharpe else None,
            "status": snap.get("status"),
            "run_summary": snap,
            "error": error,
            "elapsed_seconds": elapsed,
            "verdict": verdict,
            "deliverable": output,
            "log": log_buf.getvalue()[-40000:],
        }, fh, indent=2)
    print(f"\nfull record -> {out_path}")
    return 0


def _die(msg: str) -> int:
    print(f"FATAL: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
