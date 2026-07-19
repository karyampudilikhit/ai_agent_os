"""MCP client — connect to MCP servers, list their tools, invoke them.

Each `MCPConnection` wraps one live MCP session (a persistent stdio
subprocess or an HTTP session). Sessions are opened lazily on first
use and cached until the process exits — no spawn/tear-down per call.

`MCPRegistry` owns all connections at once. Employees ask the registry
"what tools are available" and "please call this one" — they never
need to know which server a tool came from. Tool names are
namespace-prefixed with the connection name (e.g. `notion.search`) so
two servers exposing a `search` tool don't collide.

All async work happens on the shared background loop (mcp_runtime).
The public API is sync so the rest of Vision AI (employees, pipeline,
FastAPI handlers) stays sync.
"""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack
from threading import Lock
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from backend.app.tools.mcp_runtime import get_loop
from backend.app.tools.mcp_store import get_store

logger = logging.getLogger(__name__)

# Tool calls that return more than this are truncated before being
# handed to an employee — a huge Notion database dump would blow the
# prompt window if we didn't cap it.
MAX_TOOL_RESULT_CHARS = 6000


class MCPConnectionError(Exception):
    """Raised when a connection can't be established or a call fails."""


class MCPConnection:
    """One live session to one MCP server."""

    def __init__(self, spec: Dict):
        self.spec = spec
        self.name: str = spec["name"]
        self.transport: str = spec["transport"]
        self._stack: Optional[AsyncExitStack] = None
        self._session: Optional[ClientSession] = None
        self._tools_cache: Optional[List[Dict]] = None
        self._open_lock = Lock()  # protects async-open from concurrent sync callers
        self._closed = False

    # ------------------------------------------------------------------
    # Async internals — run on the background loop
    # ------------------------------------------------------------------

    async def _open(self) -> None:
        if self._session is not None:
            return
        stack = AsyncExitStack()
        try:
            await stack.__aenter__()
            if self.transport == "stdio":
                params = StdioServerParameters(
                    command=self.spec["command"],
                    args=list(self.spec.get("args") or []),
                    env=self._child_env(),
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            else:
                raise MCPConnectionError(
                    f"Transport {self.transport!r} not supported yet — only 'stdio' for now."
                )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._session = session
            self._stack = stack
            logger.info("MCP connection %r opened (transport=%s)", self.name, self.transport)
        except Exception:
            await stack.__aexit__(None, None, None)
            raise

    def _child_env(self) -> Dict[str, str]:
        """Merge the caller's own env with the connection's declared
        env vars. Merging (rather than replacing) matters so the child
        subprocess still has PATH, HOME, etc. — without those, `npx`
        can't find node."""
        env = dict(os.environ)
        for k, v in (self.spec.get("env") or {}).items():
            env[str(k)] = str(v)
        return env

    async def _list_tools(self) -> List[Dict]:
        await self._open()
        if self._tools_cache is not None:
            return self._tools_cache
        result = await self._session.list_tools()
        tools = []
        for t in result.tools:
            tools.append({
                "name": t.name,
                "description": (t.description or "").strip(),
                "input_schema": t.inputSchema if hasattr(t, "inputSchema") else None,
            })
        self._tools_cache = tools
        return tools

    async def _call_tool(self, tool_name: str, arguments: Dict) -> str:
        await self._open()
        result = await self._session.call_tool(tool_name, arguments=arguments)
        # result.content is a list of ContentPart. We want plain text.
        parts = []
        for c in getattr(result, "content", []) or []:
            text = getattr(c, "text", None)
            if text:
                parts.append(text)
        merged = "\n".join(parts).strip()
        if not merged and getattr(result, "isError", False):
            return "(tool returned no text; call was marked as an error)"
        if len(merged) > MAX_TOOL_RESULT_CHARS:
            merged = merged[:MAX_TOOL_RESULT_CHARS].rstrip() + "\n[…tool output truncated…]"
        return merged

    async def _aclose(self) -> None:
        if self._stack is None:
            return
        try:
            await self._stack.__aexit__(None, None, None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error closing MCP connection %r: %s", self.name, exc)
        finally:
            self._stack = None
            self._session = None
            self._tools_cache = None

    # ------------------------------------------------------------------
    # Sync wrappers used by the rest of the app
    # ------------------------------------------------------------------

    def list_tools(self) -> List[Dict]:
        try:
            return get_loop().run(self._list_tools(), timeout=30.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP %r list_tools failed: %s", self.name, exc)
            return []

    def call_tool(self, tool_name: str, arguments: Dict) -> str:
        try:
            return get_loop().run(self._call_tool(tool_name, arguments), timeout=60.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP %r call_tool(%s) failed: %s", self.name, tool_name, exc)
            return f"(tool call failed: {exc})"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            get_loop().run(self._aclose(), timeout=5.0)
        except Exception:  # noqa: BLE001
            pass


class MCPRegistry:
    """All configured MCP connections, treated as one unified tool
    catalog. Connections are opened lazily on first use of any tool
    they expose."""

    def __init__(self):
        self._connections: Dict[str, MCPConnection] = {}
        self._lock = Lock()

    def _load(self) -> None:
        """(Re)build the connections dict from the persisted store.
        Existing live connections that are still enabled are kept as-is
        so we don't reopen subprocesses on every reload."""
        specs = {c["name"]: c for c in get_store().connections(only_enabled=True)}
        with self._lock:
            # Close removed/disabled connections
            for name in list(self._connections):
                if name not in specs:
                    self._connections.pop(name).close()
            # Add newly configured connections
            for name, spec in specs.items():
                if name not in self._connections:
                    self._connections[name] = MCPConnection(spec)

    def reload(self) -> None:
        self._load()

    def connections(self) -> List[MCPConnection]:
        self._load()
        with self._lock:
            return list(self._connections.values())

    def list_all_tools(self) -> List[Dict]:
        """Every tool from every enabled connection, namespaced by
        connection name so two servers with a 'search' tool don't
        collide. Shape:
            [{"qualified_name", "connection", "tool", "description",
              "input_schema"}]
        """
        out: List[Dict] = []
        for conn in self.connections():
            for t in conn.list_tools():
                out.append({
                    "qualified_name": f"{conn.name}.{t['name']}",
                    "connection": conn.name,
                    "tool": t["name"],
                    "description": t["description"],
                    "input_schema": t["input_schema"],
                })
        return out

    def call(self, qualified_name: str, arguments: Dict) -> str:
        """Call a tool by its qualified name ("notion.search")."""
        if "." not in qualified_name:
            raise MCPConnectionError(
                f"Tool name must be qualified as 'connection.tool', got {qualified_name!r}"
            )
        conn_name, tool_name = qualified_name.split(".", 1)
        with self._lock:
            conn = self._connections.get(conn_name)
        if not conn:
            self._load()
            with self._lock:
                conn = self._connections.get(conn_name)
        if not conn:
            raise MCPConnectionError(f"No enabled MCP connection named {conn_name!r}")
        return conn.call_tool(tool_name, arguments)


# Module-level singleton
_registry: Optional[MCPRegistry] = None
_registry_lock = Lock()


def get_registry() -> MCPRegistry:
    global _registry
    if _registry is not None:
        return _registry
    with _registry_lock:
        if _registry is None:
            _registry = MCPRegistry()
    return _registry
