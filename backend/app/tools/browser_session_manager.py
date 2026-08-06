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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.app.actions.live_session_manager import LiveSessionManager, new_token

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
    session.context.close()
    # Persistent contexts have no separate Browser object
    # (session.browser is None); guard for the non-persistent case in
    # case this ever changes back.
    if session.browser is not None:
        session.browser.close()
    session.playwright.stop()


class BrowserSessionManager:
    def __init__(self) -> None:
        self._live: LiveSessionManager[BrowserSession] = LiveSessionManager(
            idle_timeout_seconds=SESSION_IDLE_TIMEOUT_SECONDS,
            closer=_close_browser_session,
        )

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
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
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
