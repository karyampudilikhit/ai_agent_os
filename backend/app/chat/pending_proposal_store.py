"""In-memory store for CEO-proposed but not-yet-applied hierarchies.

Keyed on company_id. Overwritten by the next design pass for the same
Company. Not persisted — a server restart wipes pending proposals, and
that's fine: the founder can re-prompt.

Single-user assumption for MVP; multi-user needs a per-user key.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional


class PendingProposalStore:
    def __init__(self) -> None:
        self._by_company: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def set(self, company_id: str, units: List[Dict[str, Any]]) -> Dict[str, Any]:
        with self._lock:
            record = {
                "company_id": company_id,
                "units": units,
                "created_at": time.time(),
            }
            self._by_company[company_id] = record
            return dict(record)

    def get(self, company_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._by_company.get(company_id)
            return dict(record) if record else None

    def clear(self, company_id: str) -> None:
        with self._lock:
            self._by_company.pop(company_id, None)


_store: Optional[PendingProposalStore] = None


def get_store() -> PendingProposalStore:
    global _store
    if _store is None:
        _store = PendingProposalStore()
    return _store
