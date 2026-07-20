"""HTTP tool runner — executes a user-defined HTTP tool.

Takes an HTTP tool spec (from HTTPToolStore) plus arguments the LLM
picked, resolves them into an httpx request, executes, and returns a
prompt-injectable text block.

Companion to mcp_client's `MCPRegistry.call()` — same input/output
contract (qualified_name + args -> text), so both can be unified
behind one planner.

Design constraints match the rest of Vision AI's tool layer:
  - Read/write intent is signalled by HTTP method — GET is read,
    everything else is write. The planner prompt already tells the LLM
    "don't fire write tools unless the task asks." No extra enforcement
    needed here; we just execute what the planner picked.
  - Bounded output. Response text is truncated to keep prompt sizes
    reasonable; a 500KB JSON response would blow the window.
  - Quiet failures. Any exception during execution returns a short
    error string, not a raise — the employee sees "(call failed: …)"
    and moves on.
  - No cross-tool state. Each call opens a fresh httpx client.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.app.tools.http_tool_store import HTTPToolStore, get_store

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0
MAX_RESPONSE_CHARS = 6000  # matches mcp_client.MAX_TOOL_RESULT_CHARS
CONNECTION_NAMESPACE = "custom"  # qualified name: "custom.<tool>"

USER_AGENT = (
    "vision-ai/0.1 (user-defined tool runner; "
    "https://github.com/karyampudilikhit/ai_agent_os)"
)


class HTTPToolCallError(Exception):
    pass


def _apply_auth(auth: Dict[str, Any], headers: Dict[str, str]) -> Optional[tuple]:
    """Add auth headers in-place; return httpx-shaped basic-auth tuple
    if applicable, else None."""
    atype = auth.get("type") or "none"
    if atype == "bearer":
        token = auth.get("token") or ""
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif atype == "api_key_header":
        header_name = auth.get("header_name") or ""
        token = auth.get("token") or ""
        if header_name and token:
            headers[header_name] = token
    elif atype == "basic":
        u = auth.get("username") or ""
        p = auth.get("password") or ""
        if u:
            return (u, p)
    return None


def _split_arguments(
    parameters: List[Dict[str, Any]], arguments: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """Bucket the LLM-provided arguments by their declared location.
    Unknown args are ignored (not silently rerouted to query — that
    would let the LLM smuggle arbitrary query params in). Missing
    required args return under a 'missing' key for a cleaner error."""
    buckets = {"query": {}, "body": {}, "path": {}, "header": {}, "missing": []}
    declared = {p["name"]: p for p in parameters}
    for name, value in (arguments or {}).items():
        p = declared.get(name)
        if not p:
            continue  # ignore undeclared args
        buckets[p["in"]][name] = value
    for name, p in declared.items():
        if p.get("required") and name not in (arguments or {}):
            buckets["missing"].append(name)
    return buckets


def _substitute_path(url_template: str, path_args: Dict[str, Any]) -> str:
    """Replace {name} placeholders in the URL with values from path_args.
    Any unused path_args are ignored (they might've been declared but
    aren't in this particular URL — the store can't cross-check)."""
    out = url_template
    for name, value in path_args.items():
        out = out.replace("{" + name + "}", str(value))
    return out


def _format_response(spec: Dict[str, Any], resp: httpx.Response) -> str:
    """Return a compact text block describing the call and its result."""
    status = resp.status_code
    content_type = resp.headers.get("content-type", "").lower()
    lines: List[str] = [f"HTTP {status} — {spec['method']} {resp.url}"]
    if "json" in content_type:
        try:
            data = resp.json()
            body = json.dumps(data, ensure_ascii=False, indent=2)
        except ValueError:
            body = resp.text
    else:
        body = resp.text
    body = (body or "").strip()
    if not body:
        lines.append("(empty response)")
    else:
        if len(body) > MAX_RESPONSE_CHARS:
            body = body[:MAX_RESPONSE_CHARS].rstrip() + "\n[…response truncated…]"
        lines.append(body)
    return "\n".join(lines)


class HTTPToolRunner:
    """Uniform interface: `.list_tools()` and `.call(qualified_name, args)`.
    Matches MCPRegistry's shape so a planner can union both."""

    def __init__(self, store: Optional[HTTPToolStore] = None, timeout: float = DEFAULT_TIMEOUT):
        self._store = store or get_store()
        self.timeout = timeout

    # ---- discovery -------------------------------------------------

    def list_tools(self) -> List[Dict[str, Any]]:
        """Return tool listings in MCP-shape:
            [{qualified_name, connection, tool, description, input_schema}]"""
        out: List[Dict[str, Any]] = []
        for spec in self._store.enabled():
            out.append(self._render_tool_info(spec))
        return out

    def _render_tool_info(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        # Build a minimal JSON schema from the parameters so the planner's
        # existing "schema.properties -> param names" rendering works.
        properties: Dict[str, Dict[str, str]] = {}
        required: List[str] = []
        for p in spec.get("parameters", []):
            properties[p["name"]] = {
                "type": "string",
                "description": p.get("description", ""),
            }
            if p.get("required"):
                required.append(p["name"])
        description = spec.get("description") or ""
        method = spec.get("method", "GET")
        # Prefix description with method so LLM sees write-shape upfront
        prefixed = f"[{method}] {description}".strip()
        return {
            "qualified_name": f"{CONNECTION_NAMESPACE}.{spec['name']}",
            "connection": CONNECTION_NAMESPACE,
            "tool": spec["name"],
            "description": prefixed,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

    # ---- execution -------------------------------------------------

    def call(self, qualified_name: str, arguments: Dict[str, Any]) -> str:
        """Execute one HTTP tool call. Returns a text block on success or
        error message on failure — never raises to the caller."""
        if "." not in qualified_name:
            return f"(invalid tool name: {qualified_name!r})"
        namespace, tool_name = qualified_name.split(".", 1)
        if namespace != CONNECTION_NAMESPACE:
            return f"(not an HTTP tool: {qualified_name!r})"
        spec = self._store.get(tool_name)
        if not spec:
            return f"(no HTTP tool named {tool_name!r})"
        if not spec.get("enabled", True):
            return f"(HTTP tool {tool_name!r} is disabled)"
        try:
            return self._execute(spec, arguments or {})
        except Exception as exc:  # noqa: BLE001 — quiet failures by contract
            logger.warning("HTTP tool %s failed: %s", tool_name, exc)
            return f"(call failed: {exc})"

    def _execute(self, spec: Dict[str, Any], arguments: Dict[str, Any]) -> str:
        buckets = _split_arguments(spec.get("parameters", []), arguments)
        if buckets["missing"]:
            return f"(missing required arguments: {', '.join(buckets['missing'])})"

        url = _substitute_path(spec["url"], buckets["path"])
        headers: Dict[str, str] = {"User-Agent": USER_AGENT}
        headers.update(spec.get("headers") or {})
        headers.update({str(k): str(v) for k, v in buckets["header"].items()})
        basic_auth = _apply_auth(spec.get("auth") or {}, headers)

        method = spec["method"]
        request_kwargs: Dict[str, Any] = {
            "headers": headers,
            "timeout": self.timeout,
            "follow_redirects": True,
        }
        if basic_auth:
            request_kwargs["auth"] = basic_auth
        if buckets["query"]:
            request_kwargs["params"] = buckets["query"]
        if method != "GET" and buckets["body"]:
            # Body is either a full JSON object (one param named "body")
            # OR a set of top-level keys the caller declared as "in": "body".
            body_bucket = buckets["body"]
            if list(body_bucket.keys()) == ["body"] and isinstance(
                body_bucket["body"], (dict, list)
            ):
                request_kwargs["json"] = body_bucket["body"]
            else:
                request_kwargs["json"] = body_bucket

        with httpx.Client() as client:
            resp = client.request(method, url, **request_kwargs)
        return _format_response(spec, resp)
