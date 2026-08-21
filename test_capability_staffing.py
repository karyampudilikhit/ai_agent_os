"""Hire for the gap, not for the goal.

THE GAP THIS FILLS. Employees could always be created — design_team()
reads a prompt and proposes a roster. What could not happen was the
thing V1 needs: read one goal, work out that it needs web research and
extraction and report writing, check who the founder ALREADY employs,
and hire only for what is missing. Every Unit designed a team from
scratch, so the same capability was re-hired under a new name each time,
each new hire starting with an empty memory.

Also covered here: an employee's tool scope. `tools: {allow, deny}` has
been in the config schema, accepted by validate() and shown back in the
UI since per-employee config shipped — and resolve() dropped it, so the
field took a value, saved it, and changed nothing.
"""
from __future__ import annotations

import pytest

from backend.app.employees.capability import (
    ANALYSIS, CATALOGUE, COMPUTATION, DATA_EXTRACTION, REPORT_WRITING,
    WEB_RESEARCH, covers, find_cover, required_for, staff,
)
from backend.app.orchestrator.goal_spec import from_task

INTERNSHIPS = ("Find 10 Product Manager internships in India posted within "
               "the last 7 days, visit each job page, extract company, role, "
               "location, posting date and application URL.")
COMPETITORS = "Research competitors in this market and create a report."
PRICING = ("Analyze these companies, collect their pricing, compare them, "
           "and produce a recommendation.")


class FakeRegistry:
    """The real registry's contract, without touching the disk."""

    def __init__(self, records=()):
        self._records = {r["id"]: dict(r) for r in records}
        self._n = 0

    def list(self):
        return [dict(r) for r in self._records.values()]

    def create(self, role, mandate, tags=None, config=None, **kw):
        self._n += 1
        rec = {"id": f"emp_test{self._n}", "role": role, "mandate": mandate,
               "tags": list(tags or []), "is_supervisor": False,
               "config": config or {}}
        self._records[rec["id"]] = rec
        return dict(rec)


def emp(eid, role, mandate, **kw):
    return {"id": eid, "role": role, "mandate": mandate,
            "tags": kw.get("tags", []),
            "is_supervisor": kw.get("is_supervisor", False)}


# ------------------------------------------- what the goal needs doing

def test_something_finds_the_answer_and_something_writes_it():
    """The floor. Every real task needs at least these two."""
    caps = required_for("Tell me about the Indian EV market.", from_task("x"))
    assert WEB_RESEARCH in caps and REPORT_WRITING in caps


def test_a_per_item_goal_needs_extraction_whatever_words_it_used():
    """Read from the SHAPE of the goal, not from keywords — that is the
    part goal_spec already established structurally."""
    caps = required_for(INTERNSHIPS, from_task(INTERNSHIPS))
    assert DATA_EXTRACTION in caps


def test_a_comparison_goal_needs_analysis():
    caps = required_for(PRICING, from_task(PRICING))
    assert ANALYSIS in caps and DATA_EXTRACTION in caps


def test_a_metrics_goal_needs_someone_who_runs_code():
    task = "Backtest this strategy on SPY and give me the Sharpe and CAGR."
    assert COMPUTATION in required_for(task, from_task(task))


def test_an_ordinary_research_task_does_not_hire_a_quant():
    assert COMPUTATION not in required_for(COMPETITORS, from_task(COMPETITORS))


def test_writing_comes_last_however_it_was_decided():
    caps = required_for(PRICING, from_task(PRICING))
    assert caps[-1] is REPORT_WRITING


# ------------------------------------------------- who already does it

def test_an_existing_specialist_covers_its_capability():
    assert covers(emp("e1", "Market Research Analyst",
                      "Research competitors and market landscape."),
                  WEB_RESEARCH)


def test_an_unrelated_employee_does_not():
    assert not covers(emp("e1", "Payroll Clerk",
                          "Process monthly salary runs and payslips."),
                      DATA_EXTRACTION)


def test_the_closest_match_wins_not_merely_the_first():
    pool = [emp("e1", "Generalist", "Handle research and analysis and writing "
                                    "and comparison and evaluation of data"),
            emp("e2", "Competitive Pricing Analyst",
                "Compare pricing, benchmark competitors, assess and evaluate "
                "positioning, produce comparison insight")]
    assert find_cover(ANALYSIS, pool)["id"] == "e2"


def test_a_supervisor_is_never_the_capability():
    """Supervisors plan and synthesize. Assigning the work to one is how
    a Unit ends up with a delegation plan and no data."""
    pool = [emp("s1", "Research Supervisor",
                "Plan and coordinate all research and analysis work",
                is_supervisor=True)]
    assert find_cover(WEB_RESEARCH, pool) is None


# ------------------------------------------------------- the roster

def test_a_gap_is_hired_for():
    reg = FakeRegistry()
    roster = staff(COMPETITORS, from_task(COMPETITORS), registry=reg,
                   existing=[])
    assert roster.hired, "an empty registry must produce hires"
    assert all(a.employee.get("id") for a in roster.assignments)


