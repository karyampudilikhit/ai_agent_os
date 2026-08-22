"""What the goal costs, decided BEFORE the budget is spent.

THE FAILURE THIS ANSWERS. A run was asked for ten internships, each
verified on its own page. That is roughly three calls to find a source
plus two per item -- about twenty-three, against a browser budget of
twenty-two. It was unwinnable before its first step, and nothing in the
system could say so. It spent all twenty calls on results pages, opened
none of the ten, and produced nothing.

Twenty calls of silence is the worst possible answer. "This needs about
twenty-three calls and I have twenty-two, so I will verify eight
properly and tell you" is a good one, and it costs one step to decide.

WHY ESTIMATES AND NOT A SIMULATOR. The numbers below are coarse on
purpose: discovery is a couple of calls, opening a page is one, reading
it is one. Coarse is enough to separate "comfortably affordable" from
"impossible", which is the only distinction that changes what we do. A
precise model of call cost would be wrong in different ways and invite
trust it has not earned.

DESCOPING IS AN ANSWER, NOT A FAILURE. Eight verified internships,
honestly labelled, is worth more than ten unverified ones and far more
than nothing. The founder is told what was cut and why.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.app.orchestrator.goal_spec import GoalSpec, from_task

logger = logging.getLogger(__name__)

# Coarse per-stage costs, in agent steps. Measured against the live runs
# this module exists because of.
COST_DISCOVER = 3      # reach a source and get a list: navigate, search, extract
COST_OPEN_ITEM = 1     # navigate to one item
COST_READ_ITEM = 1     # extract its fields
COST_ASSEMBLE = 2      # dedupe, rank, report
# Kept free because they are code, not calls: filtering, deduping and
# constraint checks cost nothing once the data is in hand.

# Below this share of the budget a task is comfortable; above it, tight
# but allowed. Only genuinely impossible tasks are refused.
COMFORTABLE = 0.75

RUN = "run"
DESCOPE = "descope"
REFUSE = "refuse"


@dataclass
class SubGoal:
    name: str
    kind: str                 # discover | per_item | assemble
    est_calls: int
    per_item: bool = False


@dataclass
class Plan:
    spec: GoalSpec
    sub_goals: List[SubGoal] = field(default_factory=list)
    est_calls: int = 0
    budget: int = 0
    verdict: str = RUN
    feasible_items: int = 0
    reason: str = ""

    def note(self) -> str:
        """What the loop should be told before it starts. Empty when the
        plan is comfortable -- a note nobody needs is noise in a prompt."""
        if self.verdict == RUN and not self.reason:
            return ""
        return self.reason


def build(task: str, budget: int, spec: Optional[GoalSpec] = None) -> Plan:
    """Expand a goal into sub-goals and decide whether it fits.

    Never raises, and never returns REFUSE for a task whose shape was not
    understood: an unrecognised goal runs exactly as it does today.
    """
    spec = spec or from_task(task)
    plan = Plan(spec=spec, budget=max(1, int(budget or 1)))

    if not spec.confident:
        # No stated shape. Behave exactly as before this module existed.
        plan.est_calls = 0
        plan.verdict = RUN
        plan.feasible_items = spec.item_count
        return plan

    n = max(1, spec.item_count)
    plan.sub_goals.append(SubGoal("discover a source and list candidates",
                                  "discover", COST_DISCOVER))

    per_item_cost = 0
    if spec.per_item_work:
        per_item_cost = COST_OPEN_ITEM + COST_READ_ITEM
        plan.sub_goals.append(
            SubGoal(f"open and read each of {n} item(s)", "per_item",
                    per_item_cost * n, per_item=True))
    plan.sub_goals.append(SubGoal("dedupe, rank and report", "assemble",
                                  COST_ASSEMBLE))

    fixed = COST_DISCOVER + COST_ASSEMBLE
    plan.est_calls = fixed + per_item_cost * n
    plan.feasible_items = n

    if plan.est_calls <= plan.budget * COMFORTABLE:
        plan.verdict = RUN
        return plan

    if plan.est_calls <= plan.budget:
        plan.verdict = RUN
        plan.reason = (
            f"BUDGET IS TIGHT: this needs about {plan.est_calls} of your "
            f"{plan.budget} steps. Go straight for the items — do not spend "
            f"steps exploring filters or alternative sources."
        )
        return plan

    # Does not fit. How much DOES fit?
    if per_item_cost:
        affordable = (plan.budget - fixed) // per_item_cost
    else:
        affordable = 0

    if affordable >= 1:
        plan.verdict = DESCOPE
        plan.feasible_items = affordable
        plan.reason = (
            f"BUDGET: {n} item(s) each opened individually needs about "
            f"{plan.est_calls} steps and you have {plan.budget}. Do "
            f"{affordable} PROPERLY — open and verify each one — and state "
            f"plainly that you did {affordable} of {n} and why. Do NOT pad "
            f"the rest from the results list: an unverified item is not one "
            f"of the {n} that were asked for."
        )
        return plan

    plan.verdict = REFUSE
    plan.feasible_items = 0
    plan.reason = (
        f"BUDGET: this task needs about {plan.est_calls} steps and only "
        f"{plan.budget} are available — not even one item can be verified "
        f"properly. Say so plainly and do not begin: a partial answer that "
        f"looks complete is worse than none."
    )
    return plan


def describe(plan: Plan) -> str:
    """One line per sub-goal, for a log or a founder-facing note."""
    if not plan.sub_goals:
        return "(no stated shape — running as an open-ended task)"
    lines = [f"GOAL: {plan.spec.summary()}",
             f"ESTIMATE: {plan.est_calls} step(s) of {plan.budget} "
             f"→ {plan.verdict.upper()}"]
    for sg in plan.sub_goals:
        lines.append(f"  - {sg.name} (~{sg.est_calls})")
    if plan.reason:
        lines.append(plan.reason)
    return "\n".join(lines)


# ===================================================== PHASES
#
# THE FAILURE THIS ANSWERS. Asked for twenty internships, a run spent
# all twenty-one of its calls discovering. It harvested forty-nine
# candidates -- nineteen of them genuinely new -- opened two, verified
# none, and stopped because the budget ran out. It never decided to stop
# looking and start checking, because nothing in the system HELD a
# decision like that. The plan below priced the work at step zero and
# was then never consulted again.
#
# A phase is state, not advice. The loop asks which phase it is in
# before every call, and the answer is arithmetic over the ledger: how
# many candidates are in hand, how many items are still needed, how many
# steps remain, and what verifying one costs. A model cannot argue with
# it and cannot keep the run in DISCOVER by wanting to.

DISCOVER = "DISCOVER"
VERIFY = "VERIFY"
ASSEMBLE = "ASSEMBLE"


@dataclass
class Phase:
    """Where this run is, and why."""

    name: str = ""
    reason: str = ""
    requested: int = 0
    affordable: int = 0
    candidates: int = 0
    opened: int = 0
    verified: int = 0
    needed: int = 0
    steps_left: int = 0

    @property
    def active(self) -> bool:
        """False for goals with no countable shape -- and then nothing
        below this changes what the run does."""
        return bool(self.name)

    def line(self) -> str:
        if not self.active:
            return "(no phase — goal has no countable shape)"
        return (f"{self.name} — {self.candidates} candidate(s), "
                f"{self.verified} verified of {self.affordable} affordable "
                f"({self.requested} requested), {self.steps_left} step(s) left"
                + (f": {self.reason}" if self.reason else ""))


def phase_of(plan: Plan, candidates: int, opened: int, verified: int,
             steps_left: int) -> Phase:
    """Which phase this run is in. Pure arithmetic, no model input.

    SILENT UNLESS THE GOAL HAS A COUNTABLE SHAPE. A task that names no
    item count, or wants no per-item work, gets an inactive phase and
    every caller behaves exactly as it did before phases existed. A
    wrong phase would be worse than none: it could refuse the only
    action that was going to work.
    """
    ph = Phase(steps_left=max(0, int(steps_left or 0)))
    try:
        spec = plan.spec
        if not getattr(spec, "confident", False):
            return ph
        if not getattr(spec, "per_item_work", False):
            return ph
        ph.requested = int(getattr(spec, "item_count", 0) or 0)
        ph.affordable = max(0, int(plan.feasible_items or 0))
    except Exception:  # noqa: BLE001
        return Phase()
    if ph.requested < 2 or ph.affordable < 1:
        return Phase()

    ph.candidates = max(0, int(candidates or 0))
    ph.opened = max(0, int(opened or 0))
    ph.verified = max(0, int(verified or 0))
    ph.needed = max(0, ph.affordable - ph.verified)

    if ph.needed <= 0:
        ph.name = ASSEMBLE
        ph.reason = ("every item this budget can verify has been verified — "
                     "report them")
        return ph

    # What one more item costs to verify, straight from the same numbers
    # the estimate above was built from. Nothing here is tuned to any
    # particular kind of task.
    per_item = COST_OPEN_ITEM + COST_READ_ITEM
    steps_to_finish = ph.needed * per_item

    # NO BUDGET LEFT TO SPEND ON LOOKING. Whatever is in hand is what
    # this run gets to work with, so it had better start working.
    if ph.steps_left <= steps_to_finish and ph.candidates > ph.opened:
        ph.name = VERIFY
        ph.reason = (f"{ph.steps_left} step(s) left and {ph.needed} item(s) "
                     f"still to verify at {per_item} step(s) each — there is "
                     f"no budget left for more discovery")
        return ph

    # ENOUGH IN HAND TO BE WORTH CHECKING. Candidates only become items
    # by being opened, so once there are as many unopened candidates as
    # items still needed, more discovery buys nothing.
    unopened = ph.candidates - ph.opened
    if unopened >= ph.needed:
        ph.name = VERIFY
        ph.reason = (f"{unopened} candidate(s) in hand and only {ph.needed} "
                     f"more needed — open them instead of gathering more")
        return ph

    ph.name = DISCOVER
    ph.reason = (f"{unopened} unopened candidate(s) against {ph.needed} still "
                 f"needed — more are worth finding")
    return ph


# ===================================================== THE GRAPH
#
# The sub-goals above have always been a LIST. That was enough to price
# a goal and never enough to run one: a list cannot say what must finish
# before what, who owns each piece, or what would count as one of them
# being done. So the same expansion is emitted as a graph, and the costs
# come from the same arithmetic rather than a second copy of it.
#
# WHAT THIS DOES NOT DO. It does not decide how any node is executed,
# does not name a tool or a site, and does not choose an employee -- only
# the CAPABILITY a node needs, which the staffing layer already knows how
# to resolve into a reused or newly hired employee.

def build_graph(task: str, budget: int, spec: Optional[GoalSpec] = None,
                plan: Optional[Plan] = None):
    """A validated executable graph for this goal, or an empty one.

    Empty when the goal has no countable shape -- exactly as everywhere
    else in this system, an unrecognised goal runs as it always did.
    """
    from backend.app.orchestrator.task_node import (
        Graph, TaskNode, V_CANDIDATES, V_CONTENT, V_ITEMS, V_NONE,
    )

    plan = plan or build(task, budget, spec=spec)
    graph = Graph()
    if not plan.spec.confident:
        return graph, plan

    n = max(1, plan.feasible_items)
    per_item = bool(plan.spec.per_item_work)

    # Capabilities are named, not employees. Staffing resolves them.
    graph.add(TaskNode(
        id="discover", kind="discover",
        capability="web research",
        verification=V_CANDIDATES,
        est_calls=COST_DISCOVER,
    ))

    last = "discover"
    if per_item:
        graph.add(TaskNode(
            id="verify_items", kind="per_item",
            dependencies=("discover",),
            capability="data extraction",
            verification=f"{V_ITEMS}:{n}",
            est_calls=(COST_OPEN_ITEM + COST_READ_ITEM) * n,
        ))
        last = "verify_items"
    elif n >= 2:
        # A counted goal with no per-item work is satisfied by reading
        # enough entries off a page -- the finish line goal_state
        # already enforces for content goals.
        graph.add(TaskNode(
            id="read_content", kind="discover",
            dependencies=("discover",),
            capability="data extraction",
            verification=f"{V_CONTENT}:{n}",
            est_calls=COST_READ_ITEM,
        ))
        last = "read_content"

    graph.add(TaskNode(
        id="assemble", kind="assemble",
        dependencies=(last,),
        capability="report writing",
        verification=V_NONE,
        est_calls=COST_ASSEMBLE,
    ))

    graph.validate()      # cycles, dangling refs, depth — raises if wrong
    return graph, plan


def judge_node(node, task: str, ledger_calls, spec: Optional[GoalSpec] = None,
               target_items: int = 0) -> Tuple[bool, str]:
    """Does the evidence satisfy this node's finish line?

    Every branch delegates to a check that already exists and is already
    enforced elsewhere. Nothing here is a new definition of "done", and
    nothing here reads a model's opinion.
    """
    kind, want = node.requirement()
    if not kind:
        return True, "nothing required"

    calls = list(ledger_calls or ())
    try:
        if kind == "candidates":
            # ONE HARVESTER, NOT TWO.
            #
            # discovery_state counts only what the records extractor
            # emits as "--- record N ---" blocks; item_state also
            # harvests links out of ordinary page text, and is the older
            # and more exercised of the two. Judging with the narrow one
            # meant a node that genuinely read three listing pages -- but
            # read them as prose rather than as records -- was told it
            # had found nothing. Observed live.
            #
            # Two definitions of "a candidate" in one codebase is two
            # things to keep agreeing, and they had already stopped.
            from backend.app.orchestrator.item_state import derive as _items
            found = len(_items(calls, 0).discovered)
            return found > 0, f"{found} candidate(s) found"

        if kind == "items":
            from backend.app.orchestrator.item_state import derive as _items
            from backend.app.orchestrator.item_verification import tally, verify
            from backend.app.orchestrator.relevance import subject_of
            prog = _items(calls, want)
            t = tally(verify(prog, subject_of(task, spec)), wanted=want)
            return t.verified >= want, f"{t.verified} verified of {want}"

        if kind == "content":
            from backend.app.orchestrator.goal_state import content_entries
            got = max((c for _, c in content_entries(calls)), default=0)
            return got >= want, f"{got} entr(y/ies) of {want}"
    except Exception as exc:  # noqa: BLE001
        logger.info("[graph] could not judge %s: %s", node.id, exc)
        return False, f"could not be judged ({exc})"

    return False, f"unknown verification {kind!r}"
