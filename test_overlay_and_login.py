"""Overlays that swallow clicks, and pages that ask for a password.

Both end runs on real sites, and both fail in a way that looks like
something else:

  - a consent wall sits ON TOP of the page. The content is right there in
    the DOM, the observer lists it, and every click lands on the overlay.
    From the agent's side that is indistinguishable from picking the
    wrong element, so it tries again, and again, until the budget is
    gone.
  - a login wall looks like an ordinary form. Without detection the agent
    tries to fill it in, which is useless and is also the one thing this
    system must never do.

Deterministic on purpose. These build the page rather than visiting one:
a live consent wall is answered ONCE and then remembered by the
persistent profile, so the second run of a live test measures nothing.
That is not hypothetical -- it happened while building this, and briefly
looked like the detector had broken.
"""
from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.sync_api")

MODAL_PAGE = """
<html><body style="margin:0">
  <main style="padding:40px"><h1>Real content</h1>
    <p id="target">The thing the agent actually wants.</p>
    <button id="wanted">Sort by date</button>
  </main>
  <div id="wall" style="position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.7);
       display:flex;align-items:center;justify-content:center">
    <div style="background:#fff;padding:40px">
      <p>We value your privacy</p>
      <button onclick="document.getElementById('wall').remove()">No, thank you</button>
      <button>Yes, I accept</button>
    </div>
  </div>
</body></html>
"""

STICKY_HEADER_PAGE = """
<html><body style="margin:0">
  <header style="position:fixed;top:0;left:0;right:0;height:70px;z-index:500;
          background:#111;color:#fff">News Opinion Sport Culture</header>
  <main style="padding:120px 40px"><h1>Real content</h1>
    <p>Nothing is blocking this page.</p></main>
</body></html>
"""

LOGIN_PAGE = """
<html><body><form>
  <input type="email" name="email" placeholder="Email">
  <input type="password" name="password" placeholder="Password">
  <button>Sign in</button>
</form></body></html>
"""


@pytest.fixture()
def page():
    """A page served over a real https URL, not set_content.

    set_content leaves the page on about:blank, and the browser policy
    correctly refuses a URL with no host — so a fixture built that way
    tests the policy rather than the overlay. Routing a fake host keeps
    the page synthetic and the URL real.
    """
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        p = browser.new_page()
        p.route("https://fixture.test/**", lambda route: route.fulfill(
            status=200, content_type="text/html",
            body=getattr(p, "_fixture_html", "<html></html>")))

        def serve(html: str):
            p._fixture_html = html
            p.goto("https://fixture.test/page")

        p.serve = serve
        yield p
        browser.close()


def test_a_modal_that_swallows_clicks_is_detected(page):
    from backend.app.browser.primitives import _OVERLAY_JS
    page.serve(MODAL_PAGE)
    blockers = page.evaluate(_OVERLAY_JS)
    assert blockers, "a full-screen modal must be seen as blocking"
    assert "privacy" in blockers[0]["text"].lower()


def test_a_sticky_site_header_is_not_a_blocker(page):
    """The correction that mattered. The first detector asked "is
    something big and fixed" and counted theguardian.com's own navigation
    header — so a run that had successfully closed the consent wall still
    reported the page as blocked. Site furniture lives at the edges; a
    modal owns the centre."""
    from backend.app.browser.primitives import _OVERLAY_JS
    page.serve(STICKY_HEADER_PAGE)
    assert page.evaluate(_OVERLAY_JS) == []


def test_an_ordinary_page_has_no_blockers(page):
    from backend.app.browser.primitives import _OVERLAY_JS
    page.serve("<html><body><h1>Just a page</h1></body></html>")
    assert page.evaluate(_OVERLAY_JS) == []


def test_the_wall_is_actually_closed(page):
    """End to end: blocked, dismissed, and the content underneath is
    reachable afterwards."""
    from backend.app.browser.primitives import _dismiss_impl, _OVERLAY_JS

    class Sess:
        pass

    page.serve(MODAL_PAGE)
    assert page.evaluate(_OVERLAY_JS), "blocked to begin with"

    sess = Sess()
    sess.page = page
    sess.token = "t"
    sess.element_map = _element_map()
    sess.touch = lambda: None
    sess.set_status = lambda *a: None

    out = _run_dismiss(sess)
    assert "Dismissed an overlay" in out
    assert '"No, thank you"' in out, "declines rather than accepts"
    assert page.evaluate(_OVERLAY_JS) == [], "the wall is gone"
    # And the thing the agent wanted is now clickable.
    page.click("#wanted", timeout=2000)


def test_decline_is_preferred_over_accept():
    """This drives the founder's real browser with their real profile, so
    the privacy-preserving answer is the one to give on their behalf."""
    from backend.app.browser.primitives import _DISMISS_LABELS
    order = list(_DISMISS_LABELS)
    assert order.index("no thank you") < order.index("i accept")
    assert order.index("reject all") < order.index("accept all")
    assert order.index("continue without accepting") < order.index("accept all")


def test_real_world_labels_are_matched():
    """The bug this list had: it said "no thanks" and the most common
    consent wall on the web says "No, thank you". Both sides get
    flattened before they are compared."""
    from backend.app.browser.primitives import _DISMISS_LABELS, _norm_label
    assert _norm_label("No, thank you") == "no thank you"
    assert _norm_label("Yes, I accept") == "yes i accept"
    assert _norm_label("  ACCEPT ALL  ") == "accept all"
    for real in ("No, thank you", "Yes, I accept", "Reject all",
                 "Continue without accepting"):
        assert _norm_label(real) in _DISMISS_LABELS, real


def test_saying_nothing_was_there_when_nothing_was(page):
    """"I dismissed the popup" on a page that had none is the kind of
    confident nonsense the rest of this layer exists to stop."""
    from backend.app.browser.primitives import _dismiss_impl

    class Sess:
        pass

    page.serve(STICKY_HEADER_PAGE)
    sess = Sess()
    sess.page = page
    sess.token = "t"
    sess.element_map = _element_map()
    sess.touch = lambda: None
    sess.set_status = lambda *a: None
    assert "Nothing is covering the page" in _run_dismiss(sess)


def test_a_password_field_is_a_login_wall(page):
    from backend.app.browser.primitives import _on_login_wall
    page.serve(LOGIN_PAGE)
    assert _on_login_wall(page)


def test_an_ordinary_form_is_not_a_login_wall(page):
    from backend.app.browser.primitives import _on_login_wall
    page.serve("<html><body><form><input name=q placeholder=Search>"
               "<button>Go</button></form></body></html>")
    assert not _on_login_wall(page)


def test_the_login_tool_never_types_a_password():
    from backend.app.browser.primitives import AWAIT_LOGIN_SPEC
    desc = AWAIT_LOGIN_SPEC.description
    assert "never types a password" in desc
    assert "no button for them to press" in desc, (
        "it must WATCH for the sign-in, not ask for a tap — a run that "
        "stalls waiting on the founder has handed the work back")


# ---- helpers ------------------------------------------------------------

def _element_map():
    from backend.app.browser.observation import ElementMap
    return ElementMap()


def _run_dismiss(sess):
    """_dismiss_impl resolves its session through the manager, so the
    lookup is stubbed rather than a real browser session registered."""
    from backend.app.browser import primitives
    real = primitives._session
    primitives._session = lambda args: (sess, None)
    try:
        return primitives._dismiss_impl({"session_token": "t"})
    finally:
        primitives._session = real
