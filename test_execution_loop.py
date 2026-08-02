"""Smoke test: AgenticExecutor — the multi-step THINK -> ACT -> OBSERVE loop.

All offline. A scripted adapter returns pre-canned decisions so we test
the LOOP's mechanics (chaining, self-correction, every bound) rather
than an LLM's judgement, which would make these non-deterministic.

Run with:
    py -3 test_execution_loop.py
"""
from __future__ import annotations

import json

from backend.app.orchestrator.execution_loop import AgenticExecutor


class ScriptedAdapter:
    """Returns queued decisions in order; records prompts it received."""

    def __init__(self, decisions):
        self.decisions = [json.dumps(d) if isinstance(d, dict) else d for d in decisions]
        self.prompts = []

    def chat_completion(self, prompt, **kw):
        self.prompts.append(prompt)
        if not self.decisions:
            return json.dumps({"thought": "out of script", "action": "DONE"})
        return self.decisions.pop(0)


def _executor(decisions, **kw):
    ex = AgenticExecutor(ScriptedAdapter(decisions), **kw)
    # Deterministic fake tool surface — no MCP/network needed.
    ex._available_tools = lambda: [
        {"qualified_name": "action.read_file",
         "description": "Read a file",
         "input_schema": {"properties": {"path": {}}}},
        {"qualified_name": "action.send_email",
         "description": "Send an email",
         "input_schema": {"properties": {"to": {}, "subject": {}, "body": {}}}},
    ]
    return ex


def test_multi_step_chaining():
    """The core capability: call a tool, SEE the result, then choose the
    next call based on it. Single-shot planning could never do this."""
    calls = []
    ex = _executor([
        {"thought": "read the list first", "action": "action.read_file",
         "arguments": {"path": "investors.txt"}},
        {"thought": "now email the one I found", "action": "action.send_email",
         "arguments": {"to": "dana@northwind.vc", "subject": "Hi", "body": "..."}},
        {"thought": "done", "action": "DONE"},
    ])

    def fake_exec(qname, args):
        calls.append((qname, args))
        if qname == "action.read_file":
            return "dana@northwind.vc"
        return "[queued for founder approval — pending_id=pend_1]"

    ex._execute = fake_exec
    out = ex.run("email the investor in investors.txt", role="Fundraising Lead")

    assert out is not None
    assert len(calls) == 2, calls
    assert calls[0][0] == "action.read_file"
    assert calls[1][0] == "action.send_email"
    # The 2nd decision's prompt must contain the 1st call's REAL result —
    # that's the proof the model saw the observation before deciding.
    assert "dana@northwind.vc" in ex.adapter.prompts[1]
    assert "Step 1" in out and "Step 2" in out
    print("[ok] multi-step chaining: tool 2 chosen after seeing tool 1's real result")


def test_unknown_tool_self_corrects():
    """A hallucinated tool name must not kill the run — the model is told
    and gets to correct itself."""
    ex = _executor([
        {"thought": "try this", "action": "action.nonexistent", "arguments": {}},
        {"thought": "use the real one", "action": "action.read_file",
         "arguments": {"path": "a.txt"}},
        {"thought": "done", "action": "DONE"},
    ])
    ex._execute = lambda q, a: "file contents"
    out = ex.run("read a file", role="Analyst")
    assert out is not None
    assert "no such tool" in out
    assert "file contents" in out
    print("[ok] unknown tool: loop reports it, model self-corrects, run continues")


def test_failed_call_becomes_observation():
    """A tool raising must become an observation, not an exception."""
    ex = _executor([
        {"thought": "read it", "action": "action.read_file", "arguments": {"path": "x"}},
        {"thought": "done", "action": "DONE"},
    ])

    def boom(qname, args):
        raise RuntimeError("disk on fire")

    ex._execute = AgenticExecutor._execute.__get__(ex)  # use the real router
    ex._available_tools = lambda: [
        {"qualified_name": "action.read_file", "description": "Read",
         "input_schema": {"properties": {"path": {}}}},
    ]
    # Force the underlying action registry call to raise.
    import backend.app.orchestrator.execution_loop as mod
    orig = mod._action_registry.call
    mod._action_registry.call = boom
    try:
        out = ex.run("read x", role="Analyst")
    finally:
        mod._action_registry.call = orig
    assert out is not None
    assert "call failed" in out and "disk on fire" in out
    print("[ok] failing tool: captured as an observation, run survives")


def test_repeat_call_guard():
    """A model stuck re-issuing the identical call must be stopped."""
    same = {"thought": "again", "action": "action.read_file", "arguments": {"path": "a"}}
    ex = _executor([same, same, same, same])
    ex._execute = lambda q, a: "same result"
    out = ex.run("read a", role="Analyst")
    assert out is not None
    assert "repeated the same" in out
    print("[ok] repeat-call guard: identical call twice stops the loop")


def test_max_steps_bound():
    """Never exceed MAX_STEPS tool calls, even if the model never says DONE."""
    n = 0

    def unique_decision(prompt, **kw):
        nonlocal n
        n += 1
        return json.dumps({"thought": f"step {n}", "action": "action.read_file",
                           "arguments": {"path": f"file_{n}.txt"}})

    ex = _executor([], max_steps=4)
    ex.adapter.chat_completion = unique_decision
    executed = []
    ex._execute = lambda q, a: executed.append(a) or "ok"
    ex.run("keep going forever", role="Analyst")
    assert len(executed) == 4, f"expected exactly 4 calls, got {len(executed)}"
    print("[ok] max-steps bound: stopped at exactly 4 calls despite no DONE")


def test_approval_receipt_not_treated_as_done():
    """A queued (unapproved) action must be surfaced as NOT-yet-done, so
    the specialist can't claim it happened."""
    ex = _executor([
        {"thought": "send it", "action": "action.send_email",
         "arguments": {"to": "a@b.c", "subject": "s", "body": "b"}},
        {"thought": "done", "action": "DONE"},
    ])
    ex._execute = lambda q, a: "[queued for founder approval — pending_id=pend_9]"
    out = ex.run("email them", role="Fundraising Lead")
    assert "queued for founder approval" in out
    assert "has NOT happened yet" in out, "loop must warn the writer it isn't done"
    print("[ok] approval gating: queued action surfaced as not-yet-done")


def test_no_tools_returns_none():
    ex = _executor([])
    ex._available_tools = lambda: []
    assert ex.run("do something", role="Analyst") is None
    print("[ok] no tools available: returns None, caller unaffected")


def test_no_action_taken_returns_none():
    """Model says DONE immediately -> nothing to inject."""
    ex = _executor([{"thought": "nothing needed", "action": "DONE"}])
    ex._execute = lambda q, a: "should not be called"
    assert ex.run("write a poem", role="Writer") is None
    print("[ok] immediate DONE: returns None rather than an empty block")


if __name__ == "__main__":
    test_multi_step_chaining()
    test_unknown_tool_self_corrects()
    test_failed_call_becomes_observation()
    test_repeat_call_guard()
    test_max_steps_bound()
    test_approval_receipt_not_treated_as_done()
    test_no_tools_returns_none()
    test_no_action_taken_returns_none()
    print("\nAll tests passed.")
