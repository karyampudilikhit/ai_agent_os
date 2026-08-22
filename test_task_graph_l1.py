"""L1: the work as addressable units, and code deciding what is next.

WHAT THIS LAYER IS FOR. Every orchestration capability built this cycle
operated on one undifferentiated blob of work. There was no object for
"the discovery step", so nothing could say which step was running, who
owned it, what would count as it being done, or what should happen if it
failed -- and "what should I do next" was a question the Supervisor
asked a model.

THE INVARIANT THESE TESTS EXIST TO PROTECT: no path from a model's
output to a node's status. A run cannot talk its way into COMPLETED, and
`ready()` is arithmetic over the graph.
"""
from __future__ import annotations

import json
import os

import pytest

from backend.app.orchestrator.task_graph import build_graph, judge_node
from backend.app.orchestrator.task_node import (
    BLOCKED, COMPLETED, FAILED, PENDING, READY, REJECTED, RUNNING, SKIPPED,
    VERIFIED, Graph, TaskGraphError, TaskNode,
)

INTERNSHIPS = ("Find 20 Product Manager internships in India. Verify each "
               "one from its own job page.")
STARTUPS = ("Research the top 10 AI startups in India and create a "
            "comparison containing company, funding, product, and website.")
UNSHAPED = "Research the Indian EV market and write it up."


def chain():
    g = Graph()
    g.add(TaskNode(id="a", kind="discover", verification="candidates"))
    g.add(TaskNode(id="b", kind="per_item", dependencies=("a",),
                   verification="items:3"))
    g.add(TaskNode(id="c", kind="assemble", dependencies=("b",)))
    return g


# ------------------------------------------------- the graph is real

def test_a_counted_goal_becomes_a_validated_graph():
    g, plan = build_graph(INTERNSHIPS, 22)
    assert set(g.nodes) == {"discover", "verify_items", "assemble"}
    assert g.nodes["verify_items"].dependencies == ("discover",)
    assert g.nodes["assemble"].dependencies == ("verify_items",)
    assert g.validate()          # raises on cycle / dangling / depth


def test_each_node_names_a_capability_not_an_employee():
    """Staffing already knows how to turn a capability into a reused or
    newly hired employee. A node naming a person would duplicate that
    and go stale the moment the roster changed."""
    g, _ = build_graph(INTERNSHIPS, 22)
    assert g.nodes["discover"].capability == "web research"
    assert g.nodes["verify_items"].capability == "data extraction"
    assert g.nodes["assemble"].capability == "report writing"


def test_the_finish_line_carries_the_descoped_number():
    """Eight is what the budget bought; twenty is what was asked for.
    The node has to hold the one it can be judged against."""
    g, plan = build_graph(INTERNSHIPS, 22)
    assert plan.feasible_items == 8
    assert g.nodes["verify_items"].verification == "items:8"


def test_a_counted_goal_without_per_item_work_reads_content_instead():
    g, _ = build_graph(STARTUPS, 22)
    assert "read_content" in g.nodes
    assert "verify_items" not in g.nodes
    assert g.nodes["read_content"].verification.startswith("content:")


def test_an_unshaped_goal_gets_no_graph_at_all():
    """Silence is the default here as everywhere else. No graph means
    nothing below can refuse, reorder or block anything."""
    g, _ = build_graph(UNSHAPED, 22)
    assert not g.nodes


# --------------------------------- code decides what is next, not a model

def test_only_the_root_is_ready_at_the_start():
    g = chain()
    assert [n.id for n in g.ready()] == ["a"]
    assert g.next_node().id == "a"


def test_completed_is_not_enough_to_unblock_downstream():
    """The distinction the whole system rests on. COMPLETED means the
    work ran; VERIFIED means the evidence held. A node that ran and
    produced nothing usable must not release what depends on it."""
    g = chain()
    g.start("a")
    g.finish("a", ok=True)
    assert g.nodes["a"].status == COMPLETED
    assert [n.id for n in g.ready()] == [], "COMPLETED must not unblock"
    g.judge("a", satisfied=True)
    assert [n.id for n in g.ready()] == ["b"]


def test_a_rejected_node_never_unblocks_downstream():
    g = chain()
    g.start("a")
    g.finish("a", ok=True)
    g.judge("a", satisfied=False, why="nothing found")
    assert g.nodes["a"].status == REJECTED
    assert g.ready() == []


