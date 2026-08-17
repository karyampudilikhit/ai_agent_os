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
        p.route("https://fixture.test/inner", lambda r: r.fulfill(
            status=200, content_type="text/html", body=INNER))
        p.route("https://fixture.test/outer", lambda r: r.fulfill(
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
