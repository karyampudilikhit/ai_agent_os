"""Mechanical detector for the one failure this product cannot ship:
a deliverable that hands the work back to the founder.

The universal playbook rules already forbid this in several forms — no
how-to guides, no asking for credentials, no "please upload the data"
(see employees/playbooks._UNIVERSAL_RULES). Those rules are in the
prompt for every employee INCLUDING the CEO, and they keep getting
ignored anyway. Asked to find five quant research papers, a full
company run replied:

    "We cannot yet give you a vetted top-5 list because the necessary
     source list has not been provided to the Quantitative Research
     team. Research Associate: Upload a concise spreadsheet containing
     all candidate papers... Please forward the candidate list at your
     earliest convenience."

— for a request that a single web search answers. Every rule against
that was already in its context window.

The pattern across this codebase is consistent: prompt rules are
advisory and a weak local model treats them as such, while mechanical
checks hold. So this doesn't ask the model to behave — it reads the
finished draft, and a hit forces the existing refinement loop to reject
and rewrite it (see critique_agent.needs_refinement and
pipeline_controller's refine loop).

FALSE POSITIVES ARE THE REAL RISK. A legitimate deliverable can say
"users upload a CSV" while describing a product, or "provide your team
with training". Blocking those would be worse than the bug. So every
pattern below requires the SECOND-PERSON REQUEST framing that makes it
a hand-back — the AI asking the founder to give it something before it
can continue — not merely the presence of a word like "upload".
"""

from __future__ import annotations

import re
from typing import List

# Each pattern must encode "I am asking YOU to give ME something, or I
# am blocked until you do". Anchored deliberately tightly; a missed
# hand-back is recoverable, a blocked good deliverable is not.
_HANDBACK_PATTERNS = [
    # Direct requests aimed at the reader.
    r"\bplease\s+(?:provide|send|share|upload|forward|supply|attach)\b",
    r"\bkindly\s+(?:provide|send|share|upload|forward|supply)\b",
    r"\b(?:could|can)\s+you\s+please\s+(?:provide|send|share|upload)\b",
    r"\bat\s+your\s+earliest\s+convenience\b",

    # "We're blocked until you hand something over."
    # Gap allows '.' on purpose — the first real example of this was
    # "...until the cleaned price file (clean_prices.csv) is uploaded",
    # where a filename's dot broke an earlier sentence-bounded version.
    # Still line-bounded and length-capped so it can't span paragraphs.
    #
    # The verb list below was widened after a live test slipped through
    # on ONE missing word. A full quant run shipped "cannot be judged for
    # edge yet because the required historical price dataset HAS NOT BEEN
    # LOADED" — a textbook blocked-until hand-back that scored zero hits,
    # because the list covered provided/supplied/uploaded/shared and not
    # "loaded". The lesson generalises: this set has to cover how DATA
    # arrives, not only how a person hands something over.
    r"\b(?:cannot|can't|unable to)\b[^\n]{0,140}\buntil\b[^\n]{0,140}"
    r"\b(?:provided|supplied|uploaded|shared|available|received|loaded|"
    r"fetched|downloaded|populated|ingested|initialised|initialized|"
    r"configured|connected)\b",
    r"\b(?:notify|tell|let)\s+us\s+(?:know\s+)?(?:once|when|after)\b",
    r"\bonce\s+(?:you|the\s+\w+)\s+(?:provide|send|upload|share|supply)[a-z]*\b",
    r"\bonce\s+(?:the|this|that)\b[^.\n]{0,80}\bis\s+(?:provided|supplied|uploaded|received)\b",
    r"\b(?:has|have)\s+not\s+been\s+(?:provided|supplied|uploaded|shared|"
    r"loaded|fetched|downloaded|populated|ingested|initialised|initialized|"
    r"configured|connected|executed|run)\b",
    r"\bawaiting\s+(?:your|the founder's)\b",
    r"\bpending\s+(?:your|founder)\s+(?:input|upload|response|confirmation)\b",

    # Assigning the founder homework.
    r"\byou\s+(?:must|need to|should|will need to)\s+"
    r"(?:provide|upload|send|share|supply|forward)\b",
    r"\bwe\s+(?:need|require)\s+you\s+to\s+(?:provide|upload|send|share)\b",
    r"\bto\s+be\s+(?:uploaded|provided|supplied)\s+by\s+(?:you|the founder)\b",
    r"\bforward\s+the\s+\w+\s+list\b",

    # Assigning the work to staff who do not exist. This is the same
    # failure wearing a manager's hat, and the request-shaped patterns
    # above all missed it — a deliverable ended with "Assign a senior
    # quant to extract formulas from papers 1-4" and "Schedule a
    # 30-minute review meeting next week", which is a plan for someone
    # else to do the work rather than the work.
    #
    # Anchored on the imperative verb plus a person-shaped object, so
    # ordinary advice ("assign a budget", "schedule the job weekly")
    # doesn't trip it.
    r"\bassign\s+(?:a|an|the|one|two|\d+)\s+[\w\- ]{0,30}?"
    r"(?:analyst|engineer|quant|developer|designer|writer|researcher|"
    r"specialist|manager|lead|scientist|team|member|colleague|staff)\b",
    r"\bhave\s+(?:a|an|the|your)\s+[\w\- ]{0,30}?"
    r"(?:analyst|engineer|quant|developer|team|lead)\s+(?:review|extract|"
    r"build|prepare|verify|check|complete)\b",
    r"\bschedule\s+(?:a|an|the)\s+[\w\- ]{0,30}?(?:meeting|call|review|sync|"
    r"session|workshop)\b",
    r"\bdelegate\s+(?:this|that|it|the\s+\w+)\s+to\b",
    r"\bwe(?:'ll| will)\s+coordinate\s+the\s+appropriate\s+specialist\b",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _HANDBACK_PATTERNS]

