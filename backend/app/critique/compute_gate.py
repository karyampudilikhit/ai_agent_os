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
from typing import Any, Dict, List, Optional

from backend.app.orchestrator.output_contract import EXECUTED_CODE

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


_MD_ROW = re.compile(r"^\s*\|(?P<first>[^|]{1,60})\|", re.MULTILINE)
_MD_SEPARATOR = re.compile(r"^[\s|:\-—–]+$")
# A row label worth checking: an identifier, not prose and not a number.
# "TREVQ", "BRK.A", "AAPL" qualify; "Ticker", "1", "the top five" do not.
_ROW_LABEL = re.compile(r"^[A-Z][A-Z0-9./\-]{1,14}$")
_ROW_HEADERS = frozenset({
    "TICKER", "SYMBOL", "STOCK", "NAME", "COMPANY", "RANK", "ITEM", "N/A",
})


def reported_row_labels(text: str) -> List[str]:
    """Row identifiers a deliverable presents as data it read off a page.

    Deliberately narrow: it takes the FIRST cell of each markdown table
    row and keeps only what is shaped like an identifier. The cost of a
    false accusation here is a blocked honest run, and a check that fires
    on prose is worth less than no check at all.
    """
    out: List[str] = []
    for m in _MD_ROW.finditer(text or ""):
        cell = m.group("first").strip().strip("*`_ ").strip()
        if not cell or _MD_SEPARATOR.match(cell):
            continue
        parts = cell.split()
        token = parts[0].strip("*`_,:;") if parts else ""
        if not _ROW_LABEL.match(token) or token.upper() in _ROW_HEADERS:
            continue
        if token not in out:
            out.append(token)
    return out


def unbacked_row_labels(text: str, captured: str) -> List[str]:
    """Row labels the deliverable reports that appear NOWHERE this run read.

    THE FAILURE THIS ANSWERS. A live run reported a ten-row table of
    tickers with weekly percentages. Every ticker was real and every price
    was real -- and the percentages had been altered so that five of them
    read as gainers. Number provenance catches an invented FIGURE; nothing
    caught an invented ROW, because a row never had to come from anywhere.

    The same shape as untraceable_metric_values, one level up: a
    deliverable may only report records that appear in what the run
    actually captured.
    """
    if not text or not captured:
        return []
    haystack = captured.upper()
    return [label for label in reported_row_labels(text)
            if label.upper() not in haystack]


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


def declared_compute_roles(specialists: Optional[List[Dict[str, Any]]]) -> set:
    """Roles whose EMPLOYEE CONFIG says they own executing code.

    A declaration beats every heuristic below it. The founder configured
    this employee to produce computed numbers; guessing from role words
    when the answer has been stated outright is how a configuration
    silently fails to take effect.

    Accepts either shape a caller might have on hand: a flattened
    ``{role, required_outputs}`` (what EmployeeCoordinator builds) or a
    raw registry record carrying a nested ``config`` (what
    ``TeamStore.specialists()`` returns). Handling both here keeps every
    call site a one-liner instead of making each one reshape first.
    """
    out = set()
    for spec in specialists or []:
        required = spec.get("required_outputs")
        if required is None:
            required = _resolved_required_outputs(spec)
        if EXECUTED_CODE in (required or ()):
            role = str(spec.get("role") or "").strip().lower()
            if role:
                out.add(role)
    return out


def _resolved_required_outputs(spec: Dict[str, Any]) -> tuple:
    """Required outputs from a raw registry record.

    Goes through the config resolver rather than reading
    ``spec["config"]["required_outputs"]`` directly, so a declaration
    that arrives from a ROLE TEMPLATE counts too. Reading the raw dict
    would work today and quietly stop working the moment templates ship.

    Lazy import: employee_config reaches into the orchestrator for its
    budget defaults, and compute_gate should not pull that in at module
    load. Never raises -- an unreadable config means "no declaration",
    which falls back to the heuristics below.
    """
    try:
        from backend.app.employees.employee_config import resolve
        return resolve(spec).required_outputs
    except Exception:  # noqa: BLE001
        return tuple((spec.get("config") or {}).get("required_outputs") or ())


