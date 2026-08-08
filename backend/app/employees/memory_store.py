"""Minimal persistent memory for one Employee.

v0: a JSON file per employee, one entry per completed task (the task, a
summary of the output, and its completeness score). No embeddings/vector
search yet — that's the full Phase 7 design (config.yaml already
anticipates Chroma/FAISS for that once memory grows past a handful of
entries per employee). This only needs to answer "what have I already
told this founder" well enough to prove the Employee abstraction is more
than a stateless Pipeline call.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

def _default_memory_dir() -> str:
    override = os.environ.get("DATA_DIR", "").strip()
    if override:
        return os.path.join(override, "memory")
    return os.path.join(os.path.dirname(__file__), "data")

DEFAULT_MEMORY_DIR = _default_memory_dir()
MAX_CONTEXT_ENTRIES = 3
SUMMARY_CHARS = 400


class EmployeeMemoryStore:
    """Append-only history for one employee_id, backed by a JSON file."""

    def __init__(self, employee_id: str, memory_dir: str = DEFAULT_MEMORY_DIR):
        self.employee_id = employee_id
        self.memory_dir = memory_dir
        self.path = os.path.join(memory_dir, f"{employee_id}.json")
        os.makedirs(memory_dir, exist_ok=True)
        self._entries: List[Dict[str, Any]] = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Could not load memory for %s, starting fresh: %s", self.employee_id, exc
            )
            return []

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._entries, f, indent=2, default=str)

    def record(self, task: str, result: Dict[str, Any]) -> None:
        output = result.get("output") or ""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "task": task,
            "summary": output[:SUMMARY_CHARS],
            "completeness_score": (result.get("critique") or {}).get("completeness_score"),
        }
        self._entries.append(entry)
        self._save()

    def relevant_context(self, task: str) -> str:
        """Entries relevant to `task`, best match first — falling back to
        the most recent entries when nothing matches.

        This used to be recency-only ("the last 3, no similarity search
        yet"), which made the name a lie the moment an employee had done
        four things: asked about work from last week, it would show the
        founder's three most recent unrelated tasks instead. Ranking is
        now lexical (memory.retrieval), so "relevant" means it.

        The recency fallback is deliberate rather than lazy. Relevance
        returns nothing for most tasks by design, and dropping to an
        empty block would REMOVE context the prompt has always had — a
        regression dressed as an improvement. Falling back keeps this
        strictly no worse than the old behaviour in every case where
        ranking finds nothing.
        """
        if not self._entries:
            return ""

        def _render(entries: List[Dict[str, Any]]) -> str:
            return "\n".join(
                f"- ({e['timestamp'][:10]}) Task: {e['task'][:100]}\n"
                f"  Summary: {e['summary'][:200]}"
                for e in entries
            )

        try:
            from backend.app.memory import retrieval

            docs = [
                (str(i), f"{e.get('task') or ''}\n{e.get('summary') or ''}")
                for i, e in enumerate(self._entries)
            ]
            hits = retrieval.rank(task, docs, limit=MAX_CONTEXT_ENTRIES)
            if hits:
                return _render([self._entries[int(doc_id)] for doc_id, _ in hits])
        except Exception as exc:  # noqa: BLE001
            # Ranking is an optimisation over the recency list below; a
            # failure here should cost relevance, not the whole block.
            logger.warning(
                "Memory ranking failed for %s, falling back to recency: %s",
                self.employee_id, exc,
            )

        return _render(list(reversed(self._entries[-MAX_CONTEXT_ENTRIES:])))

    def all_entries(self) -> List[Dict[str, Any]]:
        return list(self._entries)
