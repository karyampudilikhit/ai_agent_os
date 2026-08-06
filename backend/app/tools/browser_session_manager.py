"""BrowserSessionManager — in-memory registry of LIVE, HEADED Playwright
browser sessions used by the interactive browser-automation action
(action.browser_task / browser_login_wait / browser_submit).

Why this is separate from DeepResearchTool: DeepResearchTool opens and
closes a headless browser within a single call — stateless, safe to run
concurrently, nothing to keep alive between calls. Interactive browser
automation is the opposite: the founder logs in themselves in a REAL
VISIBLE window (deliberately not headless — there is no in-app remote-
control surface, so "you log in, it persists" means literally that), and
that same authenticated browser context has to survive across TWO
separate HTTP requests — the run that opens it, and the founder's later
approval tap — that can be minutes apart. Playwright objects aren't
JSON-serializable, so this state can only live in-process, in memory —
a server restart loses any in-flight session, matching every other
in-memory store in this codebase (RunStore, ClarificationStore,
ProgressStore) for this single-user local MVP.

Session lifecycle:
    created (visible window opens) -> founder logs in if needed
        -> fields filled -> awaiting founder's submit approval
        -> submitted & closed | rejected (swept later) | idle-timeout (swept)

The token bookkeeping (the dict, the lock, idle-sweeping) below used to
be hand-rolled here. It's now LiveSessionManager (backend/app/actions/
live_session_manager.py) — this class kept its exact public shape
(create/get/close/sweep_idle unchanged), just delegates that mechanism
instead of owning it, so browser_task.py needed zero changes. See that
module's docstring for why this was worth generalizing.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from backend.app.actions.live_session_manager import LiveSessionManager, new_token

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------
# The single Playwright thread.
#
# Playwright's SYNC api may only have ONE running instance per thread,
# and every object it hands back (Page, Locator) belongs to the thread
# that created it. Our sessions are deliberately long-lived — they must
# survive across separate HTTP requests so the founder can log in and
# approve — which means we never call playwright.stop() between calls.
#
# Those two facts collided and produced a real production bug: the
# second browser call in a run raised
#     "It looks like you are using Playwright Sync API inside the
#      asyncio loop. Please use the Async API instead."
# The message is misleading — it has nothing to do with uvicorn's event
# loop. Reproduced in isolation with no web server involved at all: any
# second sync_playwright().start() in a thread that already has a live
# one fails exactly this way. Because the first session is still open by
# design, every browser call after the first in a run was failing.
#
# Fix, both halves needed:
#   1. ONE shared Playwright instance per manager (see _playwright()),
#      started once and never stopped while the process lives. Closing a
#      session closes its CONTEXT, not the shared instance.
#   2. All Playwright work pinned to this one dedicated thread, so
#      instance and objects are always touched from where they were made.
#
# Single worker is also correct for a second reason already documented on
# _launch_persistent_context: one on-disk profile can only be held by one
# context at a time, so browser work has to serialize regardless.
# ----------------------------------------------------------------
_BROWSER_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")

# Ceiling on how long a caller will wait for the browser thread. Needed
# because serializing onto one worker means a browser_task_async that's
# parked in a 10-minute founder-login wait would otherwise block every
# later browser call indefinitely — and the agentic loop calling one of
# those would hang past its own deadline. On timeout the caller gets a
# clear message; the queued work is NOT cancelled and still runs.
BROWSER_OP_TIMEOUT_SECONDS = 180.0


def is_browser_thread() -> bool:
    """True when the caller is ALREADY the Playwright worker. Needed
    because the executor has a single worker: code running on it that
    submits more work and waits would deadlock against itself."""
    return threading.current_thread().name.startswith("playwright")


def run_on_browser_thread(fn: Callable, *args, timeout: Optional[float] = BROWSER_OP_TIMEOUT_SECONDS, **kwargs):
    """Run `fn` on the dedicated Playwright thread and WAIT for its
    result, re-raising anything it raised. Use for any code path that
    touches a Page/Locator/BrowserContext. Raises
    concurrent.futures.TimeoutError if the thread is still busy after
    `timeout` — callers should turn that into a readable message rather
    than let it surface as a stack trace."""
    if is_browser_thread():
        return fn(*args, **kwargs)
    return _BROWSER_EXECUTOR.submit(fn, *args, **kwargs).result(timeout=timeout)


def submit_to_browser_thread(fn: Callable, *args, **kwargs) -> Future:
    """Queue `fn` on the Playwright thread WITHOUT waiting — for the
    non-blocking browser_task_async path, whose whole point is returning
    before a possibly-minutes-long founder login completes. Work queues
    behind anything already running, which is the intended serialization."""
    return _BROWSER_EXECUTOR.submit(fn, *args, **kwargs)

SESSION_IDLE_TIMEOUT_SECONDS = 15 * 60  # abandoned session gets closed
LOGIN_WAIT_TIMEOUT_SECONDS = 10 * 60    # how long we'll wait for a human to log in
LOGIN_POLL_INTERVAL_SECONDS = 2.0       # how often the wait loop checks in

# Launch args that strip the most common automation fingerprints. The
# single biggest one is --disable-blink-features=AutomationControlled,
# which removes navigator.webdriver=true — the flag Google's "this
# browser or app may not be secure" check keys off first. Combined with
# channel="chrome" (the founder's REAL installed Chrome, not Playwright's
# bundled Chromium build, which carries its own detectable signature)
# this clears the bar for the large majority of real sites. It does NOT
# reliably beat Google's own OAuth sign-in — that detection is layered
# and adversarial — so google-account-only logins may still fail; that's
# a stated limitation, not something a flag fixes.
_STEALTH_ARGS: List[str] = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--start-maximized",
]

# Persistent profile dir — the whole point of "log in once, it persists."
# A persistent context reuses the same cookies/localStorage between runs,
# so after the founder logs into a site once, later browser tasks against
# that site open ALREADY authenticated (no login-wait pause at all).
def _profile_dir() -> str:
    from backend.app.utils.paths import under_data
    d = under_data("browser_profile")
    os.makedirs(d, exist_ok=True)
    return str(d)


# ----------------------------------------------------------------
# DOM inspection — walk the live page for fillable fields + buttons.
# Lives here (not in browser_task.py, which originally defined these)
# because create()'s headless-with-safety-upgrade decision below needs
# login-detection at session-open time, before any task-flow code runs.
# browser_task.py imports these back as _snapshot_page/_looks_like_login_page.
# ----------------------------------------------------------------
_SNAPSHOT_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const labelFor = (el) => {
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return clean(lbl.innerText);
    }
    const wrap = el.closest('label');
    if (wrap) return clean(wrap.innerText);
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    if (el.placeholder) return clean(el.placeholder);
    if (el.name) return clean(el.name);
    return '';
  };
  const fields = [];
  document.querySelectorAll('input, textarea, select').forEach((el) => {
    const type = (el.type || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return;
    if (el.disabled) return;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return;  // invisible field
    let options = undefined;
    if (el.tagName.toLowerCase() === 'select') {
      options = Array.from(el.options).map(o => clean(o.textContent)).filter(Boolean);
    }
    fields.push({
      kind: el.tagName.toLowerCase(),
      type,
      label: labelFor(el),
      name: el.name || '',
      id: el.id || '',
      placeholder: el.placeholder || '',
      currentValue: el.value || '',
      options,
    });
  });

  const buttons = [];
  document.querySelectorAll('button, input[type="submit"], input[type="button"]').forEach((el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return;
    const text = clean(el.innerText || el.value || el.getAttribute('aria-label') || '');
    if (!text) return;
    buttons.push({ text, type: (el.type || '').toLowerCase() });
  });

  return { fields, buttons, url: window.location.href, title: document.title };
}
"""


