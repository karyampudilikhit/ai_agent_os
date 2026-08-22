"""Is this answer SENSIBLE, not merely verified?

THE GAP THIS CLOSES. Every other check in this system asks whether the
work was really done: did a tool run, did the page move, was the column
actually reordered, were these rows ever on a page it opened. A run can
pass all of them and still be useless.

It did. Asked for the top 5 weekly gainers, a run correctly sorted
TradingView's `Perf % 1W` column and reported:

    VALV   Shengkai Innovations   +9,999,900.00%   $0.1000
    GENNQ  Genesis Healthcare     +4,699,900.00%   $0.0470
    TREVQ  Trevali Mining           +999,900.00%   $0.0100

Every figure real, the sort genuinely performed, every ticker on the
page. And a ten-million-percent weekly gain is not a gain -- it is a
delisted shell going from $0.000001 to $0.01, or a corporate action, or
a data artefact. The answer was verified and worthless.

WHAT THIS DOES NOT DO. It does not judge whether an answer is CORRECT --
nothing mechanical can. It flags values that are impossible-in-context
and rows whose price says they are not a real instrument, and it tells
the agent to filter and re-read rather than silently dropping them. A
check that quietly edited the data would be a worse version of the
fabrication it exists to prevent.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# A weekly percentage move above this is not a market move. Real
# single-week movers top out in the hundreds of percent; five figures
# means a reverse split, a relisting, or a price that was rounding to
# zero the week before.
IMPLAUSIBLE_PERCENT = 1000.0

# Below this a listing is not something anyone can trade at the reported
# price -- sub-penny quotes are where percentage arithmetic stops meaning
# anything.
MIN_REAL_PRICE = 0.50

# Ticker suffixes exchanges use to mark a security as delisted, in
# bankruptcy, or otherwise not ordinary. Q is the classic bankruptcy
# marker; the trailing " D" is how TradingView renders delisted.
_DEAD_TICKER = re.compile(r"\b[A-Z]{2,6}Q\b|\bD\b\s*$")

_PERCENT = re.compile(r"([−\-+]?\s?\d[\d,]*\.?\d*)\s*%")
_MONEY = re.compile(r"([−\-+]?\s?\d[\d,]*\.?\d+)\s*(?:USD|\$)?")


def _num(text: str) -> Optional[float]:
    if not text:
        return None
    t = str(text).replace("−", "-").replace(",", "").replace(" ", "")
    try:
        return float(t)
    except ValueError:
        return None


def implausible_percentages(text: str) -> List[str]:
    """Percentage figures too large to be a real move.

    ONLY inside a data row. A deliverable that says "I must not report a
    weekly move above 1000%" is quoting its instructions, not reporting a
    figure -- and reading that sentence as data blocked a run for a
    threshold this module itself had asked it to respect. Prose about the
    rule is not a breach of the rule.
    """
    out: List[str] = []
    for line in (text or "").splitlines():
        # A data row: pipe-delimited, and led by something that looks
        # like an identifier rather than a sentence.
        if line.count("|") < 3:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        head = cells[0].split()[0] if cells and cells[0].split() else ""
        if not re.match(r"^[A-Z][A-Z0-9.-]{0,8}$", head):
            continue
        for raw in _PERCENT.findall(line):
            v = _num(raw)
            if v is not None and abs(v) >= IMPLAUSIBLE_PERCENT:
                shown = f"{raw.strip()}%"
                if shown not in out:
                    out.append(shown)
    return out


def untradeable_rows(text: str) -> List[str]:
    """Markdown table rows whose price is below any tradeable level."""
    out: List[str] = []
    for line in (text or "").splitlines():
        if line.count("|") < 3:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        label = cells[0].split()[0] if cells[0].split() else ""
        if not re.match(r"^[A-Z][A-Z0-9.\-]{0,8}$", label):
            continue
        for cell in cells[1:]:
            m = _MONEY.search(cell)
            if not m:
                continue
            v = _num(m.group(1))
            # A price, not a percentage or a volume: small, positive, and
            # not carrying a % sign.
            if v is not None and 0 < v < MIN_REAL_PRICE and "%" not in cell:
                if label not in out:
                    out.append(label)
                break
    return out


def check(task: str, text: str) -> Optional[str]:
    """A problem worth blocking on, or None.

    Only applies to a task that asked for a RANKING -- "top", "best",
    "biggest". Ranking is where an outlier stops being a curiosity and
    becomes the entire answer, because sorting by a broken figure puts
    the broken rows at the top by construction.
    """
    try:
        from backend.app.orchestrator.output_contract import task_wants_ranking
    except Exception:  # noqa: BLE001
        return None
    if not task_wants_ranking(task or ""):
        return None

    wild = implausible_percentages(text)
    dead = untradeable_rows(text)
    if not wild and len(dead) < 2:
        return None

    bits = []
    if wild:
        bits.append(
            f"reports moves of {', '.join(wild[:3])} — a weekly change above "
            f"{IMPLAUSIBLE_PERCENT:.0f}% is not a market move but a reverse "
            f"split, a relisting, or a price that was rounding to zero"
        )
    if len(dead) >= 2:
        bits.append(
            f"ranks securities trading below ${MIN_REAL_PRICE:.2f} "
            f"({', '.join(dead[:5])}) — at those prices the percentage "
            f"arithmetic stops meaning anything"
        )
    return (
        "the ranking is topped by figures that cannot be real: "
        + "; and ".join(bits)
        + ". Sorting an unfiltered screener by percentage change returns "
          "delisted and sub-penny shells by construction. Apply a minimum "
          "price or market-cap filter on the page and read the ranking again"
    )


RETRY_NOTE = (
    "(THE RANKING YOU HAVE IS NOT USABLE. The rows at the top are moves of "
    "thousands of percent on securities priced at fractions of a cent — "
    "delisted shells and corporate actions, not market movers. Sorting an "
    "unfiltered screener by percentage change returns these by construction, "
    "so the sort worked and the answer is still wrong. FILTER THE PAGE "
    "FIRST: set a minimum price (say $5) or a minimum market cap using the "
    "screener's own filter controls, then sort and read again. Do not simply "
    "delete the bad rows from what you report — the next ones down are the "
    "same kind of thing.)"
)
