"""In-memory progress store for team runs.

A team run of 3-5 employees takes ~10-25 minutes end to end. Blocking
the HTTP request that whole time with zero feedback breaks the UX.
This module tracks which employee is currently working / done, so the
frontend can poll a lightweight endpoint every few seconds and show
real progress on the cards without any streaming/websocket complexity.

Deliberately in-memory only. Progress is transient — if the server
restarts mid-run, the run is gone anyway, so there's nothing worth
persisting to disk. Keyed by session_id so multiple sessions can run
concurrently without stomping each other.
"""

from __future__ import annotations

from datetime import datetime
from threading import Lock
from typing import Dict, List, Optional


_lock = Lock()
_state: Dict[str, Dict] = {}


def _now() -> str:
    return datetime.utcnow().isoformat()


def start_run(session_id: str, roles: List[str]) -> None:
    with _lock:
        _state[session_id] = {
            "phase": "starting",
            "team": list(roles),
            "current_role": None,
            "completed": [],
            "started_at": _now(),
            "updated_at": _now(),
            "error": None,
        }


def mark_role_working(session_id: str, role: str) -> None:
    with _lock:
        s = _state.get(session_id)
        if not s:
            return
        s["phase"] = "role_working"
        s["current_role"] = role
        s["updated_at"] = _now()


def mark_role_done(session_id: str, role: str, metadata: Optional[Dict] = None) -> None:
    with _lock:
        s = _state.get(session_id)
        if not s:
            return
        s["completed"].append({"role": role, **(metadata or {})})
        s["updated_at"] = _now()


def mark_synthesizing(session_id: str) -> None:
    with _lock:
        s = _state.get(session_id)
        if not s:
            return
        s["phase"] = "synthesizing"
        s["current_role"] = None
        s["updated_at"] = _now()


def mark_complete(session_id: str) -> None:
    with _lock:
        s = _state.get(session_id)
        if not s:
            return
        s["phase"] = "complete"
        s["current_role"] = None
        s["updated_at"] = _now()


def mark_error(session_id: str, error: str) -> None:
    with _lock:
        s = _state.get(session_id)
        if not s:
            _state[session_id] = {
                "phase": "error", "team": [], "current_role": None,
                "completed": [], "started_at": _now(), "updated_at": _now(),
                "error": error,
            }
            return
        s["phase"] = "error"
        s["error"] = error
        s["updated_at"] = _now()


def get(session_id: str) -> Dict:
    with _lock:
        s = _state.get(session_id)
        if not s:
            return {"phase": "idle", "team": [], "current_role": None, "completed": []}
        # Return a shallow copy so callers can't mutate it under our lock
        return {**s, "completed": list(s["completed"])}


def clear(session_id: str) -> None:
    with _lock:
        _state.pop(session_id, None)
