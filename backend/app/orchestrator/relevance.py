"""Is this item the thing that was actually asked for?

THE FAILURE THIS ANSWERS. Asked for ten Product Manager internships, a
run reported "Fashion Merchandising Intern" among them. Every guard in
the system passed it. The provenance ledger agreed the page had been
fetched; the row-backing check agreed the title appeared in text the run
really read; the plausibility check found nothing implausible about it.
All of them were right. None of them was asking the question a person
would have asked first, which is whether a fashion merchandising
internship is a product manager internship.

WHAT THIS CHECKS, AND WHAT IT DELIBERATELY DOES NOT. It checks that the
words distinguishing what was ASKED FOR appear somewhere in what was
READ from that item's own page. It does not attempt to understand the
item, judge its quality, or decide whether the founder would like it. A
model could do those things; a model is exactly what this codebase does
not let decide whether its own work counts.

MATCHED AGAINST THE WHOLE PAGE, NOT THE TITLE. "Associate PM Intern" is
a product manager internship whose title says so in initials, and its
page body will say "product management" several times. Matching titles
alone would reject it, and a check that rejects real items produces a
dishonest shortfall -- "2 of 10 verified" when eight were fine is its
own kind of lie. Full page text is what makes an ALL-TERMS rule
affordable, and all-terms is what keeps fashion merchandising out.

SILENCE IS THE DEFAULT, as everywhere else here. A task whose subject
cannot be read out of the founder's own words yields no terms, and no
terms means every item passes -- exactly today's behaviour. A wrong
relevance rule is worse than none: it would reject real work
confidently, and the founder would have no way to see why.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# Where the subject phrase ENDS. Everything after one of these is a
# different clause about the items -- when they were posted, what to do
# with them, which fields to pull -- not part of what they ARE.
_CLAUSE_END = re.compile(
    r"\b(?:posted|published|listed|advertised|updated|released"
    r"|visit|open|extract|collect|gather|scrape|read|check"
    r"|remove|dedupe|deduplicate|rank|sort|order|compare|analyse|analyze"
    r"|prepare|apply|write|summar\w*|give|return|report|show|tell"
    r"|that|which|who|whose|with(?:in)?|from|over|during|between)\b",
    re.IGNORECASE,
)

# A location tail: "in India", "in the US", "across Europe". Split off
# rather than dropped -- where an item is, is a real constraint, but it
# is one an item's page states differently ("Bangalore") from the way
# the task states it ("India"), so it cannot be a required term.
_LOCATION_TAIL = re.compile(
    r"\s+(?:in|across|around|within|based\s+in|located\s+in|from)\s+"
    r"(?:the\s+)?([A-Z][A-Za-z.\-]*(?:\s+[A-Z][A-Za-z.\-]*){0,3})\s*$"
)

_NUMBER_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    "|fifteen|twenty|thirty|fifty|hundred"
)
# The count, and everything after it. "top 5 gainers", "find 20 recently
# posted PM internships", "ten companies".
_AFTER_COUNT = re.compile(
    r"\b(?:top|first|best|find|list|get|give\s+me|show\s+me)?\s*"
    r"(?:\d{1,3}|" + _NUMBER_WORDS + r")\s+(.{3,120})",
    re.IGNORECASE | re.DOTALL,
)

# Words that carry no subject. Kept SHORT on purpose: every word removed
# is a way for two different asks to look identical, and a subject that
# collapses too far stops distinguishing anything.
_STOP = {
    "a", "an", "the", "this", "that", "these", "those", "and", "or",
    "of", "for", "to", "on", "in", "at", "by", "with", "from", "into",
    "their", "its", "his", "her", "our", "your", "my",
    "recently", "recent", "new", "newest", "latest", "current", "currently",
    "good", "best", "top", "great", "relevant", "suitable", "available",
    "valid", "verified", "real", "actual", "genuine",
    "please", "some", "any", "all", "each", "every", "other", "more",
}

# Nouns that name the CONTAINER rather than the subject. "10 companies",
# "20 listings", "5 results" say how the answer is packaged, not what is
# in it. They still count as terms an item may match, but they do not
# count toward "is this ask specific enough to enforce" -- otherwise
# "find 10 companies" would enforce the single word "company" against
# every page and reject anything that phrased it "firm" or "vendor".
_CONTAINER_NOUNS = {
    "item", "items", "thing", "things", "result", "results", "record",
    "records", "entry", "entries", "listing", "listings", "posting",
    "postings", "post", "posts", "row", "rows", "option", "options",
    "candidate", "candidates", "example", "examples", "one", "ones",
    "company", "companies", "business", "businesses", "firm", "firms",
    "site", "sites", "website", "websites", "page", "pages",
    "job", "jobs", "role", "roles", "position", "positions",
    "opening", "openings", "opportunity", "opportunities",
}

# How many distinctive (non-container) terms an ask needs before this
# check is allowed to reject anything. One word is not a specification --
# "10 jobs" plus "job" would reject every page that said "vacancy".
MIN_DISTINCTIVE_TERMS = 2


def _stem(word: str) -> str:
    """Deliberately a local copy, not shared with strategy_memory.

    That module stems UI vocabulary and drops words like "button" and
    "control"; this one stems business vocabulary and must keep them.
    Two callers wanting different stopword sets is not one function with
    a flag -- it is two functions that happen to share eight lines.
    """
    w = word.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("ses"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    # management -> manag, manager -> manag. The single most common
    # shape in a role title, and the reason "Product Management Intern"
    # and "Product Manager Intern" have to read as one ask.
    #
    # "ship" is here for internship -> intern, which is not cosmetic: a
    # task says "internships" and the page that proves one says "Intern"
    # in its title. Without it every genuine internship failed the check
    # on the very word that made it an internship.
    for suffix in ("ship", "ement", "ment", "ing", "er", "or", "al", "ive"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


def _words(text: str) -> List[str]:
    return [w for w in re.split(r"[^A-Za-z0-9]+", text or "") if w]


@dataclass
class Subject:
    """What the founder asked for, in terms an item's page can be
    checked against."""

    phrase: str = ""
    terms: Set[str] = field(default_factory=set)
    distinctive: Set[str] = field(default_factory=set)
    location: str = ""
    # False when the ask was not specific enough to reject anything on.
    # The caller MUST then accept every item, exactly as before this
    # module existed.
    enforceable: bool = False

    def summary(self) -> str:
        bits = [f"subject={self.phrase!r}"]
        if self.distinctive:
            bits.append("must carry: " + ", ".join(sorted(self.distinctive)))
        if self.location:
            bits.append(f"location={self.location}")
        if not self.enforceable:
            bits.append("NOT ENFORCEABLE (too generic)")
        return "; ".join(bits)


def subject_of(task: str, spec: Any = None) -> Subject:
    """Read the subject of a counted ask out of the founder's words.

    Never raises. Returns an unenforceable Subject whenever the wording
    does not clearly name what is being counted.
    """
    text = (task or "").strip()
    subj = Subject()
    if not text:
        return subj

    # Only counted asks have a subject worth checking. A one-off
    # question ("what is X") has nothing to reject against.
    try:
        wanted = int(getattr(spec, "item_count", 0) or 0) if spec is not None else 0
    except (TypeError, ValueError):
        wanted = 0
    if spec is not None and (not getattr(spec, "confident", False) or wanted < 2):
        return subj

    m = _AFTER_COUNT.search(text)
    if not m:
        return subj

    tail = m.group(1)
    # Cut at the first sentence break, then at the first clause word.
    tail = re.split(r"[,.;:\n]", tail, 1)[0]
    clause = _CLAUSE_END.search(tail)
    if clause:
        tail = tail[: clause.start()]
    tail = tail.strip()
    if not tail:
        return subj

    loc = _LOCATION_TAIL.search(tail)
    if loc:
        subj.location = loc.group(1).strip()
        tail = tail[: loc.start()].strip()

    subj.phrase = tail
    for raw in _words(tail):
        if len(raw) < 2:
            continue
        low = raw.lower()
        if low in _STOP:
            continue
        stem = _stem(raw)
        if not stem:
            continue
        subj.terms.add(stem)
        if low not in _CONTAINER_NOUNS and _stem(low) not in {
            _stem(c) for c in _CONTAINER_NOUNS
        }:
            subj.distinctive.add(stem)

    subj.enforceable = len(subj.distinctive) >= MIN_DISTINCTIVE_TERMS
    return subj


def evidence_terms(evidence: str) -> Set[str]:
    """Every stem present in what was actually read from a page."""
    return {_stem(w) for w in _words(evidence) if len(w) > 1}


def judge(evidence: str, subj: Subject) -> Tuple[bool, str]:
    """(relevant, why). True whenever this cannot honestly say otherwise.

    ALL distinctive terms must be present, which is only affordable
    because `evidence` is a whole page rather than a title -- see the
    module docstring. An item that carries every word the ask used to
    distinguish itself is the thing that was asked for, as far as
    anything short of judgement can tell.
    """
    if not subj.enforceable:
        return True, ""
    if not (evidence or "").strip():
        return False, "nothing was read from this item's own page"

    have = evidence_terms(evidence)
    missing = sorted(t for t in subj.distinctive if t not in have)
    if not missing:
        return True, ""
    return False, (
        "this item's page never mentions "
        + ", ".join(missing)
        + f" — the task asked for {subj.phrase!r}"
    )
