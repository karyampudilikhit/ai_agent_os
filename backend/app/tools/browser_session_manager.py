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
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

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


class BrowserSessionManager:
    def __init__(self) -> None:
        self._sessions: Dict[str, BrowserSession] = {}
        self._lock = threading.Lock()

    def create(self, url: str) -> BrowserSession:
        """Launch a REAL, VISIBLE (headed) browser window navigated to
        `url`. Visible on purpose — this is how "you log in once, it
        persists" actually works: the founder interacts with a normal
        browser window on their own screen.

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
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        context = self._launch_persistent_context(pw)
        # A persistent context opens with one blank page already present.
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        token = f"bsess_{uuid.uuid4().hex[:12]}"
        session = BrowserSession(
            token=token, playwright=pw, browser=None, context=context, page=page,
        )
        session.set_status("opening", f"Opened a browser window at {url}.")
        with self._lock:
            self._sessions[token] = session
        logger.info("Browser session %s opened at %s", token, url[:80])
        return session

    def _launch_persistent_context(self, pw):
        """Try the founder's real Chrome first (best at not looking
        automated); fall back to Playwright's bundled Chromium if Chrome
        isn't installed. Both use the same persistent profile + stealth
        args. Single-user MVP caveat: one persistent profile can be held
        by one context at a time, so genuinely concurrent browser tasks
        would contend — not a concern for a single founder driving one
        task at a time."""
        profile = _profile_dir()
        try:
            return pw.chromium.launch_persistent_context(
                profile,
                headless=False,
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
                headless=False,
                args=_STEALTH_ARGS,
                no_viewport=True,
            )

    def get(self, token: str) -> Optional[BrowserSession]:
        with self._lock:
            session = self._sessions.get(token)
        if session:
            session.touch()
        return session

    def close(self, token: str) -> None:
        with self._lock:
            session = self._sessions.pop(token, None)
        if not session:
            return
        try:
            session.context.close()
            # Persistent contexts have no separate Browser object
            # (session.browser is None); guard for the non-persistent
            # case in case this ever changes back.
            if session.browser is not None:
                session.browser.close()
            session.playwright.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error closing browser session %s: %s", token, exc)
        logger.info("Browser session %s closed", token)

    def sweep_idle(self) -> int:
        """Close any session idle past SESSION_IDLE_TIMEOUT_SECONDS.
        Returns the number closed. Called opportunistically before
        creating a new session — no background timer thread needed at
        this scale. Guards against an abandoned or rejected task
        leaking a live Chromium process forever (a rejected browser_submit
        doesn't run any handler today, so this sweep is the only cleanup
        path for that case — see browser_task.py's module docstring)."""
        cutoff = time.time() - SESSION_IDLE_TIMEOUT_SECONDS
        with self._lock:
            stale = [t for t, s in self._sessions.items() if s.last_touched_at < cutoff]
        for token in stale:
            logger.info("Sweeping idle browser session %s", token)
            self.close(token)
        return len(stale)


_manager: Optional[BrowserSessionManager] = None


def get_manager() -> BrowserSessionManager:
    global _manager
    if _manager is None:
        _manager = BrowserSessionManager()
    return _manager
