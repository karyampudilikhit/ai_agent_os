"""E2: nodes get owners, and code decides everything except HOW.

Driven with a stub executor, so the whole orchestration is exercised
without a model call. That is the point of a replay-shaped test: the
decisions under test are deterministic, and paying for inference to
observe them would make them look variable when they are not.

THE INVARIANTS:
  code picks the node          never a model
  code picks the owner         from the capability, via the existing layer
  code judges the outcome      from the ledger, never from the transcript
  code decides what is next    a rejected node releases nothing
  the model picks HOW          and nothing here constrains that
"""
from __future__ import annotations

import pytest

from backend.app.orchestrator import graph_runner
from backend.app.orchestrator.goal_spec import from_task
from backend.app.orchestrator.task_graph import build_graph
from backend.app.orchestrator.task_node import (
    BLOCKED, REJECTED, SKIPPED, VERIFIED,
)

INTERNSHIPS = ("Find 20 Product Manager internships in India. Verify each "
               "one from its own job page.")
UNSHAPED = "Research the Indian EV market and write it up."


class StubExecutor:
    """Stands in for AgenticExecutor. Records how it was configured."""

    made = []

    def __init__(self, adapter, **kw):
        self.kw = kw
        self.tasks = []
        StubExecutor.made.append(self)

    def run(self, task, role="Specialist", original_task=None):
        self.tasks.append((task, role))
        return f"[{role} did some work]"


def factory(adapter, **kw):
    return StubExecutor(adapter, **kw)


@pytest.fixture(autouse=True)
def _fresh():
    StubExecutor.made = []
    yield


def run_with(ledger, task=INTERNSHIPS, budget=22):
    return graph_runner.run(task, adapter=object(), budget=budget,
                            executor_factory=factory,
                            ledger_calls=lambda: ledger)


# ------------------------------------------ silence for unshaped goals

def test_a_goal_with_no_shape_gets_no_graph_run():
    """The caller must then run exactly the single loop it always ran.
    A wrong graph is worse than no graph."""
    out = run_with([], task=UNSHAPED)
    assert not out.runs
    assert not StubExecutor.made, "nothing should have been executed"


# ------------------------------------------------ code picks the node

def test_the_nodes_run_in_dependency_order():
    out = run_with([])
    assert [r.node_id for r in out.runs][:1] == ["discover"]


def test_nothing_downstream_runs_when_a_node_is_rejected():
    """An empty ledger means discovery found nothing, so it is REJECTED
    and everything beneath it is unreachable. The report node must not
    run — a report about work that did not happen is the failure this
    whole architecture exists to prevent."""
    out = run_with([])
    assert out.graph.nodes["discover"].status == REJECTED
    assert out.graph.nodes["verify_items"].status == BLOCKED
    assert out.graph.nodes["assemble"].status == BLOCKED
    assert [r.node_id for r in out.runs] == ["discover"]


def test_a_verified_node_releases_the_next_one(records_ledger):
    out = run_with(records_ledger)
    assert out.graph.nodes["discover"].status == VERIFIED
    assert [r.node_id for r in out.runs][:2] == ["discover", "verify_items"]


# ---------------------------------------------- code picks the owner

def test_each_node_is_owned_by_a_different_employee(records_ledger):
    out = run_with(records_ledger)
    by_node = {r.node_id: r for r in out.runs}
    assert by_node["discover"].capability == "web research"
    assert by_node["verify_items"].capability == "data extraction"
    roles = {r.employee_role for r in out.runs}
    assert len(roles) > 1, f"every node ran as the same worker: {roles}"


def test_the_owner_brings_its_own_tool_scope(records_ledger):
    """A capability's hire carries a tool allow-list, and the node's
    executor is built with it. The discovery worker and the extraction
    worker are genuinely different workers, not one prompt in two hats."""
    run_with(records_ledger)
    scopes = [tuple(e.kw.get("tools_allow") or ()) for e in StubExecutor.made]
    assert any(s for s in scopes), f"no node was tool-scoped: {scopes}"


