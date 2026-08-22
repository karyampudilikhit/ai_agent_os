"""Persist MCP connection specs — which external tool servers the user
has connected. One shared file for now (no auth = one user); this
naturally extends to per-user when we add accounts.

Connection spec shape:
    {
        "name":      "notion",           # short slug the user picks
        "transport": "stdio" | "http",   # only stdio supported today
        "command":   "npx",              # stdio only
        "args":      ["-y", "@modelcontextprotocol/server-..."],  # stdio only
        "url":       "https://...",      # http only
        "env":       {"NOTION_TOKEN": "..."},  # extra env vars for the child process
        "enabled":   true,
        "added_at":  "iso...",
    }
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from threading import Lock
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_STORE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "employees", "team_data", "_mcp_connections.json"
)

VALID_TRANSPORTS = {"stdio", "http"}


class MCPStore:
    """One JSON file with the list of connection specs. Not per-Unit —
    connections are shared across all Units for now (an employee in any
    Unit can call tools from any connection)."""

    def __init__(self, path: str = DEFAULT_STORE_PATH):
        self.path = path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = Lock()
        self._data = self._load()

    def _load(self) -> Dict:
        if not os.path.exists(self.path):
            return {"connections": []}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("connections", [])
                return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("MCP store unreadable, starting fresh: %s", exc)
            return {"connections": []}

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, default=str)

    def connections(self, only_enabled: bool = False) -> List[Dict]:
        with self._lock:
            conns = list(self._data.get("connections", []))
        if only_enabled:
            conns = [c for c in conns if c.get("enabled", True)]
        return conns

    def get(self, name: str) -> Optional[Dict]:
        for c in self.connections():
            if c.get("name") == name:
                return c
        return None

    def add(
        self,
        name: str,
        transport: str,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        url: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> Dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("Connection name is required")
        if transport not in VALID_TRANSPORTS:
            raise ValueError(f"transport must be one of {VALID_TRANSPORTS}, got {transport!r}")
        if transport == "stdio" and not command:
            raise ValueError("stdio transport requires 'command'")
        if transport == "http" and not url:
            raise ValueError("http transport requires 'url'")

        spec = {
            "name": name,
            "transport": transport,
            "command": command,
            "args": list(args or []),
            "url": url,
            "env": dict(env or {}),
            "enabled": True,
            "added_at": datetime.utcnow().isoformat(),
        }
        with self._lock:
            # Replace by name if already present (natural key)
            self._data["connections"] = [
                c for c in self._data.get("connections", []) if c.get("name") != name
            ]
            self._data["connections"].append(spec)
            self._save()
        return spec

    def remove(self, name: str) -> bool:
        with self._lock:
            before = self._data.get("connections", [])
            after = [c for c in before if c.get("name") != name]
            if len(after) == len(before):
                return False
            self._data["connections"] = after
            self._save()
        return True

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            for c in self._data.get("connections", []):
                if c.get("name") == name:
                    c["enabled"] = bool(enabled)
                    self._save()
                    return True
        return False


# Module-level singleton so callers don't re-parse the JSON per request
_store: Optional[MCPStore] = None
_store_lock = Lock()


def get_store() -> MCPStore:
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is None:
            _store = MCPStore()
    return _store
