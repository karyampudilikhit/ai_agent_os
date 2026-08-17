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
import re
from typing import Any, Dict, List, Optional

from backend.app.actions.action_registry import CONNECTION_NAMESPACE as ACTION_NAMESPACE
from backend.app.actions.action_registry import get_registry as get_action_registry
from backend.app.tools.http_tool_runner import CONNECTION_NAMESPACE as HTTP_NAMESPACE
from backend.app.tools.http_tool_runner import HTTPToolRunner
from backend.app.tools.mcp_client import get_registry as get_mcp_registry

logger = logging.getLogger(__name__)

# Every subsystem reports failure as TEXT starting with a parenthesised
# marker rather than raising, so a caller cannot use try/except to notice
# one. These are the shapes in use across action/http/mcp handlers.
_FAILURE_PREFIXES = (
    "(call failed", "(action failed", "(browser action failed",
    "(refused", "(rejected", "(missing ", "(invalid ", "(no such tool",
    "(download failed", "(deploy failed", "(upload to", "(click on",
    "(typing into", "(could not", "(the browser is busy", "(that browser session",
)

# The list above is a hand-maintained enumeration, and it has now been
# short by one twice. The most recent miss:
#
#     (browser_navigate failed: could not open a browser session ...)
#
# -- a real failure, recorded ok=True, on the very step where the agent
# tried the URL shortcut. It then "recovered" into a different browser
# session and threw away everything it had set up in the first one, with
# nothing in the ledger saying so.
#
# So the SHAPE is matched rather than the exact wording: an opening
# parenthesis, a short identifier or phrase, then a word that means it did
# not work. New handlers get covered on the day they are written instead
# of the day someone notices.
_FAILURE_SHAPE = re.compile(
    r"^\(\s*[a-z0-9_.\- ]{0,48}?"
    r"\b(failed|could not|cannot|would not|is not|was not|were not|"
    r"refused|rejected|denied|not allowed|no such|timed out|unavailable)\b"
)


def _looks_failed(text: str) -> bool:
    """True when a tool result is a failure message rather than a result.

    Kept deliberately broad: a false 'failed' costs a warning line in the
    log, while a false 'succeeded' is what let a browser click fail
    silently and the run report the stale data it already had.
    """
    t = (text or "").lstrip().lower()
    if t.startswith(_FAILURE_PREFIXES):
        return True
    if _FAILURE_SHAPE.match(t):
        return True
    # run_python hands back its traceback whole rather than a marker.
    return t.startswith("python exited with code")


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

    def list_tools(self, for_planner: bool = False,
                   task_hint: str = "") -> List[Dict[str, Any]]:
        """Every tool from every source, in one list. `for_planner=True`
        additionally hides action tools marked planner_excluded (see
        ActionSpec.planner_excluded) — MCP/HTTP tools have no such
        concept today, so they're always included.

        `task_hint` is the task text, used to drop niche action tools the
        task never mentions. Optional so existing callers are unaffected.
        """
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
            action_tools = get_action_registry().list_tools(
                for_planner=for_planner, task_hint=task_hint)
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
                result = self._http_runner.call(qualified_name, arguments)
            elif namespace == ACTION_NAMESPACE:
                result = get_action_registry().call(qualified_name, arguments)
            else:
                result = get_mcp_registry().call(qualified_name, arguments)
        except Exception as exc:  # noqa: BLE001
            result = f"(call failed: {exc})"

        # Every tool call in the system funnels through here, which makes
        # it the one place a run-wide record can be kept without
        # sprinkling bookkeeping across three subsystems. It feeds the
        # claim checker, which compares a deliverable's stated causes
        # against what was actually attempted — see
        # tools/tool_call_ledger.py for the ARC run that made this
        # necessary.
        #
        # Note the failure convention: these subsystems return "(call
        # failed: ...)" as TEXT rather than raising, so success cannot be
        # inferred from the absence of an exception.
        try:
            from backend.app.critique.compute_gate import DATASET_COMPUTE_TOOLS
            from backend.app.tools.tool_call_ledger import get_call_ledger
            text = str(result)
            # A compute tool's OUTPUT is the evidence, so it is kept whole
            # rather than clipped to the 200-char preview. Clipping it hid
            # a real fabrication: the numbers a deliverable reported could
            # not be compared against what the code printed, because the
            # print was cut off before reaching them. See
            # MAX_COMPUTE_OUTPUT_CHARS in tool_call_ledger.
            #
            # Browser tools keep theirs for the same reason, learned the
            # same way: a live run's browser_click_element(e40) failed and
            # the only evidence was a MISSING side effect two log lines
            # later. The failure reason went to the model and nowhere
            # else, so diagnosing the run meant reading timestamp gaps.
            # A tool result nobody kept is a tool call nobody can audit.
            is_compute = any(
                qualified_name.endswith(f".{name}") or qualified_name == name
                for name in DATASET_COMPUTE_TOOLS
            )
            keep_whole = is_compute or ".browser_" in qualified_name

            # Handlers report failure as TEXT beginning with "(" rather
            # than raising -- so success cannot be inferred from the
            # absence of an exception, and a failed browser action looked
            # identical to a successful one in the log.
            failed = _looks_failed(text)
            if failed:
                logger.warning(
                    "%s failed: %s", qualified_name, text.strip()[:300],
                )

            get_call_ledger().record(
                qualified_name,
                arguments,
                ok=not failed,
                result_preview=text[:200],
                full_output=text if keep_whole else None,
            )
        except Exception:  # noqa: BLE001
            pass  # bookkeeping must never break the call it records
        return result


_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
