"""Async run store + background executor.

Fixes the "chat looks frozen for 3 minutes" bug. Before, /api/chat ran
the whole pipeline inline in one HTTP request. Now, for long-running
intents (run_task_company, run_task_unit) the router:
  1. Creates a run record (RunStore),
  2. Submits the pipeline call to a small thread pool,
  3. Returns immediately with the run_id.

The client polls /api/runs/{run_id} for status. On done, renders the
output + evidence and stops polling.

Thread-safety: RunStore uses one lock over the dict; the ThreadPool
handles concurrency. Single-user MVP — no per-user isolation. FINISHED
runs are persisted to disk (see RunStore's docstring for why that
stopped being optional).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Small pool — one or two concurrent runs is enough for a single founder.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="async-run")

VALID_STATUSES = {"queued", "running", "done", "failed"}

# Enough to be real history without letting one long-lived server grow a
# multi-megabyte JSON file it rewrites on every run.
MAX_PERSISTED_RUNS = 200


def _default_path() -> str:
    from backend.app.utils.paths import under_data
    return str(under_data("runs.json"))


class RunStore:
    """Run lifecycle, with FINISHED runs persisted to disk.

    This used to be memory-only, on the reasoning that losing in-flight
    runs to a restart was fine for an MVP. That was true of in-flight
    runs and wrong about finished ones, because two other things read
    this store as if it were history:

      - The clarifier's "recent deliverables" feed (routes.
        _build_clarifier_context) — how "explain the business model of
        each" resolves against the list just produced.
      - The Output tab, which has no other source for a past run.

    So a restart didn't merely drop work in progress, it gave the
    product amnesia. The founder hit both halves in one sitting: asked
    to analyse "the companies you mentioned", it answered "please
    provide the list of companies" — the list it had written two turns
    earlier — and there was no way to scroll back to that deliverable
    either.

    Queued and running runs stay memory-only on purpose: a run cannot
    outlive the process executing it, so persisting one would only
    restore it as a permanently-stuck 'running' row.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        try:
            self._path = path if path is not None else _default_path()
        except Exception:  # noqa: BLE001
            self._path = ""
        self._load()

    # ---- persistence ------------------------------------------------

    def _load(self) -> None:
        """Best-effort restore of finished runs. A missing or corrupt
        file must never stop the server booting — worst case we are back
        to the old memory-only behavior."""
        try:
            if not self._path or not os.path.exists(self._path):
                return
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            runs = data.get("runs") if isinstance(data, dict) else data
            if not isinstance(runs, list):
                return
            with self._lock:
                for r in runs:
                    if isinstance(r, dict) and r.get("id"):
                        self._runs[str(r["id"])] = dict(r)
            logger.info("RunStore: restored %d finished run(s)", len(self._runs))
        except Exception as exc:  # noqa: BLE001
            logger.warning("RunStore: could not load %s: %s", self._path, exc)

    def _save(self) -> None:
        """Write finished runs only, newest first, capped. Never raises —
        failing to persist must not fail the run that just succeeded."""
        try:
            if not self._path:
                return
            with self._lock:
                finished = [
                    dict(r) for r in self._runs.values()
                    if r.get("status") in ("done", "failed")
                ]
            finished.sort(key=lambda r: r.get("finished_at") or 0, reverse=True)
            finished = finished[:MAX_PERSISTED_RUNS]
            parent = os.path.dirname(self._path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"runs": finished}, fh, ensure_ascii=False)
            os.replace(tmp, self._path)  # atomic — never a half-written file
        except Exception as exc:  # noqa: BLE001
            logger.warning("RunStore: could not save %s: %s", self._path, exc)

    def create(
        self,
        *,
        intent: str,
        session_id: Optional[str] = None,
        company_id: Optional[str] = None,
        task: str = "",
    ) -> str:
        with self._lock:
            run_id = f"run_{uuid.uuid4().hex[:10]}"
            self._runs[run_id] = {
                "id": run_id,
                "intent": intent,
                "status": "queued",
                "session_id": session_id,
                "company_id": company_id,
                "task": task,
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
                "output": None,
                "evidence": [],
                "error": None,
            }
            return run_id

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._runs.get(run_id)
            return dict(r) if r else None

    def _set(self, run_id: str, **fields: Any) -> None:
        with self._lock:
            r = self._runs.get(run_id)
            if not r:
                return
            r.update(fields)

    def set_running(self, run_id: str) -> None:
        self._set(run_id, status="running", started_at=time.time())

    def set_done(
        self,
        run_id: str,
        output: str,
        evidence: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._set(
            run_id,
            status="done",
            finished_at=time.time(),
            output=output,
            evidence=evidence or [],
        )
        self._save()

    def set_failed(self, run_id: str, error: str) -> None:
        self._set(run_id, status="failed", finished_at=time.time(), error=error)
        self._save()

    def list_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Finished runs, newest first — what the Output tab's history
        picker lists. Deliberately excludes queued/running: an unfinished
        run has nothing to show yet."""
        with self._lock:
            finished = [
                dict(r) for r in self._runs.values()
                if r.get("status") in ("done", "failed")
            ]
        finished.sort(key=lambda r: r.get("finished_at") or 0, reverse=True)
        return finished[:limit]

    def list_recent_done(
        self,
        *,
        session_id: Optional[str] = None,
        company_id: Optional[str] = None,
        limit: int = 2,
    ) -> List[Dict[str, Any]]:
        """Return the most recent finished runs (task + output) for the
        given scope, newest first. The clarifier uses this so 'give me
        a script for THIS video idea' resolves to whatever the last
        run produced instead of a literal keyword-match interpretation.
        """
        with self._lock:
            all_done = [
                dict(r) for r in self._runs.values() if r.get("status") == "done"
            ]
        all_done.sort(key=lambda r: r.get("finished_at") or 0, reverse=True)

        scoped = []
        for r in all_done:
            # Match if EITHER scope hits: same session or same company.
            # (A Unit run inside a Company carries both ids.)
            sess_hit = (
                session_id is not None and r.get("session_id") == session_id
            )
            co_hit = (
                company_id is not None and r.get("company_id") == company_id
            )
            if sess_hit or co_hit:
                scoped.append(r)

        if scoped:
            return scoped[:limit]

        # FALLBACK — single-user MVP. If nothing matched the requested
        # scope (auto-created Unit the frontend never echoed back, a
        # Company/Unit id mismatch, a run tagged before this fix), still
        # return the most recent finished work. "Forgetting the thing it
        # just produced" is a far worse failure than occasionally
        # surfacing a deliverable from an adjacent scope. Revisit when
        # this stops being single-tenant.
        return all_done[:limit]


_store: Optional[RunStore] = None


def get_store() -> RunStore:
    global _store
    if _store is None:
        _store = RunStore()
    return _store


def submit(
    run_id: str,
    work: Callable[[], Tuple[str, List[Dict[str, Any]]]],
) -> None:
    """Submit background work. `work()` returns (output_str, evidence_list).
    Any exception is captured onto the run record — never crashes the pool."""
    store = get_store()

    def _wrapped() -> None:
        store.set_running(run_id)
        try:
            output, evidence = work()
        except Exception as exc:  # noqa: BLE001
            logger.exception("run %s failed", run_id)
            store.set_failed(run_id, str(exc))
            return
        store.set_done(run_id, output or "", evidence or [])

    _executor.submit(_wrapped)
