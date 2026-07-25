"""In-memory pending-clarification store.

Single-user MVP — one pending clarification per (company_id or session_id
or 'root') key. If a new task comes in before the previous one finished
being clarified, it overrides — the founder pivoting mid-clarification
should just work.

Not persisted across restarts: a server bounce mid-clarification loses
state, and that's fine — the founder can re-ask.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional


class ClarificationStore:
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
        intent: str,
        questions: List[str],
        answers: List[str],
        company_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self._data[key] = {
                "task": task,
                "intent": intent,
                "questions": list(questions),
                "answers": list(answers),
                "company_id": company_id,
                "session_id": session_id,
            }
            return dict(self._data[key])

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._data.get(key)
            if not rec:
                return None
            return {
                **rec,
                "questions": list(rec["questions"]),
                "answers": list(rec["answers"]),
            }

    def append_answer(self, key: str, answer: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._data.get(key)
            if not rec:
                return None
            rec["answers"].append(answer)
            return {
                **rec,
                "questions": list(rec["questions"]),
                "answers": list(rec["answers"]),
            }

    def append_questions(self, key: str, new_qs: List[str]) -> None:
        with self._lock:
            rec = self._data.get(key)
            if not rec:
                return
            rec["questions"].extend(new_qs)

    def clear(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


_store: Optional[ClarificationStore] = None


def get_store() -> ClarificationStore:
    global _store
    if _store is None:
        _store = ClarificationStore()
    return _store
