"""ToolCallLedger — the record of what this process ACTUALLY DID, so a
deliverable's claims can be checked against its own actions.

Sibling to SourceLedger. That one answers "was this URL really fetched?"
by asking the network; this one answers "did we really try that?" by
asking the call log. Both exist for the same reason: the finished text
is not evidence about itself.

Why this was needed, from a live run. Asked to play an ARC-AGI-3 game,
an agent made 8 attempts to open it and passed a wrong identifier every
time — 0, 1, 2, 3, then "default", "arcade", "test", "arcade3". The
correct id, `vc33-5430563c`, was stated verbatim in the prompt and never
sent once. The deliverable then reported:

    "the supplied game_id (vc33-5430563c) does not exist on the ARC
     server"

Every part of that is wrong, and it points the founder at someone else's
system to debug a problem that is ours. A confident false diagnosis is
worse than a raw error message, because it redirects the reader's
attention with authority.

Nothing could catch it, because catching it requires comparing the claim
against what was actually attempted — and no run-scoped record of tool
calls existed. This is that record.

Scope note, matching SourceLedger and RunStore: process-wide,
single-user MVP, never reset. The tradeoff runs one way — an older run's
calls could mask a contradiction in a newer one (a false negative), but
a call that never happened can never be invented (no false positives).
Accusing a truthful deliverable of contradicting itself would be far
worse than missing one, so the bias is deliberate.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Bounded so a long-lived server can't grow this without limit.
MAX_ENTRIES = 2000

# Args are recorded as text for substring checking. Bounded so one
# enormous payload (a full slide deck, a 64x64 grid) can't dominate.
MAX_ARGS_CHARS = 2000
MAX_ERROR_CHARS = 400


class ToolCallLedger:
    def __init__(self) -> None:
        self._calls: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(
        self,
        tool: str,
        arguments: Any,
        *,
        ok: bool,
        result_preview: str = "",
        role: Optional[str] = None,
    ) -> None:
        """Never raises — bookkeeping must not break the call it records."""
        try:
            try:
                args_text = json.dumps(arguments, ensure_ascii=False, default=str)
            except Exception:  # noqa: BLE001
                args_text = str(arguments)
            entry = {
                "tool": str(tool),
                "args_text": args_text[:MAX_ARGS_CHARS],
                "ok": bool(ok),
                "role": role,
                "at": time.time(),
                "result_preview": str(result_preview or "")[:MAX_ERROR_CHARS],
            }
            with self._lock:
                self._calls.append(entry)
                if len(self._calls) > MAX_ENTRIES:
                    del self._calls[: MAX_ENTRIES // 4]
        except Exception:  # noqa: BLE001
            return

    # ---- querying ----------------------------------------------------

    def calls(self, tool: Optional[str] = None,
              since: Optional[float] = None) -> List[Dict[str, Any]]:
        """`since` is a time.time() cutoff -- only calls recorded at or
        after it are returned.

        This parameter exists because omitting it was a real,
        shipped bug. This ledger is a process-global singleton that
        production deliberately never resets, so an unscoped query
        answers 'did this tool run since the server started', not
        'did it run during THIS run'. routes._unused_compute_capability
        asked the unscoped question: once any single run called
        run_python, the compute gate passed for every later run
        forever. Observed live -- a run whose own deliverable said
        'no backtest executed, Sharpe = unknown' sailed through the
        gate on a previous run's run_python call.
        """
        with self._lock:
            out = [dict(c) for c in self._calls]
        if since is not None:
            out = [c for c in out if (c.get("at") or 0) >= since]
        if tool:
            out = [c for c in out if c["tool"] == tool or c["tool"].endswith(f".{tool}")]
        return out

    def was_value_used(self, value: str) -> bool:
        """True if `value` appears in the arguments of ANY call made.

        This is the question the ARC failure turned on: the deliverable
        blamed an identifier that was never sent. Substring matching is
        intentional — an id can be nested anywhere in an args payload,
        and a false 'yes' here only suppresses a warning (safe), while a
        false 'no' would accuse a truthful deliverable (not safe).
        """
        needle = (value or "").strip().lower()
        if len(needle) < 3:
            return True  # too short to be a meaningful identifier
        with self._lock:
            return any(needle in c["args_text"].lower() for c in self._calls)

    def failure_streak(self, tool: str) -> int:
        """How many times the most recent run of calls to `tool` failed
        consecutively. Used by the loop guard for degenerate sweeps."""
        with self._lock:
            relevant = [c for c in self._calls if c["tool"] == tool]
        streak = 0
        for c in reversed(relevant):
            if c["ok"]:
                break
            streak += 1
        return streak

    def counts(self) -> Dict[str, int]:
        with self._lock:
            ok = sum(1 for c in self._calls if c["ok"])
            return {"total": len(self._calls), "ok": ok, "failed": len(self._calls) - ok}

    def reset(self) -> None:
        """Only for tests — production never resets (see scope note)."""
        with self._lock:
            self._calls.clear()


_ledger = ToolCallLedger()


def get_call_ledger() -> ToolCallLedger:
    return _ledger
