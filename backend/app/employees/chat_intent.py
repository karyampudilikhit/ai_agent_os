"""ChatIntentClassifier — routes a user's chat message to the right
action instead of treating everything as a task to run.

The problem this fixes: the chat used to hand every user message to the
current team as a task. So "add a supervisor" got interpreted as a
business objective and the team built a whole product plan called
"Supervisor" instead of adding a Supervisor role. One quick LLM call
classifies the intent first and extracts whatever parameters that
intent needs.

Intents:
  add_employee     — {role, mandate}
  remove_employee  — {role}
  modify_employee  — {role, new_mandate}
  design_team      — {prompt}
  clear_team       — no params
  run_task         — {task} (default when the message describes work,
                     not team management)
  unclear          — couldn't tell; frontend should ask a clarifier
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

VALID_INTENTS = {
    "add_employee",
    "remove_employee",
    "modify_employee",
    "design_team",
    "clear_team",
    "run_task",
    "unclear",
}

DEFAULT_MAX_TOKENS = 400


CLASSIFY_PROMPT = """You are the router in front of an AI-team chat. The user is talking to a
system that (a) manages a persistent team of AI employees, and (b) runs
tasks on that team. Every message is EITHER a team-management request or
a task-to-run request — you decide which and extract what's needed.

CURRENT TEAM:
{team_snapshot}

USER MESSAGE:
"{message}"

Pick exactly ONE intent, and fill only the fields for that intent:

- "add_employee": user wants to add a new role to the team.
  Fields: role (short 1-3 word title), mandate (1-2 sentences of what
  they own; if user didn't give one, infer a sensible mandate from
  the role and team context).
  Example triggers: "add a supervisor", "we need a marketer", "hire a
  designer for the client portal work", "get me a QA engineer".

- "remove_employee": user wants to remove a role.
  Fields: role (must be one of the current team roles above; match
  case-insensitively, return the exact spelling from the current team).
  Example triggers: "remove the analyst", "drop the writer", "delete
  Growth Lead".

- "modify_employee": user wants to change what an existing role does.
  Fields: role (existing team role), new_mandate.
  Example triggers: "change the Marketer's job to focus on paid ads",
  "the Analyst should also handle competitor research".

- "design_team": user wants a fresh team designed from scratch (this
  REPLACES the current team).
  Fields: prompt (the underlying project description).
  Example triggers: "start over with a team for a fitness app", "build
  me a new team for launching a Substack", "redesign the team for X".

- "clear_team": user wants to remove everyone with no replacement.
  Example triggers: "clear the team", "remove everyone", "reset".

- "run_task": user is giving the team a task to actually work on.
  Fields: task (the exact task text — usually just the user's message
  verbatim).
  Example triggers: "write our launch plan", "give me a positioning
  statement", "draft the first-week outreach emails", "how should we
  price this", "produce the go-to-market memo".

- "unclear": you cannot confidently pick from above. Only use this as
  a last resort.

Return JSON only:
{{"intent": "...", "role": null, "mandate": null, "new_mandate": null,
  "prompt": null, "task": null, "reason": "one sentence"}}

JSON only."""


class ChatIntentClassifier:
    def __init__(self, model_adapter: Any, max_tokens: int = DEFAULT_MAX_TOKENS):
        self.adapter = model_adapter
        self.max_tokens = max_tokens

    def classify(self, message: str, team_roles: List[Dict[str, str]]) -> Dict[str, Any]:
        team_snapshot = self._render_team_snapshot(team_roles)
        prompt = CLASSIFY_PROMPT.format(team_snapshot=team_snapshot, message=message)

        try:
            response = self.adapter.chat_completion(
                prompt, temperature=0.1, max_tokens=self.max_tokens
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Chat intent classification failed: %s", exc)
            return {"intent": "run_task", "task": message, "reason": "classification_error"}

        data = self._extract_json(response) or {}
        intent = data.get("intent")
        if intent not in VALID_INTENTS:
            logger.warning("Chat intent unparseable/invalid (%r), defaulting to run_task", intent)
            return {"intent": "run_task", "task": message, "reason": "unparseable_response"}

        # For run_task with no explicit task, use the raw message
        if intent == "run_task" and not (data.get("task") or "").strip():
            data["task"] = message
        return data

    def _render_team_snapshot(self, team_roles: List[Dict[str, str]]) -> str:
        if not team_roles:
            return "(no employees yet)"
        lines = []
        for m in team_roles:
            role = m.get("role", "")
            mandate = m.get("mandate", "")
            lines.append(f"- {role}: {mandate[:120]}")
        return "\n".join(lines)

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
