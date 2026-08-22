"""One node at a time, each with an owner, each judged before the next.

WHAT THIS REPLACES. Until now a whole goal was one loop. The model was
handed the entire task, the full tool surface and one budget, and asked
to get on with it; "what should I do next" was a question it answered
for itself, fifteen steps deep, from prose. Every orchestration layer
built this cycle -- phases, source health, item state -- was an attempt
to constrain that single undifferentiated run from the outside.

Here the shape is inverted. Code picks the node, code picks who owns it,
code judges whether it counted, and code decides what becomes reachable
next. The model is asked one question at a time: HOW do I do THIS.

WHAT THE MODEL STILL OWNS, entirely: which tool, which site, which
query, how to word a search, what to write. Those are the things it is
genuinely good at and there is no code here that touches them.

WHAT IT NEVER TOUCHES: which node runs, who runs it, how much budget it
gets, whether its output counted, and whether the goal is finished.

ONE EMPLOYEE PER NODE, AND WHY IT MATTERS. A node names a CAPABILITY;
staffing turns that into a real employee -- reused when one already
covers it, hired when none does. That employee brings its own model, its
own tool scope and its own memory. So the run that gathers candidates
and the run that verifies them are genuinely different workers with
different permissions, rather than one prompt asked to change hats.

SILENT UNLESS THE GOAL HAS A SHAPE. build_graph returns nothing for a
goal with no countable structure, and then this module is not used at
all: the caller runs exactly the single loop it always ran. A wrong
graph would be worse than no graph -- it would confidently execute the
wrong plan -- so an unrecognised goal keeps today's behaviour.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Headroom over a node's own estimate. A node priced at two calls that
# gets exactly two has no room to recover from one bad page, and the
# estimates are deliberately coarse. Bounded, so a node cannot eat the
# whole run.
NODE_BUDGET_SLACK = 2
MIN_NODE_STEPS = 3


@dataclass
class NodeRun:
    node_id: str
    capability: str
    employee_role: str
    employee_id: str
    hired: bool
    status: str = ""
    why: str = ""
    steps: int = 0
    seconds: float = 0.0
    transcript: str = ""

    def line(self) -> str:
        return (f"{self.node_id:14} {self.status:9} {self.employee_role:26} "
                f"{self.steps:2} step(s) {self.seconds:5.0f}s  {self.why}")


@dataclass
class GraphRun:
    graph: Any = None
    plan: Any = None
    runs: List[NodeRun] = field(default_factory=list)
    stopped: str = ""

    @property
    def transcript(self) -> str:
        parts = []
        for r in self.runs:
            if r.transcript:
                parts.append(f"=== {r.node_id} ({r.employee_role}) ===\n"
                             f"{r.transcript}")
        return "\n\n".join(parts)

    def describe(self) -> str:
        lines = [f"GRAPH RUN: {len(self.runs)} node(s) executed"]
        for r in self.runs:
            lines.append("  " + r.line())
        if self.stopped:
            lines.append(f"  stopped: {self.stopped}")
        return "\n".join(lines)


# --------------------------------------------------------- the briefs
#
# What a node's owner is told. Node-specific because the whole point is
# that a node is a NARROWER job than the goal -- a discovery worker that
# starts opening items is doing the next node's work with this node's
# budget, which is exactly what the undifferentiated loop did.
#
# These say what the node IS, never how to do it. No site, no tool, no
# query appears in any of them.

_BRIEFS = {
    "discover": (
        "YOUR JOB IS TO FIND CANDIDATES AND NOTHING ELSE.\n"
        "Locate a source that lists what the goal asks for and extract the "
        "list, so each candidate has a link. Do NOT open the individual "
        "items -- a later step does that, with its own budget. If one "
        "source will not produce a usable list, go to a different one.\n"
        # STATED IN TERMS THE WORKER CAN ACT ON.
        #
        # This used to end "you are done when you have a list of
        # candidates with links". A live node read its own SEARCH
        # RESULTS as satisfying that, stopped after one call, and was
        # judged to have found nothing -- correctly, because a search
        # result is a link to a SOURCE and a candidate is a row on a
        # listing page. The judge was right and the brief was ambiguous,
        # which made the node unwinnable rather than merely hard.
        "SEARCH RESULTS ARE NOT CANDIDATES. A search tells you where a "
        "listing might be; you still have to open that listing and pull "
        "the rows out of it. You are done only once you have extracted "
        "the actual rows from a page that lists them."
    ),
    "per_item": (
        "YOUR JOB IS TO OPEN AND READ THE CANDIDATES ALREADY FOUND.\n"
        "The candidates were gathered for you. Open each one on its OWN "
        "page and read it, so its details come from that page rather than "
        "from a results list. Do NOT gather more lists -- that work is "
        "finished and its budget is spent.\n"
        "You are done when the required number have been opened and read."
    ),
    "assemble": (
        "YOUR JOB IS TO REPORT WHAT WAS ACTUALLY VERIFIED.\n"
        "The counts you have been given are the counts. Report the items "
        "that were opened and read, state plainly how many of the number "
        "asked for that is, and do not fill any gap from a results list or "
        "from memory. An item nobody opened is not one of them."
    ),
}


def brief_for(node: Any, task: str, extra: str = "") -> str:
    """The sub-task one node's owner is given."""
    head = _BRIEFS.get(getattr(node, "kind", ""), "")
    kind, want = node.requirement()
    target = ""
    if kind == "items" and want:
        target = f"\nTHE NUMBER REQUIRED HERE IS {want}."
    elif kind == "content" and want:
        target = f"\nYOU NEED AT LEAST {want} ENTRIES."
    return (f"{head}{target}\n\n"
            f"THE OVERALL GOAL (for context — do only your part of it):\n"
            f"{task}\n{extra}").strip()


