"""SupervisorPlanner — the brain behind a Unit's Supervisor employee.

Every Unit has a Supervisor. The user only talks to the Supervisor;
the Supervisor decides who on the team does what. This is what makes
the org-chart mental model actually real instead of just visual:
specialists don't blindly all attack the raw user prompt anymore
(which produced blob-y, overlapping output). The Supervisor turns
"get me 20 warm intros this week" into concrete sub-tasks per
specialist, delegates, then merges.

Two operations:
  design_delegation(task, specialists) -> [{role, sub_task, order}]
  synthesize(task, contributions)       -> merged final deliverable

Kept as a separate module (not a subclass of DynamicEmployee) because
the Supervisor's *work* is planning+synthesis, not producing a
specialist deliverable. The visible "Supervisor" card in the UI is a
DynamicEmployee, but its runtime behaviour is driven by this planner.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DELEGATE_MAX_TOKENS = 900
SYNTH_MAX_TOKENS = 5000


DELEGATE_PROMPT = """You are the Supervisor of an AI team. The founder just gave you a task
and you must decide which of your specialists does what — no more,
no less.

FOUNDER'S TASK:
"{task}"

YOUR TEAM (each specialist is available for delegation):
{team_snapshot}

Design a delegation plan. Rules:
- Give each specialist ONE concrete sub-task written for THEM
  specifically. Not the raw founder prompt.
- Do NOT give the same work to two specialists. Their sub-tasks should
  be complementary — different angles of the same overall task.
- Order matters: earlier specialists' work is available as context to
  later ones. Put research/analysis first, drafting/production in the
  middle, review/packaging last.
- If a specialist has nothing genuinely useful to contribute for THIS
  particular task, exclude them. Do not invent make-work.
- Sub-tasks should be one to three sentences each — specific enough
  that a specialist knows exactly what to produce, short enough to
  read at a glance.

Return JSON only:
{{"plan": [
  {{"role": "<exact specialist role name>", "sub_task": "<what they should do for this task>"}},
  ...
]}}

JSON only."""


SYNTHESIS_PROMPT = """You are the Supervisor delivering the final result to the founder.

FOUNDER'S ORIGINAL TASK:
"{task}"

WHAT EACH OF YOUR SPECIALISTS PRODUCED (raw drafts, may repeat each
other, may have gaps, may be verbose):
{contributions}

Write the final deliverable as ONE tight, useful answer. This is a
briefing to a busy founder, not a consulting report. Follow every
rule below — they matter:

TONE
- One voice. No "The Market Analyst says…" or "based on our team's work".
- Direct, confident, specific. No hedging when the data is clear.
- No filler sections. No "How this was derived", no "Why these matter",
  no summary of the summary. Cut anything that doesn't help the
  founder act.

STRUCTURE
- Open with the single most useful answer to the question, up top —
  before any table or list. 1-3 sentences.
- Use headings only if the answer has genuinely distinct sections. Don't
  invent structure to look thorough.
- Prefer bullet points and short lines over paragraphs. Founders scan.
- ONE table max, only if a table is genuinely the best format for the
  data. Never duplicate the same info in two tables (a big table AND a
  "top 5" table) — pick one.
- End with a short "What to do next" of 2-4 concrete actions, if the
  task calls for it. Skip if it doesn't.

CONTENT
- Cover everything genuinely useful from the specialists' work.
- Merge overlapping content aggressively — don't repeat the same
  point twice.
- Resolve contradictions in favor of what's most useful.
- If a data point is missing, just OMIT that row/entry silently. Do
  NOT write "Not available from supplied sources" or similar noise.
- Do not fabricate specific statistics. If a number isn't real, describe
  qualitatively (or leave it out).
- If specialists cited URLs, keep them — founders click them.