def test_someone_already_employed_is_reused_not_re_hired():
    existing = [emp("e1", "Market Research Analyst",
                    "Research competitors, find and search sources, discover "
                    "market intelligence")]
    reg = FakeRegistry(existing)
    roster = staff(COMPETITORS, from_task(COMPETITORS), registry=reg,
                   existing=existing)
    web = next(a for a in roster.assignments if a.capability is WEB_RESEARCH)
    assert not web.hired and web.employee["id"] == "e1"


def test_a_second_run_of_the_same_goal_hires_nobody():
    """The failure this module exists for: the same capability re-hired
    under a new name every time, each with an empty memory."""
    reg = FakeRegistry()
    first = staff(COMPETITORS, from_task(COMPETITORS), registry=reg)
    assert first.hired
    second = staff(COMPETITORS, from_task(COMPETITORS), registry=reg)
    assert second.hired == [], second.describe()


def test_a_dry_run_hires_nobody():
    reg = FakeRegistry()
    roster = staff(COMPETITORS, from_task(COMPETITORS), registry=reg,
                   existing=[], hire=False)
    assert roster.hired == [] and reg.list() == []


def test_a_hire_gets_an_id_a_mandate_and_a_tool_scope():
    reg = FakeRegistry()
    staff(INTERNSHIPS, from_task(INTERNSHIPS), registry=reg, existing=[])
    made = {r["role"]: r for r in reg.list()}
    extractor = made.get(DATA_EXTRACTION.role)
    assert extractor and extractor["mandate"]
    assert extractor["tags"] == [DATA_EXTRACTION.name]
    assert "browser" in extractor["config"]["tools"]["allow"]


def test_one_person_may_cover_several_capabilities_once():
    existing = [emp("e1", "Research and Reporting Analyst",
                    "Research, search and discover sources; analyse and "
                    "compare findings; write and draft the report document")]
    roster = staff(PRICING, from_task(PRICING), registry=FakeRegistry(existing),
                   existing=existing, hire=False)
    ids = [e["id"] for e in roster.employees]
    assert ids.count("e1") == 1


def test_a_broken_registry_does_not_break_the_goal():
    class Broken:
        def list(self): raise RuntimeError("disk gone")
        def create(self, **kw): raise RuntimeError("disk gone")
    roster = staff(COMPETITORS, from_task(COMPETITORS), registry=Broken())
    assert roster.hired == []      # nothing invented, nothing raised


# --------------------------------------------- an employee's tool scope

def test_tool_scope_now_survives_resolve():
    from backend.app.employees.employee_config import resolve
    cfg = resolve({"config": {"tools": {"allow": ["browser"],
                                        "deny": ["send_email"]}}})
    assert cfg.tools_allow == ("browser",)
    assert cfg.tools_deny == ("send_email",)


def test_an_unrestricted_employee_still_sees_everything():
    from backend.app.employees.employee_config import resolve
    cfg = resolve({})
    assert cfg.tools_allow == () and cfg.tools_deny == ()


ALL_TOOLS = [{"name": "action.browser_navigate"}, {"name": "action.browser_extract"},
             {"name": "action.send_email"}, {"name": "action.run_python"}]


def _scoped(allow=(), deny=()):
    from backend.app.orchestrator.execution_loop import AgenticExecutor
    ex = AgenticExecutor(object(), tools_allow=allow, tools_deny=deny)
    return [t["name"] for t in ex._scoped(ALL_TOOLS)]


def test_no_scope_is_every_tool():
    assert _scoped() == [t["name"] for t in ALL_TOOLS]


def test_an_allow_list_narrows_to_it():
    assert _scoped(allow=("browser",)) == ["action.browser_navigate",
                                           "action.browser_extract"]


def test_a_deny_list_removes_one():
    assert "action.send_email" not in _scoped(deny=("send_email",))
    assert "action.run_python" in _scoped(deny=("send_email",))


def test_deny_beats_allow():
    """A founder who allowed the browser and denied one browser tool
    means both things, and the narrower one wins."""
    kept = _scoped(allow=("browser",), deny=("browser_extract",))
    assert kept == ["action.browser_navigate"]


def test_an_empty_unit_is_staffed_before_it_runs():
    """A capability layer nothing calls is the bug the audit found in
    item_state, repeated. It has to be reachable from the run path.

    Deliberately only when the Unit is EMPTY: a founder who staffed
    their own team meant that team.
    """
    import inspect
    from backend.app.api import routes
    src = inspect.getsource(routes.run_task_on_team)
    assert "_staff(" in src
    head, _, tail = src.partition("if not specialist_specs:")
    assert tail, "staffing must be conditional on the Unit being empty"
    assert "_staff(" in tail[:900]
    assert "set_members" in tail[:900]


def test_the_employee_hands_its_scope_to_the_loop():
    """A config nothing reads is the bug this replaced."""
    import inspect
    from backend.app.employees import dynamic_employee
    src = inspect.getsource(dynamic_employee.DynamicEmployee.run_task)
    assert "tools_allow=self.config.tools_allow" in src
    assert "tools_deny=self.config.tools_deny" in src
