"""The universal browser primitives — observe, click, type, select,
scroll, press, wait.

WHAT THESE REPLACE. The existing browser actions address elements by
their VISIBLE TEXT (`browser_click(target="Deploy")`) and can only read
and click. That is enough to walk through links and is not enough to
operate a web application: it cannot disambiguate two buttons reading
"Deploy", cannot reach an icon-only control, and cannot fill in a form
at all.

These are deliberately SMALL and deterministic. The model never touches
Playwright; it names a semantic element id from the last observation and
this layer resolves it. That boundary is the point -- everything the
model can do to a browser is enumerable, checkable, and refusable.

Kept in a separate module from browser_task.py, which is already 1,250
lines and owns a different concern: the founder-in-the-loop
login/fill/approve flow. These are the primitives the agentic loop drives
directly.

EVERY handler here does the same four things in the same order:
    1. resolve the session            (does this token still exist?)
    2. check the policy               (is this verb and URL in scope?)
    3. act on the browser thread      (Playwright objects are pinned)
    4. re-observe and report          (what did that actually do?)

Step 4 is not decoration. The whole reliability argument for this layer
is that the model sees the real consequence of its action rather than
being told the click succeeded.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable, Dict, Optional

from backend.app.actions.action_registry import ActionSpec
from backend.app.browser.observation import (
    FIND_SCAN_LIMIT,
    find_elements,
    observe_page,
    render_matches,
)
from backend.app.browser.policy import (
    PolicyViolation,
    active_policy,
    wrap_untrusted,
)
from backend.app.browser.session_manager import (
    BROWSER_OP_TIMEOUT_SECONDS,
    get_manager,
    run_on_browser_thread,
)

logger = logging.getLogger(__name__)

# How long a single primitive waits for its element. Short on purpose:
# these run inside an agentic step that has its own deadline, and a tool
# that hangs for 30s burns the step budget that would have let the model
# try something else.
ACTION_TIMEOUT_MS = 8000


def _policy():
    """The scope in force for this employee's run.

    Read through one function rather than threaded as an argument
    through every handler: a policy that travels as a parameter is a
    policy some call site eventually forgets to pass, and a browser
    action running with no scope is the failure this whole layer exists
    to prevent. Falls back to the hard floor when nothing was set.
    """
    return active_policy()


def _session(args: Dict[str, Any]):
    token = str(args.get("session_token") or "").strip()
    if not token:
        return None, "(missing 'session_token' — open a page with action.browser_navigate first)"
    session = get_manager().get(token)
    if not session:
        return None, ("(that browser session no longer exists — it may have timed out. "
                      "Open the page again with action.browser_navigate.)")
    session.touch()
    return session, None


def _on_thread(fn: Callable, *a, **kw) -> str:
    """Run browser work on the Playwright thread, turning the two
    failures that are not bugs into readable text rather than a
    traceback: the thread being busy, and the page having moved on."""
    try:
        return run_on_browser_thread(fn, *a, timeout=BROWSER_OP_TIMEOUT_SECONDS, **kw)
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if "Timeout" in name:
            return ("(the browser is busy with another task — it serialises onto one "
                    "window. Try again in a moment.)")
        logger.warning("browser primitive failed: %s: %s", name, exc)
        return f"(browser action failed: {exc})"


def _resolve(session, element_id: str):
    """The locator for `element_id`, or a message explaining why not.

    An id like `f1e3` lives inside a frame, so the locator is built from
    that frame rather than the page. Resolving a frame element against
    the main document would find nothing — or worse, find a same-named
    element in the wrong document.
    """
    problem = session.element_map.check(element_id)
    if problem:
        return None, f"({problem})"
    selector = session.element_map.selector_for(element_id)
    # getattr, not a direct call: several tests substitute their own
    # element_map, and a method others substitute should keep the shape
    # they substituted. Widening this signature broke five of them at
    # once the first time.
    frame_for = getattr(session.element_map, "frame_for", None)
    root = (frame_for(element_id) if callable(frame_for) else None) or session.page
    try:
        locator = root.locator(selector).first
    except Exception:  # noqa: BLE001
        return None, (f"({element_id} was inside a frame that has since gone "
                      "away. Call action.browser_observe for fresh ids.)")
    if locator.count() == 0:
        return None, (f"({element_id} is no longer on the page — it was there when you "
                      "observed, and the page has changed since. Call "
                      "action.browser_observe for fresh ids.)")
    return locator, None


def _describe(session, note: str, include_text: bool = False) -> str:
    """Re-observe and report what the action actually did.

    A login wall is called out HERE rather than in each handler, so every
    primitive reports it the same way. Without this the agent hits a
    sign-in page, sees a form it cannot fill, and spends its remaining
    budget clicking around a page that will never yield -- which is what
    happens on every social platform, where the wall is the first thing
    you meet.
    """
    obs = observe_page(session.page, session.element_map)
    body = f"{note}\n\n{obs.render(include_text=include_text)}"
    try:
        if _on_login_wall(session.page):
            body = (
                "THIS PAGE IS ASKING FOR A SIGN-IN. You cannot do this part: "
                "passwords are never typed by the AI and never enter this "
                "system. Call action.browser_await_login — a real window is "
                "already open, the founder signs in there, and the run "
                "continues by itself the moment it is done. Do not try to "
                "fill the password field and do not click around looking for "
                "a way past it.\n\n"
            ) + body
    except Exception:  # noqa: BLE001
        pass
    return wrap_untrusted(body, obs.url) if include_text else body


# ---------------------------------------------------------------- observe

def _observe_impl(args: Dict[str, Any]) -> str:
    session, err = _session(args)
    if err:
        return err
    try:
        _policy().check_action("observe")
        _policy().check_url(session.page.url)
    except PolicyViolation as exc:
        return str(exc)
    obs = observe_page(session.page, session.element_map)
    return wrap_untrusted(obs.render(include_text=True), obs.url)


# --------------------------------------------------------- blocking overlays

# A consent wall or modal sits ON TOP of the page: the content is right
# there in the DOM, the observer lists it, and every click lands on the
# overlay instead. From the agent's side that is indistinguishable from
# clicking the wrong element -- the page "doesn't move" and it tries
# again, and again, until the budget is gone.
#
# Ordered by preference: decline before accept. This runs on the
# founder's real browser with their real profile, so the privacy-
# preserving choice is the one to make on their behalf.
#
# The wordings are the real ones, taken off live walls rather than
# imagined: theguardian.com offers "No, thank you" — not "no thanks",
# which is what this list said first, and why it matched nothing on the
# most common consent wall on the web.
_DISMISS_LABELS = (
    # decline, most explicit first
    "continue without accepting", "reject all", "decline all", "refuse all",
    "reject non-essential", "only necessary", "necessary only",
    "essential only", "use necessary cookies only", "no thank you",
    "no thanks", "reject", "decline",
    # then accept, because a closed wall beats a blocked run
    "yes i accept", "accept all", "allow all", "accept cookies",
    "i accept", "i agree", "agree", "accept",
    # then plain dismissal
    "got it", "understood", "ok", "okay", "close", "dismiss",
    "maybe later", "not now", "skip", "continue",
)

# Punctuation and case differ between every implementation of the same
# button, so both sides get flattened before they are compared.
_PUNCT = re.compile(r"[^a-z0-9 ]+")


def _norm_label(text: str) -> str:
    return " ".join(_PUNCT.sub(" ", (text or "").lower()).split())

_OVERLAY_JS = r"""
() => {
  // THE QUESTION IS NOT "is something big and fixed on this page".
  //
  // It is: WHAT WOULD A CLICK IN THE MIDDLE OF THE PAGE ACTUALLY HIT?
  // The first version asked the former and counted theguardian.com's own
  // sticky navigation header as a blocking overlay — so a run that had
  // successfully closed the consent wall still reported the page as
  // blocked. Site furniture lives at the edges; a modal owns the centre.
  //
  // elementFromPoint answers the real question directly, and it answers
  // it the same way the browser will when the agent clicks.
  const vw = window.innerWidth, vh = window.innerHeight;
  const probes = [[vw / 2, vh / 2], [vw / 2, vh * 0.4], [vw / 2, vh * 0.6]];
  const found = new Map();

  for (const [x, y] of probes) {
    let el = document.elementFromPoint(x, y);
    while (el && el !== document.body && el !== document.documentElement) {
      const s = getComputedStyle(el);
      const z = parseInt(s.zIndex || '0', 10) || 0;
      const isLayer = s.position === 'fixed' || s.position === 'absolute';
      const isDialog = el.tagName === 'DIALOG' ||
                       el.getAttribute('role') === 'dialog' ||
                       el.getAttribute('aria-modal') === 'true';
      if (isDialog || (isLayer && z >= 100)) {
        const r = el.getBoundingClientRect();
        const txt = (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120);
        const key = txt.slice(0, 60) + '|' + Math.round(r.width);
        if (!found.has(key)) {
          found.set(key, {
            z: z, text: txt || '(no text)',
            area: Math.round((r.width * r.height) / (vw * vh) * 100),
          });
        }
        break;
      }
      el = el.parentElement;
    }
  }
  return Array.from(found.values()).sort((a, b) => b.z - a.z).slice(0, 4);
}
"""


# CLICKS THAT CANNOT BE TAKEN BACK.
#
# browser_click_element is mutating=False, which is right for the
# ninety-nine clicks in a run that sort a table or open a menu — and
# wrong for the one that sends a message, places an order or deletes
# something. Those went straight through, because the approval queue
# keys on the TOOL and this tool is usually harmless.
#
# So the check is on the BUTTON, not the tool. It has to be, given what
# this layer is for: the outreach flow ends in "Send", the ad flow ends
# in "Publish", and both spend something the founder cannot get back.
_IRREVERSIBLE_CLICK_WORDS = (
    # sending and posting
    "send", "send message", "send invite", "send request", "post", "publish",
    "share", "tweet", "reply", "comment", "submit",
    # money
    "buy", "buy now", "purchase", "pay", "pay now", "place order",
    "confirm order", "complete order", "checkout", "subscribe", "upgrade",
    "add funds", "withdraw", "transfer", "donate", "confirm payment",
    # destruction and commitment. "sign" is deliberately absent: "Sign
    # in" and "Sign up" are the two most common buttons on the web and
    # neither is irreversible.
    "delete", "remove", "deactivate", "close account", "cancel subscription",
    "accept offer", "agree and continue", "confirm and",
    # ads specifically
    "go live", "set live", "launch campaign", "publish campaign",
)


# Verbs where "verb + object" is unambiguously the action itself.
# Deliberately shorter than the list above.
_PREFIX_VERBS = (
    "send", "delete", "remove", "buy", "pay", "publish", "confirm",
    "launch", "withdraw", "transfer", "donate", "place order",
)


def _approved(args: Dict[str, Any]) -> bool:
    """True when this call IS the founder's approved one firing.

    The approval path re-invokes the same handler with the flag set, so
    the check has to let the second call through or an approved action
    would queue itself forever.
    """
    return bool(args.get("_approved"))


def _queue_for_approval(session, element_id: str, name: str) -> Optional[str]:
    """Put the click in front of the founder. Returns the receipt, or
    None if the queue is unavailable — in which case the click proceeds,
    because breaking every run over an unreachable queue is worse than
    the risk it manages, and every other guard still applies."""
    try:
        from backend.app.actions.approval_queue import get_queue
        record = get_queue().enqueue(
            action_name="browser_click_element",
            arguments={"session_token": session.token,
                       "element_id": element_id, "_approved": True},
            preview=f'Click "{name}" on {str(session.page.url)[:90]}',
            origin={"kind": "browser", "url": session.page.url, "label": name},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not queue %r for approval (%s) — allowing", name, exc)
        return None
    logger.info("queued an irreversible click for approval: %r", name)
    return (
        f'[queued for founder approval] "{name}" looks like it cannot be '
        f'undone — sending, publishing, paying or deleting — so it has NOT '
        f'been clicked. The founder approves it in the app '
        f'(pending id {record.get("id")}). '
        f'This is expected and correct: treat it as done-for-now, do not '
        f'queue it again, and carry on with anything else the task needs.'
    )


def _looks_irreversible(name: str) -> bool:
    """True when a control's own label says it does something final.

    Matched on the normalised WHOLE label, not a substring: "Send" is
    irreversible and "Sender name" is a field, "Post" is irreversible and
    "Posted 3 days ago" is a timestamp. Substring matching turned every
    job listing into a payment confirmation.
    """
    label = _norm_label(name)
    if not label:
        return False
    if label in _IRREVERSIBLE_CLICK_WORDS:
        return True
    # "Send message to Priya" — the verb leads the label. Only for verbs
    # where verb-plus-object is unambiguously an action: matching every
    # word this way made "Post code" a publish button and "Sign in" a
    # contract signature.
    return any(label.startswith(v + " ") for v in _PREFIX_VERBS)


def _follow_new_tab(session) -> bool:
    """Move the session to a tab the last action opened, if any.

    A click on target="_blank" — every "open in new tab" link, and every
    OAuth popup — leaves the session pointing at the ORIGINAL page. From
    the agent's side the click succeeded and nothing changed, so it
    retries on a page that will never move while the thing it wanted sits
    in a tab nobody is looking at. It is the same class of failure as the
    hidden sort control: the state is real, and unreachable.

    Returns True when the session moved.
    """
    try:
        pages = [p for p in session.context.pages if not p.is_closed()]
    except Exception:  # noqa: BLE001
        return False
    if len(pages) < 2 or pages[-1] is session.page:
        return False

    newest = pages[-1]
    try:
        newest.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        pass
    try:
        _policy().check_url(newest.url)
    except PolicyViolation:
        # A new tab outside the task's scope is closed rather than
        # followed. An ad or a tracker must not become the page the agent
        # is working on.
        try:
            newest.close()
        except Exception:  # noqa: BLE001
            pass
        return False

    session.page = newest
    # Ids belong to a document. Carrying the old map onto a new tab would
    # resolve e17 against whatever now sits in that position.
    session.element_map.new_generation()
    logger.info("followed a new tab to %s", str(newest.url)[:80])
    return True


def _frames(page):
    """The main document plus every iframe, main document first.

    Frames come and go while a page settles, so a frame that has already
    detached raises on any use -- collected defensively rather than
    trusted.
    """
    out = [page.main_frame]
    try:
        for f in page.frames:
            if f is not page.main_frame:
                out.append(f)
    except Exception:  # noqa: BLE001
        pass
    return out


def _dismiss_impl(args: Dict[str, Any]) -> str:
    """Close whatever is sitting on top of the page.

    Tries the named controls a consent wall actually uses, DECLINING
    before accepting -- this drives the founder's real browser with their
    real profile, so the privacy-preserving answer is the one to give on
    their behalf. Falls back to Escape.

    Reports what it closed, and reports honestly when nothing was in the
    way, because "I dismissed the popup" on a page that had none is the
    kind of confident nonsense the rest of this layer exists to stop.
    """
    session, err = _session(args)
    if err:
        return err
    try:
        _policy().check_action("click")
        _policy().check_url(session.page.url)
    except PolicyViolation as exc:
        return str(exc)

    try:
        blockers = session.page.evaluate(_OVERLAY_JS) or []
    except Exception:  # noqa: BLE001
        blockers = []

    # EVERY FRAME, not just the top document.
    #
    # Consent management platforms almost always render inside a
    # cross-origin iframe -- measured on theguardian.com, where the wall
    # covers 100% of the viewport and searching the main page finds
    # nothing at all. A dismisser that only looks at the top document
    # fails on the single most common blocking overlay on the web.
    # Read the buttons that are actually there, then pick by preference.
    # Matching a guessed string against the DOM was the bug: it needed
    # the site to phrase its button exactly the way this list did.
    for frame in _frames(session.page):
        try:
            labels = frame.evaluate(
                "() => Array.from(document.querySelectorAll("
                "'button,[role=button],a[role=button],input[type=button],"
                "input[type=submit]')).map(b => (b.innerText || b.value || "
                "b.getAttribute('aria-label') || '').replace(/\\s+/g,' ')"
                ".trim()).filter(t => t && t.length < 60)"
            ) or []
        except Exception:  # noqa: BLE001
            continue
        if not labels:
            continue

        by_norm = {}
        for raw in labels:
            by_norm.setdefault(_norm_label(raw), raw)

        for wanted in _DISMISS_LABELS:
            raw = by_norm.get(wanted)
            if raw is None:
                continue
            try:
                loc = frame.get_by_role(
                    "button", name=re.compile(rf"^\s*{re.escape(raw)}\s*$", re.I)
                ).first
                if not loc.count():
                    loc = frame.get_by_text(raw, exact=True).first
                if not loc.count():
                    continue
                loc.click(timeout=2500, force=True)
                session.page.wait_for_timeout(800)
                where = "" if frame is session.page.main_frame else " (in a frame)"
                return _describe(
                    session, f'Dismissed an overlay by clicking "{raw}"{where}.')
            except Exception:  # noqa: BLE001
                continue

    # Nothing named matched. Escape closes a great many dialogs.
    try:
        session.page.keyboard.press("Escape")
        session.page.wait_for_timeout(500)
        after = session.page.evaluate(_OVERLAY_JS) or []
        if len(after) < len(blockers):
            return _describe(session, "Dismissed an overlay with Escape.")
    except Exception:  # noqa: BLE001
        pass

    if not blockers:
        return _describe(
            session,
            "Nothing is covering the page — there was no overlay to dismiss. "
            "If a click is not working, the element is the problem, not a "
            "popup.",
        )
    top = blockers[0]
    return _describe(
        session,
        f"Could not close the overlay. Something is still covering about "
        f"{top.get('area')}% of the page and starts with: "
        f"{str(top.get('text'))[:90]!r}. Try clicking its own close control "
        f"by name, or work around it.",
    )


# ------------------------------------------------------------- login walls

# How long a run will hold while the founder signs in, and how often it
# looks. Long enough for a real login including a second factor; bounded
# so a founder who walked away does not pin the browser thread forever.
LOGIN_WAIT_SECONDS = int(os.environ.get("BROWSER_LOGIN_WAIT_SECONDS", "180"))
LOGIN_POLL_SECONDS = 2.0


def _on_login_wall(page) -> bool:
    """True when the page is asking for credentials.

    A password field is the signal. It is a heuristic, and the right kind:
    it never asks a model 'is this a login page', it looks for the one
    control that only exists on one kind of page.
    """
    try:
        return page.evaluate(
            "() => !!document.querySelector("
            "'input[type=password]:not([disabled])')"
        )
    except Exception:  # noqa: BLE001
        return False


def _await_login_impl(args: Dict[str, Any]) -> str:
    """Hold while the founder signs in, then carry on by itself.

    WHY IT WATCHES RATHER THAN ASKS. The founder never hands over a
    password and this codebase never types one -- so a login is the one
    step an AI employee genuinely cannot do alone. The temptation is to
    stop and ask for a tap when it is done, and that is the failure mode:
    a run that stalls waiting on the founder has handed the work back.

    So the window is already visible, the founder simply logs in, and
    this notices the password field disappear and resumes. No tap, no
    message, no approval queue. Detect the state; do not ask about it.
    """
    session, err = _session(args)
    if err:
        return err
    try:
        _policy().check_action("observe")
        _policy().check_url(session.page.url)
    except PolicyViolation as exc:
        return str(exc)

    if not _on_login_wall(session.page):
        return ("(no login is being asked for on this page — nothing to wait "
                "for. Carry on with the task.)")

    try:
        session.set_status("needs_login", f"Waiting for sign-in at {session.page.url}")
    except Exception:  # noqa: BLE001
        pass

    deadline = time.monotonic() + LOGIN_WAIT_SECONDS
    logger.info("waiting up to %ds for founder sign-in at %s",
                LOGIN_WAIT_SECONDS, str(session.page.url)[:80])
    while time.monotonic() < deadline:
        session.page.wait_for_timeout(int(LOGIN_POLL_SECONDS * 1000))
        if not _on_login_wall(session.page):
            try:
                session.set_status("navigated", f"Signed in at {session.page.url}")
            except Exception:  # noqa: BLE001
                pass
            logger.info("sign-in detected — resuming")
            return _describe(
                session,
                "Signed in. The password field is gone and the session is "
                "authenticated — it will stay signed in for later steps and "
                "later runs.",
            )

    return (
        f"(still waiting for a sign-in after {LOGIN_WAIT_SECONDS}s. A browser "
        f"window is open at {str(session.page.url)[:100]} and needs the "
        f"founder to log in. Nothing was typed and no credentials were "
        f"handled. Report honestly that this task needs a sign-in before it "
        f"can continue, and say which site.)"
    )


# ------------------------------------------------------------------- find

def _find_impl(args: Dict[str, Any]) -> str:
    """Search the page for a control instead of reading the whole list.

    WHY THIS EXISTS. browser_observe caps at MAX_ELEMENTS so its output
    stays readable, and on a dense app the control the model needs is
    below that cap. On TradingView's screener the column headers -- the
    thing you click to sort -- never appeared in a single observation
    across four runs. The model was not choosing badly between options;
    it was never shown the option.

    So this scans FIND_SCAN_LIMIT deep, stamps every element it saw, and
    prints only the matches. Everything scanned stays addressable, so an
    element that ranked 400th in document order is now usable by id.
    """
    session, err = _session(args)
    if err:
        return err
    query = str(args.get("query") or "").strip()
    if not query:
        return ("(missing 'query' — say what you are looking for, e.g. "
                "'weekly change column header' or 'sort dropdown')")
    try:
        _policy().check_action("observe")
        _policy().check_url(session.page.url)
    except PolicyViolation as exc:
        return str(exc)

    obs = observe_page(session.page, session.element_map, limit=FIND_SCAN_LIMIT)
    matches = find_elements(obs, query)
    logger.info("browser_find %r — %d/%d element(s) matched",
                query[:60], len(matches), len(obs.elements))
    return wrap_untrusted(render_matches(obs, query, matches), obs.url)


# Controls that only exist once you are pointing at them.
#
# A column header's sort arrow is the canonical case: the <th> is
# visible and labelled, and the button inside it is visibility:hidden
# until hovered. Playwright's click() runs actionability checks BEFORE
# it hovers, so a hidden-until-hover control fails them and the click
# never happens -- correct for a genuinely invisible element, wrong for
# one the page reveals under the pointer.
#
# So: try the ordinary click first and change nothing about the happy
# path. Only when it fails do we hover the container and click the
# control inside it, bypassing the visibility check that the hover has
# just made obsolete.
#
# Verified against TradingView's screener, where this is the difference
# between the default market-cap list and the actual movers: the rows
# went from NVDA/AAPL/GOOG to TREVQ/ETBI/IOBTQ.
_HOVER_REVEALED = "button, [role='button'], [aria-label*='ort'], svg"


def _click_locator(session, locator, role: str = "") -> None:
    """Click, falling back to hover-then-force for revealed controls.

    `role` matters for one case, and it is not a special case so much as
    the general shape of a container control: a COLUMN HEADER holds its
    sort handler on the button INSIDE the cell, not on the cell. Clicking
    the cell SUCCEEDS and does nothing — so the fallback below never
    fired, because it only triggers on failure.

    That bug survived being written, reviewed and tested, and was caught
    by the outcome check measuring a live page: rows identical, sort
    identical, click reported successful. It is precisely the failure this
    layer exists to make impossible, reproduced inside the fix for it.
    """
    if role == "columnheader":
        try:
            inner = locator.locator(_HOVER_REVEALED).first
            if inner.count():
                locator.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
                locator.hover(timeout=ACTION_TIMEOUT_MS, force=True)
                session.page.wait_for_timeout(150)
                inner.click(timeout=ACTION_TIMEOUT_MS, force=True)
                return
        except Exception as exc:  # noqa: BLE001
            logger.info("column-header inner click failed, falling back: %s",
                        str(exc)[:120])

    try:
        locator.click(timeout=ACTION_TIMEOUT_MS)
        return
    except Exception as first:  # noqa: BLE001
        logger.info("click needed the hover fallback: %s", str(first)[:120])

    locator.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
    locator.hover(timeout=ACTION_TIMEOUT_MS, force=True)
    session.page.wait_for_timeout(150)

    inner = locator.locator(_HOVER_REVEALED).first
    try:
        if inner.count():
            inner.click(timeout=ACTION_TIMEOUT_MS, force=True)
            return
    except Exception:  # noqa: BLE001
        pass
    # No inner control, or it refused — click the container itself.
    locator.click(timeout=ACTION_TIMEOUT_MS, force=True)


# ------------------------------------------------------------------ click

def _click_impl(args: Dict[str, Any]) -> str:
    session, err = _session(args)
    if err:
        return err
    element_id = str(args.get("element_id") or "").strip()
    try:
        _policy().check_action("click")
        _policy().check_url(session.page.url)
    except PolicyViolation as exc:
        return str(exc)

    locator, err = _resolve(session, element_id)
    if err:
        return err
    el = session.element_map.last.find(element_id) or {}
    if el.get("enabled") is False:
        return (f"({element_id} \"{el.get('name', '')}\" is disabled — something else on "
                "the page probably has to be filled in first.)")

    # An irreversible click goes to the founder, not through.
    name = str(el.get("name") or "")
    if _looks_irreversible(name) and not _approved(args):
        queued = _queue_for_approval(session, element_id, name)
        if queued:
            return queued

    url_before = session.page.url
    try:
        _click_locator(session, locator, role=str(el.get("role") or ""))
        session.page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        return f"(click on {element_id} failed: {exc})"

    # A click can open a TAB rather than change this one.
    if _follow_new_tab(session):
        return _describe(
            session,
            f"Clicked {element_id} \"{el.get('name', '')}\" — it opened a new "
            f"tab and you are now on it ({str(session.page.url)[:100]}). The "
            f"element ids below are for this tab; the previous page's ids are "
            f"no longer valid.",
        )

    url_after = session.page.url
    moved = f" — navigated to {url_after}" if url_after != url_before else ""
    return _describe(session, f"Clicked {element_id} \"{el.get('name', '')}\"{moved}.")


# ------------------------------------------------------------------- type

def _type_impl(args: Dict[str, Any]) -> str:
    session, err = _session(args)
    if err:
        return err
    element_id = str(args.get("element_id") or "").strip()
    text = str(args.get("text") or "")
    try:
        _policy().check_action("type")
        _policy().check_url(session.page.url)
    except PolicyViolation as exc:
        return str(exc)

    locator, err = _resolve(session, element_id)
    if err:
        return err
    el = session.element_map.last.find(element_id) or {}
    if el.get("role") == "password":
        # Nothing in this codebase should route a password through the
        # model. The founder types those in the visible window
        # themselves (browser_login_wait) and the persistent profile
        # keeps the session afterwards.
        return ("(refused: this is a password field. Vision AI never types "
                "passwords. Use action.browser_task_async so the founder can "
                "log in themselves in the visible window.)")
    try:
        locator.fill(text, timeout=ACTION_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        return f"(typing into {element_id} failed: {exc})"
    return _describe(session, f"Typed into {element_id} \"{el.get('name', '')}\".")


# ----------------------------------------------------------------- select

def _select_impl(args: Dict[str, Any]) -> str:
    session, err = _session(args)
    if err:
        return err
    element_id = str(args.get("element_id") or "").strip()
    option = str(args.get("option") or "").strip()
    try:
        _policy().check_action("select")
        _policy().check_url(session.page.url)
    except PolicyViolation as exc:
        return str(exc)

    locator, err = _resolve(session, element_id)
    if err:
        return err
    try:
        try:
            locator.select_option(label=option, timeout=ACTION_TIMEOUT_MS)
        except Exception:
            # Labels are what the model saw; values are what some pages
            # actually key on. Try both before reporting a failure.
            locator.select_option(value=option, timeout=ACTION_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        el = session.element_map.last.find(element_id) or {}
        opts = ", ".join(el.get("options") or [])[:200]
        return (f"(could not select {option!r} in {element_id}: {exc}. "
                f"Available: {opts or 'unknown'})")
    return _describe(session, f"Selected {option!r} in {element_id}.")


# ----------------------------------------------------------------- scroll

def _scroll_impl(args: Dict[str, Any]) -> str:
    session, err = _session(args)
    if err:
        return err
    direction = str(args.get("direction") or "down").strip().lower()
    if direction not in ("up", "down", "top", "bottom"):
        return "(direction must be one of: up, down, top, bottom)"
    try:
        _policy().check_action("scroll")
    except PolicyViolation as exc:
        return str(exc)
    js = {
        "down": "window.scrollBy(0, window.innerHeight * 0.9)",
        "up": "window.scrollBy(0, -window.innerHeight * 0.9)",
        "top": "window.scrollTo(0, 0)",
        "bottom": "window.scrollTo(0, document.body.scrollHeight)",
    }[direction]
    try:
        session.page.evaluate(js)
        session.page.wait_for_timeout(400)   # let lazy content load
    except Exception as exc:  # noqa: BLE001
        return f"(scroll failed: {exc})"
    return _describe(session, f"Scrolled {direction}.")


# ------------------------------------------------------------------ press

def _press_impl(args: Dict[str, Any]) -> str:
    session, err = _session(args)
    if err:
        return err
    key = str(args.get("key") or "").strip()
    if not key:
        return "(missing 'key' — e.g. 'Enter', 'Escape', 'Tab')"
    try:
        _policy().check_action("press")
        _policy().check_url(session.page.url)
    except PolicyViolation as exc:
        return str(exc)
    element_id = str(args.get("element_id") or "").strip()
    try:
        if element_id:
            locator, err = _resolve(session, element_id)
            if err:
                return err
            locator.press(key, timeout=ACTION_TIMEOUT_MS)
        else:
            session.page.keyboard.press(key)
        session.page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        return f"(pressing {key!r} failed: {exc})"
    return _describe(session, f"Pressed {key}.")


# ------------------------------------------------------------------- wait

def _wait_impl(args: Dict[str, Any]) -> str:
    session, err = _session(args)
    if err:
        return err
    text = str(args.get("text") or "").strip()
    seconds = args.get("seconds")
    try:
        _policy().check_action("wait")
    except PolicyViolation as exc:
        return str(exc)
    try:
        if text:
            session.page.get_by_text(text, exact=False).first.wait_for(
                timeout=min(30000, ACTION_TIMEOUT_MS * 3))
            note = f"{text!r} appeared on the page."
        else:
            secs = min(float(seconds or 3), 30.0)
            session.page.wait_for_timeout(secs * 1000)
            note = f"Waited {secs:.0f}s."
    except Exception as exc:  # noqa: BLE001
        return (f"(waited but {text!r} did not appear: {exc}. The page may have "
                "changed differently than expected — observe to see what is there.)")
    return _describe(session, note)


# -------------------------------------------------------------- upload

def _upload_impl(args: Dict[str, Any]) -> str:
    session, err = _session(args)
    if err:
        return err
    element_id = str(args.get("element_id") or "").strip()
    rel = str(args.get("path") or "").strip()
    try:
        _policy().check_action("upload")
        _policy().check_url(session.page.url)
    except PolicyViolation as exc:
        return str(exc)

    # The file must come from the workspace, resolved the same way every
    # other file action resolves it. An agent that could attach an
    # arbitrary absolute path to a web form is an exfiltration tool.
    from backend.app.actions.builtin._workspace import resolve_within, workspace_root
    try:
        target = resolve_within(workspace_root(), rel)
    except ValueError as exc:
        return f"(refused: {exc})"
    if not target.is_file():
        return (f"(workspace/{rel} does not exist — write or download it first. "
                "Check the artifact list in your brief for what is actually there.)")

    locator, err = _resolve(session, element_id)
    if err:
        return err
    try:
        locator.set_input_files(str(target), timeout=ACTION_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        return f"(upload to {element_id} failed: {exc})"
    return _describe(session, f"Attached workspace/{rel} to {element_id}.")


# ------------------------------------------------------------ download

def _download_impl(args: Dict[str, Any]) -> str:
    session, err = _session(args)
    if err:
        return err
    element_id = str(args.get("element_id") or "").strip()
    rel = str(args.get("path") or "").strip()
    try:
        _policy().check_action("download")
        _policy().check_url(session.page.url)
    except PolicyViolation as exc:
        return str(exc)

    from backend.app.actions.builtin._workspace import resolve_within, workspace_root
    root = workspace_root()
    locator, err = _resolve(session, element_id)
    if err:
        return err
    try:
        with session.page.expect_download(timeout=30000) as info:
            locator.click(timeout=ACTION_TIMEOUT_MS)
        download = info.value
        suggested = download.suggested_filename or "download.bin"
        try:
            target = resolve_within(root, rel or f"downloads/{suggested}")
        except ValueError as exc:
            return f"(refused: {exc})"
        target.parent.mkdir(parents=True, exist_ok=True)
        download.save_as(str(target))
    except Exception as exc:  # noqa: BLE001
        return (f"(download from {element_id} failed: {exc}. If clicking that "
                "element opens a page rather than a file, use "
                "action.browser_click_element instead.)")

    size = target.stat().st_size if target.exists() else 0
    from backend.app.state import run_artifacts
    run_artifacts.register_file(str(target), producer="browser_download",
                                summary=f"downloaded from {session.page.url[:100]}")
    return _describe(
        session,
        f"Downloaded {size:,} bytes to workspace/{target.relative_to(root).as_posix()}.",
    )


# ---------------------------------------------------------------- back

def _back_impl(args: Dict[str, Any]) -> str:
    session, err = _session(args)
    if err:
        return err
    try:
        _policy().check_action("navigate")
    except PolicyViolation as exc:
        return str(exc)
    try:
        session.page.go_back(timeout=ACTION_TIMEOUT_MS)
        session.page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        return f"(going back failed: {exc} — there may be no previous page)"
    # Policy is checked AFTER the move too: history can carry the browser
    # somewhere the current scope does not allow.
    try:
        _policy().check_url(session.page.url)
    except PolicyViolation as exc:
        return f"(went back to a page outside this task's scope) {exc}"
    return _describe(session, f"Went back to {session.page.url}.")


def _h(impl):
    return lambda args: _on_thread(impl, args)


_TOKEN = {"name": "session_token", "type": "string",
          "description": "Token from action.browser_navigate.", "required": True}
_ELEMENT = {"name": "element_id", "type": "string",
            "description": "Element id from the last browser_observe, e.g. 'e17'.",
            "required": True}


OBSERVE_SPEC = ActionSpec(
    name="browser_observe",
    description=(
        "See the current page as a structured list of interactive elements, each "
        "with a short id (e1, e2, …), a role and a name. Call this FIRST and again "
        "after anything changes the page — ids are only valid for the most recent "
        "observation. Every other browser primitive takes an id from here."
    ),
    parameters=[_TOKEN],
    handler=_h(_observe_impl),
    preview=lambda a: "Observe the current page",
    mutating=False,
    capability="web.page.observe",
)

DISMISS_SPEC = ActionSpec(
    name="browser_dismiss_overlay",
    description=(
        "Close a cookie banner, consent wall, modal or popup that is covering "
        "the page. Call this when clicks keep landing on nothing, or when the "
        "page text mentions cookies or consent — an overlay swallows every "
        "click while the content sits visible underneath, which looks exactly "
        "like picking the wrong element. Declines non-essential cookies where "
        "it can. Says so plainly if nothing was covering the page."
    ),
    parameters=[_TOKEN],
    handler=_h(_dismiss_impl),
    preview=lambda a: "Dismiss the overlay",
    mutating=False,
    capability="web.page.click",
)

AWAIT_LOGIN_SPEC = ActionSpec(
    name="browser_await_login",
    description=(
        "Wait for the founder to sign in on a page that is asking for "
        "credentials, then carry on. Call this the moment a page shows a "
        "password field. A real browser window is already open on the "
        "founder's screen; they log in there, and this returns by itself as "
        "soon as the sign-in completes — there is no button for them to press "
        "and nothing for you to ask. The AI never types a password. The "
        "session stays signed in for the rest of this task and for later runs."
    ),
    parameters=[_TOKEN],
    handler=_h(_await_login_impl),
    preview=lambda a: "Wait for the founder to sign in",
    mutating=False,
    capability="web.form.login_continue",
)

FIND_SPEC = ActionSpec(
    name="browser_find",
    description=(
        "Search the page for a control by describing it — 'weekly change column "
        "header', 'sort dropdown', 'next page button' — and get back only the few "
        "elements that match, with live ids you can click or select. Use this "
        "INSTEAD OF browser_observe on a busy page: observe shows the first "
        "elements it finds and stops, so a control further down the page never "
        "appears in it at all, while this searches the whole page. If it reports "
        "no match, the control is genuinely not on this page — scroll, open the "
        "menu that holds it, or set the option in the URL instead."
    ),
    parameters=[_TOKEN,
                {"name": "query", "type": "string",
                 "description": "What you are looking for, in plain words.",
                 "required": True}],
    handler=_h(_find_impl),
    preview=lambda a: f"Find {a.get('query')!r} on the page",
    mutating=False,
    capability="web.page.observe",
)

CLICK_SPEC = ActionSpec(
    name="browser_click_element",
    description=(
        "Click the element with this id from the last browser_observe. Prefer this "
        "over browser_click (which matches visible text and cannot tell two "
        "identically-labelled buttons apart). Reports the page after the click."
    ),
    parameters=[_TOKEN, _ELEMENT],
    handler=_h(_click_impl),
    preview=lambda a: f"Click {a.get('element_id')}",
    mutating=False,
    capability="web.page.click",
)

TYPE_SPEC = ActionSpec(
    name="browser_type",
    description=(
        "Type text into a textbox by its element id from the last browser_observe. "
        "Replaces whatever is already in the field. Refuses password fields — the "
        "founder logs in themselves."
    ),
    parameters=[_TOKEN, _ELEMENT,
                {"name": "text", "type": "string",
                 "description": "Text to put in the field.", "required": True}],
    handler=_h(_type_impl),
    preview=lambda a: f"Type into {a.get('element_id')}",
    mutating=False,
    capability="web.form.fill",
)

SELECT_SPEC = ActionSpec(
    name="browser_select",
    description=(
        "Choose an option in a dropdown by element id. Use the option text exactly "
        "as browser_observe listed it."
    ),
    parameters=[_TOKEN, _ELEMENT,
                {"name": "option", "type": "string",
                 "description": "Option label to choose.", "required": True}],
    handler=_h(_select_impl),
    preview=lambda a: f"Select {a.get('option')!r} in {a.get('element_id')}",
    mutating=False,
    capability="web.form.fill",
)

SCROLL_SPEC = ActionSpec(
    name="browser_scroll",
    description=(
        "Scroll the page: 'down', 'up', 'top' or 'bottom'. Use when browser_observe "
        "says more elements exist than it showed, or to trigger lazy-loaded content."
    ),
    parameters=[_TOKEN,
                {"name": "direction", "type": "string",
                 "description": "down | up | top | bottom", "required": False}],
    handler=_h(_scroll_impl),
    preview=lambda a: f"Scroll {a.get('direction', 'down')}",
    mutating=False,
    capability="web.page.scroll",
)

PRESS_SPEC = ActionSpec(
    name="browser_press",
    description=(
        "Press a key such as 'Enter', 'Escape' or 'Tab'. Give an element_id to press "
        "it while that element is focused, or omit it for the page."
    ),
    parameters=[_TOKEN,
                {"name": "key", "type": "string",
                 "description": "Key name, e.g. 'Enter'.", "required": True},
                {"name": "element_id", "type": "string",
                 "description": "Optional element to focus first.", "required": False}],
    handler=_h(_press_impl),
    preview=lambda a: f"Press {a.get('key')}",
    mutating=False,
    capability="web.page.press",
)

WAIT_SPEC = ActionSpec(
    name="browser_wait",
    description=(
        "Wait for the page to catch up — either until some text appears, or for a "
        "number of seconds. Use after an action that starts something slow, like a "
        "build or a deployment."
    ),
    parameters=[_TOKEN,
                {"name": "text", "type": "string",
                 "description": "Text to wait for on the page.", "required": False},
                {"name": "seconds", "type": "string",
                 "description": "Seconds to wait if no text given (max 30).", "required": False}],
    handler=_h(_wait_impl),
    preview=lambda a: f"Wait for {a.get('text') or (str(a.get('seconds') or 3) + 's')}",
    mutating=False,
    capability="web.page.wait",
)

UPLOAD_SPEC = ActionSpec(
    name="browser_upload",
    description=(
        "Attach a file from the Vision AI workspace to a file input on the page. "
        "The file must already exist in the workspace — write or download it first. "
        "Absolute paths outside the workspace are refused."
    ),
    parameters=[_TOKEN, _ELEMENT,
                {"name": "path", "type": "string",
                 "description": "Workspace-relative path, e.g. 'site/logo.png'.",
                 "required": True}],
    handler=_h(_upload_impl),
    preview=lambda a: f"Attach workspace/{a.get('path')} to {a.get('element_id')}",
    mutating=False,
    capability="web.form.upload",
)

DOWNLOAD_SPEC = ActionSpec(
    name="browser_download",
    description=(
        "Click an element that downloads a file and save it into the workspace. "
        "The saved file is registered so later specialists get its real path."
    ),
    parameters=[_TOKEN, _ELEMENT,
                {"name": "path", "type": "string",
                 "description": "Where to save it, workspace-relative. Defaults to downloads/<name>.",
                 "required": False}],
    handler=_h(_download_impl),
    preview=lambda a: f"Download via {a.get('element_id')}",
    mutating=False,
    capability="web.page.download",
)

BACK_SPEC = ActionSpec(
    name="browser_back",
    description="Go back to the previous page in this browser session.",
    parameters=[_TOKEN],
    handler=_h(_back_impl),
    preview=lambda a: "Go back",
    mutating=False,
    capability="web.page.navigate",
)

ALL_SPECS = [OBSERVE_SPEC, FIND_SPEC, CLICK_SPEC, TYPE_SPEC, SELECT_SPEC,
             SCROLL_SPEC, PRESS_SPEC, WAIT_SPEC, AWAIT_LOGIN_SPEC,
             UPLOAD_SPEC, DOWNLOAD_SPEC, BACK_SPEC, DISMISS_SPEC]