Deliverable:"""


class SupervisorPlanner:
    def __init__(self, model_adapter: Any):
        self.adapter = model_adapter

    def design_delegation(
        self, task: str, specialists: List[Dict[str, str]]
    ) -> List[Dict[str, str]]:
        """Returns a list of {role, sub_task} entries in delegation
        order. Falls back to "everyone does the raw task" if the LLM
        call fails or the response is unparseable — the specialists
        still work, just less coordinated."""
        if not specialists:
            return []
        team_snapshot = self._render_team_snapshot(specialists)
        try:
            response = self.adapter.chat_completion(
                DELEGATE_PROMPT.format(task=task, team_snapshot=team_snapshot),
                temperature=0.2,
                max_tokens=DELEGATE_MAX_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Supervisor delegation call failed: %s", exc)
            return self._fallback_plan(task, specialists)

        data = self._extract_json(response) or {}
        plan = data.get("plan")
        if not isinstance(plan, list) or not plan:
            logger.warning("Supervisor delegation response unparseable, using fallback")
            return self._fallback_plan(task, specialists)

        cleaned: List[Dict[str, str]] = []
        by_role = {s["role"].lower(): s["role"] for s in specialists}
        for entry in plan:
            if not isinstance(entry, dict):
                continue
            role_raw = str(entry.get("role", "")).strip()
            sub_task = str(entry.get("sub_task", "")).strip()
            if not role_raw or not sub_task:
                continue
            # Map back to the exact spelling of the role (LLMs sometimes
            # capitalize/rephrase). Skip if not on the team.
            role = by_role.get(role_raw.lower())
            if not role:
                continue
            cleaned.append({"role": role, "sub_task": sub_task})
        if not cleaned:
            return self._fallback_plan(task, specialists)
        return cleaned

    def synthesize(
        self, task: str, contributions: List[Dict[str, Any]], max_tokens: int = SYNTH_MAX_TOKENS
    ) -> Optional[str]:
        """Merge specialists' contributions into one final deliverable.
        Falls back to a raw concat if the call fails."""
        renderable = [c for c in contributions if (c.get("output") or "").strip()]
        if not renderable:
            return None
        rendered = "\n\n".join(
            f"--- {c.get('role', 'Specialist')} ---\n{(c.get('output') or '').strip()}"
            for c in renderable
        )
        try:
            response = self.adapter.chat_completion(
                SYNTHESIS_PROMPT.format(task=task, contributions=rendered),
                temperature=0.4,
                max_tokens=max_tokens,
                format=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Supervisor synthesis call failed: %s", exc)
            return self._raw_concat(renderable)
        response = (response or "").strip()
        if not response or response.lower().startswith(("connection error", "error:")):
            return self._raw_concat(renderable)
        return response

    def _fallback_plan(
        self, task: str, specialists: List[Dict[str, str]]
    ) -> List[Dict[str, str]]:
        return [
            {"role": s["role"], "sub_task": task}
            for s in specialists
        ]

    def _render_team_snapshot(self, specialists: List[Dict[str, str]]) -> str:
        lines = []
        for m in specialists:
            role = m.get("role", "")
            mandate = m.get("mandate", "")
            lines.append(f"- {role}: {mandate[:140]}")
        return "\n".join(lines)

    def _raw_concat(self, contributions: List[Dict[str, Any]]) -> str:
        parts = []
        for c in contributions:
            out = (c.get("output") or "").strip()
            if not out:
                continue
            parts.append(f"## {c.get('role') or 'Specialist'}\n\n{out}")
        return "\n\n---\n\n".join(parts)

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


# --- Default Supervisor spec ---
#
# Every Unit gets one of these hired automatically on creation. The role
# name incorporates the Unit's purpose so the user sees a Supervisor
# card that feels tied to *their* project ("Outreach Supervisor" for a
# cold-outreach unit).

DEFAULT_SUPERVISOR_MANDATE = (
    "You are the Supervisor of this AI team. Every task from the "
    "founder comes to you first. You decide which specialists work on "
    "what, publish a delegation plan, keep the specialists coordinated, "
    "and deliver the merged final result back to the founder in one "
    "voice. You do not do specialist work yourself — you plan and "
    "synthesize."
)


def default_supervisor_spec(unit_purpose: Optional[str] = None) -> Dict[str, str]:
    """Build the default Supervisor member spec for a new Unit."""
    if unit_purpose:
        # e.g. "cold outreach" -> "Cold Outreach Supervisor"
        pretty = unit_purpose.strip().rstrip(".").title()
        role = f"{pretty} Supervisor"
    else:
        role = "Supervisor"
    return {"role": role, "mandate": DEFAULT_SUPERVISOR_MANDATE, "is_supervisor": True}
