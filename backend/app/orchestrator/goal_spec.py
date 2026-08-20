"""What the founder actually asked for, as an object rather than a string.

THE GAP THIS FILLS. Until now the goal existed only as text inside a
prompt. Nothing in the system could answer "how many items were asked
for", "what must be true of each one", or "what would finished look
like" -- so nothing could plan, budget, or measure progress. Every other
orchestration weakness in the audit was downstream of that.

Measured: a run asked for 10 internships, each verified on its own page.
It spent all twenty of its calls on results pages, opened none of the ten,
and could not say so, because "10" and "each on its own page" existed
nowhere except in prose the model had read once.

DETERMINISTIC FIRST, LLM SECOND. A spec is extracted from the founder's
own words by rule. An LLM may REFINE it (propose_refinement), but never
authors it unsupervised and never edits it mid-run: a model that can
rewrite its own finish line has no finish line. The rules here are dull
on purpose -- counts, per-item phrasing, recency windows -- because a
wrong spec is worse than no spec. It would execute the wrong plan
confidently, where today the system merely wanders.

SILENCE IS THE DEFAULT. When the wording does not clearly state a goal
shape, `from_task` returns a spec with `confident=False` and the caller
must behave exactly as it does today. Nothing here is allowed to make an
existing run worse.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# How many items the founder asked for: "10 internships", "top 5 gainers",
# "five recent listings". Words as well as digits, because both appear.
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20,
}
_COUNT_RE = re.compile(
    r"\b(?:top|first|best|find|give\s+me|list|get)?\s*"
    r"(\d{1,3}|" + "|".join(_WORD_NUMBERS) + r")\s+"
    r"(?!of\b|out\b)([a-z][a-z\- ]{2,40}?)\b",
    re.IGNORECASE,
)

# Phrases that make the work PER ITEM rather than per page. This is the
# distinction the internship run could not represent: reading a results
# list is not visiting ten job pages, and the cost differs by a factor of
# twenty.
_PER_ITEM_RES = (
    re.compile(r"\bvisit\s+each\b", re.IGNORECASE),
    re.compile(r"\bopen\s+each\b", re.IGNORECASE),
    re.compile(r"\beach\s+(?:job|item|listing|page|result|candidate|one)\b",
               re.IGNORECASE),
    re.compile(r"\bevery\s+(?:job|item|listing|page|result|candidate)\b",
               re.IGNORECASE),
    re.compile(r"\b(?:its|their)\s+own\s+page\b", re.IGNORECASE),
    re.compile(r"\bfor\s+each\b", re.IGNORECASE),
)

# A recency window: "last 7 days", "past week", "posted recently".
_RECENCY_RE = re.compile(
    r"\b(?:last|past|within(?:\s+the)?)\s+(\d{1,3})\s*(day|week|month)s?\b",
    re.IGNORECASE,
)

# Fields asked for per item. Only the ones a page can actually carry.
_FIELD_WORDS = (
    "company", "role", "title", "location", "date", "url", "link",
    "price", "ticker", "name", "salary", "stipend", "duration",
    "percentage", "change", "score", "author", "summary",
)

# Ranking words. Reused rather than re-derived: output_contract already
# owns this question and two definitions would drift.
_RANK_HINT = ("top ", "best ", "worst ", "highest", "lowest", "gainers",
              "losers", "ranked", "sorted", "sort by")


@dataclass
class GoalSpec:
    """One founder request, in terms the orchestrator can act on."""

    objective: str = ""
    # How many distinct items the answer must contain. 1 for a single
    # fact ("the first three sentences" is ONE answer, not three items).
    item_count: int = 1
    # True when the task requires acting on each item separately -- the
    # difference between reading a list and opening ten pages.
    per_item_work: bool = False
    # Fields each item must carry.
    per_item_fields: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    wants_ranking: bool = False
    recency_days: Optional[int] = None
    # False when the wording did not clearly state a shape. The caller
    # MUST then behave exactly as it did before this module existed.
    confident: bool = False
    source: str = "rules"

    def summary(self) -> str:
        bits = [f"{self.item_count} item(s)"]
        if self.per_item_work:
            bits.append("each visited individually")
        if self.per_item_fields:
            bits.append("fields: " + ", ".join(self.per_item_fields[:6]))
        if self.recency_days:
            bits.append(f"posted within {self.recency_days} day(s)")
        if self.wants_ranking:
            bits.append("ranked")
        return "; ".join(bits)


def _count_in(task: str) -> int:
    """How many items were asked for.

    Takes the LARGEST plausible count rather than the first: "give me the
    top 5 gainers and the top 5 losers" is ten items, and a task naming
    several numbers usually means the biggest one.
    """
    best = 0
    for m in _COUNT_RE.finditer(task or ""):
        raw = m.group(1).lower()
        n = _WORD_NUMBERS.get(raw)
        if n is None:
            try:
                n = int(raw)
            except ValueError:
                continue
        noun = m.group(2).lower().strip()
        # "last 7 days" and "within 3 months" are windows, not counts.
        if noun.startswith(("day", "week", "month", "year", "hour",
                            "minute", "second", "sentence", "paragraph",
                            "line", "word")):
            continue
        if 1 <= n <= 100:
            best = max(best, n)
    return best


def _fields_in(task: str) -> List[str]:
    low = (task or "").lower()
    out = []
    for w in _FIELD_WORDS:
        if re.search(r"\b" + re.escape(w) + r"\b", low) and w not in out:
            out.append(w)
    return out


def from_task(task: str) -> GoalSpec:
    """Extract a spec from the founder's own words. Never raises.

    `confident` is the load-bearing field: False means the caller carries
    on exactly as before.
    """
    text = (task or "").strip()
    spec = GoalSpec(objective=text[:400])
    if not text:
        return spec

    count = _count_in(text)
    per_item = any(rx.search(text) for rx in _PER_ITEM_RES)
    fields = _fields_in(text)
    low = text.lower()

    spec.item_count = count if count else 1
    spec.per_item_work = per_item
    spec.per_item_fields = fields
    spec.wants_ranking = any(w in low for w in _RANK_HINT)

    m = _RECENCY_RE.search(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        spec.recency_days = n * (1 if unit == "day" else 7 if unit == "week" else 30)
        spec.constraints.append(f"posted within {spec.recency_days} days")

    # CONFIDENT ONLY WHEN THE SHAPE IS UNAMBIGUOUS.
    #
    # A count of items, or explicit per-item work. "Give me the first
    # three sentences" has a number and no items -- one answer, and
    # _count_in already refuses sentence/paragraph nouns. Everything else
    # falls through to today's behaviour on purpose.
    spec.confident = bool(count >= 2 or per_item)
    return spec


# ---------------------------------------------------------------- LLM

_REFINE_PROMPT = """You are reading a founder's request and describing its SHAPE.
Do not perform it. Reply with JSON only.

