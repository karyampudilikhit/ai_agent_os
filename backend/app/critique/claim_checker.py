"""claim_checker — catch a deliverable blaming something it never tried.

The worst output either live test produced was not a wrong number or a
missing section. It was this, shipped as the headline of an ARC-AGI-3 run:

    "the supplied game_id (vc33-5430563c) does not exist on the ARC
     server"

The id was correct and present in the prompt. The agent had made 8
attempts to open the game and passed a different, invented value every
time — 0, 1, 2, 3, "default", "arcade", "test", "arcade3". It never once
sent the id it was blaming.

Why that shape matters more than an ordinary error: it is a CONFIDENT
DIAGNOSIS POINTING AT SOMEONE ELSE'S SYSTEM. A founder reading it goes
and debugs ARC, or files a bug against a vendor, or concludes the input
they gave was wrong. A blank failure wastes a minute; this wastes an
afternoon and erodes trust in every other line of the report.

It is also mechanically detectable, which is the whole point. The claim
names an identifier; the tool ledger knows every identifier ever sent.
If the deliverable blames a value that appears in ZERO tool calls, the
run is contradicting itself and the draft should be rewritten.

Same discipline as handback_detector and source_ledger: this asks
recorded facts, not a model's opinion, and it is tuned so a false
accusation is far harder than a miss. A claim is only flagged when the
identifier is specific enough to be unambiguous and genuinely absent
from every call.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

# Phrases that assert a CAUSE about something external. Each captures
# the subject being blamed, so it can be checked against the ledger.
#
# Anchored on quoted/backticked/parenthesised identifiers and
# code-like tokens, because those are the ones a ledger lookup can be
# confident about. Blaming "the dataset" in prose is not checkable and
# is deliberately left alone.
_BLAME_VERB = re.compile(
    r"\b(?:does\s+not\s+exist|doesn'?t\s+exist|(?:was|were|is|are)\s+not\s+found|"
    r"not\s+found|was\s+rejected|(?:is|was)\s+invalid|is\s+not\s+valid|"
    r"(?:is|was)\s+missing|could\s+not\s+be\s+found|(?:is|was)\s+unavailable|"
    r"returned\s+404|no\s+such\b)",
    re.IGNORECASE,
)

# Identifier-shaped tokens: quoted, backticked, parenthesised, or bare
# code-like. The blamed subject is whichever of these sits closest
# before the blame verb.
_IDENT = re.compile(
    r"[`'\"(\[]\s*([A-Za-z0-9][A-Za-z0-9._\-]{3,80}?)\s*[`'\")\]]"
    r"|(?<![\w./-])([A-Za-z0-9]+(?:[._\-][A-Za-z0-9]+)+)(?![\w/-])"
)

# How far back from the blame verb to look for the subject being blamed.
LOOKBACK_CHARS = 90

# Values that look like identifiers but are too generic to check
# meaningfully — flagging on these would produce noise.
_UNCHECKABLE = {
    "none", "null", "true", "false", "data", "dataset", "file", "path",
    "url", "http", "https", "the", "this", "that", "value", "input",
    "output", "result", "error", "unknown", "n/a", "na", "tbd",
}

MAX_REPORTED = 3


def _candidates(text: str) -> List[str]:
    """Identifiers the text blames, nearest-to-the-verb first.

    Scanning BACKWARDS from each blame verb matters. The real sentence
    was "the supplied `game_id` (vc33-5430563c) does not exist", and a
    forward-matching regex captured `game_id` — a parameter name that
    genuinely does appear in every call — so the contradiction was
    missed entirely. Every candidate in the window is collected, closest
    first, so the parameter name no longer masks the value.
    """
    out: List[str] = []
    seen = set()
    for verb in _BLAME_VERB.finditer(text or ""):
        window = text[max(0, verb.start() - LOOKBACK_CHARS):verb.start()]
        found = [
            (m.group(1) or m.group(2) or "").strip().strip(".,;:")
            for m in _IDENT.finditer(window)
        ]
        for val in reversed(found):  # nearest to the verb first
            low = val.lower()
            if not val or low in _UNCHECKABLE or len(val) < 4 or low in seen:
                continue
            # Require SOME structure — a bare English word is almost
            # never an identifier, and treating one as such is how a
            # false accusation would happen.
            if not re.search(r"[0-9._\-]", val):
                continue
            seen.add(low)
            out.append(val)
    return out


def detect_contradicted_claims(text: str, ledger: Optional[object] = None) -> List[str]:
    """Return descriptions of causes the deliverable asserts about
    identifiers this run never actually used. Empty list is the normal,
    healthy case.

    Never raises: a broken checker must degrade to "nothing found", not
    take down a run at synthesis time.
    """
    if not text:
        return []
    try:
        if ledger is None:
            from backend.app.tools.tool_call_ledger import get_call_ledger
            ledger = get_call_ledger()

        # With no calls recorded at all there is nothing to contradict,
        # and every claim would look unsupported. Say nothing.
        if not getattr(ledger, "calls")():
            return []

        found: List[str] = []
        for value in _candidates(text):
            if ledger.was_value_used(value):
                continue
            logger.warning(
                "Contradicted claim: deliverable blames %r, which appears in "
                "no tool call this run", value,
            )
            found.append(
                f"The deliverable blames '{value}', but no tool call in this "
                f"run ever used that value — so this run has no evidence for "
                f"that cause."
            )
            if len(found) >= MAX_REPORTED:
                break
        return found
    except Exception as exc:  # noqa: BLE001
        logger.warning("claim check failed, continuing without it: %s", exc)
        return []


def claim_correction(matches: List[str], ledger: Optional[object] = None) -> str:
    """The instruction handed to the refinement pass. Quotes the actual
    calls back, because telling a model "that's wrong" without showing
    what really happened tends to produce a differently-worded guess."""
    detail = " ".join(matches[:MAX_REPORTED])
    tried = ""
    try:
        if ledger is None:
            from backend.app.tools.tool_call_ledger import get_call_ledger
            ledger = get_call_ledger()
        recent = [c for c in ledger.calls() if not c.get("ok")][-6:]
        if recent:
            lines = "; ".join(
                f"{c['tool']}({c['args_text'][:90]})" for c in recent
            )
            tried = f" What this run ACTUALLY called: {lines}."
    except Exception:  # noqa: BLE001
        pass
    return (
        "CRITICAL — this draft states a cause the run's own tool log "
        f"contradicts. {detail}{tried} "
        "Rewrite so the stated cause matches what actually happened. If the "
        "real reason is that the correct value was never tried, say exactly "
        "that and do not blame the founder's input, an external service, or "
        "a value you never sent."
    )