def test_a_dead_node_blocks_everything_beneath_it():
    g = chain()
    g.start("a")
    g.finish("a", ok=False, why="tool broke")
    changed = g.propagate()
    assert {n.id for n in changed} == {"b", "c"}
    assert g.nodes["b"].status == BLOCKED and g.nodes["c"].status == BLOCKED
    assert g.done


def test_the_cheapest_ready_node_goes_first():
    """A budget that runs out mid-graph should have bought as many
    finished nodes as it could."""
    g = Graph()
    g.add(TaskNode(id="big", kind="discover", est_calls=20))
    g.add(TaskNode(id="small", kind="discover", est_calls=2))
    assert g.next_node().id == "small"


def test_a_node_cannot_start_with_unsatisfied_dependencies():
    g = chain()
    with pytest.raises(TaskGraphError):
        g.start("b")


def test_a_node_cannot_be_judged_before_it_has_run():
    g = chain()
    with pytest.raises(TaskGraphError):
        g.judge("a", satisfied=True)


def test_statuses_are_code_owned():
    """No path from model output to a status. Asserted structurally
    because it is the invariant, not a nicety."""
    import inspect

    from backend.app.orchestrator import task_node
    src = inspect.getsource(task_node)
    for forbidden in ("adapter", "chat_completion", "prompt", "llm"):
        assert forbidden not in src.lower().replace("llm decides", ""), forbidden


# --------------------------------------------- the validator, exercised

def test_a_cycle_is_refused():
    g = Graph()
    g.add(TaskNode(id="x", kind="discover", dependencies=("y",)))
    g.add(TaskNode(id="y", kind="discover", dependencies=("x",)))
    with pytest.raises(TaskGraphError):
        g.validate()


def test_a_dangling_dependency_is_refused():
    g = Graph()
    g.add(TaskNode(id="x", kind="discover", dependencies=("nope",)))
    with pytest.raises(TaskGraphError):
        g.validate()


def test_a_duplicate_id_is_refused():
    g = Graph()
    g.add(TaskNode(id="x", kind="discover"))
    with pytest.raises(TaskGraphError):
        g.add(TaskNode(id="x", kind="assemble"))


def test_independent_nodes_share_a_layer():
    """The layering dependency_validator has had since Phase 4 and has
    never had a real graph to run on."""
    g = Graph()
    g.add(TaskNode(id="a", kind="discover"))
    g.add(TaskNode(id="b", kind="discover"))
    g.add(TaskNode(id="c", kind="assemble", dependencies=("a", "b")))
    layers = g.validate()
    assert {n.id for n in layers[0]} == {"a", "b"}
    assert [n.id for n in layers[1]] == ["c"]


# ------------------------------- judged against a run that really happened

FIXTURE = os.path.join(os.path.dirname(__file__), "test_fixtures",
                       "run7_indeed_trace.json")


@pytest.fixture(scope="module")
def run7():
    if not os.path.exists(FIXTURE):
        pytest.skip("run-7 fixture not present")
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def test_the_real_run_verifies_discovery_and_rejects_verification(run7):
    """Run 7 in two words: it found things and checked none of them.

    A graph makes that sayable for the first time. Before this, the run
    was one undifferentiated failure; now discovery is VERIFIED on its
    own evidence and the node that mattered is REJECTED on its own.
    """
    from backend.app.orchestrator.goal_spec import from_task
    g, plan = build_graph(run7["task"], 22)
    spec = from_task(run7["task"])

    ok, why = judge_node(g.nodes["discover"], run7["task"], run7["calls"], spec)
    assert ok, why
    assert "candidate(s) found" in why

    ok2, why2 = judge_node(g.nodes["verify_items"], run7["task"],
                           run7["calls"], spec)
    assert not ok2, why2
    assert why2.startswith("0 verified of 8")


def test_that_run_leaves_the_reporting_node_unreachable(run7):
    from backend.app.orchestrator.goal_spec import from_task
    g, _ = build_graph(run7["task"], 22)
    spec = from_task(run7["task"])

    g.start("discover")
    g.finish("discover", ok=True)
    g.judge("discover", judge_node(g.nodes["discover"], run7["task"],
                                   run7["calls"], spec)[0])
    g.start("verify_items")
    g.finish("verify_items", ok=True)
    g.judge("verify_items", judge_node(g.nodes["verify_items"], run7["task"],
                                       run7["calls"], spec)[0])
    g.propagate()

    assert g.nodes["discover"].status == VERIFIED
    assert g.nodes["verify_items"].status == REJECTED
    assert g.nodes["assemble"].status == BLOCKED, (
        "a report must not be reachable when the work it reports on failed")
    assert g.done