# ------------------------------------------- code judges the outcome

def test_the_verdict_comes_from_the_ledger_not_the_transcript(records_ledger):
    """The stub returns a cheerful string for every node. Only the
    ledger decides, so discovery passes and verification does not."""
    out = run_with(records_ledger)
    assert all("did some work" in r.transcript for r in out.runs)
    assert out.graph.nodes["discover"].status == VERIFIED
    assert out.graph.nodes["verify_items"].status == REJECTED


def test_a_node_that_raises_is_failed_and_blocks_the_rest():
    class Exploding(StubExecutor):
        def run(self, task, role="Specialist", original_task=None):
            raise RuntimeError("tool exploded")

    out = graph_runner.run(INTERNSHIPS, adapter=object(), budget=22,
                           executor_factory=lambda a, **k: Exploding(a, **k),
                           ledger_calls=lambda: [])
    assert out.runs[0].status == "FAILED"
    assert out.graph.nodes["assemble"].status == BLOCKED


# ---------------------------------------------- code owns the budget

def test_a_node_gets_its_own_slice_of_the_budget(records_ledger):
    run_with(records_ledger)
    steps = [e.kw["max_steps"] for e in StubExecutor.made]
    assert all(s >= graph_runner.MIN_NODE_STEPS for s in steps)
    assert sum(steps) <= 22 + len(steps) * graph_runner.NODE_BUDGET_SLACK


def test_a_run_that_runs_out_skips_rather_than_overspends(records_ledger):
    out = graph_runner.run(INTERNSHIPS, adapter=object(), budget=6,
                           executor_factory=factory,
                           ledger_calls=lambda: records_ledger)
    assert "budget exhausted" in out.stopped
    assert any(n.status == SKIPPED for n in out.graph.nodes.values())


# --------------------------------------------- the model still owns HOW

def test_no_brief_names_a_tool_a_site_or_a_query():
    """Code says WHAT the node is. Everything about how to do it stays
    with the model — a brief that named a site would make this a
    workflow engine."""
    g, _ = build_graph(INTERNSHIPS, 22)
    for node in g.nodes.values():
        brief = graph_runner.brief_for(node, INTERNSHIPS).lower()
        for banned in ("indeed", "internshala", "linkedin", "google",
                       "browser_navigate", "web_search", "http"):
            assert banned not in brief, f"{node.id} brief names {banned}"


def test_each_node_is_told_only_its_own_job():
    g, _ = build_graph(INTERNSHIPS, 22)
    disc = graph_runner.brief_for(g.nodes["discover"], INTERNSHIPS)
    item = graph_runner.brief_for(g.nodes["verify_items"], INTERNSHIPS)
    assert "do NOT open the individual items" in disc.lower().replace(
        "do not open the individual items", "do NOT open the individual items")
    assert "not gather more lists" in item.lower()
    assert "8" in item, "the node must carry the number it is judged on"


@pytest.fixture
def records_ledger():
    """A ledger where discovery genuinely produced candidates."""
    from backend.app.browser.policy import wrap_untrusted
    listing = "https://board.test/search?q=pm+intern"
    body = [f"[records — {listing}]",
            "10 similar item(s) on the page, showing 10.", ""]
    for i in range(1, 11):
        body += [f"--- record {i} ---",
                 f"title: Product Manager Intern {i} at Firm{i}",
                 f"link: https://board.test/job/detail/pm-{i}-99{i}0011", ""]
    return [
        {"tool": "action.browser_navigate", "ok": True, "at": 1.0,
         "args_text": f'{{"url": "{listing}"}}',
         "output": f"URL: {listing}\nTITLE: Jobs"},
        {"tool": "action.browser_extract_records", "ok": True, "at": 2.0,
         "output": wrap_untrusted("\n".join(body), listing)},
    ]


# ------------------------------------------------ wired into the app
#
# item_state passed nineteen unit tests and never fired once across four
# live runs. A layer nothing calls does not exist, so the wiring is
# asserted as hard as the behaviour.

