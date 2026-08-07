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
    r"\b(?:cannot|can't|unable to)\b[^\n]{0,140}\buntil\b[^\n]{0,140}"
    r"\b(?:provided|supplied|uploaded|shared|available|received)\b",
    r"\b(?:notify|tell|let)\s+us\s+(?:know\s+)?(?:once|when|after)\b",
    r"\bonce\s+(?:you|the\s+\w+)\s+(?:provide|send|upload|share|supply)[a-z]*\b",
    r"\bonce\s+(?:the|this|that)\b[^.\n]{0,80}\bis\s+(?:provided|supplied|uploaded|received)\b",
    r"\b(?:has|have)\s+not\s+been\s+(?:provided|supplied|uploaded|shared)\b",
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


def detect_handback(text: str) -> List[str]:
    """Return the offending phrases (with a little surrounding context),
    or [] when the deliverable stands on its own. Empty list is the
    normal, healthy case."""
    if not text:
        return []
    found: List[str] = []
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
