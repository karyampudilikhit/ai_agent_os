"""Elements inside an iframe are part of the page.

page.evaluate runs in the main document only, so an embedded form, a chat
widget, a checkout step or a consent wall simply did not exist as far as
the model was concerned. It saw an empty region where the controls were
and had no way to know why. Measured on theguardian.com, whose entire
consent dialog lives in a cross-origin frame.
"""
from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.sync_api")

INNER = """
<html><body>
  <h2>Payment</h2>
  <input type="text" name="card" placeholder="Card number">
  <button id="pay">Pay now</button>
</body></html>
"""
OUTER = """
<html><body>
  <h1>Checkout</h1>
  <button id="outer">Back to cart</button>
  <iframe src="https://fixture.test/inner" width="500" height="300"></iframe>
</body></html>
"""


@pytest.fixture()
def page():
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        p = browser.new_page()
        # Routed on the CONTEXT, not the page: a new tab does not
        # inherit page-level routes, and one registered on the page loads
        # chrome-error:// in the new tab instead of the fixture.
        p.context.route("https://fixture.test/inner", lambda r: r.fulfill(
            status=200, content_type="text/html", body=INNER))
        p.context.route("https://fixture.test/outer", lambda r: r.fulfill(
            status=200, content_type="text/html", body=OUTER))
        yield p
        browser.close()


def test_frame_elements_are_observed(page):
    from backend.app.browser.observation import ElementMap, observe_page
    page.goto("https://fixture.test/outer")
    page.wait_for_timeout(600)
    em = ElementMap()
    obs = observe_page(page, em)
    names = [e.get("name") for e in obs.elements]
    assert "Back to cart" in names, "main document still works"
    assert "Pay now" in names, "the frame's button must be reachable"


def test_frame_elements_carry_a_frame_prefixed_id(page):
    from backend.app.browser.observation import ElementMap, observe_page
    page.goto("https://fixture.test/outer")
    page.wait_for_timeout(600)
    em = ElementMap()
    obs = observe_page(page, em)
    pay = next(e for e in obs.elements if e.get("name") == "Pay now")
    assert pay["id"].startswith("f1e"), f"got {pay['id']}"
    assert em.frame_for(pay["id"]) is not None, "the frame is remembered"
    assert em.frame_for("e1") is None, "main-document ids have no frame"


def test_a_frame_element_actually_resolves_and_clicks(page):
    """The whole point: addressable AND operable."""
    from backend.app.browser.observation import ElementMap, observe_page
    from backend.app.browser.primitives import _resolve

    page.goto("https://fixture.test/outer")
    page.wait_for_timeout(600)
    em = ElementMap()
    obs = observe_page(page, em)
    pay = next(e for e in obs.elements if e.get("name") == "Pay now")

    class Sess:
        pass
    s = Sess()
    s.page = page
    s.element_map = em

    loc, err = _resolve(s, pay["id"])
    assert err is None, err
    assert loc.count() == 1
    loc.click(timeout=2000)


def test_the_id_scheme_is_unambiguous():
    from backend.app.browser.observation import ElementMap
    em = ElementMap()
    # The stamp inside a frame is the UNPREFIXED id — the prefix names
    # the document, not the element within it.
    assert em.selector_for("f2e7") == '[data-vai-id="e7"]'
    assert em.selector_for("e7") == '[data-vai-id="e7"]'


def test_a_frame_id_is_accepted_by_the_checker(page):
    from backend.app.browser.observation import ElementMap, observe_page
    page.goto("https://fixture.test/outer")
    page.wait_for_timeout(600)
    em = ElementMap()
    obs = observe_page(page, em)
    pay = next(e for e in obs.elements if e.get("name") == "Pay now")
    assert em.check(pay["id"]) is None
    assert em.check("f9e9") is not None, "an invented frame id is still refused"


# ---------------------------------------------- clicks that open a new tab

NEWTAB_PAGE = """
<html><body>
  <h1>Listings</h1>
  <a id="open" href="https://fixture.test/detail" target="_blank">View job</a>
</body></html>
"""
DETAIL_PAGE = """
<html><body><h1>Senior Product Manager</h1>
  <button id="apply">Apply now</button></body></html>
"""


def test_a_click_that_opens_a_tab_moves_the_session(page):
    """A click on target="_blank" left the session pointing at the
    ORIGINAL page. From the agent's side the click succeeded and nothing
    changed, so it retried on a page that would never move while the
    thing it wanted sat in a tab nobody was looking at."""
    from backend.app.browser.observation import ElementMap
    from backend.app.browser.primitives import _follow_new_tab

    page.context.route("https://fixture.test/newtab", lambda r: r.fulfill(
        status=200, content_type="text/html", body=NEWTAB_PAGE))
    page.context.route("https://fixture.test/detail", lambda r: r.fulfill(
        status=200, content_type="text/html", body=DETAIL_PAGE))
    page.goto("https://fixture.test/newtab")

    class Sess:
        pass
    s = Sess()
    s.page = page
    s.context = page.context
    s.element_map = ElementMap()

    with page.context.expect_page():
        page.click("#open")

    assert _follow_new_tab(s) is True
    assert s.page is not page, "the session moved to the new tab"
    assert "detail" in s.page.url
    # And the new tab is operable.
    s.page.click("#apply", timeout=2000)


def test_no_new_tab_means_no_move(page):
    from backend.app.browser.observation import ElementMap
    from backend.app.browser.primitives import _follow_new_tab
    page.context.route("https://fixture.test/plain", lambda r: r.fulfill(
        status=200, content_type="text/html", body="<html><body>x</body></html>"))
    page.goto("https://fixture.test/plain")

    class Sess:
        pass
    s = Sess()
    s.page = page
    s.context = page.context
    s.element_map = ElementMap()
    assert _follow_new_tab(s) is False
    assert s.page is page


def test_ids_are_regenerated_when_the_session_moves(page):
    """Ids belong to a document. Carrying the old map onto a new tab
    would resolve e17 against whatever now sits in that position."""
    from backend.app.browser.observation import ElementMap
    from backend.app.browser.primitives import _follow_new_tab

    page.context.route("https://fixture.test/newtab", lambda r: r.fulfill(
        status=200, content_type="text/html", body=NEWTAB_PAGE))
    page.context.route("https://fixture.test/detail", lambda r: r.fulfill(
        status=200, content_type="text/html", body=DETAIL_PAGE))
    page.goto("https://fixture.test/newtab")

    class Sess:
        pass
    s = Sess()
    s.page = page
    s.context = page.context
    s.element_map = ElementMap()
    gen_before = s.element_map.generation

    with page.context.expect_page():
        page.click("#open")
    _follow_new_tab(s)
    assert s.element_map.generation > gen_before