def snapshot_page(page) -> Dict[str, Any]:
    return page.evaluate(_SNAPSHOT_JS)


def looks_like_login_page(snapshot: Dict[str, Any]) -> bool:
    """Heuristic: a password-type field present. Covers the large
    majority of real login pages without needing an LLM call just to
    answer 'is this a login page'."""
    return any(f.get("type") == "password" for f in snapshot.get("fields") or [])


@dataclass
class BrowserSession:
    token: str
    playwright: Any    # the running sync_playwright() context object
    browser: Any        # playwright Browser
    context: Any         # playwright BrowserContext
    page: Any            # playwright Page
    created_at: float = field(default_factory=time.time)
    last_touched_at: float = field(default_factory=time.time)
    login_event: threading.Event = field(default_factory=threading.Event)
    # Status tracking for the ASYNC entry point (action.browser_task_async
    # / browser_task_status) — the loop's caller isn't blocked on this
    # session, so it needs somewhere to check "what's happening now"
    # instead of a return value. The sync action.browser_task path
    # (dynamic_employee's pre-flight dispatch) doesn't read these; it
    # still gets its answer as a normal return value, unchanged.
    status: str = "opening"
    last_message: str = ""
    _status_lock: threading.Lock = field(default_factory=threading.Lock)

    def touch(self) -> None:
        self.last_touched_at = time.time()

    def set_status(self, status: str, message: str) -> None:
        with self._status_lock:
            self.status = status
            self.last_message = message
        self.touch()

    def get_status(self) -> tuple[str, str]:
        with self._status_lock:
            return self.status, self.last_message


