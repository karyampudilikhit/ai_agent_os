"""Required-output contracts: an employee can declare what a finished
turn must contain, and the loop refuses DONE until it does.

The critical property is that config can only ever ADD a requirement.
A settings page that could switch a guard off would be a way to disable
the compute gate, which is the opposite of why it exists.
"""
from __future__ import annotations

import json
import os

from backend.app.orchestrator.execution_loop import (
    MAX_DONE_REFUSALS,
    AgenticExecutor,
)
from backend.app.orchestrator.output_contract import (
    EXECUTED_CODE,
    FETCHED_URL,
    FILE_WRITTEN,
    satisfied_kinds,
    unsatisfied_kinds,
)

COMPUTE_TASK = "backtest SPY and report CAGR, Sharpe and max drawdown"
PLAIN_TASK = "summarise the attached meeting notes"


class ScriptedAdapter:
    def __init__(self, decisions):
        self.decisions = [json.dumps(d) if isinstance(d, dict) else d
                          for d in decisions]
        self.prompts = []
        self.max_tokens_seen = []

    def chat_completion(self, prompt, **kw):
        self.prompts.append(prompt)
        self.max_tokens_seen.append(kw.get("max_tokens"))
        if not self.decisions:
            return json.dumps({"thought": "out of script", "action": "DONE"})
        return self.decisions.pop(0)


def _executor(decisions, **kw):
    ex = AgenticExecutor(ScriptedAdapter(decisions), **kw)
    ex._available_tools = lambda: [
        {"qualified_name": "action.fetch_market_data", "description": "Fetch bars",
         "input_schema": {"properties": {"symbol": {}}}},
        {"qualified_name": "action.run_python", "description": "Execute Python",
         "input_schema": {"properties": {"code": {}}}},
        {"qualified_name": "action.write_file", "description": "Write a file",
         "input_schema": {"properties": {"path": {}, "content": {}}}},
    ]
    return ex


def _fetch_then_done(n=6):
    return [
        {"thought": "get data", "action": "action.fetch_market_data",
         "arguments": {"symbol": "SPY"}},
    ] + [{"thought": "finished", "action": "DONE"}] * n


# ------------------------------------------- the OR that must not weaken

def test_an_employee_contract_adds_a_requirement_the_task_did_not():
    """A plain summarisation task carries no compute requirement. An
    employee whose job is always to compute can still demand one."""
    ex = _executor(_fetch_then_done(), required_outputs=(EXECUTED_CODE,))
    ex._execute = lambda q, a: "ok"

    ex.run(PLAIN_TASK, role="Quant Analyst")

    assert any("REFUSED" in p for p in ex.adapter.prompts)


def test_an_empty_contract_cannot_weaken_the_task_requirement():
    """THE PROPERTY THAT MATTERS. Configuring an employee with no
    required outputs must not switch off the requirement the task text
    established -- otherwise the settings page is a way to disable the
    compute gate."""
    ex = _executor(_fetch_then_done(), required_outputs=())
    ex._execute = lambda q, a: "ok"

    ex.run(COMPUTE_TASK, role="Quant Analyst")

    assert any("REFUSED" in p for p in ex.adapter.prompts), (
        "an empty contract silently disabled the task-derived compute gate")


def test_no_requirement_at_all_means_no_refusal():
    ex = _executor(_fetch_then_done(), required_outputs=())
    ex._execute = lambda q, a: "ok"

    ex.run(PLAIN_TASK, role="Analyst")

    assert not any("REFUSED" in p for p in ex.adapter.prompts)


# ------------------------------------------------------------- the kinds

def test_fetched_url_is_satisfied_by_a_real_fetch():
    ex = _executor([
        {"thought": "fetch", "action": "action.fetch_market_data",
         "arguments": {"symbol": "SPY"}},
        {"thought": "done", "action": "DONE"},
    ], required_outputs=(FETCHED_URL,))
    ex._execute = lambda q, a: "3776 rows"

    ex.run(PLAIN_TASK, role="Researcher")

    assert not any("REFUSED" in p for p in ex.adapter.prompts)


def test_fetched_url_refuses_when_nothing_was_fetched():
    ex = _executor([
        {"thought": "compute", "action": "action.run_python",
         "arguments": {"code": "print(1)"}},
        {"thought": "done", "action": "DONE"},
        {"thought": "still done", "action": "DONE"},
    ], required_outputs=(FETCHED_URL,))
    ex._execute = lambda q, a: "Python output:\n1"

    ex.run(PLAIN_TASK, role="Researcher")

    assert any("REFUSED" in p for p in ex.adapter.prompts)
    assert any("not retrieving it" in p for p in ex.adapter.prompts)


