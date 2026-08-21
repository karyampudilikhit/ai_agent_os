"""What this goal needs someone to be able to DO, and who that is.

THE GAP THIS FILLS. Employees could always be created -- design_team()
reads a prompt and proposes a roster. What could not happen was the
thing V1 actually needs: looking at one goal, working out that it
requires web research and data extraction and report writing, checking
whether the founder ALREADY employs someone for each, and hiring only
for the gaps. A team was designed wholesale per Unit and never
reconciled against the people already on the books, so the same
capability was re-hired under a new name every time, each with an empty
memory.

CAPABILITIES ARE DERIVED FROM THE GOAL, BY RULE. Not by asking a model
what roles it fancies. A goal that names a count of items each visited
individually needs extraction, whatever it is about; a goal that asks
for a comparison needs analysis. These are properties of the ASK, and
reading them from the ask is deterministic, free, and cannot hallucinate
a Chief Vision Officer.

REUSE BEFORE HIRING, AND MATCHING IS BY PROPORTION. An existing employee
covers a capability when its role and mandate carry enough of that
capability's words -- the same overlap measure playbook.py and
strategy_memory use, for the same reason: a count of shared words makes
long mandates match everything.

HIRING IS DELIBERATELY BORING. A missing capability becomes one employee
with a stated role, a mandate that names the capability, a registry id,
and a tool scope. No LLM call: a hire is a record, and a record this
system will still be reading in a month should not depend on what a
model felt like calling the job that afternoon.

WHAT THIS DOES NOT DO. It does not fire anyone, run anything, or decide
who goes first -- the Supervisor still plans delegation and the
orchestrator still owns execution. It answers one question: given this
goal, who should be on the team, and which of them do we need to create?
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Capability:
    """One thing the goal needs someone able to do."""

    name: str
    role: str               # the job title a hire for this gets
    mandate: str            # what that hire is told their job is
    terms: Tuple[str, ...]  # words that identify it in a role/mandate
    tools: Tuple[str, ...] = ()   # tool scope for a fresh hire


# The catalogue. Small on purpose: every entry has to be something a
# real V1 goal asks for and a real employee can be accountable for.
# Adding a capability nobody's goal needs adds a hire nobody needs.
WEB_RESEARCH = Capability(
    "web research", "Web Research Specialist",
    "Find and open the sources that answer the question. Work from pages "
    "you actually opened, and say plainly when a source could not be "
    "reached rather than filling the gap from memory.",
    ("research", "search", "source", "find", "discover", "web", "browse",
     "market", "competitor", "intelligence"),
    ("browser", "web_search", "web_read"),
)
DATA_EXTRACTION = Capability(
    "data extraction", "Data Extraction Specialist",
    "Open each item on its own page and read the required fields off it. "
    "An item you did not open is not an item you found — report the ones "
    "you verified and state how many of the asked-for total that is.",
    ("extract", "extraction", "scrape", "collect", "gather", "capture",
     "data", "field", "record", "listing"),
    ("browser", "web_read"),
)
ANALYSIS = Capability(
    "analysis", "Analyst",
    "Compare what was gathered and say what it means. Every figure you "
    "state must come from something this run actually read or computed.",
    ("analysis", "analyse", "analyze", "compare", "comparison", "evaluate",
     "assess", "benchmark", "insight", "pricing", "quantitative"),
    (),
)
COMPUTATION = Capability(
    "computation", "Quantitative Analyst",
    "Compute every figure by running code over the real data. Never state "
    "a metric that your own script did not print.",
    ("comput", "calculat", "metric", "statistic", "model", "backtest",
     "quantitative", "financial", "number"),
    ("run_python", "calculate", "fetch_market_data"),
)
REPORT_WRITING = Capability(
    "report writing", "Report Writer",
    "Turn verified findings into the deliverable that was asked for. "
    "Report only what the evidence supports, including the shortfalls.",
    ("report", "write", "writing", "draft", "summar", "document",
     "deliverable", "recommend", "strategy", "brief"),
    (),
)

CATALOGUE: Tuple[Capability, ...] = (
    WEB_RESEARCH, DATA_EXTRACTION, ANALYSIS, COMPUTATION, REPORT_WRITING,
)

# Words in the goal that call for each capability beyond what the
# GoalSpec's shape already implies.
_TRIGGERS: Dict[str, Tuple[str, ...]] = {
    "web research": ("research", "find", "search", "look up", "competitor",
                     "market", "source", "discover", "who else", "landscape"),
    "data extraction": ("extract", "collect", "gather", "scrape", "pricing",
                        "price", "field", "details", "listing", "each"),
    "analysis": ("analys", "analyz", "compare", "comparison", "versus", " vs ",
                 "evaluate", "assess", "rank", "best", "recommend", "why"),
    "computation": ("calculate", "comput", "sharpe", "cagr", "backtest",
                    "metric", "statistic", "average", "median", "growth rate",
                    "percentage change", "model"),
    "report writing": ("report", "write", "draft", "summar", "document",
                       "deck", "memo", "recommendation", "strategy", "brief",
                       "prepare"),
}

_WORD = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> Set[str]:
    return set(_WORD.findall((text or "").lower()))


def required_for(task: str, spec: Any = None) -> List[Capability]:
    """The capabilities this goal needs, in the order work happens.

    Never empty for a real task: something has to find the answer and
    something has to write it, so web research and report writing are
    the floor. Everything else is earned by the ask.
    """
    low = f" {(task or '').lower()} "
    needed: List[Capability] = []

    def add(cap: Capability) -> None:
        if cap not in needed:
            needed.append(cap)

    # THE SHAPE OF THE GOAL COMES FIRST, because it is the part that was
    # read structurally rather than by keyword. A goal that says ten
    # items each on its own page needs extraction whatever words it used.
    per_item = bool(getattr(spec, "per_item_work", False)) if spec else False
    try:
        count = int(getattr(spec, "item_count", 0) or 0) if spec else 0
    except (TypeError, ValueError):
        count = 0

    add(WEB_RESEARCH)
    if per_item or count >= 2:
        add(DATA_EXTRACTION)

    for cap in CATALOGUE:
        if cap in needed:
            continue
        if any(t in low for t in _TRIGGERS.get(cap.name, ())):
            add(cap)

    add(REPORT_WRITING)
    # Report writing is the last thing that happens, whenever it was
    # decided. Keep the catalogue's work order otherwise.
    order = {c.name: i for i, c in enumerate(CATALOGUE)}
    needed.sort(key=lambda c: order.get(c.name, 99))
    return needed


# --------------------------------------------------------- matching

# How much of a capability's vocabulary a role+mandate must carry before
# that person counts as covering it. Proportion, not a count: a
# four-line mandate would otherwise match every capability by sheer
# length.
COVERS = 0.25


def covers(employee: Dict[str, Any], cap: Capability) -> bool:
    """Does this employee already do this?"""
    text = " ".join(str(employee.get(k) or "") for k in ("role", "mandate"))
    text += " " + " ".join(str(t) for t in (employee.get("tags") or ()))
    have = _terms(text)
    if not have:
        return False
    hit = sum(1 for t in cap.terms if any(w.startswith(t) for w in have))
    return hit / len(cap.terms) >= COVERS


def find_cover(cap: Capability,
               employees: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The best existing employee for this capability, or None.

    Best = most of the capability's vocabulary covered, so a
    "Competitive Pricing Analyst" beats a "Generalist" for analysis even
    though both clear the bar.
    """
    scored: List[Tuple[int, Dict[str, Any]]] = []
    for emp in employees or ():
        if emp.get("is_supervisor"):
            continue          # supervisors plan; they are not the capability
        if not covers(emp, cap):
            continue
        text = " ".join(str(emp.get(k) or "") for k in ("role", "mandate"))
        have = _terms(text)
        scored.append((sum(1 for t in cap.terms
                           if any(w.startswith(t) for w in have)), emp))
    if not scored:
        return None
    scored.sort(key=lambda p: p[0], reverse=True)
    return scored[0][1]


