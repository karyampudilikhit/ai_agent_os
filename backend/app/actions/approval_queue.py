"""ApprovalQueue — JSON-backed store of pending mutating actions.

Every mutating action call (send email, post to Slack, write file,
transfer money later) enqueues here instead of firing directly. The
founder sees the pending action in the UI, hits Approve, and the
executor fires it against the real environment.

Trust model: the queue file lives at repo root as `.pending_actions.json`,
alongside `.http_tools.json`. Gitignored. Same trust as the token store.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

QUEUE_FILENAME = ".pending_actions.json"
MAX_QUEUE_SIZE = 500  # runaway guard — anything beyond this is a bug


def _queue_path() -> Path:
    from backend.app.utils.paths import under_data
    return under_data(QUEUE_FILENAME)


class ApprovalQueueError(Exception):
    pass


class ApprovalQueue:
    """Persistent queue of pending actions.

    Statuses: pending -> approved -> executed | failed
                     \\-> rejected

    An approved action stays in the queue with status=approved until
    the executor picks it up (worker or synchronous, we do synchronous
    for now — approve endpoint runs the action inline). Rejected and
    executed entries stay in history until pruned.
    """

    def __init__(self, path: Optional[Path] = None):
        self._path = path or _queue_path()
        self._lock = threading.Lock()

    def _read_all(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8") or "[]")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("approval queue read failed (%s); starting empty", exc)
            return []
        return data if isinstance(data, list) else []

    def _write_all(self, items: List[Dict[str, Any]]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def enqueue(
        self,
        action_name: str,
        arguments: Dict[str, Any],
        preview: str,
        origin: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Add a new pending action. Returns the stored record."""
        with self._lock:
            items = self._read_all()
            if len(items) >= MAX_QUEUE_SIZE:
                raise ApprovalQueueError(
                    f"pending queue is full ({MAX_QUEUE_SIZE}); approve or reject some first"
                )
            record = {
                "id": f"pend_{uuid.uuid4().hex[:12]}",
                "action_name": action_name,
                "arguments": arguments,
                "preview": preview,
                "origin": origin or {},
                "status": "pending",
                "created_at": time.time(),
                "resolved_at": None,
                "result": None,
                "error": None,
            }
            items.append(record)
            self._write_all(items)
            return dict(record)

    def list(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = self._read_all()
        if status:
            items = [i for i in items if i.get("status") == status]
        return items

    def get(self, action_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for item in self._read_all():
                if item.get("id") == action_id:
                    return dict(item)
        return None

    def set_status(
        self,
        action_id: str,
        status: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update a record's status. Returns the updated record."""
        with self._lock:
            items = self._read_all()
            for item in items:
                if item.get("id") == action_id:
                    item["status"] = status
                    item["resolved_at"] = time.time()
                    if result is not None:
                        item["result"] = result
                    if error is not None:
                        item["error"] = error
                    self._write_all(items)
                    return dict(item)
            raise ApprovalQueueError(f"no pending action with id {action_id!r}")

    def prune_resolved(self, older_than_seconds: float = 7 * 24 * 3600) -> int:
        """Drop executed/failed/rejected entries older than the given
        age. Returns the number pruned. Pending entries are never
        pruned."""
        cutoff = time.time() - older_than_seconds
        with self._lock:
            items = self._read_all()
            keep = [
                i
                for i in items
                if i.get("status") == "pending"
                or (i.get("resolved_at") or 0) > cutoff
            ]
            removed = len(items) - len(keep)
            if removed:
                self._write_all(keep)
        return removed


_queue: Optional[ApprovalQueue] = None


def get_queue() -> ApprovalQueue:
    global _queue
    if _queue is None:
        _queue = ApprovalQueue()
    return _queue
