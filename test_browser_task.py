"""Smoke test: interactive browser automation (Phase 2 — login/fill/submit).

Covers the full flow: DOM snapshot -> LLM field-mapping -> fill -> pause
for approval (never auto-submits) -> resume-on-approval -> real submit,
plus the login-detection + wait + approve/reject branches.

Needs a local HTTP server serving test_fixtures/*.html and a live
Ollama daemon for the field-mapping LLM call. Skips cleanly if
Playwright isn't installed or no server is reachable.

Run with (two terminals, from the repo root):
    py -3 -m http.server 8899 --directory test_fixtures
    py -3 test_browser_task.py
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

TEST_FORM_URL = "http://127.0.0.1:8899/test_form.html"
TEST_LOGIN_URL = "http://127.0.0.1:8899/test_login_form.html"


def _isolated_queue(name: str):
    """Fresh ApprovalQueue pointed at a scratch file — never touches the
    real .pending_actions.json a live server would be using."""
    import backend.app.actions.approval_queue as aq_mod
    from backend.app.actions.approval_queue import ApprovalQueue
    import tempfile

    path = Path(tempfile.gettempdir()) / f"test_pending_{name}.json"
    # Delete first — ApprovalQueue LOADS an existing file, so without this
    # every run appends to the last one's leftovers and the "exactly one
    # pending item" assertions below start failing on the second run.
    # (Latent until the flow actually got far enough to enqueue anything.)
    path.unlink(missing_ok=True)
    queue = ApprovalQueue(path=path)
    aq_mod._queue = queue
    return queue


def test_dom_snapshot() -> None:
    from playwright.sync_api import sync_playwright
    from backend.app.actions.builtin.browser_task import _snapshot_page, _looks_like_login_page

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(TEST_FORM_URL, timeout=10000)
        snap = _snapshot_page(page)
        browser.close()

    assert not _looks_like_login_page(snap)
    assert len(snap["fields"]) == 5, snap["fields"]
    labels = {f["label"] for f in snap["fields"]}
    assert labels == {"Full Name", "Email Address", "Company", "Inquiry Type", "Message"}, labels
    submit_texts = {b["text"] for b in snap["buttons"]}
    assert "Send Message" in submit_texts and "Cancel" in submit_texts
    print("[ok] DOM snapshot: 5 fields + 2 buttons correctly extracted")


def test_field_mapping_and_locators() -> None:
    from playwright.sync_api import sync_playwright
    from backend.app.actions.builtin.browser_task import (
        _snapshot_page, _map_goal_to_fills, _resolve_field_locator, _resolve_button_locator,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(TEST_FORM_URL, timeout=10000)
        snap = _snapshot_page(page)

        goal = ("fill out the contact form: name=Jane Doe, email=jane@acme.com, "
                "company=Acme Corp, this is a partnership inquiry, message: "
                "We would love to explore a partnership.")
        mapped = _map_goal_to_fills(goal, snap)
        assert len(mapped["fills"]) == 5, mapped
        assert mapped["submit_label"] == "Send Message", mapped["submit_label"]

        for f in mapped["fills"]:
            loc = _resolve_field_locator(page, f["label"])
            assert loc is not None, f"could not resolve {f['label']!r}"
            tag = loc.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                loc.select_option(label=f["value"])
            else:
                loc.fill(f["value"])

        assert page.locator("#full_name").input_value() == "Jane Doe"
        assert page.locator("#inquiry_type").input_value() == "partnership"

        submit_loc = _resolve_button_locator(page, "Send Message")
        assert submit_loc is not None
        # Must resolve to the SUBMIT button, not "Cancel" (both present on page).
        assert submit_loc.evaluate("el => el.innerText").strip() == "Send Message"
        browser.close()
    print("[ok] field mapping + locator resolution: correct fields, correct button (not Cancel)")


def test_full_fill_pause_approve_submit_loop() -> None:
    """The core promise: filled BEFORE approval, NOT submitted until
    approval, resumes the SAME live session on approve."""
    queue = _isolated_queue("submit_loop")
    from backend.app.actions.builtin import browser_task
    from backend.app.tools.browser_session_manager import get_manager

    goal = ("fill out the contact form: name=Test Founder, email=founder@startup.com, "
            "company=Vision AI, this is a partnership inquiry, message: Testing.")
    result = browser_task._browser_task_handler({"url": TEST_FORM_URL, "goal": goal})
    assert "queued for founder approval" in result, result

    pending = queue.list(status="pending")
    assert len(pending) == 1 and pending[0]["action_name"] == "browser_submit"
    record = pending[0]
    token = record["arguments"]["session_token"]

    session = get_manager().get(token)
    assert session is not None, "session should stay alive until approval"
    # Playwright objects belong to the thread that created them, and all
    # browser work now runs on one dedicated thread (see
    # browser_session_manager's module docstring — that pinning is what
    # fixed the "every browser call after the first in a run fails" bug).
    # A test poking session.page straight from the pytest main thread is
    # therefore a cross-thread violation and dies with a greenlet error;
    # marshal it the same way production code now does.
    from backend.app.tools.browser_session_manager import run_on_browser_thread

    assert run_on_browser_thread(
        lambda: session.page.locator("#full_name").input_value()
    ) == "Test Founder"
    assert run_on_browser_thread(
        lambda: session.page.locator("#result").inner_text()
    ) == "", "must NOT be submitted yet"

    queue.set_status(record["id"], "approved")
    submit_result = browser_task._browser_submit_handler(record["arguments"])
    assert "Submitted" in submit_result, submit_result

    assert get_manager().get(token) is None, "session should be released after submit"
    print("[ok] full loop: filled before approval, not submitted until approval, resumes on approve")


def test_login_wait_reject_abandons_cleanly() -> None:
    queue = _isolated_queue("login_reject")
    from backend.app.actions.builtin import browser_task
    from backend.app.tools.browser_session_manager import get_manager

    results = {}

    def run():
        results["outcome"] = browser_task._browser_task_handler(
            {"url": TEST_LOGIN_URL, "goal": "log in"},
        )

    t = threading.Thread(target=run, daemon=True)
    t.start()

    deadline = time.monotonic() + 15
    record = None
    while time.monotonic() < deadline:
        waits = [p for p in queue.list(status="pending") if p["action_name"] == "browser_login_wait"]
        if waits:
            record = waits[0]
            break
        time.sleep(0.2)
    assert record is not None, "browser_login_wait was never queued — login page not detected"
    token = record["arguments"]["session_token"]

    queue.set_status(record["id"], "rejected", error="test")
    t.join(timeout=15)
    assert not t.is_alive(), "background thread did not notice the reject in time"
    assert "rejected" in results["outcome"], results["outcome"]
    assert get_manager().get(token) is None, "session should be closed after reject-driven abandon"
    print("[ok] login-wait reject: thread unblocks promptly, session cleaned up")


def test_login_wait_approve_signals_correctly() -> None:
    queue = _isolated_queue("login_approve")
    from backend.app.actions.builtin import browser_task

    results = {}

    def run():
        results["outcome"] = browser_task._browser_task_handler(
            {"url": TEST_LOGIN_URL, "goal": "log in"},
        )

    t = threading.Thread(target=run, daemon=True)
    t.start()

    deadline = time.monotonic() + 15
    record = None
    while time.monotonic() < deadline:
        waits = [p for p in queue.list(status="pending") if p["action_name"] == "browser_login_wait"]
        if waits:
            record = waits[0]
            break
        time.sleep(0.2)
    assert record is not None

    queue.set_status(record["id"], "approved")
    signal_result = browser_task._browser_login_wait_handler(record["arguments"])
    assert "resuming" in signal_result.lower(), signal_result

    t.join(timeout=15)
    assert not t.is_alive(), "background thread did not unblock after approve"
    # This login-only page has no fields matching "log in" as a goal, so
    # the run correctly fails past that point — what matters here is
    # that the wait unblocked at all, not what happens after.
    print("[ok] login-wait approve: signal correctly unblocks the waiting thread")


def test_timeout_never_suggests_credentials() -> None:
    """Regression test for a real production failure: a specialist,
    given the old terse 'timed_out — no form was filled or submitted'
    message, CONFABULATED a story that GitHub credentials were needed
    and asked the founder to hand over a username/password to be
    'stored/used'. That's both a fabrication (nothing about the actual
    failure involves missing credentials) and dangerous advice (this
    system is built specifically so it never touches a password).

    Fix: the returned text is now explicit enough that even a weak
    model has nothing to confabulate around. This test shrinks the
    timeout to a few seconds (patching the value browser_task.py
    already imported by value) so it runs fast, and asserts on the
    literal returned string — the one thing not subject to LLM
    non-determinism."""
    _isolated_queue("timeout_credentials")
    from backend.app.actions.builtin import browser_task

    original_timeout = browser_task.LOGIN_WAIT_TIMEOUT_SECONDS
    browser_task.LOGIN_WAIT_TIMEOUT_SECONDS = 3
    try:
        result = browser_task._browser_task_handler(
            {"url": TEST_LOGIN_URL, "goal": "log in"},
        )
    finally:
        browser_task.LOGIN_WAIT_TIMEOUT_SECONDS = original_timeout

    lowered = result.lower()
    # Not "does this text mention the word password at all" — the fix
    # legitimately says "never needs a password" to head off exactly
    # this confusion. What must NEVER appear is a REQUEST for one.
    forbidden_asks = [
        "provide your password", "supply your password", "give us your password",
        "share your credentials", "send your password", "we need your github",
        "provide a personal access token", "supply a pat", "need your token",
    ]
    hit = [w for w in forbidden_asks if w in lowered]
    assert not hit, f"timeout text still ASKS for credentials: {hit} in {result!r}"
    assert "NOT A CREDENTIALS PROBLEM" in result, result
    assert "never" in lowered and "password" in lowered, (
        f"expected an explicit 'never needs a password' style disclaimer, got: {result!r}"
    )
    print("[ok] timeout return text never asks for credentials, states the real cause explicitly")


if __name__ == "__main__":
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        print("[skip] Playwright not installed — browser_task tests skipped")
        raise SystemExit(0)

    import httpx
    try:
        httpx.get(TEST_FORM_URL, timeout=3)
    except Exception:
        print(f"[skip] no server at {TEST_FORM_URL} — run: py -3 -m http.server 8899")
        raise SystemExit(0)

    test_dom_snapshot()
    test_field_mapping_and_locators()
    test_full_fill_pause_approve_submit_loop()
    test_login_wait_reject_abandons_cleanly()
    test_login_wait_approve_signals_correctly()
    test_timeout_never_suggests_credentials()
    print("\nAll tests passed.")
