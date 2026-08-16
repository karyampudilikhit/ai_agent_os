"""What an employee's configuration does to its actual prompt.

The load-bearing test here is the FIRST one: with no config, the
assembled prompt must be byte-identical to what shipped before this
feature existed. Everything else in this file is a deliberate deviation
from that baseline, and each one has to be asked for.
"""
from __future__ import annotations

from backend.app.employees.dynamic_employee import DynamicEmployee
from backend.app.employees.employee_config import resolve

TASK = "backtest SPY and report CAGR and Sharpe"

# The prompt exactly as build_objective produced it before per-employee
# config existed. Any diff against this is a behaviour change for every
# employee currently on disk.
BASELINE = """You are the Quant Analyst on this project team.

Your mandate — what only you are responsible for:
compute things

Task the whole team is working on:
backtest SPY and report CAGR and Sharpe

Do the part of this task that belongs to your role, and only your part.
If a piece of the task belongs to a different role on this team, say so
briefly and skip it — do not do work outside your mandate.

Do not fabricate specific statistics, survey results, or claims that
work has already been completed. If you don't know a real number,
describe things qualitatively (unless the web results below give you a
real one)."""

ANTI_FABRICATION = "Do not fabricate specific statistics"


class _Memory:
    """No history, no cross-run recall — isolates the persona block."""

    def relevant_context(self, task):
        return ""

    def record(self, task, result):
        pass


def _employee(config=None):
    emp = DynamicEmployee(
        employee_id="emp_test",
        role="Quant Analyst",
        mandate="compute things",
        pipeline=object(),
        memory_store=_Memory(),
        config=resolve({"config": config} if config else {}),
    )
    return emp


def _prompt(config=None, task=TASK):
    return _employee(config).build_objective(task)


# ------------------------------------------------- the no-regression test

def test_an_unconfigured_employee_gets_the_exact_old_prompt():
    """Character-for-character. ~40 employees on disk have no config;
    if this drifts, all of them change behaviour silently."""
    assert _prompt() == BASELINE


# ------------------------------------------------------ collaboration

def test_solo_replaces_the_lane_instruction():
    out = _prompt({"collaboration": "solo"})
    assert "only your part" not in out
    assert "You are working alone on this task" in out
    assert ANTI_FABRICATION in out, "posture must not touch the anti-fabrication line"


def test_required_outputs_auto_promote_the_lane_to_flexible():
    """The Data Engineer failure, at the prompt level: an employee that
    owes a computed number must not also be told to skip work outside
    its lane."""
    out = _prompt({"required_outputs": ["executed_code"]})

    assert "never refuse work your own required outputs demand" in out
    assert "and only your part" not in out


# ------------------------------------------------------- domain rules

def test_domain_rules_render_after_the_mandate():
    out = _prompt({"domain_rules": ["always adjusted close", "T+1 execution"]})

    assert "STANDING RULES FOR YOUR WORK" in out
    assert "1. always adjusted close" in out
    assert "2. T+1 execution" in out
    # Before the task, so they frame the work rather than trail it.
    assert out.index("always adjusted close") < out.index("Task the whole team")


def test_no_rules_block_when_there_are_no_rules():
    assert "STANDING RULES" not in _prompt()


# ---------------------------------------------------- prompt override

def test_override_replaces_the_persona_including_anti_fabrication():
    """Decision: full override, NOTHING protected. This test pins that
    so a future contributor does not quietly reinstate the guard rail
    and break a founder's deliberate configuration.

    Note what is unaffected: the compute gate, number-provenance check,
    source ledger and hand-back detector all read the ledger rather than
    the prompt, so removing this text does not disable them.
    """
    out = _prompt({"prompt_override": "You are {role}. Do this: {task}"})

    assert out.startswith("You are Quant Analyst. Do this: backtest SPY")
    assert ANTI_FABRICATION not in out
    assert "only your part" not in out


def test_override_still_receives_the_task_when_it_forgets_the_placeholder():
    """Structural guard, not a content guard: an override with no {task}
    would hand the employee no task at all."""
    out = _prompt({"prompt_override": "You are a machine. Say nothing."})

    assert "You are a machine" in out
    assert TASK in out


def test_override_renders_mandate_and_rules_placeholders():
    out = _prompt({
        "prompt_override": "ROLE={role} MANDATE={mandate} RULES=[{domain_rules}] TASK={task}",
        "domain_rules": ["adjusted close"],
    })

    assert "ROLE=Quant Analyst" in out
    assert "MANDATE=compute things" in out
    assert "RULES=[1. adjusted close]" in out


def test_an_unknown_placeholder_does_not_explode_mid_run():
    """A founder's override is free text and may contain {ticker} or a
    stray brace from pasted JSON. Raising KeyError here would fail the
    task at execution time for an edit that looked fine when saved."""
    out = _prompt({"prompt_override": "Trade {ticker} now. {task}"})

    assert "{ticker}" in out
    assert TASK in out


# ------------------------------------------------- context still flows

def test_runtime_context_is_appended_even_under_a_full_override():
    """Fetched pages and teammates' work are DATA, not persona. An
    employee that cannot see them is broken, not customized."""
    emp = _employee({"prompt_override": "Just do it: {task}"})
    out = emp.build_objective(
        TASK,
        teammates_context="the Data Engineer fetched SPY_10y.csv",
        web_context="a real page",
    )

    assert "SPY_10y.csv" in out
    assert "a real page" in out


def test_supervisor_brief_survives_a_full_override():
    emp = _employee({"prompt_override": "Just do it: {task}"})
    out = emp.build_objective(TASK, task_brief="use adjusted close")

    assert "BRIEF FROM YOUR SUPERVISOR" in out
    assert out.index("BRIEF FROM YOUR SUPERVISOR") < out.index("Just do it")
