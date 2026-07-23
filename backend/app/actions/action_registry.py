"""ActionRegistry — third leg of the tool listing (alongside MCP + HTTP).

Actions are Python callables the AI can invoke. Unlike HTTP tools they
can touch the process (SMTP, filesystem, native SDKs), so we vet each
one by hand and put them in `builtin/`.

Mutating actions (send email, post Slack, write file, transfer money)
never fire directly from the planner. They enqueue on ApprovalQueue and
return a receipt string. Read-only actions (read_file, list_files) run
inline.

Interface matches HTTPToolRunner:
    - list_tools() -> MCP-shaped listings with namespace "action"
    - call(qualified_name, arguments) -> str (never raises)
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from backend.app.actions.approval_queue import ApprovalQueue, get_queue

logger = logging.getLogger(__name__)

CONNECTION_NAMESPACE = "action"
MAX_RESULT_CHARS = 6000


@dataclass
class ActionSpec:
    """One built-in action.

    handler(arguments: dict) -> str  — returns a text block for the LLM.
    preview(arguments: dict) -> str  — one-line human-readable summary
                                       shown in the approval UI.
    Params follow HTTP tool spec shape so the planner renders them the
    same way (see http_tool_runner._render_tool_info).
    """

    name: str
    description: str
    parameters: List[Dict[str, Any]]
    handler: Callable[[Dict[str, Any]], str]
    preview: Callable[[Dict[str, Any]], str]
    mutating: bool = True

    def qualified_name(self) -> str:
        return f"{CONNECTION_NAMESPACE}.{self.name}"


class ActionRegistry:
    def __init__(self, queue: Optional[ApprovalQueue] = None):
        self._actions: Dict[str, ActionSpec] = {}
        self._queue = queue or get_queue()

    # ---- registration ----------------------------------------------

    def register(self, spec: ActionSpec) -> None:
        if spec.name in self._actions:
            raise ValueError(f"duplicate action name {spec.name!r}")
        self._actions[spec.name] = spec

    def known_names(self) -> List[str]:
        return sorted(self._actions.keys())

    # ---- discovery (MCP-shape) -------------------------------------

    def list_tools(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for spec in self._actions.values():
            properties: Dict[str, Dict[str, str]] = {}
            required: List[str] = []
            for p in spec.parameters:
                properties[p["name"]] = {
                    "type": p.get("type", "string"),
                    "description": p.get("description", ""),
                }
                if p.get("required"):
                    required.append(p["name"])
            prefix = "[MUTATING — asks approval] " if spec.mutating else "[READ] "
            out.append(
                {
                    "qualified_name": spec.qualified_name(),
                    "connection": CONNECTION_NAMESPACE,
                    "tool": spec.name,
                    "description": f"{prefix}{spec.description}".strip(),
                    "input_schema": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                }
            )
        return out

    # ---- execution -------------------------------------------------

    def call(self, qualified_name: str, arguments: Dict[str, Any]) -> str:
        if "." not in qualified_name:
            return f"(invalid tool name: {qualified_name!r})"
        namespace, tool_name = qualified_name.split(".", 1)
        if namespace != CONNECTION_NAMESPACE:
            return f"(not an action tool: {qualified_name!r})"
        spec = self._actions.get(tool_name)
        if not spec:
            return f"(no action named {tool_name!r})"
        args = arguments or {}
        missing = [p["name"] for p in spec.parameters if p.get("required") and p["name"] not in args]
        if missing:
            return f"(missing required arguments: {', '.join(missing)})"

        if spec.mutating:
            try:
                preview = spec.preview(args)
            except Exception as exc:  # noqa: BLE001
                preview = f"{spec.name}({json.dumps(args)[:120]})"
            try:
                record = self._queue.enqueue(
                    action_name=spec.name,
                    arguments=args,
                    preview=preview,
                )
            except Exception as exc:  # noqa: BLE001
                return f"(could not enqueue: {exc})"
            return (
                f"[queued for founder approval — pending_id={record['id']}]\n"
                f"action: {spec.name}\n"
                f"preview: {preview}\n"
                f"This action will NOT execute until the founder approves it "
                f"in the Pending Actions panel. Do not assume it has run."
            )

        try:
            result = spec.handler(args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("action %s failed: %s", spec.name, exc)
            return f"(action failed: {exc})"
        return _truncate(result)

    def execute_now(self, action_id: str) -> Dict[str, Any]:
        """Run an already-approved queued action against the environment.
        Returns the updated queue record. Only called by the API's
        approve endpoint — the planner never calls this."""
        record = self._queue.get(action_id)
        if not record:
            return {"error": f"no pending action {action_id!r}"}
        if record["status"] != "approved":
            return {"error": f"action not in approved state (was {record['status']!r})"}
        spec = self._actions.get(record["action_name"])
        if not spec:
            updated = self._queue.set_status(
                action_id, "failed", error=f"unknown action {record['action_name']!r}"
            )
            return updated
        try:
            result = spec.handler(record["arguments"] or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("approved action %s failed: %s", spec.name, exc)
            updated = self._queue.set_status(action_id, "failed", error=str(exc))
            return updated
        updated = self._queue.set_status(action_id, "executed", result=_truncate(result))
        return updated


def _truncate(text: str) -> str:
    text = str(text or "")
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS].rstrip() + "\n[…result truncated…]"
    return text


# ---- module-level singleton ----------------------------------------

_registry: Optional[ActionRegistry] = None


def get_registry() -> ActionRegistry:
    global _registry
    if _registry is None:
        _registry = ActionRegistry()
        _load_builtins(_registry)
    return _registry


def _load_builtins(registry: ActionRegistry) -> None:
    """Register the first-party built-ins. Kept lazy so an import-time
    error in one built-in doesn't kill the whole module."""
    from backend.app.actions.builtin import send_email, post_slack, write_file, read_file
    registry.register(send_email.SPEC)
    registry.register(post_slack.SPEC)
    registry.register(write_file.SPEC)
    registry.register(read_file.SPEC)
