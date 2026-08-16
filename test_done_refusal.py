"""The required-outputs gate: DONE is REFUSED while the computation the
task asked for has not actually run.

The live failure, 2026-08-15. Asked to backtest SPY and report CAGR,
Sharpe and max drawdown, the Quant Analyst called fetch_market_data at
step 1 and returned DONE at step 2. The advisory DONE-challenge fired
once, the model said DONE again, and the loop conceded -- having computed
nothing, with 8 steps unused and `run_python` ranked FIRST in its tool
list. The deliverable reported no figures at all.

The distinction under test: the old challenge ASKS and accepts whatever
comes back. This gate queries a recorded fact -- did a compute tool
actually succeed -- and refuses DONE until it did.

Offline. A scripted adapter returns canned decisions so this tests the
LOOP, not an LLM's judgement.
"""
from __future__ import annotations

import json

from backend.app.orchestrator.execution_loop import (
    MAX_DONE_REFUSALS,
    AgenticExecutor,
)

COMPUTE_TASK = "backtest SPY and report CAGR, Sharpe ratio and max drawdown"
PLAIN_TASK = "summarise the attached meeting notes"


class ScriptedAdapter:
    def __init__(self, decisions):
        self.decisions = [json.dumps(d) if isinstance(d, dict) else d
                          for d in decisions]
        self.prompts = []

    def chat_completion(self, prompt, **kw):
        self.prompts.append(prompt)
        if not self.decisions:
            return json.dumps({"thought": "out of script", "action": "DONE"})
        return self.decisions.pop(0)


def _executor(decisions, **kw):
    ex = AgenticExecutor(ScriptedAdapter(decisions), **kw)
    ex._available_tools = lambda: [
        {"qualified_name": "action.fetch_market_data",
         "description": "Fetch daily price bars",
         "input_schema": {"properties": {"symbol": {}}}},
        {"qualified_name": "action.run_python",
         "description": "Execute Python and return stdout",
         "input_schema": {"properties": {"code": {}}}},
    ]
    return ex


def _fetch_then_done(extra_done=6):
    """The exact live trace: one fetch, then DONE forever after."""
    return [
        {"thought": "get the data", "action": "action.fetch_market_data",
         "arguments": {"symbol": "SPY"}},
    ] + [{"thought": "finished", "action": "DONE"}] * extra_done


def test_done_is_refused_until_the_computation_actually_runs():
    """The regression. Same script that shipped an empty deliverable --
    this time the loop must not accept it."""
    calls = []
    ex = _executor(_fetch_then_done(2) + [
        {"thought": "fine, computing it", "action": "action.run_python",
         "arguments": {"code": "print('CAGR 11.2%')"}},
        {"thought": "now it is really done", "action": "DONE"},
    ])

    def fake_exec(qname, args):
        calls.append(qname)
        return "CAGR 11.26% Sharpe 0.93" if qname == "action.run_python" else "ok, 3776 rows"

    ex._execute = fake_exec
    out = ex.run(COMPUTE_TASK, role="Quant Analyst")

    assert "action.run_python" in calls, (
        "the loop accepted DONE without ever computing anything: %s" % calls)
    assert out is not None and "CAGR 11.26%" in out

    # The refusal has to actually TELL it what to do, or it is just a
    # slower way to reach the same DONE.
    refusal_prompt = next(p for p in ex.adapter.prompts if "REFUSED" in p)
    assert "action.run_python" in refusal_prompt
    assert "Fetching the data is not computing the answer" in refusal_prompt


def test_refusal_is_bounded_so_a_stuck_specialist_can_still_stop():
    """A specialist that genuinely cannot compute must not be trapped
    arguing until the step budget is gone."""
    ex = _executor(_fetch_then_done(extra_done=8))
    ex._execute = lambda q, a: "ok"

    out = ex.run(COMPUTE_TASK, role="Quant Analyst")

    # Counted inside the LAST prompt, not across prompts: the transcript
    # accumulates, so every prompt after the first refusal contains one.
    refusals = ex.adapter.prompts[-1].count("REFUSED")
    assert refusals == MAX_DONE_REFUSALS, (
        "expected exactly %d refusals, saw %d" % (MAX_DONE_REFUSALS, refusals))
    assert out is not None, "the loop must still return the work it did do"


