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

from backend.app.employees.playbooks import (
    classify_task_type,
    format_rules_for_prompt,
)

logger = logging.getLogger(__name__)

DELEGATE_MAX_TOKENS = 1600  # bumped from 900 — briefs need room to be substantive
SYNTH_MAX_TOKENS = 5000


DELEGATE_PROMPT = """You are the Supervisor of an AI team. The founder just gave you a task
and you must decide who does what — AND write each specialist a
concrete briefing so they know how to produce premium-quality output.

FOUNDER'S TASK:
"{task}"

TASK TYPE: {task_type}

QUALITY RULES that apply to this task type (weave the relevant ones
into each specialist's brief — this is how a weaker model produces
premium-quality output, by following explicit rules rather than
guessing):
{playbook_rules}

YOUR TEAM (each specialist is available for delegation):
{team_snapshot}

Design a delegation plan. For each specialist you use, produce:

1. `sub_task` — ONE concrete sub-task written for them, 1-3 sentences.
   Do not give the same work to two specialists. Order the plan so
   research/analysis comes before drafting/synthesis.

2. `task_brief` — a task-specific briefing (~3-6 short sentences or
   bullets) that combines:
   • what this specialist should produce for THIS task
   • the specific quality rules from above that apply to their sub-task
     (pick the relevant ones — don't dump all of them)
   • format expectations for their output
   • the specific failure mode they must avoid (e.g. "don't estimate
     numbers without URLs", "quote pricing verbatim, don't paraphrase")

Rules for you as Supervisor:
- Exclude specialists who have nothing useful to contribute. No make-work.
- Every task_brief must incorporate at least 1-2 of the quality rules
  above VERBATIM (paraphrasing dilutes them). This is not optional.
- Keep briefs specific to the sub-task — don't just paste the full
  rules block. The Market Researcher's brief differs from the
  Copywriter's brief.

Return JSON only:
{{"plan": [
  {{"role": "<exact specialist role name>",
    "sub_task": "<what they should do>",
    "task_brief": "<the briefing including relevant quality rules>"}},
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
- Do not fabricate specific statistics. If a number isn't real, describe
  qualitatively (or leave it out).
- PRESERVE "unknown" answers from specialists. If a specialist wrote
  "unknown — no primary source found" for a number, keep that exact
  phrasing. Do NOT fill in an estimate during synthesis to make the
  output look complete — an honest "unknown" is the point.
- PRESERVE URL citations from specialists — a claim followed by a URL
  in the source draft must keep that URL in the merged output.
- PRESERVE any adversarial paragraph (a "here's why this might fail"
  block) from the specialists — don't smooth it out of existence.
- If a data point is missing AND no specialist marked it unknown, omit
  the row silently rather than writing "Not available" filler.

Deliverable:"""


class SupervisorPlanner:
    def __init__(self, model_adapter: Any):
        self.adapter = model_adapter

    def design_delegation(
        self, task: str, specialists: List[Dict[str, str]]
    ) -> List[Dict[str, str]]:
        """Returns a list of {role, sub_task, task_brief} entries in
        delegation order.

        Classifies the task type first (heuristic), pulls the matching
        playbook rules, then asks the LLM to compose a delegation plan
        where each specialist gets both a sub-task AND a task-specific
        briefing that incorporates the relevant quality rules.

        This is the mechanism that lets a weaker base model produce
        premium-quality output: the specialist is told EXPLICITLY how
        to be good (mark unknown, cite verbatim, prove absence, etc.)
        rather than having to intuit it.

        Falls back to "everyone does the raw task with no brief" if
        the LLM call fails — specialists still work, just less
        coordinated and without playbook rules.
        """
        if not specialists:
            return []
        task_type = classify_task_type(task)
        playbook_rules = format_rules_for_prompt(task_type)
        logger.info("Supervisor classified task as %r; playbook has rules", task_type)

        team_snapshot = self._render_team_snapshot(specialists)
        try:
            response = self.adapter.chat_completion(
                DELEGATE_PROMPT.format(
                    task=task,
                    task_type=task_type,
                    playbook_rules=playbook_rules,
                    team_snapshot=team_snapshot,
                ),
                temperature=0.2,
                max_tokens=DELEGATE_MAX_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Supervisor delegation call failed: %s", exc)
            return self._fallback_plan(task, specialists, task_type)

        data = self._extract_json(response) or {}
        plan = data.get("plan")
        if not isinstance(plan, list) or not plan:
            logger.warning("Supervisor delegation response unparseable, using fallback")
            return self._fallback_plan(task, specialists, task_type)

        cleaned: List[Dict[str, str]] = []
        by_role = {s["role"].lower(): s["role"] for s in specialists}
        for entry in plan:
            if not isinstance(entry, dict):
                continue
            role_raw = str(entry.get("role", "")).strip()
            sub_task = str(entry.get("sub_task", "")).strip()
            task_brief = str(entry.get("task_brief", "")).strip()
            if not role_raw or not sub_task:
                continue
            role = by_role.get(role_raw.lower())
            if not role:
                continue
            item = {"role": role, "sub_task": sub_task}
            # Only attach the brief if the LLM actually produced one.
            # A missing brief is not fatal — the specialist can still
            # run on just the sub_task and its persistent mandate.
            if task_brief:
                item["task_brief"] = task_brief
            cleaned.append(item)
        if not cleaned:
            return self._fallback_plan(task, specialists, task_type)
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
        self,
        task: str,
        specialists: List[Dict[str, str]],
        task_type: str = "general",
    ) -> List[Dict[str, str]]:
        """When the delegation call fails, still inject the playbook
        rules as a raw brief so specialists at least see the quality
        rules — output quality doesn't collapse just because planning
        did."""
        rules_block = format_rules_for_prompt(task_type)
        brief = (
            f"Quality rules for this task type ({task_type}):\n{rules_block}"
            if rules_block
            else ""
        )
        return [
            {"role": s["role"], "sub_task": task, **({"task_brief": brief} if brief else {})}
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
