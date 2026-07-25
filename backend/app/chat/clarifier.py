"""Clarifier — asks the founder sharp questions BEFORE running a task.

The problem: the founder asks "design a hackathon sponsor deck" — a
seven-word prompt. Without clarification the CEO/Unit ships a generic
deck with placeholders. With 3-5 targeted questions up front, the same
run ships something actually usable.

The rules the LLM must follow:
  - MAX 5 questions on the first pass.
  - MAX 10 questions total across the whole conversation.
  - Skip clarification entirely if the prompt is already well-specified.
  - Questions must be the FEWEST needed to unblock a great run — no
    filler. "What's your budget?" is only worth asking if the answer
    changes the deliverable.
  - After the founder answers, either return more questions (tight) or
    declare `ready: true`.

Output on every call:
  {
    "ready": bool,               # true when we have enough to run
    "questions": [str, ...]      # empty when ready=true
  }
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

INITIAL_MAX = 5
TOTAL_MAX = 7   # Hard cap — even the LLM can't go past this


INITIAL_PROMPT = """You are a senior consultant about to work on a task for a founder.
Before you touch it, decide whether you need clarification. Great work
starts by asking the fewest possible high-leverage questions.

FOUNDER'S TASK:
"{task}"

Rules:
- Ask AT MOST {initial_max} questions. Aim for 3, not 5.
- Only ask what genuinely changes the deliverable. Skip fluff.
  Examples of useless questions: "What tone should it have?",
  "Do you want it to be professional?" (obviously yes).
  Examples of high-leverage questions: audience specifics, budget
  ranges, must-include vs. must-avoid, existing assets you'd reuse,
  deadline, success definition.
- If the prompt is already specific enough to produce a good first
  draft, return NO questions and set ready=true.
- Each question is one sentence, plain English, no jargon.

Return JSON only:
{{"ready": <bool>, "questions": [<string>, ...]}}"""


FOLLOWUP_PROMPT = """You already asked the founder some questions and they've answered.
Decide: do you have enough to do great work, or is there a genuine
information gap that will make the deliverable clearly worse?

ORIGINAL TASK:
"{task}"

Q&A SO FAR (in order):
{qa_block}

STRICT RULES — read carefully:
- STRONGLY prefer ready=true. The founder wants work done, not a
  survey. Only ask more if a specific answer is MISSING and its
  absence will visibly hurt the output.
- Do NOT re-ask anything the founder has already addressed, even
  partially. "No branding guidelines yet" is a complete answer to a
  branding question — do not follow up.
- Do NOT ask for "clarifications" of answers that are perfectly
  usable. "$10k Platinum" doesn't need "can you elaborate on the
  Platinum pricing?".
- Do NOT ask for exact dates/formats/style specifics unless the
  original task specifically required them — pick sensible defaults
  instead and note them in the deliverable.
- Total questions asked SO FAR (before this pass): {asked_so_far}.
  Hard cap: {total_max} total. You have {remaining} left.
- If you do ask more, ask ONE at most. Not two.

Default when in doubt: ready=true, questions=[].

Return JSON only:
{{"ready": <bool>, "questions": [<string>, ...]}}"""


class Clarifier:
    def __init__(self, model_adapter: Any):
        self.adapter = model_adapter

    def initial_questions(self, task: str) -> Dict[str, Any]:
        """First pass — see if the task needs clarification at all.
        On any parse failure, default to ready=true so we don't
        dead-end the founder."""
        task = (task or "").strip()
        if not task:
            return {"ready": True, "questions": []}
        try:
            raw = self.adapter.chat_completion(
                INITIAL_PROMPT.format(task=task[:2000], initial_max=INITIAL_MAX),
                temperature=0.2,
                max_tokens=500,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Clarifier initial call failed: %s", exc)
            return {"ready": True, "questions": []}
        return self._parse(raw, cap=INITIAL_MAX)

    def next_step(
        self,
        original_task: str,
        questions_asked: List[str],
        answers: List[str],
    ) -> Dict[str, Any]:
        """Given the running Q&A, decide: more questions, or ready?
        Hard-caps at TOTAL_MAX so we can't spiral."""
        asked = len(questions_asked)
        if asked >= TOTAL_MAX:
            return {"ready": True, "questions": []}
        remaining = TOTAL_MAX - asked
        qa_lines = []
        for i, q in enumerate(questions_asked):
            a = answers[i] if i < len(answers) else "(no answer yet)"
            qa_lines.append(f"Q{i + 1}: {q}\nA{i + 1}: {a}")
        qa_block = "\n\n".join(qa_lines) if qa_lines else "(none)"
        try:
            raw = self.adapter.chat_completion(
                FOLLOWUP_PROMPT.format(
                    task=(original_task or "")[:2000],
                    qa_block=qa_block[:6000],
                    asked_so_far=asked,
                    total_max=TOTAL_MAX,
                    remaining=remaining,
                ),
                temperature=0.2,
                max_tokens=400,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Clarifier follow-up call failed: %s", exc)
            return {"ready": True, "questions": []}
        parsed = self._parse(raw, cap=min(1, remaining))
        # Hard-enforce the total cap regardless of what the LLM returned
        if asked + len(parsed["questions"]) > TOTAL_MAX:
            parsed["questions"] = parsed["questions"][: TOTAL_MAX - asked]
            if not parsed["questions"]:
                parsed["ready"] = True
        return parsed

    # ----------------------------------------------------------------

    def _parse(self, raw: str, cap: int) -> Dict[str, Any]:
        data = _extract_json(raw) or {}
        ready = bool(data.get("ready"))
        qs_raw = data.get("questions") or []
        qs: List[str] = []
        if isinstance(qs_raw, list):
            for q in qs_raw:
                s = str(q or "").strip()
                if s:
                    qs.append(s)
        qs = qs[:cap]
        # If ready was not set explicitly and there are no questions,
        # treat as ready. If questions came back, ready must be false.
        if qs:
            ready = False
        elif not ready:
            ready = True
        return {"ready": ready, "questions": qs}


def format_questions_for_chat(questions: List[str], first_pass: bool = True) -> str:
    """The reply the founder sees in the chat when we're gathering
    context. Numbered list; brief lead-in so it reads like a person,
    not a form."""
    if not questions:
        return ""
    lead = (
        "A few quick things before I get to work — the tighter your "
        "answers, the sharper the output:"
        if first_pass
        else "Quick follow-up so I nail this:"
    )
    body = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
    tail = (
        "\n\nJust type your answers in one message. If any question "
        "doesn't apply, say so and I'll skip it."
    )
    return f"{lead}\n\n{body}{tail}"


def enrich_task_with_qa(
    original_task: str,
    questions: List[str],
    answers: List[str],
) -> str:
    """Fold the Q&A back into the task so the run pipeline sees it as
    part of the brief instead of trying to invent the missing details."""
    if not questions:
        return original_task
    lines = ["FOUNDER'S BRIEF FROM CLARIFICATION:"]
    for i, q in enumerate(questions):
        a = answers[i] if i < len(answers) else ""
        if a:
            lines.append(f"- Q: {q}\n  A: {a}")
    if len(lines) == 1:  # nothing but the header
        return original_task
    context = "\n".join(lines)
    return f"{original_task}\n\n{context}"


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    stripped = text.strip()
    stripped = _JSON_FENCE_RE.sub("", stripped).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    s, e = stripped.find("{"), stripped.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(stripped[s : e + 1])
        except json.JSONDecodeError:
            return None
    return None
