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


_MAX_PRIOR_QA = 8  # Keep the last N (Q, A) pairs per key across turns.


class ClarificationStore:
    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, Any]] = {}
        # Cross-turn memory: prior (Q, A) pairs that survive after
        # dispatch, plus the last user prompt. Feeds the clarifier so
        # answers from turn N are still known on turn N+1.
        self._memory: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key_for(
        company_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        return company_id or session_id or "root"

    # ----- cross-turn memory (persists after dispatch) -----

    def record_qa(
        self,
        key: str,
        questions: List[str],
        answers: List[str],
    ) -> None:
        """After a clarification cycle finishes and we hand off to the
        run, keep the (Q, A) pairs so the next task in the same session
        sees them as already-answered context."""
        with self._lock:
            mem = self._memory.setdefault(key, {"prior_qa": [], "last_user_prompt": ""})
            for i, q in enumerate(questions):
                a = answers[i] if i < len(answers) else ""
                if q and a:
                    mem["prior_qa"].append({"q": q, "a": a})
            # Bound the buffer.
            if len(mem["prior_qa"]) > _MAX_PRIOR_QA:
                mem["prior_qa"] = mem["prior_qa"][-_MAX_PRIOR_QA:]

    def record_prompt(self, key: str, prompt: str) -> None:
        """Remember the founder's latest raw prompt on this key so
        the clarifier can spot 'this / that / the plan' references."""
        with self._lock:
            mem = self._memory.setdefault(key, {"prior_qa": [], "last_user_prompt": ""})
            mem["last_user_prompt"] = (prompt or "").strip()[:1000]

    def get_memory(self, key: str) -> Dict[str, Any]:
        with self._lock:
            mem = self._memory.get(key)
            if not mem:
                return {"prior_qa": [], "last_user_prompt": ""}
            return {
                "prior_qa": [dict(p) for p in mem["prior_qa"]],
                "last_user_prompt": mem.get("last_user_prompt", ""),
            }

    def clear_memory(self, key: str) -> None:
        """Called on '+ New' hard reset so a fresh chat starts clean."""
        with self._lock:
            self._memory.pop(key, None)

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
