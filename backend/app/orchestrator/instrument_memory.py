"""Which instrument has already failed on which host, this run.

THE FAILURE THIS ANSWERS. Once there were two ways to reach a page --
the browser and web_read -- a new kind of waste appeared that neither
tool could see on its own.

Measured on a live Reddit run, 19 calls: web_read was tried on
old.reddit.com at step 1, came back "Failed to fetch url", and the agent
correctly switched to a headed browser at step 3 and got everything it
needed. Then at steps 11, 12 and 19 it went BACK to web_read on the same
host. Five of nineteen calls spent re-learning a fact the run had
established in its first thirty seconds.

The existing repeat guard cannot catch this. It fingerprints the exact
call, and these were different URLs every time --
/r/Entrepreneur/top/, /r/Entrepreneur/top/.json, /r/ycombinator/top/.
Different arguments, same doomed instrument against the same host.

WHAT IS RECORDED IS A FACT, NOT A JUDGEMENT. The entry is written when a
tool result SAYS it could not retrieve the page -- the same shapes the
tools already emit for "blocked" and "unreachable". Nothing asks the
model whether its own call went well, because a model that has just been
blocked is the least reliable narrator of whether it was blocked.

IT NUDGES, IT DOES NOT BLOCK. A host can start working: a bot check can
expire, a login can complete, a transient 502 can clear. So a repeat
attempt is allowed through with a note saying what happened last time and
which instrument DID work. Refusing outright would trade five wasted
calls for a run that cannot recover when the wall comes down -- which is
the more expensive mistake, and the one this codebase keeps refusing to
make.
"""

from __future__ import annotations


import threading
from typing import Dict, Optional, Set, Tuple
from urllib.parse import urlparse

# The shapes a tool uses to say "I could not retrieve this page". Every
# one of these is text THIS codebase writes, not text a site writes --
# matching a site's own wording would be matching untrusted content.
# Each carries punctuation or a tool name that only OUR messages have.
# A bare "could not fetch" was tried first and matched an article
# sentence -- "the report could not fetch enough support" -- which would
# have recorded a wall from a page we had successfully read. Every
# marker here has to be unwritable by a site.
_UNREACHABLE_MARKERS = (
    "(web_read got no content from",
    "(web_read could not fetch",
    "(browser_extract failed",
    "THIS PAGE RETURNED ALMOST NO TEXT",
    "Report it as UNREACHABLE rather than",
    "the site is refusing automated access",
)

# Which instrument a tool belongs to. A host that turns away
# browser_navigate turns away browser_extract on the same page, so they
# share a verdict; web_read and web_search likewise.
_BROWSER = "the browser"
_WEB = "web_read"


def instrument_of(tool: str) -> str:
    name = (tool or "").lower()
    if "browser" in name:
        return _BROWSER
    if "web_read" in name or "web_search" in name:
        return _WEB
    return ""


def host_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    # old.reddit.com and www.reddit.com are one wall, not two.
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else host


def looks_unreachable(result: str) -> bool:
    return any(m in (result or "") for m in _UNREACHABLE_MARKERS)


class InstrumentMemory:
    """Per-run, in memory. A wall is a fact about right now, not a fact
    worth persisting to the next run an hour later."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failed: Set[Tuple[str, str]] = set()
        self._worked: Dict[str, str] = {}

    def record(self, tool: str, url: str, result: str) -> None:
        host, instrument = host_of(url), instrument_of(tool)
        if not host or not instrument:
            return
        with self._lock:
            if looks_unreachable(result):
                self._failed.add((host, instrument))
            elif result:
                self._worked[host] = instrument

    def note_for(self, tool: str, url: str) -> Optional[str]:
        """What to tell the model BEFORE it repeats a doomed call."""
        host, instrument = host_of(url), instrument_of(tool)
        if not host or not instrument:
            return None
        with self._lock:
            if (host, instrument) not in self._failed:
                return None
            worked = self._worked.get(host)
        note = (
            f"(NOTE BEFORE THIS CALL: {instrument} already failed to reach "
            f"{host} earlier in this run — it was blocked or returned nothing. "
        )
        if worked and worked != instrument:
            note += (
                f"{worked} DID work on {host}: use that instead unless you have "
                f"a specific reason to think this attempt differs. "
            )
        else:
            note += (
                "No instrument has reached this host yet. If you try again, "
                "change something real — a visible browser with interactive "
                "true, or a different host altogether. "
            )
        return note + "Trying the same instrument on the same host with a "\
                      "different path is the thing that already did not work.)"


_memory: Optional[InstrumentMemory] = None
_memory_lock = threading.Lock()


def get_memory() -> InstrumentMemory:
    global _memory
    with _memory_lock:
        if _memory is None:
            _memory = InstrumentMemory()
        return _memory


def reset() -> None:
    """A new run starts with no verdicts. Tests, and the loop's start."""
    global _memory
    with _memory_lock:
        _memory = InstrumentMemory()
