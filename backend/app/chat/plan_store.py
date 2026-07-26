"""In-memory store of the last PLAN produced for a given context.

Powers the plan -> work handoff. In Plan mode the AI produces a plan
(who does what, in what order) and stashes it here. When the founder
flips to Work mode and says "go", the run picks up this stored plan and
executes against it instead of re-planning from scratch.

Keyed by context (company_id or session_id or 'root'), same convention
as clarification_store + pending_proposal_store. Not persisted — a
restart clears plans, which is fine: the founder can re-plan.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class PlanStore:
    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key_for(
        company_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        return company_id or session_id or "root"

    def set(
        self,
        key: str,
        *,
        task: str,
        plan_markdown: str,
        altitude: str,  # "company" | "unit"
    ) -> Dict[str, Any]:
        with self._lock:
            rec = {
                "task": task,
                "plan_markdown": plan_markdown,
                "altitude": altitude,
                "created_at": time.time(),
            }
            self._data[key] = rec
            return dict(rec)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._data.get(key)
            return dict(rec) if rec else None

    def clear(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


_store: Optional[PlanStore] = None


def get_store() -> PlanStore:
    global _store
    if _store is None:
        _store = PlanStore()
    return _store