# Cap so one badly-shaped draft can't produce a hundred-line issue list.
MAX_REPORTED = 5


# ---------------------------------------------------------------------
# The deferred-work hand-back: "here is your to-do list"
#
# The patterns above all look for a REQUEST ("please provide X"). Two
# live tests shipped a hand-back with no request in it at all, and both
# scored zero hits. They looked like this:
#
#   **What to do next**
#   1. Approve the data pull: run the FMP API calls for every ticker...
#   2. Verify the CSV...
#   3. Execute the back-test...
#
#   **What to do next**
#   - Verify the correct `game_id`...
#   - Run `arc_reset` with the valid ID...
#
# That is the assigned work, handed back as instructions. It is the most
# common shape this failure takes and it was completely invisible.
#
# A plain "Next steps" match would be far too broad — a good strategy
# deliverable is ALLOWED to recommend actions, and blocking those is
# worse than the bug (see the module docstring). So this requires
# CO-OCCURRENCE with the deliverable admitting the work was not done.
# A genuine recommendation doesn't come attached to "no back-test,
# Sharpe, or draw-down numbers exist"; a hand-back always does.
_NEXT_STEPS_HEADING = re.compile(
    r"^[\s#>*_-]*(?:what\s+to\s+do\s+next|next\s+steps?|"
    r"recommended\s+(?:next\s+)?(?:actions?|steps?)|action\s+items?)\b",
    re.IGNORECASE | re.MULTILINE,
)

# Imperatives that describe EXECUTING THE ASSIGNED WORK, deliberately
# not business advice ("hire", "launch", "negotiate" are absent on
# purpose — telling a founder to hire someone is a legitimate
# recommendation, telling them to run the backtest is not).
_WORK_IMPERATIVE = re.compile(
    r"^\s*(?:[-*•–]|\d+[.)])\s*(?:\*\*|`)?\s*"
    r"(approve|execute|re-?run|run|verify|confirm|populate|fetch|download|"
    r"pull|load|ingest|install|configure|obtain|gather|collect|compute|"
    r"calculate|perform|resume|retry|acquire|align|assemble|prepare|"
    r"implement|clean|build|extend|supply|provide)\b",
    re.IGNORECASE | re.MULTILINE,
)