def _close_browser_session(session: "BrowserSession") -> None:
    """Close one session's CONTEXT only.

    Deliberately does NOT call playwright.stop(): the Playwright instance
    is shared by every session (see the module docstring above and
    BrowserSessionManager._playwright). Stopping it here — which this
    function used to do — is what made the next session's start() fail.

    Marshalled onto the browser thread, because closing touches the
    BrowserContext and that object belongs to the thread that made it.
    Without this, close() called from a request/test thread died with
    "Cannot switch to a different thread", the context never actually
    closed, and Chrome processes piled up holding the shared on-disk
    profile — which then made every later browser launch fail with
    "profile is already in use". That was the real source of the stray
    chrome.exe processes and the flaky browser tests.
    """
    def _do_close() -> None:
        session.context.close()
        # Persistent contexts have no separate Browser object
        # (session.browser is None); guard for the non-persistent case in
        # case this ever changes back.
        if session.browser is not None:
            session.browser.close()

    run_on_browser_thread(_do_close)


class BrowserSessionManager:
    def __init__(self) -> None:
        self._live: LiveSessionManager[BrowserSession] = LiveSessionManager(
            idle_timeout_seconds=SESSION_IDLE_TIMEOUT_SECONDS,
            closer=_close_browser_session,
        )
        # The one shared Playwright instance, started lazily on first use
        # and kept for the life of the process. See module docstring.
        self._pw: Any = None

    def _playwright(self):
        """The single shared Playwright instance. MUST be called from the
        browser thread (everything reaching it goes through
        run_on_browser_thread / submit_to_browser_thread), which is why no
        lock is needed here — that executor has exactly one worker, so
        these calls are already serialized."""
        if self._pw is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            logger.info("Started the shared Playwright instance")
        return self._pw

    def create(self, url: str, prefer_headless: bool = False) -> BrowserSession:
        """Open a session at `url`. Two modes:

        prefer_headless=False (default, unchanged behavior) — a REAL,
        VISIBLE window. This is how "you log in once, it persists"
        actually works: the founder interacts with a normal browser
        window on their own screen. Use this whenever the task might
        need a login (action.browser_task / browser_task_async both do).

        prefer_headless=True — tries an invisible window first (faster,
        no window popping up for a task that's just reading a page).
        SAFETY GATE, not a best-effort: the moment the first snapshot of
        that page looks like a login page, this closes the headless
        session and re-opens the SAME url headed instead, before
        anything else happens. A headless window can never satisfy "the
        founder logs in themselves" — there is nothing to weaken here,
        the upgrade is unconditional. Used by action.browser_navigate,
        which doesn't know in advance whether a login is needed.

        Uses a PERSISTENT context (a real on-disk profile) launched from
        the founder's actual installed Chrome where available, with
        automation-fingerprint flags stripped. Two payoffs over the old
        bundled-headless-signature Chromium:
          - Google's "this browser may not be secure" block (which the
            bundled Chromium tripped on a real GitHub login attempt) is
            cleared for the large majority of sites.
          - Cookies persist between runs, so a site the founder has
            already logged into once opens ALREADY authenticated — the
            login-wait pause simply doesn't fire the second time.
        """
        session = self._open(url, headless=prefer_headless)
        if prefer_headless:
            try:
                if looks_like_login_page(snapshot_page(session.page)):
                    logger.info(
                        "Headless session at %s needs a login — upgrading to a "
                        "visible window (unconditional, not best-effort)", url[:80],
                    )
                    self.close(session.token)
                    session = self._open(url, headless=False)
            except Exception as exc:  # noqa: BLE001
                # Can't confirm the page's shape (mid-navigation, JS error,
                # etc.) — err toward the SAFE side and upgrade to headed
                # rather than risk silently staying headless on a login page.
                logger.info(
                    "Headless login-check failed (%s) — upgrading to a visible "
                    "window to be safe", exc,
                )
                self.close(session.token)
                session = self._open(url, headless=False)
        return session

    def _open(self, url: str, headless: bool) -> BrowserSession:
        """The actual Playwright launch — factored out from create() so
        the headless-upgrade DECISION logic above can be exercised in
        tests by monkeypatching this one method, without needing a real
        browser."""
        pw = self._playwright()
        context = self._launch_persistent_context(pw, headless=headless)
        # A persistent context opens with one blank page already present.
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        token = new_token("bsess")
        session = BrowserSession(
            token=token, playwright=pw, browser=None, context=context, page=page,
        )
        session.set_status("opening", f"Opened a browser window at {url}.")
        self._live.register(token, session)
        logger.info(
            "Browser session %s opened at %s (headless=%s)", token, url[:80], headless,
        )
        return session

    def _launch_persistent_context(self, pw, headless: bool = False):
        """Try the founder's real Chrome first (best at not looking
        automated); fall back to Playwright's bundled Chromium if Chrome
        isn't installed. Both use the same persistent profile + stealth
        args. Single-user MVP caveat: one persistent profile can be held
        by one context at a time (headless or headed — same profile
        either way), so genuinely concurrent browser tasks would
        contend — not a concern for a single founder driving one task
        at a time."""
        profile = _profile_dir()
        try:
            return pw.chromium.launch_persistent_context(
                profile,
                headless=headless,
                channel="chrome",
                args=_STEALTH_ARGS,
                no_viewport=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "Real-Chrome launch unavailable (%s); using bundled Chromium. "
                "Google-account logins are more likely to be blocked this way.",
                exc,
            )
            return pw.chromium.launch_persistent_context(
                profile,
                headless=headless,
                args=_STEALTH_ARGS,
                no_viewport=True,
            )

    def get(self, token: str) -> Optional[BrowserSession]:
        return self._live.get(token)

    def close(self, token: str) -> None:
        if self._live.close(token):
            logger.info("Browser session %s closed", token)

    def sweep_idle(self) -> int:
        """Close any session idle past SESSION_IDLE_TIMEOUT_SECONDS.
        Returns the number closed. Called opportunistically before
        creating a new session — no background timer thread needed at
        this scale. Guards against an abandoned or rejected task
        leaking a live Chromium process forever (a rejected browser_submit
        doesn't run any handler today, so this sweep is the only cleanup
        path for that case — see browser_task.py's module docstring)."""
        return self._live.sweep_idle()


_manager: Optional[BrowserSessionManager] = None


def get_manager() -> BrowserSessionManager:
    global _manager
    if _manager is None:
        _manager = BrowserSessionManager()
    return _manager
