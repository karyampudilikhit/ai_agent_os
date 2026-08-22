"""LiveSessionManager — generic in-memory registry for a LIVE,
non-serializable resource that must survive across a founder's approval
tap on a SEPARATE HTTP request, possibly minutes later.

Extracted from BrowserSessionManager (backend/app/tools/browser_
session_manager.py), which was the first — and until a second tool
needs this, the only — concrete user of the pattern. See
[[vision-ai-browser-automation-priority]] in memory for why this was
worth pulling out now rather than waiting for a second consumer.

THE PATTERN: ApprovalQueue's normal shape is "enqueue arguments, replay
them from scratch at execute_now() time" (see approval_queue.py /
action_registry.py's mutating-action path). That works when an action
is a pure function of its arguments. It does NOT work when the tool
opens a live resource first — a browser session, a phone call, a
spawned OS process — and the founder's approval has to act on THAT SAME
live object, not a fresh one replayed from the original arguments.
Every tool shaped like this needs identically: an opaque token handed
back to the caller, the live resource kept in a process-memory dict
keyed by that token (Python objects generally aren't JSON-serializable,
so this can only live in-process — a server restart loses any in-flight
resource, same as every other in-memory store in this single-instance
MVP), and the eventual approval handler resolving token -> resource.
That bookkeeping — the dict, the lock, idle-sweeping abandoned
resources — is what lives here, exactly once.

NOT a base class by inheritance. A holder each concrete manager wraps
and forwards to (composition) — so BrowserSessionManager keeps its own
resource-specific create() signature (launches Playwright, needs a
`url`) and close() cleanup (closes a browser context), while token
bookkeeping and idle-sweeping are shared. A future tool with a
completely different create()/close() shape (e.g. a phone-call
manager: create(phone_number), close() hangs up) reuses this the same
way without forcing an artificial shared interface across unrelated
resource types.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Callable, Dict, Generic, Optional, Protocol, TypeVar

logger = logging.getLogger(__name__)


class Touchable(Protocol):
    """Minimal contract a resource must satisfy to be held here: it can
    report when it was last touched, and update that timestamp. Duck-
    typed on purpose — BrowserSession already has exactly this shape
    (it needed it before this module existed), so no wrapper is needed."""

    last_touched_at: float

    def touch(self) -> None: ...


T = TypeVar("T", bound=Touchable)


def new_token(prefix: str) -> str:
    """Mint an opaque token. A separate function (not baked into
    register()) because most callers need the token to construct the
    resource itself BEFORE it can be registered — e.g. BrowserSession
    is a dataclass whose `token` field is set at construction time."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class LiveSessionManager(Generic[T]):
    def __init__(self, idle_timeout_seconds: float, closer: Callable[[T], None]) -> None:
        """`closer` does whatever real cleanup this resource type needs
        (close a browser context, hang up a call, kill a process) — kept
        as an injected callback rather than a method a subclass
        overrides, since there's no shared base behavior to override."""
        self._idle_timeout_seconds = idle_timeout_seconds
        self._closer = closer
        self._resources: Dict[str, T] = {}
        self._lock = threading.Lock()

    def register(self, token: str, resource: T) -> None:
        with self._lock:
            self._resources[token] = resource

    def get(self, token: str) -> Optional[T]:
        with self._lock:
            resource = self._resources.get(token)
        if resource is not None:
            resource.touch()
        return resource

    def newest(self) -> Optional[T]:
        """The most recently registered live resource, or None.

        Exists for resources that are exclusive by nature. A browser
        holding one on-disk profile can have exactly one live context, so
        a caller asked to open a second URL has only two options: fail, or
        continue in the one that is already open. Failing is what used to
        happen, and it cost an agent the entire page state it had built up
        -- it "recovered" into a fresh browser and silently lost the work.
        """
        with self._lock:
            if not self._resources:
                return None
            resource = next(reversed(self._resources.values()))
        resource.touch()
        return resource

    def close(self, token: str) -> bool:
        """Returns True if a resource was actually found and closed,
        False if the token was already gone — lets callers avoid logging
        a "closed" line for a close() on an already-closed/unknown token."""
        with self._lock:
            resource = self._resources.pop(token, None)
        if resource is None:
            return False
        try:
            self._closer(resource)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LiveSessionManager: error closing %s: %s", token, exc)
        return True

    def sweep_idle(self) -> int:
        """Close every resource idle past idle_timeout_seconds. Returns
        the number closed. No background timer thread — callers sweep
        opportunistically (e.g. right before creating a new resource),
        which is enough at this scale and matches the rest of this
        single-instance MVP's in-memory stores."""
        cutoff = time.time() - self._idle_timeout_seconds
        with self._lock:
            stale = [t for t, r in self._resources.items() if r.last_touched_at < cutoff]
        for token in stale:
            logger.info("LiveSessionManager: sweeping idle resource %s", token)
            self.close(token)
        return len(stale)
