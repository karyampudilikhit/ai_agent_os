"""A session that has died must not be handed out again.

MEASURED ON A LIVE RUN, 2026-08-21. Asked for twenty PM internships, a
run opened a browser session on internshala.com, used it successfully,
and then its page died. Nothing noticed. `current()` returned the same
corpse to every later call, and five of the run's nineteen calls came
back with:

    Page.goto: Target page, context or browser has been closed

...against THREE DIFFERENT URLS on two different hosts. A quarter of the
budget spent on a session that had been dead since call six, and the run
concluded those sites were unreachable. Nothing was wrong with the
sites; the manager simply had no notion that a session it was holding
could stop working.

Two layers, because there are two ways to meet a dead session: find one
already dead (the manager reaps it on read) and have one die under you
mid-navigation (browser_navigate opens a fresh window, once).

The distinction that keeps this honest is between a session that is GONE
and a site that REFUSED us. The first is our problem and worth a silent
retry. The second is a fact about the page and must be reported.
"""
from __future__ import annotations

import pytest

from backend.app.browser.task_flow import _session_is_gone


# ------------------------------------------- ours to retry, or theirs

@pytest.mark.parametrize("message", [
    "Page.goto: Target page, context or browser has been closed",
    "Target closed",
    "Browser has been closed",
    "Playwright: Page has been closed",
    "Connection closed while reading from the driver",
])
def test_a_gone_session_is_recognised(message):
    assert _session_is_gone(Exception(message))


@pytest.mark.parametrize("message", [
    "Timeout 30000ms exceeded",
    "net::ERR_NAME_NOT_RESOLVED",
    "net::ERR_CONNECTION_REFUSED",
    "Navigation failed because page crashed",   # a crash is not a closure
    "403 Forbidden",
])
def test_a_site_refusing_us_is_not_a_gone_session(message):
    """The expensive direction of this mistake. Silently reopening a
    window on a bot wall would hide the wall from the run and from the
    founder, and the run would keep paying for it."""
    assert not _session_is_gone(Exception(message))


# ------------------------------------- the manager reaps what is dead

class FakePage:
    def __init__(self, closed=False, raises=False):
        self._closed = closed
        self._raises = raises

    def is_closed(self):
        if self._raises:
            raise RuntimeError("driver gone")
        return self._closed


def _session(page):
    from backend.app.browser.session_manager import BrowserSession
    return BrowserSession(token="bsess_test", playwright=None, browser=None,
                          context=None, page=page)


def test_a_live_session_is_alive():
    assert _session(FakePage()).is_alive()


def test_a_closed_page_is_not():
    assert not _session(FakePage(closed=True)).is_alive()


def test_a_page_whose_own_check_raises_is_not_handed_out():
    assert not _session(FakePage(raises=True)).is_alive()


def test_a_session_with_no_page_at_all_is_not():
    assert not _session(None).is_alive()


class FakeManager:
    """The real _alive_or_reap against a stand-in registry."""

    def __init__(self, session):
        self.session = session
        self.closed = []

        class _Live:
            def __init__(self, outer):
                self.outer = outer

            def get(self, token):
                return self.outer.session

            def newest(self):
                return self.outer.session

            def close(self, token):
                self.outer.closed.append(token)
                self.outer.session = None
                return True

        self._live = _Live(self)


def _reap(session):
    from backend.app.browser.session_manager import BrowserSessionManager
    mgr = FakeManager(session)
    return (BrowserSessionManager._alive_or_reap(mgr, session), mgr)


def test_a_live_session_is_returned_unchanged():
    s = _session(FakePage())
    got, mgr = _reap(s)
    assert got is s and mgr.closed == []


def test_a_dead_session_is_dropped_not_returned():
    """The exact bug: current() kept handing the same corpse back."""
    s = _session(FakePage(closed=True))
    got, mgr = _reap(s)
    assert got is None, "a dead session must never be handed out"
    assert mgr.closed == ["bsess_test"], "and it must leave the registry"


def test_reaping_nothing_is_safe():
    assert _reap(None)[0] is None


def test_current_and_get_both_reap():
    """Either entry point can hand back the corpse, so both must check."""
    import inspect

    from backend.app.browser.session_manager import BrowserSessionManager
    for name in ("get", "current"):
        src = inspect.getsource(getattr(BrowserSessionManager, name))
        assert "_alive_or_reap" in src, f"{name}() does not check liveness"


# --------------------------------- and one fresh window if it dies mid-flight

def test_navigate_reopens_once_when_the_session_dies_under_it():
    import inspect

    from backend.app.browser import task_flow
    src = inspect.getsource(task_flow._browser_navigate_impl)
    _, _, tail = src.partition("mgr.goto(session, url)")
    assert "_session_is_gone(exc)" in tail[:1400], (
        "a goto that fails on a dead session must reopen, not report")
    assert "session = None" in tail[:1400]


def test_a_refusal_is_still_reported_rather_than_retried():
    import inspect

    from backend.app.browser import task_flow
    src = inspect.getsource(task_flow._browser_navigate_impl)
    _, _, tail = src.partition("mgr.goto(session, url)")
    assert "browser_navigate failed: could not open" in tail[:2000], (
        "a site that refused us must still surface as a failure")