def choose_compute_owner_index(
    plan: List[Dict[str, Any]],
    specialists: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Index of the assignment that should own executing the code.

    Three tiers, in order:

    1. A specialist whose CONFIG declares `executed_code`. Stated intent
       always beats inference -- including over the presentation-role
       exclusion below. If a founder deliberately configures their Report
       Writer to run the numbers, that is their architecture to define,
       and quietly overruling it would make the setting a lie.
    2. Otherwise the earliest non-presentation assignment with
       data/analysis affinity -- earliest because every downstream
       specialist needs the numbers to already exist, non-presentation
       because a report author asked to also compute is exactly the split
       that produced a deliverable full of asserted metrics.
    3. Otherwise the first non-presentation assignment, then the first.

    THE GAP TIER 1 CLOSES. Per-employee `required_outputs` stopped a
    specialist REFUSING to compute, but the Supervisor still ELECTED the
    owner by role-word weights (`quant` 5, `analyst` 4, `data` 1,
    `engineer` 1) with no idea any employee had declared anything. So a
    founder could configure their Quant Analyst to own computation and
    still watch the work land on the Data Engineer.
    """
    if not plan:
        return -1

    def is_presentation(entry: Dict[str, Any]) -> bool:
        return any(w in str(entry.get("role") or "").lower()
                   for w in _PRESENTATION_ROLE_WORDS)

    declared = declared_compute_roles(specialists)
    if declared:
        for i, entry in enumerate(plan):
            if str(entry.get("role") or "").strip().lower() in declared:
                return i

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


def ensure_compute_owner(
    task: str,
    plan: List[Dict[str, Any]],
    specialists: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """If the founder asked for computed figures and no assignment owns
    executing code, give that ownership to one specialist explicitly.

    `specialists` carries each employee's declared `required_outputs` so
    a configured owner wins over a guessed one. Optional, so every
    existing caller keeps working on heuristics alone.

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
    # A declared owner is honoured even when the founder's wording never
    # named a metric. That is the point of configuring it: the role owns
    # computation as a standing fact, not one phrasing at a time.
    declared = declared_compute_roles(specialists)
    if not plan or not (task_requires_computation(task) or declared):
        return plan
    if plan_has_compute_owner(plan):
        return plan
    idx = choose_compute_owner_index(plan, specialists)
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


# ---------------------------------------------------------------------
# Do the stated numbers TRACE to what the code actually printed?
# ---------------------------------------------------------------------
#
# The third failure class, and the one the other two gates cannot see.
#
#   provenance  -- did a compute tool run?           _unused_compute_capability
#   substance   -- are numbers stated at all?        missing_metric_values
#   correctness -- are they THE numbers it computed?  <- this
#
# Observed 2026-08-16. A run reported "CAGR 7.65%, Sharpe 0.7565, max
# drawdown -20.70%" for a 50/200 SMA crossover on SPY. run_python
# genuinely ran and real figures appeared, so both gates above passed.
# Re-deriving from the same CSV gave Sharpe 0.4457 and drawdown -34.10%
# under every variant tried -- and the script the agent produced when
# asked for its code contained no Sharpe calculation anywhere, used 20/50
# windows over 5 years rather than 50/200 over 10, and applied none of
# the costs it quoted. The numbers were not computed. They were written.
#
# A founder acting on a fabricated Sharpe ratio is the worst thing this
# system can produce, and it is worse than a loud failure precisely
# because it arrives dressed as success.

_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def _numbers_in(text: str) -> List[float]:
    out: List[float] = []
    for m in _NUMBER_RE.finditer(text or ""):
        try:
            out.append(float(m.group(0).replace(",", "")))
        except ValueError:
            continue
    return out


def _traceable(claimed: str, candidates: List[float]) -> bool:
    """Does a claimed figure match something the code printed?

    Deliberately forgiving, because accusing a truthful deliverable is
    far worse than missing one -- the same bias SourceLedger takes.
    Three allowances, none of which lets a wrong number through:

      - ROUNDING. Tolerance is half the claimed value's last decimal
        place, so "7.65" matches a printed 7.6523.
      - SCALE. A percentage may be printed as a fraction, so 7.65 also
        matches 0.0765.
      - SIGN. Drawdown is printed positive about as often as negative,
        so magnitudes are compared.

    What it does not forgive is a figure that appears nowhere at any
    scale -- which is what a fabricated number looks like.
    """
    try:
        value = float(claimed.replace(",", ""))
    except ValueError:
        return True  # unparseable: do not accuse
    decimals = len(claimed.split(".")[1]) if "." in claimed else 0
    tol = 0.5 * (10.0 ** -decimals)
    mag = abs(value)
    variants = ((mag, tol), (mag / 100.0, tol / 100.0), (mag * 100.0, tol * 100.0))
    for cand in candidates:
        a = abs(cand)
        if any(abs(a - v) <= t + 1e-12 for v, t in variants):
            return True
    return False


def untraceable_metric_values(task: str, output: str,
                              computed_text: str) -> List[str]:
    """Figures the deliverable states that appear nowhere in the compute
    tool's real output, formatted as "metric = value".

    Returns [] when `computed_text` is empty: with nothing captured there
    is nothing to check against, and the provenance gate already covers
    "nothing ever computed". Never accuse on absence of evidence.
    """
    if not (computed_text or "").strip():
        return []

    ask = (task or "").lower()
    body = _prose_only(output)
    printed = _numbers_in(computed_text)
    if not printed:
        return []

    bad: List[str] = []
    for group in METRIC_VALUE_GROUPS:
        if not any(alias in ask for alias in group):
            continue
        for alias in group:
            found = False
            for m in re.finditer(re.escape(alias), body, re.IGNORECASE):
                window = body[m.end(): m.end() + 60]
                if any(mk in window.lower() for mk in _NO_VALUE_MARKERS):
                    continue
                num = _NUMBER_RE.search(window)
                if not num:
                    continue
                found = True
                if not _traceable(num.group(0), printed):
                    claim = "%s = %s" % (group[0], num.group(0).strip())
                    if claim not in bad:
                        bad.append(claim)
                break  # the first stated value for this alias is enough
            if found:
                break
    return bad
