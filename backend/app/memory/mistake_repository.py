"""MistakeRepository — recall of past FAILURES on similar tasks.

Counterpart to memory_manager: that one recalls what worked, this one
recalls what did not. Same substrate (RunStore is the durable episodic
log), same lexical ranker, opposite status filter.

It also closes a dangling reference. config_loader has defined
MistakeRepositoryConfig (enabled / retention_days / max_patterns) since
before this module existed, pointing at a 0-byte file — configuration
for a subsystem that was not there.

HONEST SCOPE, because the name promises more than the data currently
supports. A failed run records an EXCEPTION STRING: "OllamaAdapterError:
HTTP 502", a timeout, a tool error. That is an infrastructure fault, not
a task-level mistake, and knowing about it rarely changes how an
employee should approach the work. So this deliberately does NOT feed
employee prompts — telling a specialist "a similar task once hit a 502"
is noise that costs context and buys nothing.

What it is genuinely good for is the founder-facing and operational
view: surfacing that a task like this one has failed repeatedly, which
is a signal about the system rather than about the task.

The richer version — mistakes extracted from what the critique loop
rejected, what the hand-back detector caught, and which citations came
back flagged as fabricated — needs those signals persisted per run
first. They exist today only as transient per-run state, so that is the
prerequisite, not this file.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from backend.app.memory import retrieval

logger = logging.getLogger(__name__)

# Matches MistakeRepositoryConfig.retention_days. A failure from last
# month says nothing useful about today's system — most of the causes
# have been fixed since.
DEFAULT_RETENTION_DAYS = 30

# Ranking failures is looser than ranking successes. A near-miss on a
# past failure costs a slightly noisy warning; a near-miss on a past
# deliverable risks an employee answering out of the wrong document.
FAILURE_MIN_RELEVANCE = 0.12


@dataclass(frozen=True)
class PastFailure:
    run_id: str
    task: str
    error: str
    finished_at: float
    score: float

    def age_days(self, now: Optional[float] = None) -> float:
        return ((now or time.time()) - self.finished_at) / 86400.0


class MistakeRepository:
    def __init__(
        self,
        store: Optional[Any] = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self._store = store
        self.retention_days = retention_days

    def _failed_runs(self, now: float) -> List[Dict[str, Any]]:
        try:
            store = self._store
            if store is None:
                from backend.app.chat.async_runs import get_store
                store = get_store()
            runs = store.list_history(limit=500)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MistakeRepository: could not read history: %s", exc)
            return []
        cutoff = now - (self.retention_days * 86400.0)
        return [
            r for r in runs
            if r.get("status") == "failed"
            and float(r.get("finished_at") or 0.0) >= cutoff
        ]

    def similar_failures(
        self,
        task: str,
        *,
        limit: int = 3,
        now: Optional[float] = None,
    ) -> List[PastFailure]:
        """Failures on tasks resembling `task`, most relevant first.

        Ranks on the TASK text alone, never the error string. Error
        strings share heavy boilerplate vocabulary ("error", "failed",
        "timeout", a stack of module names), so including them makes
        every failure look similar to every other one — the ranker would
        be matching the shape of a traceback rather than the work.
        """
        task = (task or "").strip()
        if not task:
            return []
        now = now or time.time()
        runs = self._failed_runs(now)
        if not runs:
            return []

        by_id = {str(r.get("id")): r for r in runs}
        docs = [(str(r.get("id")), str(r.get("task") or "")) for r in runs if r.get("id")]
        hits = retrieval.rank(
            task, docs, limit=limit, min_relevance=FAILURE_MIN_RELEVANCE
        )
        out: List[PastFailure] = []
        for run_id, score in hits:
            r = by_id.get(run_id)
            if not r:
                continue
            out.append(
                PastFailure(
                    run_id=run_id,
                    task=str(r.get("task") or ""),
                    error=str(r.get("error") or ""),
                    finished_at=float(r.get("finished_at") or 0.0),
                    score=score,
                )
            )
        return out

    def summarize(self, failures: Sequence[PastFailure]) -> str:
        """One-line-per-failure summary for the founder or the log.

        Not prompt material — see the module docstring on why employee
        prompts are deliberately left out of this.
        """
        if not failures:
            return ""
        lines = [f"{len(failures)} similar task(s) failed recently:"]
        for f in failures:
            lines.append(
                f"- {f.task[:120]} — failed {f.age_days():.1f}d ago: "
                f"{f.error[:160]}"
            )
        return "\n".join(lines)


_repo: Optional[MistakeRepository] = None


def get_mistake_repository() -> MistakeRepository:
    global _repo
    if _repo is None:
        _repo = MistakeRepository()
    return _repo
