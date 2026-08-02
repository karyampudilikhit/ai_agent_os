"""AgenticExecutor — the real multi-step tool-use loop.

This is the Phase-2 upgrade MCPPlanner's docstring promised. The
difference is the whole ballgame for "Vision AI does the actual work":

  MCPPlanner (old):  ONE planning call picks up to 4 tools BLIND —
                     before seeing a single result — fires them flat,
                     then the specialist writes. It cannot react to
                     what a tool returned, cannot chain work whose
                     shape is only discovered mid-task, and cannot
                     retry a failed call differently.

  AgenticExecutor:   THINK -> ACT -> OBSERVE -> repeat. The model sees
                     each result before choosing the next action, so
                     dependent multi-step work becomes possible:
                     "find 10 investors, then draft an email to each",
                     "check their pricing page, and IF there's an
                     enterprise tier, write the comparison".

What this deliberately does NOT change:
  - The loop GATHERS and ACTS; it does not write the deliverable. Its
    transcript is injected as context and the specialist writes from
    it, so the critique/verification pass (the no-fabrication USP)
    still runs over the shipped text exactly as before.
  - Approval gating is untouched. A mutating action still enqueues and
    returns a "[queued for founder approval]" receipt; the loop treats
    that receipt as an observation and moves on. It never blocks
    waiting for a tap, and never fires an unapproved action.
  - browser_task (the SYNCHRONOUS, founder-present entry point) stays
    out of the loop (planner_excluded) — it can block for minutes
    waiting on a human login, which would stall every other step; it
    keeps its own explicit-dispatch path in dynamic_employee. The loop
    CAN reach browser automation, just through the non-blocking pair
    browser_task_async (returns at once, runs the real flow on a
    background thread) + browser_task_status (poll for progress) — see
    browser_task.py's module docstring. browser_task_status is marked
    `pollable` on its ActionSpec, which exempts it from the repeat-call
    guard below: calling it again with the SAME session_token while
    waiting is the correct next step, not a stuck loop.

Bounds, because an unbounded agent loop is how you burn an LLM quota
in one task: MAX_STEPS, a wall-clock deadline, a per-observation
character cap, and repeat-call detection that breaks a model stuck
calling the same thing forever.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from backend.app.actions.action_registry import CONNECTION_NAMESPACE as ACTION_NAMESPACE
from backend.app.actions.action_registry import get_registry as get_action_registry
from backend.app.tools.http_tool_runner import CONNECTION_NAMESPACE as HTTP_NAMESPACE
from backend.app.tools.http_tool_runner import HTTPToolRunner
from backend.app.tools.mcp_client import get_registry as get_mcp_registry

logger = logging.getLogger(__name__)

_http_runner = HTTPToolRunner()
_action_registry = get_action_registry()

MAX_STEPS = 10               # hard ceiling on THINK->ACT cycles
DEADLINE_SECONDS = 240.0     # wall-clock budget for the whole loop
MAX_OBSERVATION_CHARS = 1500  # per-tool-result cap, keeps context bounded
MAX_TRANSCRIPT_CHARS = 9000   # total transcript cap fed back into the prompt
STEP_MAX_TOKENS = 700


STEP_PROMPT = """You are {role}, working on a task. You can call tools one at a time.
After each call you SEE the result, then decide the next action. Work
step by step until the task is genuinely done.

YOUR TASK:
"{task}"

TOOLS YOU CAN CALL:
{tools_block}

WORK SO FAR (your previous actions and their real results):
{transcript}

Decide the SINGLE next action.

Rules:
- Call ONE tool per step. Base it on what the results above actually
  say — not on what you assumed before calling.
- If a call failed or returned nothing useful, try a DIFFERENT approach
  or different arguments. Do not repeat an identical call.
- If a tool result says something is "queued for founder approval",
  that action has NOT happened yet. That is expected and correct —
  treat it as done-for-now and move on. Never re-queue the same action.
- If a tool result tells you to poll it again (e.g. browser_task_status),
  calling it again with the same arguments is correct and expected —
  that is not a repeated mistake, it's checking on background progress.
- When you have everything needed to write the deliverable, or no
  remaining tool would help, return the DONE action. Do not keep
  calling tools just to look busy.
- You have used {steps_used} of {max_steps} steps.

Return JSON only, one of these two shapes:
{{"thought": "<one sentence: why this action next>", "action": "<connection.tool_name>", "arguments": {{...}}}}
{{"thought": "<one sentence: why you're finished>", "action": "DONE"}}

