"""Canonical definition of "this task demands a computed number".

Lives in one module because the list is used in three places that must
agree, and this codebase has already been bitten twice by the same list
existing in two files and drifting apart (see handback_detector.py's
comment about the blocked-until verbs: "Same omission, two places").

  1. routes._unused_compute_capability -- fails a run that asked for
     computed figures and never ran a compute tool.
  2. supervisor.design_delegation -- makes sure SOMEBODY on the team is
     explicitly told to do the computing.
  3. Anything else that needs the same question answered.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

# Deliberately narrow: these are results you can only get by running
# something over a dataset, so their presence in the ask plus the
# absence of any compute call is a provable gap rather than a guess.
COMPUTED_METRIC_WORDS = (
    "sharpe", "cagr", "drawdown", "annualised return", "annualized return",
    "win rate", "win-rate", "backtest", "back-test", "back‑test",
    "volatility", "sortino", "alpha", "beta", "correlation matrix",
)

# Tools that can actually produce such a number.
COMPUTE_TOOLS = ("run_python", "calculate")


def task_requires_computation(task: str) -> bool:
    """True when the ask names a figure you can only get by executing
    something over data."""
    return any(w in (task or "").lower() for w in COMPUTED_METRIC_WORDS)


# Phrases that show a sub-task ALREADY makes execution someone's job.
# Deliberately stricter than the word "compute" on its own: a live run
# failed with a sub-task that said "compute the exact CAGR, Sharpe and
# max drawdown" and the specialist still never executed anything -- its
# own next-step reached for action.calculate instead. Naming the metric
# is not the same as owning the execution, so ownership has to be
# recognised by an explicit reference to running code.
_EXECUTION_OWNERSHIP_MARKERS = (
    "run_python",
    "execute python",
    "execute the python",
    "run python",
    "running python",
    "execute code",
    "run the code",
    "executing code",
)


def plan_has_compute_owner(plan: List[Dict[str, Any]]) -> bool:
    """True when some assignment already explicitly owns executing code."""
    for entry in plan or []:
        text = " ".join([
            str(entry.get("sub_task") or ""),
            str(entry.get("task_brief") or ""),
        ]).lower()
        if any(m in text for m in _EXECUTION_OWNERSHIP_MARKERS):
            return True
    return False


# Role/sub-task words suggesting the assignment that SHOULD do the
# computing, WEIGHTED by how strongly each implies "this person derives
# numbers" rather than merely "this person touches data".
#
# The weighting is load-bearing, not cosmetic. Every specialist's system
# prompt says "do the part that belongs to your role, and only your part
# -- if a piece belongs to a different role, say so briefly and skip it."
# So a compute mandate injected into a role whose identity is retrieval
# ("Data Engineer") is liable to be refused as out-of-mandate, while the
# same mandate on an analysis role is congruent with it. An unweighted
# count tied Data Engineer with Quant Analyst and picked the wrong one.
_COMPUTE_OWNER_HINTS = {
    # Identity: deriving numbers IS the job.
    "quant": 5, "analyst": 4, "scientist": 4, "analysis": 3,
    "backtest": 4, "back-test": 4, "model": 2, "statistic": 4,
    # Identity: handling data, which may or may not include computing it.
    "data": 1, "engineer": 1, "research": 1,
}

# Roles whose job is presentation, not computation. Never pick these as
# the compute owner even if they score on a hint word -- the numbers have
# to exist before the writer runs, and handing execution to the report
# author is how the numbers end up asserted rather than calculated.
_PRESENTATION_ROLE_WORDS = (
    "writer", "designer", "report", "editor", "presentation", "document",
)


def choose_compute_owner_index(plan: List[Dict[str, Any]]) -> int:
    """Index of the assignment that should own executing the code.

    Prefers the EARLIEST non-presentation assignment with data/analysis
    affinity -- earliest because every downstream specialist needs the
    numbers to already exist, and non-presentation because a report
    author asked to also compute is exactly the split that produced a
    deliverable full of asserted metrics.
    """
    if not plan:
        return -1

    def is_presentation(entry: Dict[str, Any]) -> bool:
        return any(w in str(entry.get("role") or "").lower()
                   for w in _PRESENTATION_ROLE_WORDS)

    scored = []
    for i, entry in enumerate(plan):
        if is_presentation(entry):
            continue
        text = " ".join([
            str(entry.get("role") or ""),
            str(entry.get("sub_task") or ""),
        ]).lower()
        score = sum(w for h, w in _COMPUTE_OWNER_HINTS.items() if h in text)
        if score:
            scored.append((-score, i))
    if scored:
        # Highest hint score wins; ties break toward the earliest.
        scored.sort()
        return scored[0][1]

    # Nothing scored: fall back to the first non-presentation
    # assignment, then to the very first.
    for i, entry in enumerate(plan):
        if not is_presentation(entry):
            return i
    return 0


COMPUTE_MANDATE = (
    " YOU own producing these numbers. Call action.run_python on the real "
    "fetched data to compute them yourself, in this sub-task, before "
    "reporting. Do not estimate them, do not describe how they would be "
    "computed, and do not leave them for another specialist -- report "
    "exactly what the executed code printed."
)


def ensure_compute_owner(task: str, plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """If the founder asked for computed figures and no assignment owns
    executing code, give that ownership to one specialist explicitly.

    THE BUG THIS FIXES, observed live twice. The Supervisor decomposes
    "backtest this and report CAGR/Sharpe/drawdown" into Data Engineer
    (fetch the prices) -> Report Designer (write it up), and the actual
    computation belongs to nobody. Every specialist then does its own
    slice correctly, the metrics are never calculated, and the run dies
    on the compute gate with "run_python was never called".

    Mechanical on purpose. The delegation prompt could ask for this, but
    prompt rules are advisory to a weak model and this codebase's whole
    history says the mechanical check is the one that holds.
    """
    if not plan or not task_requires_computation(task):
        return plan
    if plan_has_compute_owner(plan):
        return plan
    idx = choose_compute_owner_index(plan)
    if idx < 0:
        return plan
    owner = dict(plan[idx])
    owner["sub_task"] = (str(owner.get("sub_task") or "").rstrip() + COMPUTE_MANDATE)
    out = list(plan)
    out[idx] = owner
    return out


# ---------------------------------------------------------------------
# Which tool can actually produce a given figure
# ---------------------------------------------------------------------
#
# `calculate` is an AST walker that evaluates ONE arithmetic expression.
# It cannot load a price series, roll a moving average, or derive a
# Sharpe ratio from daily returns. Counting it as evidence that a
# backtest happened is how a run satisfies the compute gate without
# computing anything, so metrics that require a dataset accept only
# run_python.
DATASET_COMPUTE_TOOLS = ("run_python",)

# Metrics that denote a NUMBER the deliverable should actually state,
# grouped by SYNONYM. A group is satisfied when ANY of its aliases is
# reported with a value -- "annualised return (CAGR)" is one figure with
# two names, and demanding both flagged a perfectly good deliverable that
# said "CAGR: 10.68%".
#
# Deliberately excludes "backtest"/"back-test" (an activity, not a value)
# and "alpha"/"beta" (too easily matched by "beta version" and similar
# prose) -- a false block is worse than a miss.
METRIC_VALUE_GROUPS = (
    ("cagr", "annualised return", "annualized return", "annual return"),
    ("sharpe",),
    ("drawdown", "draw-down", "draw down"),
    ("win rate", "win-rate"),
    ("sortino",),
)

# Flat view, kept for callers that just want the vocabulary.
METRIC_VALUE_WORDS = tuple(w for group in METRIC_VALUE_GROUPS for w in group)

# Phrases that explicitly admit a metric has no value, so their presence
# next to a metric name counts as missing even if a digit is nearby.
_NO_VALUE_MARKERS = (
    "unknown", "n/a", "not available", "not computed", "not calculated",
    "could not", "couldn't", "unavailable", "pending", "tbd", "to be determined",
    "see the", "see attached", "in the pdf", "in the report", "saved at",
)

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def _prose_only(text: str) -> str:
    """Deliverable text with fenced code removed. Source code is a claim
    about what WOULD be computed; only prose reports what WAS."""
    return _FENCE_RE.sub("", text or "")


def missing_metric_values(task: str, output: str) -> List[str]:
    """Metrics the task asked for that the deliverable never states a
    number for.

    THE FAILURE THIS CATCHES. A run satisfied the compute gate -- a
    specialist really did call run_python -- and then shipped a report
    whose entire results section read "the PDF report (including the
    exact CAGR, Sharpe ratio, and maximum draw-down) is saved at
    D:\\...\\backtest_output\\SPY_SMA_Crossover_Backtest_Report.pdf".
    That directory did not exist, and no figure appeared anywhere in the
    deliverable. Every existing guard passed it: nothing was fabricated,
    nothing was handed back, and a compute tool genuinely ran.
    Provenance was satisfied; SUBSTANCE was not.
    """
    ask = (task or "").lower()
    body = _prose_only(output)
    missing: List[str] = []

    def has_value(alias: str) -> bool:
        for m in re.finditer(re.escape(alias), body, re.IGNORECASE):
            window = body[m.end(): m.end() + 60].lower()
            if any(marker in window for marker in _NO_VALUE_MARKERS):
                continue  # explicitly says there is no value here
            if re.search(r"-?\d+(?:\.\d+)?\s*%?", window):
                return True
        return False

    for group in METRIC_VALUE_GROUPS:
        if not any(alias in ask for alias in group):
            continue  # this figure was not requested
        if not any(has_value(alias) for alias in group):
            missing.append(group[0])
    return missing
