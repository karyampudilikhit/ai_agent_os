"""Interactive browser automation — Phase 2 of "make the AI do any work."

Phase 1 (browser_automation.py / DeepResearchTool) is read-only: crawl a
site, read pages, never touch anything. This is the opposite half — log
into a real site, fill out a real form, and submit it — for the cases
Phase 1 explicitly couldn't reach (a YC application portal, an investor's
intake form, anything behind a login with no OAuth).

Three ActionSpecs share one flow, split across TWO approval-queue pauses
because the founder decided on two things that shape this design:
  1. Credentials: "you log in once per session, it persists" — no
     password ever touches this codebase. The founder logs into a REAL,
     VISIBLE (headed, not headless) browser window on their own screen.
  2. Approval: "pause right before the irreversible step" — the AI does
     login-wait, navigation, and form-filling on its own, then STOPS
     and shows exactly what it's about to submit before firing.

  action.browser_task(url, goal)   — planner-invokable, runs INLINE
      (not enqueue-then-approve — the safe part needs no approval).
      Opens a session, waits for login if needed, fills the form,
      enqueues browser_submit for approval, returns a receipt.
  browser_login_wait   — INTERNAL, never planner-invokable
      (planner_excluded). Its enqueue happens only from inside
      browser_task; approving it just signals the blocked background
      thread to stop waiting and continue. Reusing Approve/Reject here
      is a deliberate stretch of ApprovalQueue's normal "fire an action"
      semantics — there's nothing to "execute," it's a continue signal —
      but it means the founder never sees a second UI surface: this
      shows up in the exact same Pending Actions panel as everything
      else.
  browser_submit        — INTERNAL, never planner-invokable. Resumes the
      SAME live browser session by token and clicks the identified
      submit control. THIS is the irreversible step.

Two more ActionSpecs (added when the agentic loop in execution_loop.py
needed a way to act on a URL discovered MID-task, without blocking on a
founder login it can't be sure is happening — see Open Decision #4 in
past HANDOFF.md): browser_task and browser_task_async/browser_task_status
share the same underlying flow (factored into _run_browser_flow) but
differ in when they return control to the caller:
  action.browser_task_async(url, goal) — planner-invokable, NOT
      planner_excluded. Opens the session and returns AT ONCE, running
      the actual login-wait/fill/queue-submit flow on a background
      thread instead of the calling thread. This is the one the
      agentic loop calls.
  action.browser_task_status(session_token) — planner-invokable,
      pollable=True (exempt from the loop's identical-repeat-call
      guard). Reads BrowserSession.status/last_message, set by
      _run_browser_flow as it progresses.
browser_task itself (the synchronous, founder-present entry point used
by dynamic_employee's pre-flight dispatch) is UNCHANGED — it still
blocks and still returns the final outcome directly, exactly as before.

Why THIS needs new infrastructure instead of the standard mutating-
action path (see action_registry.py): a standard mutating ActionSpec's
`handler(args)` only ever runs once, at execute_now() time, replaying
the ORIGINAL arguments from scratch — there's no hook for "do real work
first, generate a preview FROM that work, then resume the same live
objects on approval." Login-wait and submit both need to act on a LIVE
Playwright Page that must survive between the founder's approval taps —
so this bypasses ActionRegistry.call()'s normal enqueue path and calls
ApprovalQueue.enqueue() directly, passing a `session_token` that
resolves back into a live BrowserSession via BrowserSessionManager.
execute_now() ends up calling browser_login_wait/browser_submit's
`handler(args)` exactly as it would any other action — those two specs
are still perfectly normal ActionSpecs, just never chosen by the LLM.

Known limitation: rejecting a browser_submit or browser_login_wait item
doesn't run any handler (ApprovalQueue.reject only sets status, by
design — see approval_queue.py), so a rejected session isn't closed
immediately. BrowserSessionManager.sweep_idle() reaps it within 15
minutes. Acceptable for a single-user local MVP; would need a reject
hook if this became multi-tenant.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from backend.app.actions.action_registry import ActionSpec
from backend.app.actions.approval_queue import get_queue
from backend.app.tools.browser_session_manager import (
    LOGIN_POLL_INTERVAL_SECONDS,
    LOGIN_WAIT_TIMEOUT_SECONDS,
    get_manager,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-oss:120b-cloud"
# How long to let the founder actually type credentials before we start
# checking whether the login form is gone. Guards against "detecting"
# completion in the split second before the login page finishes render.
LOGIN_DETECT_GRACE_SECONDS = 6.0
MAX_FIELDS_IN_PROMPT = 40  # sane cap — a form with more than this is unusual
POST_SUBMIT_WAIT_MS = 2500


# ----------------------------------------------------------------
# Lazy module-level adapter. Mirrors the _web_search/_web_fetch
# singleton pattern in dynamic_employee.py — this file needs an LLM to
# map "fill this out with X, Y, Z" onto real form fields, but
# ActionSpec.handler's fixed (args) -> str signature has no channel to
# receive the calling specialist's pipeline/adapter. Building a fresh
# OllamaAdapter here (not via routes._build_adapter, which probes the
# server on every call) is cheap and reads the same env vars every
# other adapter in this app reads.
# ----------------------------------------------------------------
_adapter = None


def _get_adapter():
    global _adapter
    if _adapter is None:
        from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter
        base_url = os.environ.get("OLLAMA_HOST", "").strip() or "http://localhost:11434"
        api_key = os.environ.get("OLLAMA_API_KEY", "").strip() or None
        _adapter = OllamaAdapter(base_url=base_url, model=DEFAULT_MODEL, api_key=api_key)
    return _adapter


# ----------------------------------------------------------------
# DOM inspection — walk the live page for fillable fields + buttons.
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


def _snapshot_page(page) -> Dict[str, Any]:
    return page.evaluate(_SNAPSHOT_JS)


def _looks_like_login_page(snapshot: Dict[str, Any]) -> bool:
    """Heuristic: a password-type field present. Covers the large
    majority of real login pages without needing an LLM call just to
    answer 'is this a login page'."""
    return any(f.get("type") == "password" for f in snapshot.get("fields") or [])


# ----------------------------------------------------------------
# LLM: map a founder's goal + the page's real fields onto fill values.
# ----------------------------------------------------------------
_MAP_PROMPT = """You are filling out a real web form on the founder's behalf.