# The admission that the work did not happen.
#
# This list has to track the blocked-until verbs above, and did not: a
# quant re-run shipped "full S&P 500 price data not yet LOADED" and
# "cannot be JUDGED yet" under a "What to do next" heading, and scored
# zero — because neither verb was here, even though "loaded" had just
# been added to the pattern above. Same omission, two places. Anything
# describing work NOT HAPPENING belongs in both.
#
# 'unavailable' and 'N/A' are matched standalone: they are already
# negations, so requiring a preceding "not" misses the most common way
# a results table admits it has no results.
_WORK_NOT_DONE = re.compile(
    r"\b(?:no|not|never|cannot|can't|could\s+not|couldn't|unable\s+to|"
    r"has\s+not\s+been|have\s+not\s+been|was\s+not|were\s+not)\b[^\n]{0,90}?"
    r"\b(?:exists?|executed|run|ran|completed|performed|available|produced|"
    r"generated|obtained|retrieved|initiali[sz]ed|discovered|calculated|"
    r"loaded|fetched|downloaded|acquired|gathered|collected|judged|"
    r"evaluated|assessed|measured|determined|populated|computed|built)\b"
    r"|\bunavailable\b|(?<![\w/])N/A(?![\w/])",
    re.IGNORECASE,
)


def _detect_deferred_work(text: str) -> List[str]:
    """Catch a to-do list handed to the founder in place of the work.

    Requires BOTH the instruction shape AND an admission that the work
    was not done, because either alone produces false positives on
    legitimate deliverables that recommend actions.
    """
    not_done = _WORK_NOT_DONE.search(text)
    if not not_done:
        return []

    imperatives = list(_WORK_IMPERATIVE.finditer(text))
    if not imperatives:
        return []

    heading = _NEXT_STEPS_HEADING.search(text)
    # A heading plus one instruction is enough; without a heading, want
    # a run of them before calling it a to-do list.
    if not heading and len(imperatives) < 3:
        return []

    verbs = ", ".join(sorted({m.group(1).lower() for m in imperatives})[:6])
    admission = " ".join(text[not_done.start():not_done.end() + 40].split())
    label = (
        f"deferred work: {len(imperatives)} instruction(s) to the founder "
        f"({verbs}) alongside \"{admission}\""
    )
    return [label]


def detect_handback(text: str) -> List[str]:
    """Return the offending phrases (with a little surrounding context),
    or [] when the deliverable stands on its own. Empty list is the
    normal, healthy case."""
    if not text:
        return []
    # Checked first: this is the shape that shipped past every other
    # pattern in both live tests, so it should lead the issue list the
    # refinement pass sees.
    found: List[str] = list(_detect_deferred_work(text))
    seen: set = set()
    for pattern in _COMPILED:
        for match in pattern.finditer(text):
            start = max(0, match.start() - 40)
            end = min(len(text), match.end() + 60)
            snippet = " ".join(text[start:end].split())
            key = match.group(0).lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(snippet)
            if len(found) >= MAX_REPORTED:
                return found
    return found


def handback_correction(matches: List[str]) -> str:
    """The instruction handed to the refinement pass. Deliberately
    concrete about what to do INSTEAD — telling a model only what not to
    write tends to produce the same thing with softer wording."""
    quoted = "; ".join(f'"{m}"' for m in matches[:3])
    return (
        "CRITICAL — this draft hands the work back to the founder instead "
        f"of doing it. Offending passages: {quoted}. "
        "Rewrite so it does NOT ask the founder to provide, upload, send or "
        "confirm anything, and does not say the work is blocked pending "
        "their input. You have web search, web fetch and a browser: go get "
        "whatever you were about to ask for. Deliver the actual answer with "
        "real, specific values. If some part genuinely cannot be obtained, "
        "state that in ONE line, deliver everything you DID get, and never "
        "make the founder's next action a prerequisite for your own work."
    )
