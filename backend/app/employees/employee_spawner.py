"""EmployeeSpawner — reads a user's prompt and decides which employees
to spawn to solve it.

The gap this closes: today, Employees are hand-coded classes
(IdeaValidationEmployee is the only one that exists). The real Vision
AI product needs the *user's prompt itself* to determine what team gets
spun up — a Substack-launch prompt should produce a Writer + a
Marketer + a Growth employee that didn't exist before that prompt.

Uses one cheap LLM call to design the team, then hands construction of
each employee to DynamicEmployee. Sits BELOW the adaptive supervisor:
the supervisor decides "is a team even needed for this task?" first;
this only runs if the answer was multi_agent_critique.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from backend.app.employees.dynamic_employee import DynamicEmployee
from backend.app.orchestrator.pipeline_controller import Pipeline

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 800
MIN_TEAM_SIZE = 2
MAX_TEAM_SIZE = 5

SPAWN_PROMPT = """You are staffing a small AI team to complete a specific user request.
Design the smallest team that could realistically do the whole job —
not the biggest.

USER REQUEST:
{prompt}

Rules for the team you design:
- Between {min_size} and {max_size} employees. Fewer is better if the
  job doesn't genuinely need more.
- Each employee must have a distinct role a real team would assign to
  a different specialist. Do NOT create two roles that would obviously
  step on each other's work.
- Each role should be nameable in 1-3 words (e.g. "Market Researcher",
  "Copywriter", "Growth Strategist"), not a sentence.
- The mandate for each role is 1-2 sentences describing exactly what
  that person owns on this specific task, and nothing outside it.
- Order matters: list the team in the order their work should happen
  (upstream roles first — e.g. research before writing before
  distribution). Later employees will see earlier employees' output.

Return JSON only:
{{
  "team": [
    {{"role": "…", "mandate": "…"}},
    …
  ]
}}

JSON only."""


# A role NAME that names a file format AND an explicit production noun
# is staffed for a job the agentic loop cannot perform: create_pdf/docx/
# pptx/xlsx are all planner_excluded and fire automatically after
# synthesis. Such a specialist has no path to produce the file, so its
# only honest move is to ask the founder for assets -- which the
# Supervisor then repeats as a "provide X" instruction, tripping the
# hand-back gate on a run that had already shipped a working file.
#
# Reproduced live: a self-designed "PDF Production Specialist" did
# exactly this. Matches on the role NAME only (not the mandate), because
# role names are short and deliberate per SPAWN_PROMPT's own "1-3 words"
# rule -- "Report Writer" and "Report Designer", both observed producing
# real content in live runs, name no format and survive.
_DOC_FORMAT_WORDS = ("pdf", "docx", "pptx", "xlsx", "powerpoint", "excel", "word doc")
_DOC_PRODUCTION_ROLE_WORDS = (
    "production", "producer", "formatting", "formatter", "assembly",
    "assembler", "generation", "export", "publisher", "publishing",
    "compiler", "compilation",
)


def _is_redundant_doc_production_role(role):
    text = (role or "").lower()
    return (any(w in text for w in _DOC_FORMAT_WORDS)
            and any(w in text for w in _DOC_PRODUCTION_ROLE_WORDS))


class EmployeeSpawner:
    def __init__(self, model_adapter: Any, max_tokens: int = DEFAULT_MAX_TOKENS):
        self.adapter = model_adapter
        self.max_tokens = max_tokens

    def design_team(self, prompt: str) -> List[Dict[str, str]]:
        """Returns a list of {"role", "mandate"} dicts. Never empty —
        falls back to a single "Generalist" if the design call fails, so
        the flow always has *someone* to hand the task to."""
        try:
            response = self.adapter.chat_completion(
                SPAWN_PROMPT.format(prompt=prompt, min_size=MIN_TEAM_SIZE, max_size=MAX_TEAM_SIZE),
                temperature=0.3,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Team design call failed, falling back to solo Generalist: %s", exc)
            return [self._fallback_generalist(prompt)]

        data = self._extract_json(response)
        team = (data or {}).get("team")
        if not isinstance(team, list) or not team:
            logger.warning("Team design response unparseable/empty, falling back to Generalist")
            return [self._fallback_generalist(prompt)]

        cleaned: List[Dict[str, str]] = []
        dropped: List[str] = []
        for member in team[:MAX_TEAM_SIZE]:
            if not isinstance(member, dict):
                continue
            role = str(member.get("role", "")).strip()
            mandate = str(member.get("mandate", "")).strip()
            if not (role and mandate):
                continue
            if _is_redundant_doc_production_role(role):
                dropped.append(role)
                continue
            cleaned.append({"role": role, "mandate": mandate})

        if dropped:
            logger.info(
                "Dropped redundant document-production role(s) %s -- final-format "
                "file generation is automatic; a specialist staffed for that job "
                "cannot succeed and will hand back asking for assets.", dropped)

        if not cleaned:
            return [self._fallback_generalist(prompt)]
        return cleaned

    def spawn(
        self,
        prompt: str,
        pipeline: Pipeline,
        session_id: Optional[str] = None,
    ) -> List[DynamicEmployee]:
        """Convenience: design AND instantiate in one call.
        For the UI flow where the team persists across tasks, use
        design_team() + instantiate() separately so the spec can be
        stored/edited between the two."""
        team_spec = self.design_team(prompt)
        sess = session_id or f"session_{uuid.uuid4().hex[:8]}"
        return self.instantiate(team_spec, pipeline=pipeline, session_id=sess)

    def instantiate(
        self,
        team_spec: List[Dict[str, str]],
        pipeline: Pipeline,
        session_id: str,
    ) -> List[DynamicEmployee]:
        """Build DynamicEmployees from an already-decided team spec.
        Splits the spec-authoring step from the construction step, so a
        team stored in TeamStore (possibly hand-edited by the user)
        can still be turned into live employees for a task run.

        Uses each member's `employee_id` if the spec came from the
        registry-backed TeamStore, so this same Employee's memory file
        is what the task run reads/writes to. Falls back to the legacy
        `session_id__role_slug` for ad-hoc spec dicts that don't carry
        an id (e.g. a fresh design_team() output not yet persisted)."""
        employees: List[DynamicEmployee] = []
        for member in team_spec:
            eid = member.get("employee_id") or (
                f"{session_id}__{self._slugify(member['role'])}"
            )
            employees.append(DynamicEmployee(
                employee_id=eid,
                role=member["role"],
                mandate=member["mandate"],
                pipeline=pipeline,
            ))
        return employees

    def _fallback_generalist(self, prompt: str) -> Dict[str, str]:
        return {
            "role": "Generalist",
            "mandate": (
                "Handle the entire user request end-to-end as best you can. "
                "You are the only member of this team because the staffing "
                "step failed; do not assume specialist teammates exist."
            ),
        }

    def _slugify(self, text: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
        return s or "role"

    def _extract_json(self, text: str):
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