# --------------------------------------------------------- the roster

@dataclass
class Assignment:
    capability: Capability
    employee: Dict[str, Any]
    hired: bool = False       # True when this employee was created just now

    def line(self) -> str:
        how = "HIRED" if self.hired else "reused"
        return (f"{self.capability.name:<16} -> {self.employee.get('role')} "
                f"({how}, id={self.employee.get('id')})")


@dataclass
class Roster:
    assignments: List[Assignment] = field(default_factory=list)

    @property
    def employees(self) -> List[Dict[str, Any]]:
        """Each employee once, in work order, however many capabilities
        they cover."""
        out, seen = [], set()
        for a in self.assignments:
            eid = a.employee.get("id")
            if eid in seen:
                continue
            seen.add(eid)
            out.append(a.employee)
        return out

    @property
    def hired(self) -> List[Dict[str, Any]]:
        return [a.employee for a in self.assignments if a.hired]

    def describe(self) -> str:
        return "\n".join(a.line() for a in self.assignments) or "(no capabilities)"


def staff(task: str, spec: Any = None, registry: Any = None,
          existing: Optional[Sequence[Dict[str, Any]]] = None,
          hire: bool = True) -> Roster:
    """Work out who should do this goal, hiring only for real gaps.

    `existing` defaults to everyone in the registry. `hire=False` reports
    the gaps without creating anyone, which is what a feasibility or
    dry-run caller wants.

    Never raises: a registry that cannot be read yields a roster of
    whoever was passed in, and a hire that fails leaves that capability
    unassigned rather than failing the whole goal.
    """
    roster = Roster()
    if registry is None:
        try:
            from backend.app.employees.employee_registry import get_registry
            registry = get_registry()
        except Exception:  # noqa: BLE001
            registry = None

    if existing is None:
        try:
            existing = registry.list() if registry else []
        except Exception:  # noqa: BLE001
            existing = []
    pool: List[Dict[str, Any]] = list(existing or ())

    for cap in required_for(task, spec):
        found = find_cover(cap, pool)
        if found is not None:
            roster.assignments.append(Assignment(cap, found, hired=False))
            continue
        if not hire or registry is None:
            continue
        try:
            created = registry.create(
                role=cap.role,
                mandate=cap.mandate,
                tags=[cap.name],
                config={"tools": {"allow": list(cap.tools)}} if cap.tools else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not hire for %r: %s", cap.name, exc)
            continue
        pool.append(created)
        roster.assignments.append(Assignment(cap, created, hired=True))

    logger.info("[capability] %s", roster.describe().replace("\n", " | "))
    return roster
