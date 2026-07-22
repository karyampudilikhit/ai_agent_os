"""CEOManager — the active Manager at the Company layer of the org.

Companies own Units. Each Unit already has its own Supervisor (Phase 1
shipped that). The CEO is the layer above the Supervisors — the
"supervisor of supervisors" that takes a Company-level task and
decides which Units handle which pieces of it, then synthesizes across
Units into one deliverable in one voice.

Where a Unit's Supervisor is thinking specialist-level (Market
Researcher does research, Copywriter drafts copy, Editor polishes),
the CEO is thinking Unit-level ("Marketing Unit handles positioning
and copy, Sales Unit handles outreach and demos, Product Unit
validates the roadmap").

Design parallels SupervisorPlanner deliberately — same lifecycle
(plan → dispatch → synthesize), same fallback patterns, same JSON
extraction. Different altitude.

Phase 2a scope (this file): CEO is a purely computational construct
(class + LLM adapter, no persistent Employee record). Phase 3 will
promote the CEO to a first-class registered Employee with memory of
past Company-level delegations, so the CEO learns how each Unit
performs on different task types over time.

Playbook integration: the CEO uses the same playbooks.py rules as
Supervisors — a Company-level "validation" task should still enforce
"mark unknown" across every Unit's output. Playbook rules flow down.
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

DELEGATE_MAX_TOKENS = 1800
SYNTH_MAX_TOKENS = 6000


DELEGATE_PROMPT = """You are the CEO of an AI-run company. The founder just handed you a
Company-level task. You do NOT do specialist work yourself — you decide
which Unit inside this Company handles which piece of the task, hand
each Unit a specific brief, and later synthesize their outputs into
one Company-level deliverable in one voice.

FOUNDER'S TASK:
"{task}"

TASK TYPE: {task_type}