JSON only."""


class AgenticExecutor:
    """Runs a bounded THINK -> ACT -> OBSERVE loop for one specialist.

    `run()` returns a prompt-injectable transcript of what ACTUALLY
    happened (real calls, real results), or None when no tools are
    available / nothing was done — matching MCPPlanner's contract so
    callers can swap between them.
    """

    def __init__(
        self,
        model_adapter: Any,
        max_steps: int = MAX_STEPS,
        deadline_seconds: float = DEADLINE_SECONDS,
    ):
        self.adapter = model_adapter
        self.max_steps = max_steps
        self.deadline_seconds = deadline_seconds

    # ---- tool surface -------------------------------------------------

    def _available_tools(self) -> List[Dict[str, Any]]:
        """Same unified surface the old planner used: MCP servers the
        founder connected + their custom HTTP tools + built-in actions.
        for_planner=True hides tools whose arguments are a whole
        deliverable (create_pptx/docx/xlsx — those fire post-synthesis
        from finished text) and tools that block on a human."""
        try:
            mcp_tools = get_mcp_registry().list_all_tools()
        except Exception:  # noqa: BLE001
            mcp_tools = []
        try:
            http_tools = _http_runner.list_tools()
        except Exception:  # noqa: BLE001
            http_tools = []
        try:
            action_tools = _action_registry.list_tools(for_planner=True)
        except Exception:  # noqa: BLE001
            action_tools = []
        return mcp_tools + http_tools + action_tools

    def _render_tools(self, tools: List[Dict[str, Any]]) -> str:
        lines = []
        for t in tools:
            schema = t.get("input_schema") or {}
            props = list((schema.get("properties") or {}).keys())[:8]
            params = f"  params: {', '.join(props)}" if props else ""
            lines.append(f"- {t['qualified_name']}: {str(t.get('description') or '')[:180]}" + (f"\n{params}" if params else ""))
        return "\n".join(lines)

    # ---- execution ----------------------------------------------------

    def _execute(self, qname: str, args: Dict[str, Any]) -> str:
        """Route one call by namespace. Never raises — a failure is an
        observation the model can react to, not a crashed run."""
        try:
            namespace = qname.split(".", 1)[0]
            if namespace == HTTP_NAMESPACE:
                return _http_runner.call(qname, args)
            if namespace == ACTION_NAMESPACE:
                return _action_registry.call(qname, args)
            return get_mcp_registry().call(qname, args)
        except Exception as exc:  # noqa: BLE001
            return f"(call failed: {exc})"

    def run(self, task: str, role: str = "Specialist") -> Optional[str]:
        task = (task or "").strip()
        if not task:
            return None
        tools = self._available_tools()
        if not tools:
            return None

        tools_block = self._render_tools(tools)
        known_names = {t["qualified_name"] for t in tools}
        # Tools marked pollable (browser_task_status) are MEANT to be
        # called again with identical arguments while something else
        # finishes in the background — exempt them from the repeat-call
        # guard below, which exists to stop a model stuck calling the
        # same thing hoping for a different answer, not to stop a poll.
        pollable_names = {t["qualified_name"] for t in tools if t.get("pollable")}
        deadline = time.monotonic() + self.deadline_seconds

        steps: List[Dict[str, str]] = []   # rendered transcript entries
        seen_calls: set = set()            # (qname, args-json) repeat guard
        acted = False

        for step_i in range(self.max_steps):
            if time.monotonic() > deadline:
                logger.info("[%s] agentic loop hit wall-clock deadline at step %d", role, step_i)
                steps.append({"note": "(stopped: time budget for tool use reached)"})
                break

            transcript = self._render_transcript(steps)
            try:
                raw = self.adapter.chat_completion(
                    STEP_PROMPT.format(
                        role=role,
                        task=task[:1200],
                        tools_block=tools_block,
                        transcript=transcript,
                        steps_used=step_i,
                        max_steps=self.max_steps,
                    ),
                    temperature=0.1,
                    max_tokens=STEP_MAX_TOKENS,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] agentic step call failed: %s", role, exc)
                break

            decision = _extract_json(raw) or {}
            action = str(decision.get("action") or "").strip()
            thought = str(decision.get("thought") or "").strip()

            if not action or action.upper() == "DONE":
                logger.info("[%s] agentic loop finished after %d step(s)", role, step_i)
                break

            if action not in known_names:
                # Unknown tool: tell the model, let it correct itself.
                steps.append({
                    "thought": thought,
                    "call": f"{action}(...)",
                    "result": f"(no such tool: {action!r}. Pick one from the tool list exactly as written.)",
                })
                continue

            args = decision.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}

            fingerprint = (action, json.dumps(args, sort_keys=True)[:400])
            if action not in pollable_names:
                if fingerprint in seen_calls:
                    logger.info("[%s] agentic loop repeated an identical call, stopping", role)
                    steps.append({"note": f"(stopped: repeated the same {action} call — no new information)"})
                    break
                seen_calls.add(fingerprint)

            logger.info("[%s] agentic step %d: %s(%s)", role, step_i + 1, action, json.dumps(args)[:120])
            result = self._execute(action, args)
            acted = True
            steps.append({
                "thought": thought,
                "call": f"{action}({json.dumps(args, ensure_ascii=False)[:300]})",
                "result": _truncate(result, MAX_OBSERVATION_CHARS),
            })

        if not acted:
            return None
        return self._wrap(steps)

    # ---- rendering ----------------------------------------------------

    def _render_transcript(self, steps: List[Dict[str, str]]) -> str:
        if not steps:
            return "(nothing yet — this is your first action)"
        chunks = []
        for i, s in enumerate(steps, 1):
            if s.get("note"):
                chunks.append(s["note"])
                continue
            chunks.append(
                f"Step {i}:\n"
                f"  thought: {s.get('thought', '')}\n"
                f"  called:  {s.get('call', '')}\n"
                f"  result:  {s.get('result', '')}"
            )
        text = "\n\n".join(chunks)
        # Keep the most RECENT context when trimming — the latest results
        # are what the next decision depends on.
        if len(text) > MAX_TRANSCRIPT_CHARS:
            text = "[…earlier steps trimmed…]\n\n" + text[-MAX_TRANSCRIPT_CHARS:]
        return text

    def _wrap(self, steps: List[Dict[str, str]]) -> str:
        return (
            "REAL WORK YOU ALREADY DID (multi-step tool use)\n"
            "(You called these tools one at a time and saw each result before "
            "the next. Use these REAL results as your source data — do not "
            "invent facts they didn't return. If a result says something is "
            "queued for founder approval, it has NOT happened yet: say so "
            "plainly, don't imply it's done.)\n\n"
            + self._render_transcript(steps)
            + "\n"
        )


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) > limit:
        return text[:limit].rstrip() + "\n[…result truncated…]"
    return text


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    stripped = _JSON_FENCE_RE.sub("", text.strip()).strip()
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