def test_the_employee_path_runs_the_graph():
    import inspect

    from backend.app.employees.dynamic_employee import DynamicEmployee
    src = inspect.getsource(DynamicEmployee.run_task)
    assert "graph_runner.run(" in src, "the app path never runs the graph"
    assert "ledger_calls=_run_ledger_calls" in src


def test_the_single_loop_still_runs_when_there_is_no_graph():
    """Most real tasks have no countable shape. They must keep behaving
    exactly as they did — the fallback is the safety property, not a
    nicety."""
    import inspect

    from backend.app.employees.dynamic_employee import DynamicEmployee
    src = inspect.getsource(DynamicEmployee.run_task)
    assert "and not graph_context:" in src
    head, _, tail = src.partition("and not graph_context:")
    assert "AgenticExecutor(" in tail, "the fallback loop must still exist"


def test_the_graph_reads_only_this_runs_ledger():
    """The ledger is process-global and never reset. An unscoped read
    would let a previous run's evidence satisfy this run's nodes —
    a bug this codebase has already shipped once."""
    import inspect

    from backend.app.employees.dynamic_employee import DynamicEmployee
    src = inspect.getsource(DynamicEmployee.run_task)
    assert "calls(since=_graph_started)" in src


def test_a_node_is_never_re_planned_as_a_fresh_goal():
    """FOUND LIVE, the first time a node ran. The executor re-derived a
    GoalSpec from the node's BRIEF, and the briefs describe per-item work
    ("each candidate", "the individual items"), so a discovery brief read
    as "1 item, visited individually", was priced at seven steps against
    the node's five, REFUSED, and the model was told not to begin. It
    returned DONE at step one having done nothing.

    The orchestration was correct throughout. It was refusing itself.
    """
    made = []

    class Recorder(StubExecutor):
        def __init__(self, adapter, **kw):
            super().__init__(adapter, **kw)
            made.append(kw)

    graph_runner.run(INTERNSHIPS, adapter=object(), budget=22,
                     executor_factory=lambda a, **k: Recorder(a, **k),
                     ledger_calls=lambda: [])
    assert made, "no node was executed"
    assert all(kw.get("subtask") is True for kw in made), (
        "a node executed as a top-level goal will re-plan and refuse itself")


def test_subtask_mode_skips_planning_but_a_whole_goal_still_plans():
    import inspect

    from backend.app.orchestrator.execution_loop import AgenticExecutor
    src = inspect.getsource(AgenticExecutor.run)
    assert 'goal_spec_from_task("" if self.subtask else task)' in src
    assert 'build_plan("" if self.subtask else task' in src


def test_a_delegated_subtask_does_not_spawn_its_own_graph():
    """FOUND LIVE. A Supervisor-delegated sub-task is already one slice
    of a decomposition; building a graph from it decomposes a
    decomposition. On a real run both specialists parsed their own
    sub-task as a countable goal and each spawned its own three-node
    graph, staffed with the same two employees — the work was done four
    times by two nested layers of workers and reported by neither."""
    import inspect

    from backend.app.employees.dynamic_employee import DynamicEmployee
    src = inspect.getsource(DynamicEmployee.run_task)
    assert "_delegated" in src
    assert "None if _delegated else graph_runner.run(" in src


def test_the_ceiling_retry_is_bounded_but_not_removed():
    """The adapter briefly refused to retry once at the ceiling, on the
    reasoning that an identical request cannot give a different answer.
    That is wrong for this failure — empty-at-length is stochastic — and
    because _remember_floor is process-global, the WRITING stage started
    at the ceiling and raised instead of retrying. Both specialists on a
    live run produced no output at all."""
    import inspect

    from backend.app.models.provider_adapters import openai_adapter
    src = inspect.getsource(openai_adapter)
    assert "at_ceiling = wider <= attempt_tokens" in src
    assert "attempt < (1 if at_ceiling else 2)" in src