COMPANY-LEVEL QUALITY RULES that apply (weave the relevant ones into
each Unit's brief so specialists inside those Units follow them too):
{playbook_rules}

COMPANY:
Name: {company_name}
Purpose: {company_purpose}

UNITS IN THIS COMPANY (each Unit has its own Supervisor + specialist
roster; you delegate at Unit level, the Unit's Supervisor takes over
from there):
{units_snapshot}

Design a Company-level delegation plan. For each Unit you use:

1. `unit_id` — must exactly match one of the unit_ids above.

2. `sub_task` — ONE Company-level sub-task written for this Unit,
   1-3 sentences. Not the raw founder prompt. What THIS Unit
   specifically owns for THIS task. Do not give the same slice to
   two Units — their sub-tasks must be complementary.

3. `unit_brief` — 2-4 sentences briefing THIS Unit's Supervisor on:
   • what the Company expects from them for this task
   • the specific quality rules from above that apply to their piece
     (pick 1-2 relevant ones and quote them — don't dump all rules)
   • format expectations for their deliverable back to you (CEO)

Rules for you as CEO:
- Only include Units that have a real contribution for THIS task.
  If Marketing Unit has nothing to contribute to a legal review
  task, exclude them. No make-work.
- Order the plan by dependency — research/analysis Units first,
  execution/production Units after them.
- If the Company has ONE relevant Unit, delegate the whole task to
  it. That's fine — the CEO's job is delegation, not fabrication of
  multi-Unit theater.

Return JSON only:
{{"plan": [
  {{"unit_id": "<exact unit_id from above>",
    "sub_task": "<what this Unit should own for this task>",
    "unit_brief": "<the Company-level brief for their Supervisor>"}},
  ...
]}}

JSON only."""


SYNTHESIS_PROMPT = """You are the CEO delivering the final result to the founder.

FOUNDER'S ORIGINAL COMPANY-LEVEL TASK:
"{task}"

WHAT EACH UNIT PRODUCED (each Unit's Supervisor already synthesized
their specialists' work — these are Unit-level deliverables):
{contributions}

Write the final Company-level deliverable as ONE tight, useful answer.
This is a briefing to a busy founder from their CEO — not a compilation
of Unit reports.

TONE
- One voice, at Company altitude. No "The Marketing Unit says…" or
  "Sales Unit reports…". Absorb the Units' work and present it as one
  coherent Company perspective.
- Direct, confident, specific. No hedging when the data is clear.

STRUCTURE
- Open with the single most useful answer to the founder's question,
  up top — before any table or breakdown. 1-3 sentences.
- Use headings only if the answer has genuinely distinct sections.
- Prefer bullets and short lines over paragraphs. Founders scan.
- ONE table max.
- End with a short "What to do next" of 2-4 concrete actions.

CONTENT
- Cover everything genuinely useful from each Unit's contribution.
- Merge overlapping content aggressively — don't repeat.
- Resolve contradictions in favor of what's most useful to the founder.
- PRESERVE "unknown" answers from any Unit's output. If a Unit wrote
  "unknown — no primary source found," keep that exact phrasing. Do
  NOT fill in an estimate during synthesis.
- PRESERVE URL citations from Units — a claim followed by a URL in
  the source Unit output must keep that URL.
- PRESERVE adversarial "why this could fail" analysis from any Unit —
  don't smooth it away.
- If a data point is missing AND no Unit marked it unknown, omit the
  row silently.
- Do not fabricate specific statistics.

Deliverable:"""


class CEOManager:
    """The active Manager at the Company layer. Plans delegation across
    Units, then synthesizes cross-Unit output. Uses the same playbooks
    as Supervisors — quality rules propagate top-down."""

    def __init__(self, model_adapter: Any):
        self.adapter = model_adapter

    # ---- planning --------------------------------------------------

    def plan_company_delegation(
        self,
        task: str,
        company: Dict[str, Any],
        units: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Design which Unit handles which slice of the Company task.

        `units` shape: [{unit_id, name, purpose, members: [{role, mandate, is_supervisor?}]}]

        Returns [{unit_id, sub_task, unit_brief}] in delegation order.
        Falls back to "one entry per Unit with the raw task" if the LLM
        call fails — the Units still work, just less coordinated and
        without the playbook briefs.
        """
        if not units:
            return []
        task_type = classify_task_type(task)
        playbook_rules = format_rules_for_prompt(task_type)
        logger.info(
            "CEO classified task as %r; delegating across %d Unit(s)",
            task_type, len(units),
        )

        units_snapshot = self._render_units_snapshot(units)
        try:
            response = self.adapter.chat_completion(
                DELEGATE_PROMPT.format(
                    task=task,
                    task_type=task_type,
                    playbook_rules=playbook_rules,
                    company_name=company.get("name") or "(unnamed)",
                    company_purpose=company.get("purpose") or "(no stated purpose)",
                    units_snapshot=units_snapshot,
                ),
                temperature=0.2,
                max_tokens=DELEGATE_MAX_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("CEO delegation call failed: %s", exc)
            return self._fallback_plan(task, units, task_type)

        data = self._extract_json(response) or {}
        plan = data.get("plan")
        if not isinstance(plan, list) or not plan:
            logger.warning("CEO delegation response unparseable, using fallback")
            return self._fallback_plan(task, units, task_type)

        valid_ids = {u["unit_id"] for u in units}
        cleaned: List[Dict[str, Any]] = []
        for entry in plan:
            if not isinstance(entry, dict):
                continue
            uid = str(entry.get("unit_id", "")).strip()
            sub_task = str(entry.get("sub_task", "")).strip()
            unit_brief = str(entry.get("unit_brief", "")).strip()
            if not uid or not sub_task or uid not in valid_ids:
                continue
            item = {"unit_id": uid, "sub_task": sub_task}
            if unit_brief:
                item["unit_brief"] = unit_brief
            cleaned.append(item)
        if not cleaned:
            return self._fallback_plan(task, units, task_type)
        return cleaned

    # ---- synthesis -------------------------------------------------

    def synthesize(
        self,
        task: str,
        contributions: List[Dict[str, Any]],
        max_tokens: int = SYNTH_MAX_TOKENS,
    ) -> Optional[str]:
        """Merge Unit-level deliverables into one Company-level answer.
        Falls back to raw concat if the call fails.

        `contributions` shape: [{unit_id, unit_name, output}]"""
        renderable = [c for c in contributions if (c.get("output") or "").strip()]
        if not renderable:
            return None
        rendered = "\n\n".join(
            f"--- {c.get('unit_name') or c.get('unit_id') or 'Unit'} ---\n"
            f"{(c.get('output') or '').strip()}"
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
            logger.warning("CEO synthesis call failed: %s", exc)
            return self._raw_concat(renderable)
        response = (response or "").strip()
        if not response or response.lower().startswith(("connection error", "error:")):
            return self._raw_concat(renderable)
        return response

    # ---- fallbacks + rendering ------------------------------------

    def _fallback_plan(
        self,
        task: str,
        units: List[Dict[str, Any]],
        task_type: str = "general",
    ) -> List[Dict[str, Any]]:
        """When the CEO's delegation call fails, still inject the
        playbook rules as a raw brief so Units at least see the quality
        rules. Every Unit gets the same raw task."""
        rules_block = format_rules_for_prompt(task_type)
        brief = (
            f"Company-level quality rules ({task_type}):\n{rules_block}"
            if rules_block
            else ""
        )
        return [
            {
                "unit_id": u["unit_id"],
                "sub_task": task,
                **({"unit_brief": brief} if brief else {}),
            }
            for u in units
        ]

    def _render_units_snapshot(self, units: List[Dict[str, Any]]) -> str:
        lines: List[str] = []
        for u in units:
            uid = u.get("unit_id", "")
            name = u.get("name") or "(unnamed)"
            purpose = (u.get("purpose") or "").strip() or "(no stated purpose)"
            members = u.get("members") or []
            roster = ", ".join(
                m.get("role", "?") + ("★" if m.get("is_supervisor") else "")
                for m in members
            ) or "(empty roster)"
            lines.append(
                f"- unit_id: {uid}\n"
                f"  name:    {name}\n"
                f"  purpose: {purpose[:200]}\n"
                f"  roster:  {roster}"
            )
        return "\n".join(lines)

    def _raw_concat(self, contributions: List[Dict[str, Any]]) -> str:
        parts = []
        for c in contributions:
            out = (c.get("output") or "").strip()
            if not out:
                continue
            heading = c.get("unit_name") or c.get("unit_id") or "Unit"
            parts.append(f"## {heading}\n\n{out}")
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
