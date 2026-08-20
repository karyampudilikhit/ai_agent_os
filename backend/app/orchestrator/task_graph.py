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
