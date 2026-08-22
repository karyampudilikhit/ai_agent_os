"""Asking for a real window has to get one.

Session reuse is right: a fresh session throws away every click a run has
made, and a live TradingView run lost its whole built-up state that way.
But reuse read `interactive` only on the create() path, so on every call
after the first the flag did nothing at all.

Measured on a live Reddit run. Every route returned "You've been blocked
by network security" to headless Chrome. The agent drew the correct
conclusion -- retry with interactive=true, get a real window, look less
like a robot -- called it at step 7, and was silently handed back the
same invisible window it had just been blocked in. It then reported the
site unreachable by any means, which was a statement about plumbing
rather than about Reddit.

The asymmetry matters: an UPGRADE is worth the lost state, a DOWNGRADE
never is.
"""
from __future__ import annotations

import pytest


class FakeSession:
    def __init__(self, token="bsess_1", headless=True):
        self.token = token
        self.headless = headless
        self.page = FakePage()

    def touch(self):
        pass

    def set_status(self, *_a, **_k):
        pass

    def reset_elements(self, *_a, **_k):
        pass


class FakePage:
    url = "https://fixture.test/"

    def title(self):
        return "Fixture"

    def evaluate(self, *_a, **_k):
        # {} reads as an empty list to len() and as an empty mapping to
        # .get(), which covers both shapes the page JS returns.
        return {}

    def content(self):
        return "<html></html>"


class FakeManager:
    """Records what was asked of it, so the DECISION is what gets tested
    rather than Playwright."""

    def __init__(self, existing=None):
        self.existing = existing
        self.closed = []
        self.created = []      # (url, prefer_headless)
        self.went = []         # (token, url)

    def sweep_idle(self):
        pass

    def get(self, token):
        return self.existing if self.existing and token == self.existing.token else None

    def current(self):
        return self.existing

    def close(self, token):
        self.closed.append(token)
        self.existing = None

    def goto(self, session, url):
        self.went.append((session.token, url))

    def create(self, url, prefer_headless=False):
        self.created.append((url, prefer_headless))
        s = FakeSession(token="bsess_new", headless=prefer_headless)
        self.existing = s
        return s


@pytest.fixture()
def mgr(monkeypatch):
    from backend.app.browser import task_flow

    holder = {}

    def install(manager):
        holder["m"] = manager
        monkeypatch.setattr(task_flow, "get_manager", lambda: manager)
        return manager

    return install


def _navigate(**args):
    from backend.app.browser.task_flow import _browser_navigate_impl
    return _browser_navigate_impl({"url": "https://old.reddit.com/r/startups/top/",
                                   **args})


# ------------------------------------------------------- the upgrade

def test_interactive_replaces_a_headless_session(mgr):
    """The exact live sequence: blocked headless, retry interactive."""
    m = mgr(FakeManager(FakeSession(headless=True)))
    _navigate(interactive=True)
    assert m.closed == ["bsess_1"], "the invisible window must be closed first"
    assert m.created and m.created[0][1] is False, "a VISIBLE window was asked for"


def test_a_headed_session_is_reused_not_relaunched(mgr):
    """Once it is already visible there is nothing to upgrade, and
    relaunching would throw away the run's state for nothing."""
    m = mgr(FakeManager(FakeSession(headless=False)))
    _navigate(interactive=True)
    assert m.closed == []
    assert m.created == []
    assert m.went, "it should have navigated the existing session"


def test_a_headless_request_never_downgrades_a_visible_window(mgr):
    """Only ever headless -> headed. A run that has earned a real window
    -- past a login, past a bot check -- must not lose it because a later
    step forgot to pass the flag."""
    m = mgr(FakeManager(FakeSession(headless=False)))
    _navigate(interactive=False)
    assert m.closed == []
    assert m.created == []


def test_headless_stays_headless_without_the_flag(mgr):
    m = mgr(FakeManager(FakeSession(headless=True)))
    _navigate(interactive=False)
    assert m.closed == []
    assert m.went, "plain reuse, as before"


def test_the_first_navigate_still_chooses_by_the_flag(mgr):
    m = mgr(FakeManager(existing=None))
    _navigate(interactive=True)
    assert m.created and m.created[0][1] is False


def test_the_first_navigate_defaults_to_headless(mgr):
    m = mgr(FakeManager(existing=None))
    _navigate()
    assert m.created and m.created[0][1] is True


def test_an_explicit_token_is_upgraded_too(mgr):
    """The loop passes session_token on nearly every call, so an upgrade
    that only worked for the implicit current() session would never fire
    in practice."""
    m = mgr(FakeManager(FakeSession(token="bsess_1", headless=True)))
    _navigate(interactive=True, session_token="bsess_1")
    assert m.closed == ["bsess_1"]
    assert m.created and m.created[0][1] is False


def test_the_session_records_which_kind_it_is():
    """The upgrade cannot be decided without it, and nothing recorded it
    before."""
    import dataclasses
    from backend.app.browser.session_manager import BrowserSession
    names = {f.name for f in dataclasses.fields(BrowserSession)}
    assert "headless" in names