# ------------------------------------------------------------ the run

def run(task: str, adapter: Any, role: str = "Specialist",
        budget: int = 22, deadline_seconds: float = 600.0,
        executor_factory: Optional[Callable[..., Any]] = None,
        ledger_calls: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        spec: Any = None, graph: Any = None, plan: Any = None) -> GraphRun:
    """Execute a goal node by node. Returns what happened, never raises.

    `executor_factory(**kwargs) -> object with .run(task, role=...)`
    exists so tests can drive this without a model, and so the real
    caller can hand in an executor already configured with its own
    connection pool.
    """
    from backend.app.orchestrator.task_graph import build_graph, judge_node
    from backend.app.orchestrator.task_node import COMPLETED

    out = GraphRun()
    try:
        if graph is None or plan is None:
            graph, plan = build_graph(task, budget, spec=spec)
    except Exception as exc:  # noqa: BLE001
        logger.info("[graph-run] no graph for this goal (%s)", exc)
        return out
    out.graph, out.plan = graph, plan
    if not graph.nodes:
        return out          # unshaped goal: the caller runs as it always did

    spec = spec or plan.spec
    ledger_calls = ledger_calls or (lambda: [])
    started = time.time()
    spent = 0

    logger.info("[graph-run] %s", graph.describe().replace("\n", " | "))

    while True:
        node = graph.next_node()
        if node is None:
            break

        left = budget - spent
        if left < MIN_NODE_STEPS or time.time() - started > deadline_seconds:
            out.stopped = (f"budget exhausted before {node.id} "
                           f"({left} step(s) left)")
            graph.skip(node.id, out.stopped)
            graph.propagate()
            continue

        owner = _staff(node, task, spec)
        node_budget = max(MIN_NODE_STEPS,
                          min(left, (node.est_calls or MIN_NODE_STEPS)
                              + NODE_BUDGET_SLACK))

        nr = NodeRun(node_id=node.id, capability=node.capability,
                     employee_role=owner["role"], employee_id=owner["id"],
                     hired=owner["hired"])
        node_started = time.time()
        calls_before = len(ledger_calls())
        graph.start(node.id)

        try:
            ex = _executor(executor_factory, adapter, owner, node_budget,
                           deadline_seconds - (time.time() - started))
            nr.transcript = ex.run(brief_for(node, task),
                                   role=owner["role"],
                                   original_task=task) or ""
            graph.finish(node.id, ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[graph-run] %s raised: %s", node.id, exc)
            nr.why = f"{type(exc).__name__}: {exc}"
            graph.finish(node.id, ok=False, why=nr.why)
            nr.status = node.status
            out.runs.append(nr)
            graph.propagate()
            spent += node_budget
            continue

        # WHETHER IT COUNTED IS NOT THE EMPLOYEE'S CALL. Judged from the
        # ledger by the same checks that enforce this elsewhere.
        ok, why = judge_node(node, task, ledger_calls(), spec)
        if node.status == COMPLETED:
            graph.judge(node.id, ok, why)
        nr.status, nr.why = node.status, why
        # Real work done, counted from the ledger. This was a field that
        # was declared and never filled, so every node reported "0 steps"
        # however much it did -- the same shape of lie as a config the
        # UI shows and the loop ignores.
        nr.steps = max(0, len(ledger_calls()) - calls_before)
        nr.seconds = time.time() - node_started
        out.runs.append(nr)
        spent += node_budget
        graph.propagate()

    if not out.stopped:
        out.stopped = "every reachable node was executed"
    logger.info("[graph-run] %s", out.describe().replace("\n", " | "))
    return out


def _staff(node: Any, task: str, spec: Any) -> Dict[str, Any]:
    """The employee that owns this node.

    Reuses the capability layer wholesale: it already knows how to find
    someone who covers a capability and to hire one when nobody does.
    Falls back to a plain worker rather than failing the node, because a
    goal that cannot be staffed is still worth attempting.
    """
    fallback = {"role": "Specialist", "id": "", "hired": False,
                "tools": (), "loop_model": None}
    cap_name = getattr(node, "capability", "") or ""
    if not cap_name:
        return fallback
    try:
        from backend.app.employees import capability as cap_mod
        cap = next((c for c in cap_mod.CATALOGUE if c.name == cap_name), None)
        if cap is None:
            return fallback
        roster = cap_mod.staff(task, spec=spec)
        for a in roster.assignments:
            if a.capability.name == cap_name:
                emp = a.employee or {}
                cfg = (emp.get("config") or {}).get("tools") or {}
                return {"role": emp.get("role") or cap.role,
                        "id": emp.get("id") or "",
                        "hired": bool(a.hired),
                        "tools": tuple(cfg.get("allow") or ()),
                        "loop_model": ((emp.get("config") or {})
                                       .get("model") or {}).get("loop_model")}
    except Exception as exc:  # noqa: BLE001
        logger.info("[graph-run] could not staff %s (%s)", cap_name, exc)
    return fallback


def _executor(factory: Optional[Callable[..., Any]], adapter: Any,
              owner: Dict[str, Any], steps: int, seconds: float) -> Any:
    """One executor for one node, scoped to that employee."""
    kwargs = {
        "max_steps": max(MIN_NODE_STEPS, int(steps)),
        "deadline_seconds": max(30.0, float(seconds)),
        "tools_allow": owner.get("tools") or (),
        "loop_model": owner.get("loop_model"),
        # This is ONE NODE of an already-planned goal. Without this the
        # executor re-derives a goal spec from the node's brief, prices
        # that against the node's slice of the budget, and refuses
        # itself -- which is exactly what happened the first time a node
        # ran live. See AgenticExecutor.subtask.
        "subtask": True,
    }
    if factory is not None:
        return factory(adapter, **kwargs)
    from backend.app.orchestrator.execution_loop import AgenticExecutor
    return AgenticExecutor(adapter, **kwargs)
