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
from typing import Any, Callable, Dict, Optional

from backend.app.actions.action_registry import ActionSpec
from backend.app.tools.browser_observation import (
    FIND_SCAN_LIMIT,
    find_elements,
    observe_page,
    render_matches,
)
from backend.app.tools.browser_policy import (
    PolicyViolation,
    active_policy,
    wrap_untrusted,
)
from backend.app.tools.browser_session_manager import (
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
    """The locator for `element_id`, or a message explaining why not."""
    problem = session.element_map.check(element_id)
    if problem:
        return None, f"({problem})"
    selector = session.element_map.selector_for(element_id)
    locator = session.page.locator(selector).first
    if locator.count() == 0:
        return None, (f"({element_id} is no longer on the page — it was there when you "
                      "observed, and the page has changed since. Call "
                      "action.browser_observe for fresh ids.)")
    return locator, None


def _describe(session, note: str, include_text: bool = False) -> str:
    """Re-observe and report what the action actually did."""
    obs = observe_page(session.page, session.element_map)
    body = f"{note}\n\n{obs.render(include_text=include_text)}"
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


def _click_locator(session, locator) -> None:
    """Click, falling back to hover-then-force for revealed controls."""
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

    url_before = session.page.url
    try:
        _click_locator(session, locator)
        session.page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        return f"(click on {element_id} failed: {exc})"

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
             SCROLL_SPEC, PRESS_SPEC, WAIT_SPEC,
             UPLOAD_SPEC, DOWNLOAD_SPEC, BACK_SPEC]