def test_file_written_checks_the_disk_not_the_call_log(tmp_path):
    """A write tool succeeding is not proof a file exists. A run once
    reported figures 'saved at' a path in a directory that had never
    been created."""
    real = tmp_path / "report.pdf"
    real.write_text("x")
    ghost = str(tmp_path / "nope" / "missing.pdf")

    calls_real = [{"tool": "action.write_file", "ok": True,
                   "args_text": json.dumps({"path": str(real)})}]
    calls_ghost = [{"tool": "action.write_file", "ok": True,
                    "args_text": json.dumps({"path": ghost})}]

    assert FILE_WRITTEN in satisfied_kinds(set(), False, calls_real)
    assert FILE_WRITTEN not in satisfied_kinds(set(), False, calls_ghost), (
        "a claimed path that does not exist satisfied the contract")


def test_a_failed_write_does_not_count(tmp_path):
    real = tmp_path / "report.pdf"
    real.write_text("x")
    calls = [{"tool": "action.write_file", "ok": False,
              "args_text": json.dumps({"path": str(real)})}]

    assert FILE_WRITTEN not in satisfied_kinds(set(), False, calls)


def test_unsatisfied_kinds_is_deterministically_ordered():
    """Refusal notes must not shuffle between runs."""
    out = unsatisfied_kinds(
        [FILE_WRITTEN, EXECUTED_CODE, FETCHED_URL], set(), False, [])
    assert out == [EXECUTED_CODE, FETCHED_URL, FILE_WRITTEN]


def test_unknown_kinds_are_ignored_not_crashed_on():
    assert unsatisfied_kinds(["make_coffee"], set(), False, []) == []


# ----------------------------------------------------------- the budgets

def test_max_steps_caps_the_loop():
    ex = _executor([
        {"thought": "fetch", "action": "action.fetch_market_data",
         "arguments": {"symbol": "SPY"}},
        {"thought": "again", "action": "action.run_python",
         "arguments": {"code": "print(1)"}},
        {"thought": "more", "action": "action.write_file",
         "arguments": {"path": "x", "content": "y"}},
    ], max_steps=2)
    calls = []
    ex._execute = lambda q, a: calls.append(q) or "ok"

    ex.run(PLAIN_TASK, role="Analyst")

    assert len(calls) == 2, calls


def test_step_max_tokens_reaches_the_model_call():
    ex = _executor([{"thought": "done", "action": "DONE"}], step_max_tokens=1234)
    ex._execute = lambda q, a: "ok"

    ex.run(PLAIN_TASK, role="Analyst")

    # More than one call is expected: DONE with zero successful tool calls
    # trips the pre-existing advisory challenge. Every call must carry the
    # configured budget, not just the first.
    assert ex.adapter.max_tokens_seen, "no model call was made"
    assert set(ex.adapter.max_tokens_seen) == {1234}


def test_refusals_stay_bounded_with_multiple_kinds():
    """Two unmet kinds must not double the refusal budget."""
    ex = _executor(_fetch_then_done(8),
                   required_outputs=(EXECUTED_CODE, FILE_WRITTEN))
    ex._execute = lambda q, a: "ok"

    ex.run(PLAIN_TASK, role="Analyst")

    last = ex.adapter.prompts[-1]
    assert last.count("REFUSED") == MAX_DONE_REFUSALS * 2, (
        "expected %d rounds x 2 kinds" % MAX_DONE_REFUSALS)


# ----------------------------------------------------- the standing rules

def test_standing_rules_reach_the_step_prompt():
    """The half that matters for correctness: 'always adjusted close' is
    a rule about the CODE the loop writes, and until now the loop never
    saw the employee's mandate at all."""
    ex = _executor([{"thought": "done", "action": "DONE"}],
                   standing_rules=("always adjusted close", "T+1 execution"))
    ex._execute = lambda q, a: "ok"

    ex.run(PLAIN_TASK, role="Quant Analyst")

    prompt = ex.adapter.prompts[0]
    assert "STANDING RULES FOR YOUR WORK" in prompt
    assert "- always adjusted close" in prompt
    assert "- T+1 execution" in prompt


def test_no_standing_rules_leaves_the_prompt_clean():
    ex = _executor([{"thought": "done", "action": "DONE"}])
    ex._execute = lambda q, a: "ok"

    ex.run(PLAIN_TASK, role="Analyst")

    assert "STANDING RULES" not in ex.adapter.prompts[0]