def test_no_refusal_when_the_task_needs_no_computation():
    """The gate must not fire on ordinary work. A summarisation task that
    returns DONE after one call is finished, not slacking."""
    ex = _executor([
        {"thought": "read it", "action": "action.fetch_market_data",
         "arguments": {"symbol": "SPY"}},
        {"thought": "done", "action": "DONE"},
    ])
    ex._execute = lambda q, a: "ok"

    ex.run(PLAIN_TASK, role="Analyst")

    assert not any("REFUSED" in p for p in ex.adapter.prompts)


def test_gate_fires_on_the_founders_wording_not_just_the_paraphrase():
    """D3's lesson, applied here. The Supervisor's paraphrase routinely
    drops the metric names -- 'design a rules-based strategy' does not
    contain 'Sharpe'. The gate reads the founder's original task too, so
    a paraphrase cannot silently disable it."""
    ex = _executor(_fetch_then_done(extra_done=6))
    ex._execute = lambda q, a: "ok"

    ex.run(
        "Design a rules-based trading strategy for the selected ETF",
        role="Quant Analyst",
        original_task=COMPUTE_TASK,
    )

    assert any("REFUSED" in p for p in ex.adapter.prompts), (
        "the paraphrased sub-task hid the metric names and the gate missed it")


def test_a_successful_compute_lets_done_through_immediately():
    """No nagging once the recorded fact exists."""
    ex = _executor([
        {"thought": "compute it", "action": "action.run_python",
         "arguments": {"code": "print(1)"}},
        {"thought": "done", "action": "DONE"},
    ])
    ex._execute = lambda q, a: "Sharpe 0.74"

    out = ex.run(COMPUTE_TASK, role="Quant Analyst")

    assert not any("REFUSED" in p for p in ex.adapter.prompts)
    assert out is not None and "Sharpe 0.74" in out


    # These are run_python's REAL return strings for the two
    # computed-nothing outcomes, copied from
    # actions/builtin/run_python.py. Using invented prose here would make
    # the test pass while the production shape still slipped through --
    # which is exactly what happened on the first draft of this file.
CRASHED = (
    "Python exited with code 1. This code did NOT run successfully - do not "
    "report its intended results as if it had.\n\nTraceback (most recent call "
    "last):\n  File \"<stdin>\", line 1\nNameError: name 'boom' is not defined"
)
PRINTED_NOTHING = (
    "Python ran successfully but printed nothing. Results are only visible if "
    "you print() them - add print() around the numbers you need, then run it "
    "again."
)


def test_a_crashed_compute_does_not_count_as_computing():
    """A run_python that errored has computed nothing. Letting it satisfy
    the gate would rebuild D6 -- passing 'yes, it computed' on a call that
    produced no number.

    This also covers a real bug this test found: run_python does NOT use
    the parenthesised failure marker the loop recognises, so a crashed
    script was reading as a successful call system-wide."""
    ex = _executor([
        {"thought": "try", "action": "action.run_python",
         "arguments": {"code": "boom"}},
        {"thought": "give up", "action": "DONE"},
        {"thought": "still giving up", "action": "DONE"},
    ])
    ex._execute = lambda q, a: CRASHED

    ex.run(COMPUTE_TASK, role="Quant Analyst")

    assert any("REFUSED" in p for p in ex.adapter.prompts), (
        "a crashed run_python was accepted as evidence the work was done")


def test_a_compute_that_printed_nothing_does_not_count():
    """Exit code 0 with no output is a successful call that produced no
    figure. The deliverable has nothing to quote, so DONE stays refused."""
    ex = _executor([
        {"thought": "compute", "action": "action.run_python",
         "arguments": {"code": "sharpe = 0.74"}},
        {"thought": "done", "action": "DONE"},
        {"thought": "really done", "action": "DONE"},
    ])
    ex._execute = lambda q, a: PRINTED_NOTHING

    ex.run(COMPUTE_TASK, role="Quant Analyst")

    assert any("REFUSED" in p for p in ex.adapter.prompts), (
        "a run that printed nothing was accepted as having computed something")
