"""When to stop looking, and when to look somewhere else.

THE FAILURE THIS ANSWERS. Twenty items requested. One source, twenty-one
calls, six different searches of it, forty-nine candidates harvested of
which thirty were repeats, two opened, none verified, budget gone.

The two decisions that were never made:

  STOP LOOKING   nineteen candidates were in hand by the sixth call and
                 the run kept gathering for fifteen more.
  LOOK ELSEWHERE every call went to one host. A second source was never
                 considered, because nothing tracked that there was only
                 one.

Both are arithmetic, and both are made here rather than asked of a
model. What is NOT decided here is which source to try next -- that is
world knowledge and it stays with the LLM. This module says "this way
in is spent, pick another"; it never says which, and no site is named
anywhere in it.

REFUSAL, NOT ADVICE, AND ONLY ON EVIDENCE. Notes were tried first and
this codebase has watched them fail repeatedly -- the descope
instruction, the item nudge, the budget note. So these refuse. The cost
of being wrong is a run blocked from the one action that would have
worked, so every refusal needs two independent facts behind it, and
anything short of that is a note or silence.

WHAT IS DELIBERATELY NOT REFUSED:
  * page two of a query that is still producing        (pagination)
  * a first, or second, search of a new source         (exploration)
  * anything at all when the goal has no countable shape
  * anything at all while candidates are genuinely short
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

# Discovery tools -- the ones that gather candidates rather than advance
# an item. Refusals only ever apply to these; opening and reading are
# never blocked by this module.
_DISCOVERY_TOOLS = ("browser_extract_records", "browser_extract_table",
                    "web_search")

# A run must hold at least this many candidates before "you have enough,
# go and check them" is a fair thing to say, however small the ask.
MIN_CANDIDATES_TO_ENFORCE = 3


def is_discovery(action: str) -> bool:
    return any(t in (action or "") for t in _DISCOVERY_TOOLS)


def verdict(action: str, phase: Any, disco: Any,
            current_source: str = "") -> Optional[str]:
    """The refusal for this call, or None.

    `phase` is a task_graph.Phase, `disco` a discovery_state.DiscoveryState.
    Returns None for every call that is not a discovery call, for every
    goal without a countable shape, and for every case where the evidence
    is short of certain.
    """
    if not is_discovery(action):
        return None
    if phase is None or not getattr(phase, "active", False):
        return None

    candidates = int(getattr(phase, "candidates", 0) or 0)
    opened = int(getattr(phase, "opened", 0) or 0)
    needed = int(getattr(phase, "needed", 0) or 0)
    unopened = max(0, candidates - opened)

    # 1. ENOUGH IN HAND. The phase already decided this by arithmetic;
    #    all that is added here is a floor, so a tiny ask cannot trigger
    #    a refusal off one or two candidates.
    if (getattr(phase, "name", "") in ("VERIFY", "ASSEMBLE")
            and candidates >= MIN_CANDIDATES_TO_ENFORCE
            and unopened > 0):
        return (
            f"(REFUSED: you already have {unopened} candidate(s) you have "
            f"not opened, and only {needed} more item(s) are needed. "
            f"Gathering another list cannot get you closer — a candidate "
            f"becomes an item by being OPENED and READ on its own page, "
            f"and nothing else counts. Open one of the ones you already "
            f"have. {phase.reason})"
        )

    # 2. THIS WAY IN IS SPENT. Only while still short of candidates --
    #    otherwise rule 1 already applies and says something more useful.
    src = disco.source_of(current_source) if (disco and current_source) else None
    if src is None:
        return None
    strat = (disco.strategy_of(current_source, disco.current_strategy)
             if disco.current_strategy else None)

    source_spent = src.status in ("EXHAUSTED", "BLOCKED")
    strategy_spent = bool(strat and strat.exhausted)
    if not (source_spent or strategy_spent):
        return None

    tried = ", ".join(disco.tried_sources) or current_source
    if source_spent:
        return (
            f"(REFUSED: {current_source} has returned nothing new across its "
            f"last few searches — {src.new_candidates} new candidate(s) in "
            f"{src.calls} call(s), the rest repeats of what you already have. "
            f"Searching it again in different words cannot produce what it "
            f"does not list. Sources tried so far: {tried}. Go to a DIFFERENT "
            f"source — a different site, directory, or the organisations' own "
            f"pages — or open the candidates you already hold.)"
        )
    return (
        f"(REFUSED: this exact search of {current_source} has brought back "
        f"nothing new twice running. Change the search itself, move to a "
        f"different source, or open the candidates you already have — "
        f"repeating this one cannot return anything it has not already "
        f"returned. Sources tried so far: {tried}.)"
    )


def note(phase: Any, disco: Any) -> Optional[str]:
    """A standing line for the prompt: where this run is, in numbers.

    Not a refusal and not an instruction -- the counts themselves, so a
    model choosing its next call is choosing against the same figures
    the code is judging it by. Silent when the goal has no shape.
    """
    if phase is None or not getattr(phase, "active", False):
        return None
    bits = [f"PROGRESS: {phase.verified} verified of {phase.affordable} "
            f"affordable ({phase.requested} requested); "
            f"{phase.candidates} candidate(s) found, {phase.opened} opened"]
    if disco is not None and disco.tried_sources:
        bits.append("sources tried: " + ", ".join(disco.tried_sources))
        spent = disco.exhausted_sources
        if spent:
            bits.append("spent: " + ", ".join(spent))
    bits.append(f"phase: {phase.name}")
    return "(" + " | ".join(bits) + ")"


def verdict_for_open(url: str, phase: Any, disco: Any) -> Optional[str]:
    """The refusal for opening one more item on a source that will not
    let its items be read.

    THE FAILURE THIS ANSWERS. A source answered every search with fifteen
    records and refused every job page behind them. The run tried seven
    times, because "this source has no more candidates" and "this source
    has candidates it will not let you read" were the same state to
    everything watching. They are opposite problems: the first says
    search it differently, the second says its search is irrelevant
    because the task can never be satisfied from it.

    Refused, not advised, and the run is sent back to choosing a source
    -- WITHOUT being told which. The code establishes that this way in
    cannot work; the model knows what else exists.
    """
    if phase is None or not getattr(phase, "active", False):
        return None
    if not url or disco is None:
        return None
    try:
        from backend.app.orchestrator.discovery_state import _host_of
        host = _host_of(url)
    except Exception:  # noqa: BLE001
        return None
    src = disco.source_of(host) if host else None
    if src is None or src.can_verify:
        return None

    tried = ", ".join(disco.tried_sources) or host
    return (
        f"(REFUSED: {host} has let you open {src.item_opens} item page(s) and "
        f"refused {src.item_failures}, and none of them could be read. Its "
        f"SEARCH works — you have {src.new_candidates} candidate(s) from it — "
        f"but the pages behind those results are closed to automated access, "
        f"so no candidate from {host} can ever become a verified item. Trying "
        f"another one cannot change that. Go to a DIFFERENT source: another "
        f"site that lists this kind of thing, or the organisations' own pages. "
        f"Sources tried so far: {tried}. The candidates already found stay on "
        f"the record as discovered-but-unverified.)"
    )
