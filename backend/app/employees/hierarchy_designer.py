"""HierarchyDesigner — turns a founder's plain-English company
description into a proposed org chart in one LLM call.

Mirrors EmployeeSpawner one altitude up. Where EmployeeSpawner designs
a team of specialists for one task, HierarchyDesigner designs an
entire Company's Units and each Unit's initial roster from a company
description.

The user then approves/edits the proposal before it materializes —
this module ONLY produces specs, it never touches the store. The API
layer is responsible for the transactional "accept" that turns specs
into real Units + Employees.

Prompt-driven from day one — matches how Units get their teams designed
today (chat.design_team), so the founder never has to click through
forms to build their org.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 2000
MIN_UNITS = 2
MAX_UNITS = 6
MIN_SPECIALISTS_PER_UNIT = 1
MAX_SPECIALISTS_PER_UNIT = 4


DESIGN_PROMPT = """You are a startup CEO designing the initial org chart for a founder
who just told you what they're building. Design the smallest, most
useful company structure that would actually get the work done — not
the biggest.

FOUNDER'S DESCRIPTION OF THE COMPANY:
"{description}"

Design {min_units}-{max_units} Units and, for each Unit, propose an
initial roster of {min_specialists}-{max_specialists} specialists.
Rules:

- Each Unit is a persistent functional area (e.g., "Market Research
  Unit", "Go-To-Market Unit", "Product Discovery Unit", "Operations
  Unit"). Not a project. Not a task.
- Each Unit must have a distinct scope. Do NOT create two Units that
  would obviously step on each other's work.
- A Unit's name should be 2-4 words and end with "Unit". Its purpose
  should be one sentence describing what it OWNS.
- For each Unit, propose {min_specialists}-{max_specialists} initial
  specialists as {{role, mandate}} pairs. Roles are 1-3 words. Mandates
  are one sentence describing what only that person owns on this Unit.
- Skip Units that don't apply to this specific company. A pre-launch
  solo-founder SaaS with no customers does NOT need a Sales Unit yet.
- Order the Units from most immediately useful to least.

Return JSON only:
{{"units": [
  {{"name": "Market Research Unit",
    "purpose": "Own market sizing, competitor tracking, and pricing intel.",
    "specialists": [
      {{"role": "Market Researcher", "mandate": "..."}},
      {{"role": "Competitive Analyst", "mandate": "..."}}
    ]}},
  ...
]}}

JSON only."""


class HierarchyDesigner:
    """Turns a company description into a proposed hierarchy spec.
    Deliberately stateless — produces specs, never mutates stores."""

    def __init__(self, model_adapter: Any, max_tokens: int = DEFAULT_MAX_TOKENS):
        self.adapter = model_adapter
        self.max_tokens = max_tokens

    def design(self, description: str) -> List[Dict[str, Any]]:
        """Returns a list of {name, purpose, specialists:[{role,mandate}]}.
        Falls back to a single "General Unit" if the LLM call fails or
        response is unparseable — the founder still gets a scaffold to
        edit, they just don't get the CEO's structuring."""
        description = (description or "").strip()
        if not description:
            return [self._fallback_unit(description)]

        try:
            response = self.adapter.chat_completion(
                DESIGN_PROMPT.format(
                    description=description[:1500],
                    min_units=MIN_UNITS,
                    max_units=MAX_UNITS,
                    min_specialists=MIN_SPECIALISTS_PER_UNIT,
                    max_specialists=MAX_SPECIALISTS_PER_UNIT,
                ),
                temperature=0.3,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Hierarchy design call failed: %s", exc)
            return [self._fallback_unit(description)]

        data = self._extract_json(response) or {}
        units = data.get("units")
        if not isinstance(units, list) or not units:
            logger.warning("Hierarchy design response unparseable, using fallback")
            return [self._fallback_unit(description)]

        cleaned: List[Dict[str, Any]] = []
        for u in units[:MAX_UNITS]:
            if not isinstance(u, dict):
                continue
            name = str(u.get("name", "")).strip()
            purpose = str(u.get("purpose", "")).strip()
            if not name or not purpose:
                continue
            specs_raw = u.get("specialists") or []
            specialists: List[Dict[str, str]] = []
            if isinstance(specs_raw, list):
                for s in specs_raw[:MAX_SPECIALISTS_PER_UNIT]:
                    if not isinstance(s, dict):
                        continue
                    role = str(s.get("role", "")).strip()
                    mandate = str(s.get("mandate", "")).strip()
                    if role and mandate:
                        specialists.append({"role": role, "mandate": mandate})
            if not specialists:
                # Every Unit needs at least a Generalist to do the work;
                # a Unit with only a Supervisor and no specialists can't
                # produce anything.
                specialists = [{
                    "role": "Generalist",
                    "mandate": f"Handle work assigned to the {name} until a specialist joins.",
                }]
            cleaned.append({
                "name": name,
                "purpose": purpose,
                "specialists": specialists,
            })

        if not cleaned:
            return [self._fallback_unit(description)]
        return cleaned

    def design_one_unit(
        self,
        description: str,
        existing_units: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Design a SINGLE new Unit to add to an existing Company.

        Differs from `design()` in three ways:
          1. Returns exactly one Unit spec, not a list.
          2. Sees the current org so it doesn't duplicate an existing
             Unit's scope.
          3. Anchored on the founder's specific ask ("add a QA unit",
             "add a unit that evaluates other units' output") — the
             description is treated as the Unit's mandate, not the
             whole company.
        """
        description = (description or "").strip()
        if not description:
            return self._fallback_unit(description)

        existing_block = ""
        if existing_units:
            lines = []
            for u in existing_units[:12]:  # cap so the prompt stays small
                name = str(u.get("name") or "").strip()
                purpose = str(u.get("purpose") or "").strip()
                if name:
                    lines.append(f"- {name}: {purpose or '(no purpose)'}")
            if lines:
                existing_block = (
                    "EXISTING UNITS IN THIS COMPANY — do NOT duplicate their scope:\n"
                    + "\n".join(lines)
                    + "\n\n"
                )

        prompt = f"""You are the CEO of a running company and the founder just asked
you to add ONE new Unit. Design it — nothing more.

{existing_block}FOUNDER'S ASK FOR THE NEW UNIT:
"{description[:1500]}"

Rules:
- Design exactly ONE Unit whose scope is what the founder just asked
  for. Do not sneak in extra Units.
- The Unit's scope must NOT overlap with any existing Unit.
- Name is 2-4 words and ends with "Unit".
- Purpose is one sentence describing what this Unit OWNS.
- Roster: {MIN_SPECIALISTS_PER_UNIT}-{MAX_SPECIALISTS_PER_UNIT} specialists
  as {{role, mandate}} pairs. Roles 1-3 words. Mandates one sentence each.

Return JSON only, this shape:
{{"name": "...", "purpose": "...", "specialists": [{{"role": "...", "mandate": "..."}}]}}

JSON only."""

        try:
            response = self.adapter.chat_completion(
                prompt, temperature=0.3, max_tokens=self.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("design_one_unit call failed: %s", exc)
            return self._fallback_unit(description)

        data = self._extract_json(response) or {}
        name = str(data.get("name") or "").strip()
        purpose = str(data.get("purpose") or "").strip()
        if not name or not purpose:
            return self._fallback_unit(description)

        specs_raw = data.get("specialists") or []
        specialists: List[Dict[str, str]] = []
        if isinstance(specs_raw, list):
            for s in specs_raw[:MAX_SPECIALISTS_PER_UNIT]:
                if not isinstance(s, dict):
                    continue
                role = str(s.get("role", "")).strip()
                mandate = str(s.get("mandate", "")).strip()
                if role and mandate:
                    specialists.append({"role": role, "mandate": mandate})
        if not specialists:
            specialists = [{
                "role": "Generalist",
                "mandate": f"Handle work assigned to the {name} until a specialist joins.",
            }]
        return {"name": name, "purpose": purpose, "specialists": specialists}

    def _fallback_unit(self, description: str) -> Dict[str, Any]:
        return {
            "name": "General Unit",
            "purpose": (
                "Handle all work for the company until a specialized "
                "structure is designed."
            ),
            "specialists": [{
                "role": "Generalist",
                "mandate": (
                    "Do whatever work the CEO delegates. The org design "
                    "step failed; this is a placeholder Unit the founder "
                    "should reshape by chatting with the CEO."
                ),
            }],
        }

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