FOUNDER'S GOAL:
"{goal}"

THE FORM'S ACTUAL FIELDS (from the live page — only use labels/names
that appear here, never invent a field):
{fields_block}

BUTTONS ON THE PAGE:
{buttons_block}

Rules:
- Only map fields that the founder's goal gives you a real value for.
  Leave anything else out — don't invent placeholder data.
- Never fill in a password field. If the goal doesn't give you
  everything needed, map what you can and note what's missing.
- For "submit_label", pick the button text that submits/sends/confirms
  the form (not "cancel", "back", or a nav link disguised as a button).
  If genuinely unclear, pick the most plausible one — the founder
  reviews before it fires.
- For select fields, use the exact option text as it appears in the
  field's "options" list, not something inferred.

Return JSON only:
{{"fills": [{{"label": "<field label or name from the list above>", "value": "<value>"}}], "submit_label": "<button text>", "missing": ["<things the goal didn't specify, if any>"]}}"""


def _fields_block(fields: List[Dict[str, Any]]) -> str:
    lines = []
    for f in fields[:MAX_FIELDS_IN_PROMPT]:
        label = f.get("label") or f.get("name") or f.get("placeholder") or "(unlabeled)"
        opts = f" options={f['options']}" if f.get("options") else ""
        cur = f" current={f['currentValue']!r}" if f.get("currentValue") else ""
        lines.append(f"- [{f.get('kind')}/{f.get('type')}] \"{label}\"{opts}{cur}")
    return "\n".join(lines) if lines else "(no fillable fields found)"


def _buttons_block(buttons: List[Dict[str, Any]]) -> str:
    lines = [f'- "{b.get("text")}"' for b in buttons]
    return "\n".join(lines) if lines else "(no buttons found)"


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    stripped = _JSON_FENCE_RE.sub("", text.strip()).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    s, e = stripped.find("{"), stripped.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(stripped[s : e + 1])
        except json.JSONDecodeError:
            return None
    return None


def _map_goal_to_fills(goal: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    fields = snapshot.get("fields") or []
    buttons = snapshot.get("buttons") or []
    prompt = _MAP_PROMPT.format(
        goal=goal[:1500],
        fields_block=_fields_block(fields),
        buttons_block=_buttons_block(buttons),
    )
    try:
        raw = _get_adapter().chat_completion(prompt, temperature=0.1, max_tokens=1200)
    except Exception as exc:  # noqa: BLE001
        logger.warning("browser_task field-mapping LLM call failed: %s", exc)
        return {"fills": [], "submit_label": "", "missing": []}
    data = _extract_json(raw) or {}
    fills = data.get("fills") or []
    if not isinstance(fills, list):
        fills = []
    return {
        "fills": [f for f in fills if isinstance(f, dict) and f.get("label")],
        "submit_label": str(data.get("submit_label") or "").strip(),
        "missing": data.get("missing") or [],
    }


# ----------------------------------------------------------------
# Locator resolution — multiple strategies since a real-world form's
# actual DOM rarely matches an LLM's notion of a field "label" exactly.
# ----------------------------------------------------------------
def _resolve_field_locator(page, hint: str):
    hint = (hint or "").strip()
    if not hint:
        return None
    strategies = [
        lambda: page.get_by_label(hint, exact=False),
        lambda: page.get_by_placeholder(hint, exact=False),
        lambda: page.locator(f'[name="{hint}"]'),
        lambda: page.locator(f'#{hint}') if hint.replace("-", "").replace("_", "").isalnum() else None,
        lambda: page.get_by_role("textbox", name=hint),
    ]
    for strat in strategies:
        try:
            loc = strat()
            if loc is not None and loc.count() >= 1:
                return loc.first
        except Exception:  # noqa: BLE001
            continue
    return None


def _resolve_button_locator(page, hint: str):
    hint = (hint or "").strip()
    if not hint:
        return None
    strategies = [
        lambda: page.get_by_role("button", name=hint, exact=False),
        lambda: page.get_by_text(hint, exact=False),
        lambda: page.locator(f'button:has-text("{hint}")'),
        lambda: page.locator(f'input[type="submit"][value*="{hint}" i]'),
    ]
    for strat in strategies:
        try:
            loc = strat()
            if loc is not None and loc.count() >= 1:
                return loc.first
        except Exception:  # noqa: BLE001
            continue
    return None


# ----------------------------------------------------------------
# action.browser_task — the ONE tool a specialist can call.
# ----------------------------------------------------------------
# ----------------------------------------------------------------
# Reliability helpers — screenshot-on-failure, cookie/consent banner
# dismissal, friendlier open-failure classification. Added because "it
# failed" with no visual and a raw exception string is not debuggable
# at 2am when a site changed its DOM; see task tracked as "Harden
# browser automation reliability" in [[vision-ai-browser-automation-priority]].
# ----------------------------------------------------------------
def _capture_failure_screenshot(session, tag: str) -> Optional[str]:
    """Best-effort screenshot into the sandboxed workspace so a broken
    run is debuggable instead of just a text error. Never raises — a
    screenshot failing must never mask the REAL failure it documents."""
    try:
        from backend.app.actions.builtin._workspace import workspace_root
        shots_dir = workspace_root() / "browser_failures"
        shots_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{tag}_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        session.page.screenshot(path=str(shots_dir / filename))
        return f"workspace/browser_failures/{filename}"
    except Exception as exc:  # noqa: BLE001
        logger.info("browser_task: could not capture failure screenshot: %s", exc)
        return None


def _with_screenshot(session, tag: str, message: str) -> str:
    """Append a screenshot path to a failure message when one could be
    captured; otherwise return the message unchanged (never blocks on a
    missing/failed screenshot)."""
    path = _capture_failure_screenshot(session, tag)
    return f"{message}\n(screenshot saved: {path})" if path else message


_CONSENT_BUTTON_TEXTS = (
    "accept all", "accept cookies", "i accept", "i agree", "accept",
    "got it", "allow all", "agree",
)


def _try_dismiss_consent_banner(page) -> bool:
    """Best-effort: click the first cookie/consent-banner accept button
    found, if any. A consent overlay sitting on top of the real form is
    a common real-world blocker for field resolution and clicking.
    Never raises — a missed banner just means the normal flow proceeds
    exactly as it did before this existed (a field/button not found is
    still reported, just without this extra recovery attempt)."""
    for text in _CONSENT_BUTTON_TEXTS:
        try:
            btn = page.get_by_role("button", name=text, exact=False)
            if btn.count() >= 1:
                btn.first.click(timeout=1500)
                logger.info("browser_task: dismissed a likely consent/cookie banner (%r)", text)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _classify_open_error(exc: Exception) -> str:
    """Turn a raw Playwright exception into a message that tells the
    founder/specialist something actionable, instead of a bare stack
    string. Text-based classification (not exception-type based) since
    Playwright raises the same generic Error class for most of these —
    the useful signal is in the message."""
    msg = str(exc)
    lowered = msg.lower()
    if "timeout" in lowered:
        return f"the page took too long to load — it may be slow or unreachable ({msg[:150]})"
    if any(s in lowered for s in ("err_name_not_resolved", "net::err", "getaddrinfo", "err_connection")):
        return f"could not reach that URL — double-check it's correct and the site is up ({msg[:150]})"
    return msg[:200]


def _run_browser_flow(mgr, queue, session, url: str, goal: str) -> str:
    """The actual login-wait -> fill -> queue-submit flow, on an already-
    open session. Shared by BOTH entry points:
      - action.browser_task (sync) calls this directly and returns
        whatever it returns — unchanged behavior for the existing
        founder-present pre-flight dispatch (dynamic_employee.py).
      - action.browser_task_async calls this from a background thread
        so the caller (the agentic loop) never blocks on it; progress
        is readable via session.set_status() instead of the return value.
    `session.set_status(...)` calls below are read by
    action.browser_task_status — harmless no-op work for the sync path,
    which ignores them and just uses the return value as always.
    """
    try:
        _try_dismiss_consent_banner(session.page)
        snapshot = _snapshot_page(session.page)
        if _looks_like_login_page(snapshot):
            login_msg = (
                f"A browser window is open at {url} and needs a login. "
                f"Just log in in that window — the AI detects it automatically "
                f"and keeps going on its own. (Approve only if it doesn't "
                f"continue after you're in; Reject to abandon the task.)"
            )
            session.set_status("awaiting_login", login_msg)
            record = queue.enqueue(
                action_name="browser_login_wait",
                arguments={"session_token": session.token},
                preview=login_msg,
            )
            logger.info("[browser_task] waiting for founder login (pending_id=%s)", record["id"])
            outcome = _wait_for_login(queue, record["id"], session)
            if outcome == "timed_out":
                msg = (
                    "BROWSER TASK STOPPED — NOT A CREDENTIALS PROBLEM. Do not ask the "
                    "founder for a password or API token; this system never uses or "
                    "stores either. What actually happened: a real, visible browser "
                    f"window opened at {url} and correctly detected that a login was "
                    "needed. It waited 10 minutes for the founder to log in themselves "
                    "in that window and approve the 'browser_login_wait' item in the "
                    "Pending Actions panel — nobody did within that time, so it gave "
                    "up and closed the window. Tell the founder plainly: re-run the "
                    "task, and this time check the Pending Actions panel right after "
                    "sending it — a browser window will open, log into the site "
                    "yourself, then approve there to continue. Nothing was filled or "
                    f"submitted. (pending_id={record['id']})"
                )
                session.set_status("timed_out", msg)
                mgr.close(session.token)
                return msg
            if outcome == "rejected":
                msg = (
                    "BROWSER TASK ABANDONED — the founder rejected the login-wait "
                    "prompt, so this browser task was intentionally stopped. Nothing "
                    f"was filled or submitted. (pending_id={record['id']})"
                )
                session.set_status("rejected", msg)
                mgr.close(session.token)
                return msg
            # Re-snapshot — the page after login is a different DOM. Some
            # sites only show their cookie/consent banner post-login.
            _try_dismiss_consent_banner(session.page)
            snapshot = _snapshot_page(session.page)

        session.set_status("filling", f"Logged in / no login needed — filling out the form at {url}.")
        mapped = _map_goal_to_fills(goal, snapshot)
        fills = mapped["fills"]
        if not fills:
            msg = _with_screenshot(session, "no_fields", (
                "(browser_task: could not find any fields on this page matching "
                "the goal — nothing was filled. Double-check the URL and goal wording.)"
            ))
            session.set_status("failed", msg)
            mgr.close(session.token)
            return msg

        filled_summary: List[str] = []
        skipped_summary: List[str] = []
        for f in fills:
            label = str(f.get("label") or "")
            value = str(f.get("value") or "")
            loc = _resolve_field_locator(session.page, label)
            if loc is None:
                skipped_summary.append(f'"{label}" (field not found on page)')
                continue
            try:
                tag = loc.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    loc.select_option(label=value)
                else:
                    loc.fill(value)
                filled_summary.append(f'"{label}" = "{value}"')
            except Exception as exc:  # noqa: BLE001
                logger.info("browser_task: could not fill %r: %s", label, exc)
                skipped_summary.append(f'"{label}" (fill failed: {exc})')

        submit_loc = _resolve_button_locator(session.page, mapped.get("submit_label") or "")
        if submit_loc is None:
            msg = _with_screenshot(session, "no_submit_button", (
                f"(browser_task: filled {len(filled_summary)} field(s) but could not "
                f"identify the submit button — nothing will be submitted. Filled: "
                f"{'; '.join(filled_summary) or '(none)'})"
            ))
            session.set_status("failed", msg)
            mgr.close(session.token)
            return msg

        preview_lines = [
            f"About to submit the form at {snapshot.get('url', url)}:",
            *[f"  - {s}" for s in filled_summary],
        ]
        if skipped_summary:
            preview_lines.append(f"  (could not fill: {'; '.join(skipped_summary)})")
        if mapped.get("missing"):
            preview_lines.append(f"  (goal didn't specify: {', '.join(mapped['missing'])})")
        preview_lines.append(
            f"Will click: \"{mapped.get('submit_label')}\". "
            f"The browser window is still open — you can inspect it directly before approving."
        )
        preview = "\n".join(preview_lines)

        submit_record = queue.enqueue(
            action_name="browser_submit",
            arguments={"session_token": session.token, "submit_label": mapped.get("submit_label") or ""},
            preview=preview,
        )
        logger.info("[browser_task] form filled, awaiting submit approval (pending_id=%s)", submit_record["id"])
        result = (
            f"[queued for founder approval — pending_id={submit_record['id']}]\n"
            f"{preview}\n"
            f"This will NOT submit until the founder approves it in the Pending Actions panel."
        )
        session.set_status("awaiting_submit", result)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("browser_task failed: %s", exc)
        msg = _with_screenshot(session, "unhandled_error", f"(browser_task failed: {exc})")
        try:
            session.set_status("failed", msg)
        except Exception:  # noqa: BLE001
            pass
        try:
            mgr.close(session.token)
        except Exception:  # noqa: BLE001
            pass
        return msg


def _validate_and_open(args: Dict[str, Any]):
    """Shared arg-parsing + session-open step for both entry points.
    Returns (url, goal, mgr, queue, session, error). `error` is a ready-
    to-return string when something failed before a session even opened
    (in which case session is None); otherwise error is None."""
    url = str(args.get("url") or "").strip()
    goal = str(args.get("goal") or "").strip()
    if not url:
        return None, None, None, None, None, "(browser_task failed: no url given)"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not goal:
        return None, None, None, None, None, "(browser_task failed: no goal given — what should be filled in?)"

    mgr = get_manager()
    mgr.sweep_idle()
    try:
        session = mgr.create(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("browser_task: failed to open session at %s: %s", url[:80], exc)
        return (
            None, None, None, None, None,
            f"(browser_task failed: could not open a browser session — {_classify_open_error(exc)})",
        )
    return url, goal, mgr, get_queue(), session, None


def _browser_task_handler(args: Dict[str, Any]) -> str:
    """Synchronous entry point — unchanged behavior. Runs the FULL flow
    inline, including any login-wait block. This is what
    dynamic_employee.py's pre-flight dispatch calls, where the founder is
    expected to be present right after sending the task — blocking here
    is deliberate, not a bug (see should_browser_automate's docstring)."""
    url, goal, mgr, queue, session, error = _validate_and_open(args)
    if error:
        return error
    return _run_browser_flow(mgr, queue, session, url, goal)


def _browser_task_async_handler(args: Dict[str, Any]) -> str:
    """Non-blocking entry point for the agentic loop (execution_loop.py).
    Unlike action.browser_task, this returns the instant the browser
    window opens — the potentially-minutes-long login-wait runs on a
    background thread instead of the calling thread, so a mid-task
    discovery ("here's an application URL, go fill it out") never
    stalls the loop's other steps or blows its wall-clock deadline.
    Progress is checked via action.browser_task_status(session_token).
    """
    url, goal, mgr, queue, session, error = _validate_and_open(args)
    if error:
        return error

    thread = threading.Thread(
        target=_run_browser_flow,
        args=(mgr, queue, session, url, goal),
        daemon=True,
        name=f"browser-task-{session.token}",
    )
    thread.start()
    return (
        f"[browser_task_async started — session_token={session.token}]\n"
        f"Opened a real browser window at {url} and started working toward: "
        f"{goal[:150]}\n"
        f"This runs in the background — it does NOT block you from doing "
        f"other steps. Call action.browser_task_status(session_token="
        f"{session.token!r}) whenever you want to check progress (it's fine "
        f"to check more than once — this is a poll, not a repeat mistake). "
        f"If it needs a founder login, that shows up as a 'browser_login_wait' "
        f"item in the founder's Pending Actions panel; nothing else is "
        f"required from you until the status changes."
    )


def _browser_task_status_handler(args: Dict[str, Any]) -> str:
    token = str(args.get("session_token") or "").strip()
    if not token:
        return "(browser_task_status failed: no session_token given)"
    session = get_manager().get(token)
    if not session:
        return (
            "(no such browser session — it has already finished, failed, or "
            "timed out and was closed. If you were waiting on it, that means "
            "it's done: look for the outcome in your own earlier observations, "
            "or start a new action.browser_task_async if the task still needs doing.)"
        )
    status, message = session.get_status()
    return f"[browser session status: {status}]\n{message or '(no detail yet)'}"


def _wait_for_login(queue, pending_id: str, session) -> str:
    """Wait for the founder to finish logging in, then continue.

    THREE ways this ends, and the first one is the important one:

      1. AUTO-DETECTED (the normal path) — we watch the actual page. The
         moment the login form is gone, the founder is in, so we just
         carry on. No tap, no panel, no hunting for a button.
      2. Explicit Approve tap — still honored (sets login_event), for the
         rare page whose shape we can't read confidently.
      3. Reject — the founder abandoned the task.

    Why (1) exists: requiring an approval tap here was a real design
    error. Approval-gating is for IRREVERSIBLE actions (submitting a
    form, sending an email). Logging in is neither irreversible nor
    something the founder needs to authorize — THEY just did it; that
    IS the signal. Demanding a second confirmation in a separate panel
    meant a founder logged in, returned to an empty form, and nothing
    happened until they found and clicked a button they didn't know
    existed. That hands the work back to the human, which is the exact
    opposite of the product's promise.

    Returns 'approved' | 'rejected' | 'timed_out'.
    """
    deadline = time.monotonic() + LOGIN_WAIT_TIMEOUT_SECONDS
    # Small grace period: the founder needs a moment to actually type
    # credentials. Without it we'd "detect" completion during the
    # split-second before the login form finishes rendering.
    grace_until = time.monotonic() + LOGIN_DETECT_GRACE_SECONDS

    while time.monotonic() < deadline:
        # (2) explicit tap — checked first so it always wins immediately
        if session.login_event.wait(timeout=LOGIN_POLL_INTERVAL_SECONDS):
            return "approved"

        # (3) founder gave up
        record = queue.get(pending_id)
        if record and record.get("status") == "rejected":
            return "rejected"

        # (1) auto-detect: is the login form gone from the live page?
        if time.monotonic() >= grace_until and _login_appears_complete(session):
            logger.info(
                "[browser_task] login auto-detected as complete — continuing "
                "without waiting for an approval tap (pending_id=%s)", pending_id,
            )
            # Resolve the now-pointless queue item so it doesn't linger as
            # a stale 'pending' row the founder has to clean up by hand.
            try:
                queue.set_status(
                    pending_id, "executed",
                    result="Login completed — detected automatically, no approval needed.",
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("could not auto-resolve login-wait item: %s", exc)
            return "approved"

    return "timed_out"


def _login_appears_complete(session) -> bool:
    """True when the live page no longer looks like a login screen.

    Deliberately conservative — a false positive here means we try to
    fill a form that isn't there yet, which surfaces as a clean "could
    not find fields" message rather than anything destructive. A false
    negative just means the founder taps Approve like before.
    """
    try:
        snapshot = _snapshot_page(session.page)
    except Exception:  # noqa: BLE001
        return False  # page mid-navigation; try again next poll
    return not _looks_like_login_page(snapshot)


def _browser_task_preview(args: Dict[str, Any]) -> str:
    return f"Open a browser and work toward: {str(args.get('goal') or '')[:150]} at {args.get('url')}"


BROWSER_TASK_SPEC = ActionSpec(
    name="browser_task",
    description=(
        "Open a real, visible browser, log in if needed (the founder logs "
        "in themselves — no password ever touches this app), fill out a "
        "form based on the goal, and pause for approval right before "
        "submitting. Use for sites with no API/OAuth — application "
        "portals, investor intake forms, sign-up flows."
    ),
    parameters=[
        {"name": "url", "type": "string", "description": "The page to open.", "required": True},
        {
            "name": "goal",
            "type": "string",
            "description": (
                "What to fill in and with what values — be concrete "
                "(e.g. 'fill the contact form: name=Jane Doe, "
                "email=jane@acme.com, message=...'). "
            ),
            "required": True,
        },
    ],
    handler=_browser_task_handler,
    preview=_browser_task_preview,
    mutating=False,  # runs inline — the safe part (login-wait, fill) needs no pre-approval
    planner_excluded=True,  # explicit dispatch only, see dynamic_employee.should_browser_automate
    capability="web.form.fill_sync",
)


BROWSER_TASK_ASYNC_SPEC = ActionSpec(
    name="browser_task_async",
    description=(
        "Same as browser_task (open a real browser, log in if needed, fill "
        "a form, pause for approval before submitting) but returns IMMEDIATELY "
        "instead of waiting for the founder to log in — use this one, not "
        "browser_task, when you discover a URL mid-task and want to keep "
        "working on other steps while it runs. Check progress with "
        "browser_task_status."
    ),
    parameters=[
        {"name": "url", "type": "string", "description": "The page to open.", "required": True},
        {
            "name": "goal",
            "type": "string",
            "description": (
                "What to fill in and with what values — be concrete "
                "(e.g. 'fill the contact form: name=Jane Doe, "
                "email=jane@acme.com, message=...'). "
            ),
            "required": True,
        },
    ],
    handler=_browser_task_async_handler,
    preview=_browser_task_preview,
    mutating=False,  # runs inline — kicks off a background thread and returns at once
    planner_excluded=False,  # this IS the loop-safe variant — the whole point is to be callable here
    capability="web.form.fill",
)


BROWSER_TASK_STATUS_SPEC = ActionSpec(
    name="browser_task_status",
    description=(
        "Check progress on a browser_task_async session — is it still "
        "waiting for a founder login, filling the form, queued for submit "
        "approval, or finished/failed. Safe to call more than once while "
        "waiting; it does not repeat any action, only reports current state."
    ),
    parameters=[
        {"name": "session_token", "type": "string", "description": "The token returned by browser_task_async.", "required": True},
    ],
    handler=_browser_task_status_handler,
    preview=lambda args: "Check browser task status",
    mutating=False,
    planner_excluded=False,
    pollable=True,  # exempt from the agentic loop's identical-repeat-call guard
    capability="web.form.status",
)


# ----------------------------------------------------------------
# browser_login_wait — internal signal-only action.
# ----------------------------------------------------------------
def _browser_login_wait_handler(args: Dict[str, Any]) -> str:
    token = str(args.get("session_token") or "")
    session = get_manager().get(token)
    if not session:
        return "(browser session no longer exists — it may have timed out)"
    session.login_event.set()
    return "Founder confirmed login — resuming automation."


BROWSER_LOGIN_WAIT_SPEC = ActionSpec(
    name="browser_login_wait",
    description="[internal] Signals a waiting browser_task that the founder has logged in.",
    parameters=[{"name": "session_token", "type": "string", "description": "", "required": True}],
    handler=_browser_login_wait_handler,
    preview=lambda args: "Continue past login",
    mutating=True,
    planner_excluded=True,
    capability="web.form.login_continue",
)


# ----------------------------------------------------------------
# browser_submit — internal, the actual irreversible click.
# ----------------------------------------------------------------
def _browser_submit_handler(args: Dict[str, Any]) -> str:
    token = str(args.get("session_token") or "")
    submit_label = str(args.get("submit_label") or "")
    session = get_manager().get(token)
    if not session:
        return "(browser session no longer exists — it may have timed out; nothing was submitted)"
    try:
        loc = _resolve_button_locator(session.page, submit_label)
        if loc is None:
            return _with_screenshot(
                session, "submit_button_not_found",
                f'(could not re-locate the submit button "{submit_label}" — nothing was submitted)',
            )
        loc.click()
        session.page.wait_for_timeout(POST_SUBMIT_WAIT_MS)
        result_url = session.page.url
        result_title = session.page.title()
        return f"Submitted. Page after submit: \"{result_title}\" ({result_url})"
    except Exception as exc:  # noqa: BLE001
        logger.warning("browser_submit failed: %s", exc)
        return _with_screenshot(session, "submit_failed", f"(submit failed: {exc})")
    finally:
        get_manager().close(token)


BROWSER_SUBMIT_SPEC = ActionSpec(
    name="browser_submit",
    description="[internal] Clicks the submit control on a filled, founder-approved form.",
    parameters=[
        {"name": "session_token", "type": "string", "description": "", "required": True},
        {"name": "submit_label", "type": "string", "description": "", "required": False},
    ],
    handler=_browser_submit_handler,
    preview=lambda args: "Submit the form",
    mutating=True,
    planner_excluded=True,
    capability="web.form.submit",
)
