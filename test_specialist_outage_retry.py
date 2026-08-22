"""Turn-level recovery when the model backend drops a specialist's turn.

Covers the failure from the 2026-08-15 incident report: the Quant Analyst
took four consecutive HTTP 500s, returned nothing, and the whole team run
was reported to the founder as the team failing to do its work.

The three cases that matter are the three asserted here:
  1. backend died, retry succeeds        -> run recovers, nobody re-runs
     the specialists that already worked
  2. backend stays dead                  -> marked as an OUTAGE, not as a
     refusal, and the banner says so
  3. empty output with NO outage recorded -> NO retry. A specialist that
     genuinely declines must not be re-run in a loop; only a recorded
     backend failure earns a second attempt.
"""

import time

import pytest

from backend.app.employees import employee_coordinator as ec_mod
from backend.app.employees.employee_coordinator import EmployeeCoordinator
from backend.app.tools.tool_call_ledger import get_call_ledger


# ---------------------------------------------------------------- stubs

class _Adapter:
    def chat_completion(self, prompt, **kw):
        raise RuntimeError("no model in this test")


class _Pipeline:
    adapter = _Adapter()


class _Planner:
    """Stands in for SupervisorPlanner: fixed plan, concatenating merge."""

    def __init__(self, model_adapter=None):
        pass

    def design_delegation(self, prompt, specialists_spec):
        return [{"role": s["role"], "sub_task": "do " + s["role"]}
                for s in specialists_spec]

    def synthesize(self, prompt, contributions):
        return " | ".join((c.get("output") or "") for c in contributions
                          if (c.get("output") or "").strip())


class _Employee:
    """Specialist whose turns are scripted.

    Each entry in `script` is the output for that attempt; an entry of
    "" also records a `backend.unavailable` ledger entry when
    `outage_on_empty` is set, mimicking what execution_loop does when it
    gives up on a step call.
    """

    def __init__(self, role, script, outage_on_empty=True):
        self.role = role
        self.mandate = "test mandate"
        self.script = list(script)
        self.outage_on_empty = outage_on_empty
        self.calls = 0

    def run_task(self, sub_task, teammates_context=None, task_brief=None,
                 original_task=None):
        out = self.script[self.calls] if self.calls < len(self.script) else ""
        self.calls += 1
        if not out and self.outage_on_empty:
            get_call_ledger().record(
                "backend.unavailable", {"role": self.role, "attempts": 4},
                ok=False, result_preview="HTTP 500", role=self.role,
            )
        return {"output": out, "critique": {"completeness_score": 0.9}}


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    """No real sleeping, no cross-test ledger bleed.

    Resetting matters here for the same reason the `since=` parameter had
    to be added to the ledger: it is a process-global singleton, so a
    leftover `backend.unavailable` from an earlier test would make a
    later one retry for the wrong reason.
    """
    monkeypatch.setattr(ec_mod, "SPECIALIST_RETRY_DELAY", 0.0)
    monkeypatch.setattr(ec_mod, "SupervisorPlanner", _Planner)
    get_call_ledger().reset()
    yield
    get_call_ledger().reset()


def _run(specialists):
    coord = EmployeeCoordinator(_Pipeline(), synthesis_engine=object())
    supervisor = _Employee("Supervisor", ["unused"])
    return coord.run_with_supervisor("backtest SPY", supervisor, specialists)


# ---------------------------------------------------------------- tests

def test_specialist_recovers_on_retry_and_others_are_not_rerun():
    quant = _Employee("Quant Analyst", ["", "Sharpe 0.74"])
    writer = _Employee("Report Writer", ["the writeup"])

    result = _run([quant, writer])

    assert quant.calls == 2, "the dead specialist should be re-run exactly once"
    assert writer.calls == 1, "a healthy specialist must NOT be re-run"

    contribs = {c["role"]: c for c in result["contributions"]}
    assert contribs["Quant Analyst"]["output"] == "Sharpe 0.74"
    assert contribs["Quant Analyst"].get("recovered_after_outage") is True
    assert not contribs["Quant Analyst"].get("blocked_by_outage")

    # The run delivers instead of dying -- the whole point.
    assert "Sharpe 0.74" in result["final_output"]
    assert "Team completeness note" not in result["final_output"]


def test_sustained_outage_is_reported_as_outage_not_refusal():
    quant = _Employee("Quant Analyst", ["", ""])          # never recovers
    writer = _Employee("Report Writer", ["the writeup"])

    result = _run([quant, writer])

    assert quant.calls == 2, "one retry, then give up -- not an endless loop"

    contribs = {c["role"]: c for c in result["contributions"]}
    assert contribs["Quant Analyst"].get("blocked_by_outage") is True

    banner = result["final_output"]
    assert "Team completeness note" in banner
    assert "infrastructure outage" in banner
    assert "not a refusal" in banner
    # The misleading old wording must be gone for this case.
    assert "produced no output at all" not in banner


def test_no_retry_when_no_outage_was_recorded():
    """A specialist that returns nothing WITHOUT a recorded backend
    failure is not retried. Retrying a genuine refusal just burns quota
    and, on a bursty backend, makes the real outage harder to see."""
    quiet = _Employee("Quant Analyst", ["", "would have worked"],
                      outage_on_empty=False)
    writer = _Employee("Report Writer", ["the writeup"])

    result = _run([quiet, writer])

    assert quiet.calls == 1, "no ledger evidence of an outage -> no retry"

    contribs = {c["role"]: c for c in result["contributions"]}
    assert not contribs["Quant Analyst"].get("blocked_by_outage")
    assert "produced no output at all" in result["final_output"]


def test_retry_budget_caps_a_fully_dead_backend():
    """Three dead specialists must not cost three retries. The budget
    stops the run from taking ~2x wall-clock when nothing can succeed."""
    dead = [_Employee("Quant Analyst", ["", ""]),
            _Employee("Data Engineer", ["", ""]),
            _Employee("Report Writer", ["", ""])]

    _run(dead)

    total_calls = sum(e.calls for e in dead)
    expected = len(dead) + ec_mod.SPECIALIST_OUTAGE_RETRY_BUDGET
    assert total_calls == expected, (
        "expected %d calls (%d first attempts + %d budgeted retries), got %d"
        % (expected, len(dead), ec_mod.SPECIALIST_OUTAGE_RETRY_BUDGET, total_calls))


def test_outage_during_reads_the_ledger_not_the_text():
    coord = EmployeeCoordinator(_Pipeline(), synthesis_engine=object())
    started = time.time()

    assert coord._outage_during(started) is False

    get_call_ledger().record("backend.unavailable", {}, ok=False, role="X")
    assert coord._outage_during(started) is True

    # Scoped: an outage recorded BEFORE the window does not count. This is
    # the same class of bug as the unscoped compute gate.
    assert coord._outage_during(time.time() + 60) is False
