"""UniversalChatRouter — one prompt-first entry point for the whole app.

Kills the "click here, then type in that box, then toggle this mode"
UX. The founder types anything and this router figures out what they
meant and does it. No mode toggle. No wrong-path errors.

Intents (v1):
  create_company           Founder wants a new Company. Extracts name +
                           purpose from the message (or falls back to a
                           sensible default when only a description is
                           given). If the message ALSO carries a
                           description worth designing against, we
                           chain a design pass in the same turn.
  design_hierarchy         Founder wants the CEO to design the org
                           chart for the current Company from a
                           description in the message.
  apply_proposal           Founder confirmed the pending proposal
                           (words like "yes", "apply", "go ahead").
  discard_proposal         Founder rejected the pending proposal.
  run_task_company         Founder gave the CEO a task to actually do
                           (delegate across Units + synthesize).
  run_task_unit            Founder gave a specific Unit's Supervisor a
                           task — kicks the classic Unit pipeline.
  casual_chat              Question/greeting/small talk. No side effect.

Response shape:
  {
    "intent": <one of the above>,
    "reply":  <human-facing message to render in chat>,
    "side_effects": {
      "company_id": <if a Company was created or focused>,
      "session_id": <if a Unit was focused / created>,
      "pending_proposal": {units: [...], company_id: ...} | null,
      "org_refreshed": bool,
      "run_output": <if a task ran — the final deliverable>,
      "evidence": [...],
    }
  }
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


VALID_INTENTS = {
    "create_company",
    "design_hierarchy",
    "apply_proposal",
    "discard_proposal",
    "run_task_company",
    "run_task_unit",
    "casual_chat",
}

CLASSIFY_PROMPT = """You are the front-door router for Vision AI — an app where a founder
runs a company of AI employees. The founder types ONE message. Your job
is to pick the ONE intent that matches what they want and extract only
the fields for that intent. Do not narrate. Return JSON only.

WHAT THE APP HAS RIGHT NOW:
{state_snapshot}

USER MESSAGE:
"{message}"

INTENTS — pick exactly one and fill only its fields:

- "create_company": founder wants to spin up a new Company. Almost any
  message that describes "a company that does X" or "I want to build
  Y" or "set up an AI agency for Z" is this — even if they don't say
  the word "company". Fields:
    - name         (string; if they gave one, use it; otherwise infer a
                    short 1-3 word name from the description)
    - purpose      (string; a one-sentence description of what the
                    company does, derived from the message)
    - description  (string; the fuller paragraph the CEO should design
                    the hierarchy against — usually just the founder's
                    original message, cleaned up)
    - auto_design  (boolean; true if the message also gave enough info
                    to design the org chart in the same turn — which
                    is almost always. Default true.)

- "design_hierarchy": founder ALREADY has a Company (see state) and now
  wants the CEO to propose the org chart. Fields:
    - description  (string; the paragraph the CEO designs against)

- "apply_proposal": founder confirmed the pending proposal shown to
  them last turn. Triggers: "yes", "apply", "go ahead", "do it",
  "hire them", "looks good", "ship it". Only pick this if the state
  shows a pending proposal exists. No fields.

- "discard_proposal": founder rejected the pending proposal. Triggers:
  "no", "cancel", "scrap it", "start over", "redo". No fields.

- "run_task_company": founder gave the CEO a task to actually execute
  (across the whole company). Triggers: "launch our...", "find
  clients for...", "write me a...", "get me...", "research...". Only
  pick this if a Company is currently selected. Fields:
    - task  (string; the raw task text)

- "run_task_unit": same as above but for a specific Unit — only when
  a Unit is selected AND no Company is selected (i.e. classic
  single-Unit playground use). Fields:
    - task  (string)

- "casual_chat": greeting, question about the app, thanks, or anything
  that doesn't need a side effect. Fields:
    - reply  (string; a short friendly reply)

Return JSON only, this shape:
{{"intent": "<one of the above>", "<intent's fields>": ...}}

