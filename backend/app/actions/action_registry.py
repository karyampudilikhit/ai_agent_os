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
    # True for tools whose whole point is to carry a large, carefully
    # written deliverable (a full slide deck, a full document) as their
    # arguments. The MCPPlanner's pre-flight tool-call step happens
    # BEFORE a specialist has written anything — asking it to also
    # freehand 5 slides of real content in one cramped, token-capped
    # JSON blob produces empty/placeholder output (confirmed: this is
    # exactly what create_pptx got the first time it was wired in —
    # every slide came back as {} and rendered as "Slide 1", "Slide 2"
    # with zero bullets). These tools are still directly callable
    # (registry.call), just hidden from the planner's tool listing;
    # the real path to a real file is the post-synthesis auto-convert
    # in routes._maybe_generate_document, which works off the
    # specialist's ALREADY-WRITTEN, properly-reasoned deliverable text.
    planner_excluded: bool = False
    # True for tools meant to be called repeatedly with the SAME
    # arguments while something else finishes in the background (e.g.
    # browser_task_status polling a session token). The agentic loop's
    # repeat-call guard (execution_loop.py) exists to stop a model stuck
    # calling the same thing forever expecting a different answer — but
    # for a poll, an identical call is the correct next step, not a
    # stuck loop. See execution_loop.py's `pollable_names` handling.
    pollable: bool = False
    # Abstract capability tag ("web.form.fill", "repo.create") — the
    # indirection the wider architecture needs so a caller can ask the
    # ToolRegistry (backend/app/tools/tool_registry.py) "give me
    # whatever can do web.form.fill" instead of hardcoding this spec's
    # `name`. Optional and unset ("") for most existing built-ins today
    # — ToolRegistry falls back to qualified_name() when empty, so
    # leaving it blank is safe, just less abstract. Not yet consumed by
    # the planner prompt itself (see execution_loop.py's STEP_PROMPT,
    # which still shows qualified_name) — that's the next step once
    # enough tools carry a real tag to make capability-based selection
    # worthwhile over plain listing.
    capability: str = ""

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

    def list_tools(self, for_planner: bool = False) -> List[Dict[str, Any]]:
        """for_planner=True hides tools marked planner_excluded — see
        ActionSpec.planner_excluded for why. GET /api/actions (the
        founder-visible listing) always calls this with the default,
        so every registered action still shows up there."""
        out: List[Dict[str, Any]] = []
        for spec in self._actions.values():
            if for_planner and spec.planner_excluded:
                continue
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
                    "pollable": spec.pollable,
                    "capability": spec.capability or spec.qualified_name(),
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

        type_error = _check_arg_types(spec, args)
        if type_error:
            return type_error
        return self._dispatch(spec, args)


    def _dispatch(self, spec: "ActionSpec", args: Dict[str, Any]) -> str:
        """Enqueue-or-run. Split out of call() only so the validation
        there reads as a guard clause instead of being buried above 40
        lines of dispatch."""
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


# Declared type -> what Python types are acceptable. Deliberately
# permissive where coercion is unambiguous (an int is a fine "number",
# and "42" is a fine integer), strict where it hides a real mistake.
_TYPE_CHECKS = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def _check_arg_types(spec: "ActionSpec", args: Dict[str, Any]) -> Optional[str]:
    """Reject an argument whose type contradicts the spec, and say what
    was expected.

    Why this exists: asked to play a game whose id was stated verbatim in
    the prompt ('vc33-5430563c'), the planner called arc_reset with the
    INTEGER 0, then 1, then 2, then 3 — against a parameter declared
    "string". Nothing checked, so each call reached the tool, failed
    somewhere downstream with a generic error, and the model simply
    guessed a different wrong value. Eight attempts, zero valid actions.

    A type mismatch is one of the few planner mistakes that is provable
    at the boundary, and saying "expected a string like 'vc33-5430563c'"
    is far more recoverable than "game 0 not found". Booleans are checked
    before integers because bool is a subclass of int in Python.
    """
    declared = {p["name"]: p for p in spec.parameters}
    for name, value in (args or {}).items():
        p = declared.get(name)
        if not p or value is None:
            continue
        expected = str(p.get("type") or "").strip().lower()
        allowed = _TYPE_CHECKS.get(expected)
        if not allowed:
            continue
        if expected != "boolean" and isinstance(value, bool):
            ok = False  # True is not a sensible string/int/number here
        elif expected == "string":
            ok = isinstance(value, str)
        elif expected == "integer":
            # "42" is an honest integer; 42.0 is too. 42.5 is not.
            ok = isinstance(value, int) or (
                isinstance(value, (str, float))
                and str(value).strip().lstrip("-").replace(".0", "").isdigit()
            )
        else:
            ok = isinstance(value, allowed)
        if ok:
            continue

        hint = str(p.get("description") or "").strip()
        hint = f" {hint}" if hint else ""
        return (
            f"(invalid argument {name!r}: expected {expected}, got "
            f"{type(value).__name__} {value!r}.{hint} "
            f"Re-read the task for the correct value — do not guess.)"
        )
    return None


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
    from backend.app.actions.builtin import (
        send_email, post_slack, write_file, read_file, read_inbox, reply_email,
        create_pptx, create_docx, create_xlsx, browser_task, create_github_repo,
        calculate, arc_game,
    )
    registry.register(send_email.SPEC)
    registry.register(reply_email.SPEC)
    registry.register(read_inbox.SPEC)
    registry.register(post_slack.SPEC)
    registry.register(write_file.SPEC)
    registry.register(read_file.SPEC)
    registry.register(create_pptx.SPEC)
    registry.register(create_docx.SPEC)
    registry.register(create_xlsx.SPEC)
    registry.register(create_github_repo.SPEC)
    registry.register(calculate.SPEC)
    registry.register(browser_task.BROWSER_TASK_SPEC)
    registry.register(browser_task.BROWSER_TASK_ASYNC_SPEC)
    registry.register(browser_task.BROWSER_TASK_STATUS_SPEC)
    registry.register(browser_task.BROWSER_NAVIGATE_SPEC)
    registry.register(browser_task.BROWSER_EXTRACT_SPEC)
    registry.register(browser_task.BROWSER_EXTRACT_TABLE_SPEC)
    registry.register(browser_task.BROWSER_CLICK_SPEC)
    registry.register(browser_task.BROWSER_LOGIN_WAIT_SPEC)
    registry.register(browser_task.BROWSER_SUBMIT_SPEC)
    registry.register(arc_game.ARC_RESET_SPEC)
    registry.register(arc_game.ARC_CLICK_SPEC)
