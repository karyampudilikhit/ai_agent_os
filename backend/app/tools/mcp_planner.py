"""MCPPlanner — the "which tools should this employee call" pre-flight.

Before an employee runs its sub-task, we ask a cheap LLM call:
"given this task and these tools, which should you call?" We then
execute those calls on the employee's behalf and hand the results back
as context.

This is deliberately not a full tool-use loop — the LLM doesn't get to
call a tool, see the result, then call another tool based on it. That's
a Phase-2 upgrade. For now, one round of "pick all the tools you'll
need up-front, we call them all, then you write your output" gets 80%
of the value with 20% of the complexity.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from backend.app.tools.http_tool_runner import CONNECTION_NAMESPACE as HTTP_NAMESPACE
from backend.app.tools.http_tool_runner import HTTPToolRunner
from backend.app.tools.mcp_client import get_registry

logger = logging.getLogger(__name__)

# Shared runner for user-defined HTTP tools. Uniform interface with the
# MCP registry — the planner merges tools from both when picking calls.
_http_runner = HTTPToolRunner()

MAX_PLAN_TOKENS = 800
MAX_TOOL_CALLS_PER_TASK = 4  # bound so an over-eager LLM can't burn quota


PLAN_PROMPT = """You are a specialist about to work on a task. Before you write anything,
decide which external tools (if any) to CALL first so you have real
information to work with — instead of guessing.

TASK:
"{task}"

AVAILABLE TOOLS:
{tools_json}

Rules:
- Only call tools that are directly useful for THIS task.
- If none are useful, return an empty list. That's fine — write from
  what you already know.
- Prefer READ-ONLY tools (search, list, get, read). Do NOT call tools
  that write / create / update / delete / send unless the task EXPLICITLY
  asks for that action ("post to my Notion", "email X", "create a page").
- Each tool call must include VALID JSON arguments matching the tool's
  input_schema. Don't invent argument names.
- Maximum {max_calls} calls.

Return JSON only:
{{"calls": [
  {{"tool": "connection.tool_name", "arguments": {{...}}}},
  ...
]}}

JSON only."""


class MCPPlanner:
    def __init__(self, model_adapter: Any):
        self.adapter = model_adapter

    def plan_and_execute(self, task: str) -> Optional[str]:
        """Decide which tools to call for this task, call them, return
        a formatted block ready to inject into the employee's prompt.
        Returns None if no tools available or none picked.

        Tools come from two sources unified into one listing:
          - MCP servers the user connected in the sidebar
          - User-defined HTTP tools from HTTPToolStore
        Both use qualified names ("connection.tool") — the executor
        routes to the right backend by the connection namespace.
        """
        registry = get_registry()
        mcp_tools = registry.list_all_tools()
        http_tools = _http_runner.list_tools()
        tools = mcp_tools + http_tools
        if not tools:
            return None

        tools_json = self._render_tools_for_prompt(tools)
        try:
            response = self.adapter.chat_completion(
                PLAN_PROMPT.format(
                    task=task, tools_json=tools_json, max_calls=MAX_TOOL_CALLS_PER_TASK
                ),
                temperature=0.1,
                max_tokens=MAX_PLAN_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP planning call failed: %s", exc)
            return None

        data = self._extract_json(response) or {}
        calls = data.get("calls") or []
        if not isinstance(calls, list) or not calls:
            return None

        outputs: List[str] = []
        for entry in calls[:MAX_TOOL_CALLS_PER_TASK]:
            if not isinstance(entry, dict):
                continue
            qname = str(entry.get("tool") or "").strip()
            args = entry.get("arguments") or {}
            if not qname or "." not in qname:
                continue
            if not isinstance(args, dict):
                args = {}
            logger.info("Tool call: %s(%s)", qname, json.dumps(args)[:120])
            # Route by namespace: HTTP tools go to the HTTP runner, MCP
            # tools to the MCP registry. Same call signature both sides.
            try:
                namespace = qname.split(".", 1)[0]
                if namespace == HTTP_NAMESPACE:
                    result_text = _http_runner.call(qname, args)
                else:
                    result_text = registry.call(qname, args)
            except Exception as exc:  # noqa: BLE001
                result_text = f"(call failed: {exc})"
            outputs.append(self._format_call(qname, args, result_text))

        if not outputs:
            return None
        return self._wrap_block(outputs)

    # ------------------------------------------------------------------

    def _render_tools_for_prompt(self, tools: List[Dict]) -> str:
        """Compact tool listing. We don't dump the full input_schema —
        just enough for the LLM to write valid arguments."""
        rendered = []
        for t in tools:
            schema_hint = ""
            schema = t.get("input_schema") or {}
            if isinstance(schema, dict) and schema.get("properties"):
                params = list(schema["properties"].keys())[:8]
                schema_hint = f"  params: {', '.join(params)}"
            rendered.append(
                f"- {t['qualified_name']}: {t['description'][:200]}" + (f"\n{schema_hint}" if schema_hint else "")
            )
        return "\n".join(rendered)

    def _format_call(self, qname: str, args: Dict, result_text: str) -> str:
        args_pretty = json.dumps(args, ensure_ascii=False)
        return f"=== {qname}({args_pretty}) ===\n{result_text}"

    def _wrap_block(self, sections: List[str]) -> str:
        return (
            "REAL EXTERNAL TOOL RESULTS\n"
            "(You called these tools before starting. Use their output as "
            "source data — do not invent facts these tools didn't return.)\n\n"
            + "\n\n".join(sections)
            + "\n"
        )

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
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                return None
        return None