REQUEST:
{task}

RULES-BASED READING (may be wrong or incomplete):
{rules}

Reply with this exact JSON shape:
{{"item_count": <int>, "per_item_work": <true|false>,
  "per_item_fields": ["..."], "constraints": ["..."]}}

item_count: how many DISTINCT items the answer must contain (1 if the
answer is a single fact, however many sentences it runs to).
per_item_work: true only if each item must be opened or acted on
separately, rather than read off one list."""


def propose_refinement(task: str, adapter: Any,
                       base: Optional[GoalSpec] = None) -> GoalSpec:
    """Let a model refine a rules-derived spec. Optional, and safe.

    The model may only adjust NUMBERS and FLAGS on a spec the rules
    already built; it never authors one from nothing and never sees this
    again mid-run. Any failure returns the rules-based spec unchanged --
    an unavailable model must not change what a run does.
    """
    spec = base or from_task(task)
    if adapter is None:
        return spec
    try:
        import json
        raw = adapter.chat_completion(
            _REFINE_PROMPT.format(task=(task or "")[:1500], rules=spec.summary()),
            temperature=0.0, max_tokens=400)
        blob = raw[raw.find("{"): raw.rfind("}") + 1]
        data = json.loads(blob)
    except Exception as exc:  # noqa: BLE001
        logger.info("goal spec refinement unavailable (%s) — keeping rules", exc)
        return spec

    try:
        n = int(data.get("item_count", spec.item_count))
        if 1 <= n <= 100:
            spec.item_count = n
        if isinstance(data.get("per_item_work"), bool):
            spec.per_item_work = data["per_item_work"]
        fields = data.get("per_item_fields")
        if isinstance(fields, list) and fields:
            spec.per_item_fields = [str(f)[:30] for f in fields[:10]]
        cons = data.get("constraints")
        if isinstance(cons, list):
            for c in cons[:8]:
                c = str(c)[:80]
                if c and c not in spec.constraints:
                    spec.constraints.append(c)
        spec.source = "rules+llm"
        spec.confident = spec.confident or spec.item_count >= 2 or spec.per_item_work
    except Exception:  # noqa: BLE001
        return spec
    return spec