JSON only. No prose, no code fences, no explanation."""


class UniversalChatRouter:
    def __init__(self, model_adapter: Any):
        self.adapter = model_adapter

    def classify(
        self,
        message: str,
        state_snapshot: str,
    ) -> Dict[str, Any]:
        """Ask the LLM to route the message. Returns the parsed intent
        dict; falls back to {"intent": "casual_chat", "reply": …} on
        any parse error so the chat never dead-ends."""
        try:
            raw = self.adapter.chat_completion(
                CLASSIFY_PROMPT.format(
                    state_snapshot=state_snapshot, message=message
                ),
                temperature=0.1,
                max_tokens=600,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat router LLM call failed: %s", exc)
            return {
                "intent": "casual_chat",
                "reply": (
                    "I couldn't reach the model right now. Try again "
                    "in a moment — if this keeps happening, check that "
                    "Ollama is running."
                ),
            }

        parsed = _extract_json(raw) or {}
        intent = str(parsed.get("intent") or "").strip()
        if intent not in VALID_INTENTS:
            return {
                "intent": "casual_chat",
                "reply": (
                    "I wasn't sure what you meant. Try something like "
                    "'build me an AI agency', or 'run market research on "
                    "our competitors'."
                ),
            }
        return parsed


def build_state_snapshot(
    *,
    current_company: Optional[Dict[str, Any]] = None,
    current_unit_members: Optional[List[Dict[str, Any]]] = None,
    current_session_id: Optional[str] = None,
    pending_proposal: Optional[Dict[str, Any]] = None,
    known_companies: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Compact readable snapshot for the LLM. Only fields that change
    routing go in — verbose state confuses the classifier."""
    lines: List[str] = []

    if current_company:
        lines.append(
            f"- current Company: {current_company.get('name')!r} "
            f"(id {current_company.get('id')}), "
            f"{len(current_company.get('unit_ids') or [])} Unit(s), "
            f"CEO hired: {'yes' if current_company.get('ceo_employee_id') else 'no'}"
        )
        if current_company.get("purpose"):
            lines.append(f"  purpose: {current_company['purpose']}")
    else:
        lines.append("- current Company: none selected")
        if known_companies:
            names = ", ".join(c.get("name", "?") for c in known_companies[:5])
            lines.append(f"  (other Companies that exist: {names})")

    if current_session_id:
        lines.append(f"- current Unit id: {current_session_id}")
        if current_unit_members:
            roles = ", ".join(m.get("role", "?") for m in current_unit_members[:8])
            lines.append(f"  current Unit team: {roles}")

    if pending_proposal:
        units = pending_proposal.get("units") or []
        names = ", ".join(u.get("name", "?") for u in units)
        lines.append(
            f"- PENDING CEO PROPOSAL for Company "
            f"{pending_proposal.get('company_id')}: "
            f"{len(units)} Unit(s) [{names}] — awaiting founder confirmation."
        )
    else:
        lines.append("- no pending proposal")

    return "\n".join(lines) if lines else "(nothing yet)"


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


def format_proposal_for_chat(units: List[Dict[str, Any]]) -> str:
    """Turn the CEO's proposal into a Slack-style chat message the
    founder can eyeball, then say 'yes' or 'no'."""
    if not units:
        return "(the CEO returned an empty proposal — try describing the company more specifically)"
    lines = [f"Here's what I'd stand up — {len(units)} Unit(s):\n"]
    for i, u in enumerate(units, 1):
        name = u.get("name", "?")
        purpose = u.get("purpose") or ""
        specialists = u.get("specialists") or []
        lines.append(f"{i}. **{name}** — {purpose}")
        for s in specialists:
            role = s.get("role", "?")
            mandate = s.get("mandate") or ""
            lines.append(f"   • {role}: {mandate}")
        lines.append("")
    lines.append("Say **yes** to hire everyone, or tell me what to change.")
    return "\n".join(lines)
