"""ToolRegistry — the single place that lists and dispatches every tool
the AI can call, across the three systems this codebase has grown:
built-in actions (action_registry.py), founder-added HTTP tools
(http_tool_runner.py), and connected MCP servers (mcp_client.py).

Why this exists: those three systems already share an identical shape by
convention —
    list_tools() -> [{"qualified_name", "connection", "tool",
                       "description", "input_schema", ...}]
    call(qualified_name, arguments) -> str   (never raises, action/http;
                                               DOES raise, mcp — normalized
                                               here)
— but every caller that wants "all the tools" (execution_loop.py,
mcp_planner.py, routes.py) has re-implemented the same three-way union
and the same qname-prefix dispatch by hand. That duplication is exactly
what makes "add a new tool TYPE" (not just a new tool) expensive: three
places to update instead of one. This module is that one place.

Migration status (deliberately incremental, not a flag-day rewrite —
see [[vision-ai-browser-automation-priority]] and HANDOFF.md): as of
this pass, execution_loop.py (the agentic loop) is migrated to call
this facade instead of importing all three systems directly. routes.py,
dynamic_employee.py's explicit browser_task pre-flight dispatch, and the
legacy mcp_planner.py still call action_registry / http_tool_runner /
mcp_client directly — that's SAFE (this is a pure facade over the same
underlying singletons, not a new source of truth) and left alone
because touching the live API surface (routes.py) carries more risk
than benefit for this pass. Migrating them is a follow-up, not a
prerequisite for anything else in flight.

Capability tags: `list_tools()` output now always carries a
"capability" key. For built-in actions it's ActionSpec.capability when
the spec author set one (e.g. browser_task_async -> "web.form.fill",
create_github_repo -> "repo.create"); tools that haven't been tagged
yet fall back to their own qualified_name, which still works as a
resolvable capability, just a less abstract one. Same fallback for
MCP/HTTP tools, which are founder-defined at connect-time and have no
fixed taxonomy to tag against. `resolve_capability()` lets a caller ask
for "give me whatever can do web.form.fill" instead of hardcoding a
tool name — the mechanism the wider architecture needs so the planner
never has to change when a new tool is registered.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.app.actions.action_registry import CONNECTION_NAMESPACE as ACTION_NAMESPACE
from backend.app.actions.action_registry import get_registry as get_action_registry
from backend.app.tools.http_tool_runner import CONNECTION_NAMESPACE as HTTP_NAMESPACE
from backend.app.tools.http_tool_runner import HTTPToolRunner
from backend.app.tools.mcp_client import get_registry as get_mcp_registry

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Aggregated discovery + dispatch across action/HTTP/MCP tools."""

    def __init__(self) -> None:
        # Each underlying system already keeps its own singleton state
        # (action_registry's approval queue, http_tool_runner's store,
        # mcp_client's connections) — this class holds no tool state of
        # its own, just a stateless HTTPToolRunner wrapper matching the
        # pattern used everywhere else this system is unioned.
        self._http_runner = HTTPToolRunner()

    # ---- discovery ---------------------------------------------------

    def list_tools(self, for_planner: bool = False) -> List[Dict[str, Any]]:
        """Every tool from every source, in one list. `for_planner=True`
        additionally hides action tools marked planner_excluded (see
        ActionSpec.planner_excluded) — MCP/HTTP tools have no such
        concept today, so they're always included."""
        try:
            mcp_tools = get_mcp_registry().list_all_tools()
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool_registry: MCP listing failed: %s", exc)
            mcp_tools = []
        try:
            http_tools = self._http_runner.list_tools()
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool_registry: HTTP tool listing failed: %s", exc)
            http_tools = []
        try:
            action_tools = get_action_registry().list_tools(for_planner=for_planner)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool_registry: action listing failed: %s", exc)
            action_tools = []

        out: List[Dict[str, Any]] = []
        for t in mcp_tools + http_tools + action_tools:
            if "capability" not in t:
                t = {**t, "capability": t["qualified_name"]}
            out.append(t)
        return out

    def resolve_capability(self, capability: str, for_planner: bool = True) -> Optional[str]:
        """Best-match a capability tag to a concrete qualified tool name,
        or None if nothing registered claims it. First match wins — if
        two enabled tools ever claim the same capability, whichever
        listed first (MCP, then HTTP, then built-in actions) is used;
        no ranking logic beyond that exists yet."""
        for t in self.list_tools(for_planner=for_planner):
            if t.get("capability") == capability:
                return t["qualified_name"]
        return None

    # ---- dispatch ------------------------------------------------------

    def call(self, qualified_name: str, arguments: Dict[str, Any]) -> str:
        """Route one call by namespace prefix. Never raises — MCP's
        underlying .call() DOES raise (MCPConnectionError etc.); this
        normalizes that to the same '(call failed: ...)' shape the
        action/HTTP systems already return, so every caller can treat
        the result as plain text regardless of which system handled it."""
        if "." not in qualified_name:
            return f"(invalid tool name: {qualified_name!r})"
        namespace = qualified_name.split(".", 1)[0]
        try:
            if namespace == HTTP_NAMESPACE:
                return self._http_runner.call(qualified_name, arguments)
            if namespace == ACTION_NAMESPACE:
                return get_action_registry().call(qualified_name, arguments)
            return get_mcp_registry().call(qualified_name, arguments)
        except Exception as exc:  # noqa: BLE001
            return f"(call failed: {exc})"


_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
