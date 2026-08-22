"""The work, as addressable units with owners and finish lines.

THE FAILURE THIS ANSWERS. Everything this orchestrator learned to do
this cycle -- price a goal, track items, notice a source going dry, know
when it is finished -- it learned to do for ONE undifferentiated blob of
work. There was no object for "the discovery step", so nothing could say
which step was running, which employee owned it, what would count as
that step being done, or what should happen when it failed. The
Supervisor asked a model what to do next and the model answered from
prose.

A node fixes the narrow thing that makes all of that impossible: work
you can point at.

DELIBERATELY SIX FIELDS. A twelve-field schema was specified and is not
what is built here. Every field a node carries has to be filled by
something and read by something, and a field nobody reads is a lie about
how much structure the system really has -- this codebase has shipped
that exact lie once already, in `tools: {allow, deny}`, a setting the UI
displayed and the loop ignored. So: the six that have readers today, and
the rest when a test needs them.

    id            what to point at
    kind          discover | per_item | assemble
    dependencies  what must be VERIFIED before this may start
    capability    which employee owns it (not which employee -- the
                  capability; staffing already knows how to resolve one)
    status        code-owned, nine states, never model-set
    verification  what must be true for this to count as done

`est_calls` rides along because the feasibility gate already computes it
per sub-goal. It is plumbing, not a seventh idea.

WHO DECIDES WHAT. Code owns every status transition and the answer to
"which node is ready". The model owns how a ready node gets executed --
which tool, which site, which query. There is no path from a model's
output to a status here, and that is the whole point: a run cannot talk
its way into COMPLETED.

REUSES THE VALIDATOR THAT WAS WAITING FOR IT. dependency_validator has
had cycle detection, dangling-reference checks, a depth limit and Kahn
layering since Phase 4, and its own docstring records that the factory
never emitted real dependencies, so it has had nothing to validate. The
fields below are named `id` and `dependencies` because that is what it
reads. It is unproven rather than proven code -- it has never run on a
non-trivial graph -- so its failure modes get tests here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# ---- statuses. Code-owned, every one of them. -----------------------
PENDING = "PENDING"        # not startable yet
READY = "READY"            # every dependency satisfied
RUNNING = "RUNNING"        # an employee is working on it
COMPLETED = "COMPLETED"    # the work ran; whether it counts is not yet known
VERIFIED = "VERIFIED"      # the work ran AND the evidence satisfies it
REJECTED = "REJECTED"      # the work ran and the evidence does not
FAILED = "FAILED"          # the work could not be run
BLOCKED = "BLOCKED"        # something upstream makes it unreachable
SKIPPED = "SKIPPED"        # descoped before it was attempted

STATUSES = (PENDING, READY, RUNNING, COMPLETED, VERIFIED, REJECTED,
            FAILED, BLOCKED, SKIPPED)

# A node is finished-and-usable only when VERIFIED. COMPLETED is not
# enough: it means the work ran, which is exactly the claim this
# codebase does not accept without evidence.
SATISFIED = (VERIFIED,)
# States nothing downstream can ever be unblocked by.
DEAD = (FAILED, REJECTED, BLOCKED, SKIPPED)

# ---- verification vocabulary ---------------------------------------
#
# What a node's `verification` string may say. Kept tiny and mapped onto
# checks that ALREADY EXIST -- item_verification, discovery_state,
# goal_state -- so a node's finish line is the same finish line the rest
# of the system already enforces. A new verification language here would
# be a second definition of "done" and they would drift.
V_NONE = ""                    # nothing to check; COMPLETED is enough
V_CANDIDATES = "candidates"    # discovery produced at least one new one
V_ITEMS = "items"              # "items:N" -- N opened, read and relevant
V_CONTENT = "content"          # "content:N" -- N entries read off a page


@dataclass
class TaskNode:
    id: str
    kind: str
    dependencies: Tuple[str, ...] = ()
    capability: str = ""
    status: str = PENDING
    verification: str = V_NONE
    # Carried from the feasibility gate's own estimate. Not a new idea.
    est_calls: int = 0

    def requirement(self) -> Tuple[str, int]:
        """(what to check, how many). ("", 0) when nothing is required."""
        v = (self.verification or "").strip()
        if not v:
            return "", 0
        if ":" in v:
            head, _, tail = v.partition(":")
            try:
                return head.strip(), int(tail.strip())
            except ValueError:
                return head.strip(), 0
        return v, 0

    def line(self) -> str:
        deps = ("<- " + ", ".join(self.dependencies)) if self.dependencies else ""
        return (f"{self.id:16} {self.status:10} {self.kind:9} "
                f"{self.capability or '-':18} {self.verification or '-':12} {deps}")


class TaskGraphError(Exception):
    pass


@dataclass
class Graph:
    """The nodes, and the only place their statuses change."""

    nodes: Dict[str, TaskNode] = field(default_factory=dict)

    # ---- construction ----------------------------------------------

    def add(self, node: TaskNode) -> TaskNode:
        if node.id in self.nodes:
            raise TaskGraphError(f"duplicate node id {node.id!r}")
        self.nodes[node.id] = node
        return node

    def validate(self) -> List[List[TaskNode]]:
        """Cycle, dangling-reference and depth checks, then layers.

        Delegated to dependency_validator rather than reimplemented: it
        already does exactly this, and two topological sorts in one
        codebase is two things to keep agreeing.
        """
        try:
            from backend.app.orchestrator.dependency_validator import (
                DependencyError, plan_execution,
            )
        except Exception as exc:  # noqa: BLE001
            raise TaskGraphError(f"dependency validator unavailable: {exc}")
        try:
            return plan_execution(list(self.nodes.values()))
        except DependencyError as exc:
            raise TaskGraphError(str(exc)) from exc

    # ---- reading ---------------------------------------------------

    def get(self, node_id: str) -> Optional[TaskNode]:
        return self.nodes.get(node_id)

    def _deps_of(self, node: TaskNode) -> List[TaskNode]:
        return [self.nodes[d] for d in node.dependencies if d in self.nodes]

    def ready(self) -> List[TaskNode]:
        """Every node whose dependencies are all VERIFIED.

        THE QUESTION THE LOOP MUST NEVER ASK A MODEL. It is a fact about
        the graph, and it is answered here.

        Dependencies must be VERIFIED, not merely COMPLETED: a node that
        ran but whose evidence did not hold has not produced anything
        downstream may build on.
        """
        out = []
        for node in self.nodes.values():
            if node.status not in (PENDING, READY):
                continue
            deps = self._deps_of(node)
            if all(d.status in SATISFIED for d in deps):
                out.append(node)
        return out

    def next_node(self) -> Optional[TaskNode]:
        """One ready node, cheapest first.

        Cheapest-first because a budget that runs out mid-graph should
        have bought as many finished nodes as it could, and because the
        alternative -- asking a model which to do next -- is the thing
        this class exists to remove.
        """
        candidates = self.ready()
        if not candidates:
            return None
        return sorted(candidates, key=lambda n: (n.est_calls, n.id))[0]

    def unreachable(self) -> List[TaskNode]:
        """Nodes that can never start, because something upstream died."""
        out = []
        for node in self.nodes.values():
            if node.status in DEAD or node.status in (RUNNING, COMPLETED,
                                                      VERIFIED):
                continue
            if any(d.status in DEAD for d in self._deps_of(node)):
                out.append(node)
        return out

    @property
    def done(self) -> bool:
        """Nothing left that could still run."""
        return not self.ready() and not any(
            n.status in (RUNNING, COMPLETED) for n in self.nodes.values())

    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for n in self.nodes.values():
            out[n.status] = out.get(n.status, 0) + 1
        return out

    def describe(self) -> str:
        lines = [f"GRAPH: {len(self.nodes)} node(s)"]
        for node in self.nodes.values():
            lines.append("  " + node.line())
        return "\n".join(lines)

    # ---- transitions. The only way a status changes. ----------------

    def _set(self, node: TaskNode, status: str, why: str = "") -> TaskNode:
        if status not in STATUSES:
            raise TaskGraphError(f"unknown status {status!r}")
        before = node.status
        node.status = status
        logger.info("[graph] %s: %s -> %s%s", node.id, before, status,
                    f" ({why})" if why else "")
        return node

    def start(self, node_id: str) -> TaskNode:
        node = self.nodes[node_id]
        if node.status not in (PENDING, READY):
            raise TaskGraphError(
                f"{node_id} cannot start from {node.status}")
        if any(d.status not in SATISFIED for d in self._deps_of(node)):
            raise TaskGraphError(f"{node_id} has unsatisfied dependencies")
        return self._set(node, RUNNING)

    def finish(self, node_id: str, ok: bool = True, why: str = "") -> TaskNode:
        """The work ran, or it could not be run. Says nothing about
        whether what it produced counts -- see `judge`."""
        node = self.nodes[node_id]
        return self._set(node, COMPLETED if ok else FAILED, why)

    def judge(self, node_id: str, satisfied: bool, why: str = "") -> TaskNode:
        """Whether the evidence meets this node's finish line.

        Called with a verdict computed from the ledger by the existing
        verification modules. Never with a model's opinion.
        """
        node = self.nodes[node_id]
        if node.status != COMPLETED:
            raise TaskGraphError(
                f"{node_id} cannot be judged from {node.status}")
        return self._set(node, VERIFIED if satisfied else REJECTED, why)

    def skip(self, node_id: str, why: str = "") -> TaskNode:
        return self._set(self.nodes[node_id], SKIPPED, why)

    def propagate(self) -> List[TaskNode]:
        """Mark everything a dead node has stranded as BLOCKED.

        Run after any node dies, so the graph never reports work as
        pending that nothing can reach. Returns what it changed.
        """
        changed = []
        moved = True
        while moved:
            moved = False
            for node in self.unreachable():
                self._set(node, BLOCKED, "an upstream node did not succeed")
                changed.append(node)
                moved = True
        return changed
