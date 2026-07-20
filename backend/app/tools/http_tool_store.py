"""HTTP tool store — JSON-backed registry of user-defined HTTP tools.

Companion to mcp_store.py. Where mcp_store persists MCP server specs
(command + args + env), this persists custom HTTP endpoint specs the
user defined themselves in the Playground — for tools that don't have
an MCP server (their own internal API, a niche SaaS, a curl-shaped
integration they already have working).

File lives at repo root as `.http_tools.json`, gitignored, may hold
API tokens.

Spec shape (v1):
{
    "name":        "my_search",              # unique across HTTP tools
    "description": "Search my knowledge base",
    "method":      "GET" | "POST" | "PUT" | "PATCH" | "DELETE",
    "url":         "https://api.example.com/search",   # may contain {path_param}
    "auth": {
        "type":        "none" | "bearer" | "api_key_header" | "basic",
        "token":       "…",              # bearer / api_key_header
        "header_name": "X-API-Key",      # api_key_header only
        "username":    "…",              # basic only
        "password":    "…"               # basic only
    },
    "parameters": [                          # what args the LLM must supply
        {"name": "q",       "in": "query", "description": "…", "required": true},
        {"name": "user_id", "in": "path",  "description": "…"},
        {"name": "payload", "in": "body",  "description": "…"}
    ],
    "headers":  {"X-Static": "…"},           # extra static headers (optional)
    "enabled":  true
}

Everything except method/url/name is optional. Tokens are stored in
plaintext just like the MCP env vars — same trust model.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STORE_FILENAME = ".http_tools.json"

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
ALLOWED_AUTH_TYPES = {"none", "bearer", "api_key_header", "basic"}
ALLOWED_PARAM_LOCATIONS = {"query", "body", "path", "header"}


def _repo_root() -> Path:
    """Repo root = 3 levels up from this file: tools/ -> app/ -> backend/ -> root."""
    return Path(__file__).resolve().parents[3]


def _store_path() -> Path:
    return _repo_root() / STORE_FILENAME


class HTTPToolStoreError(Exception):
    """Raised on invalid tool specs — spec validation errors."""


def _validate_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize + reject bad specs before they hit disk. Returns the
    cleaned spec. Never mutates the input."""
    if not isinstance(spec, dict):
        raise HTTPToolStoreError("spec must be an object")

    name = str(spec.get("name") or "").strip()
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        raise HTTPToolStoreError(
            "name is required, alphanumeric/underscore/hyphen only"
        )

    method = str(spec.get("method") or "GET").upper()
    if method not in ALLOWED_METHODS:
        raise HTTPToolStoreError(f"method must be one of {sorted(ALLOWED_METHODS)}")

    url = str(spec.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPToolStoreError("url must start with http:// or https://")

    auth = spec.get("auth") or {"type": "none"}
    if not isinstance(auth, dict):
        raise HTTPToolStoreError("auth must be an object")
    auth_type = str(auth.get("type") or "none").lower()
    if auth_type not in ALLOWED_AUTH_TYPES:
        raise HTTPToolStoreError(
            f"auth.type must be one of {sorted(ALLOWED_AUTH_TYPES)}"
        )
    # Enforce required fields per auth type — catch typos before runtime
    if auth_type == "bearer" and not auth.get("token"):
        raise HTTPToolStoreError("bearer auth requires 'token'")
    if auth_type == "api_key_header":
        if not auth.get("token") or not auth.get("header_name"):
            raise HTTPToolStoreError(
                "api_key_header auth requires 'token' and 'header_name'"
            )
    if auth_type == "basic":
        if not auth.get("username") or auth.get("password") is None:
            raise HTTPToolStoreError("basic auth requires 'username' and 'password'")

    params_in = spec.get("parameters") or []
    if not isinstance(params_in, list):
        raise HTTPToolStoreError("parameters must be a list")
    parameters: List[Dict[str, Any]] = []
    for i, p in enumerate(params_in):
        if not isinstance(p, dict):
            raise HTTPToolStoreError(f"parameters[{i}] must be an object")
        pname = str(p.get("name") or "").strip()
        if not pname:
            raise HTTPToolStoreError(f"parameters[{i}].name required")
        loc = str(p.get("in") or "query").lower()
        if loc not in ALLOWED_PARAM_LOCATIONS:
            raise HTTPToolStoreError(
                f"parameters[{i}].in must be one of {sorted(ALLOWED_PARAM_LOCATIONS)}"
            )
        parameters.append({
            "name": pname,
            "in": loc,
            "description": str(p.get("description") or "").strip(),
            "required": bool(p.get("required", False)),
        })

    headers = spec.get("headers") or {}
    if not isinstance(headers, dict):
        raise HTTPToolStoreError("headers must be an object")
    headers = {str(k): str(v) for k, v in headers.items()}

    return {
        "name": name,
        "description": str(spec.get("description") or "").strip(),
        "method": method,
        "url": url,
        "auth": {
            "type": auth_type,
            "token": auth.get("token") or "",
            "header_name": auth.get("header_name") or "",
            "username": auth.get("username") or "",
            "password": auth.get("password") or "",
        },
        "parameters": parameters,
        "headers": headers,
        "enabled": bool(spec.get("enabled", True)),
    }


class HTTPToolStore:
    def __init__(self, path: Optional[Path] = None):
        self._path = path or _store_path()
        self._lock = threading.RLock()
        self._cache: Optional[List[Dict[str, Any]]] = None

    # ---- disk IO ---------------------------------------------------

    def _load(self) -> List[Dict[str, Any]]:
        if self._cache is not None:
            return self._cache
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            data = []
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("HTTPToolStore: could not read %s: %s", self._path, exc)
            data = []
        if not isinstance(data, list):
            logger.warning("HTTPToolStore: %s is not a list, resetting", self._path)
            data = []
        self._cache = data
        return data

    def _save(self, tools: List[Dict[str, Any]]) -> None:
        try:
            self._path.write_text(
                json.dumps(tools, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("HTTPToolStore: could not write %s: %s", self._path, exc)
            raise
        self._cache = tools

    # ---- public API ------------------------------------------------

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(t) for t in self._load()]

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for t in self._load():
                if t.get("name") == name:
                    return dict(t)
        return None

    def add(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        cleaned = _validate_spec(spec)
        with self._lock:
            tools = self._load()
            if any(t.get("name") == cleaned["name"] for t in tools):
                raise HTTPToolStoreError(
                    f"a tool named {cleaned['name']!r} already exists"
                )
            tools = tools + [cleaned]
            self._save(tools)
            return dict(cleaned)

    def update(self, name: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            tools = self._load()
            for i, t in enumerate(tools):
                if t.get("name") == name:
                    merged = {**t, **patch, "name": name}  # name is immutable
                    cleaned = _validate_spec(merged)
                    tools[i] = cleaned
                    self._save(tools)
                    return dict(cleaned)
        raise HTTPToolStoreError(f"no HTTP tool named {name!r}")

    def delete(self, name: str) -> bool:
        with self._lock:
            tools = self._load()
            filtered = [t for t in tools if t.get("name") != name]
            if len(filtered) == len(tools):
                return False
            self._save(filtered)
            return True

    def enabled(self) -> List[Dict[str, Any]]:
        return [t for t in self.list() if t.get("enabled", True)]


_store_singleton: Optional[HTTPToolStore] = None
_store_lock = threading.Lock()


def get_store() -> HTTPToolStore:
    global _store_singleton
    with _store_lock:
        if _store_singleton is None:
            _store_singleton = HTTPToolStore()
        return _store_singleton
