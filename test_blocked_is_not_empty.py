"""A blocked page and an empty page are different facts.

Measured on a live news run. reuters.com served headless Chrome an empty
shell, and browser_extract returned EIGHTY-THREE characters:

    [page text - https://www.reuters.com/world/us/]
    DATA: (no data table on this page)

Nothing in that says "blocked", so the agent read it as "this page has no
content", tried the same site three more ways, and spent four of its
twenty-two steps before moving on. The same run reported the site as
having nothing to show, which was false -- Reuters had plenty, it just
would not hand any of it to a robot.

Finviz cost a second run the same way, with a Cloudflare "Just a
moment..." interstitial.
"""
from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.sync_api")

REAL_ARTICLE = (
    "<html><head><title>Markets today</title></head><body><main>"
    + "<p>The index closed higher on Friday as traders weighed inflation "
      "data against a softer jobs report. </p>" * 12
    + "</main></body></html>"
)
CLOUDFLARE = ("<html><head><title>Just a moment...</title></head>"
              "<body><div>Verifying you are human.</div></body></html>")
EMPTY_SHELL = ("<html><head><title>Reuters World News</title></head>"
               "<body><div id='root'></div></body></html>")


@pytest.fixture()
def page():
    with playwright.sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_page()
        p.context.route("https://fixture.test/**", lambda r: r.fulfill(
            status=200, content_type="text/html",
            body=getattr(p, "_html", "<html></html>")))

        def serve(html):
            p._html = html
            p.goto("https://fixture.test/page")

        p.serve = serve
        yield p
        b.close()


def _extract(page):
    """_browser_extract_impl resolves its session through the manager, so
    the lookup is stubbed rather than a real session registered."""
    from backend.app.browser import task_flow

    class S:
        pass

    s = S()
    s.page, s.token, s.touch = page, "t", lambda: None

    class Mgr:
        def get(self, _):
            return s

    real = task_flow.get_manager
    task_flow.get_manager = lambda: Mgr()
    try:
        return task_flow._browser_extract_impl({"session_token": "t"})
    finally:
        task_flow.get_manager = real


# ------------------------------------------------- the blocked cases

def test_a_bot_wall_is_named_as_one(page):
    page.serve(CLOUDFLARE)
    out = _extract(page)
    assert "ALMOST NO TEXT" in out
    assert "bot-check interstitial" in out
    assert "refusing automated access" in out


def test_an_unrendered_shell_says_what_it_might_be(page):
    """Reuters' actual shape. Without a telltale title the honest answer
    is the three things it could be, not a guess between them."""
    page.serve(EMPTY_SHELL)
    out = _extract(page)
    assert "ALMOST NO TEXT" in out
    assert "blocking automated access" in out
    assert "login or paywall" in out
    assert "JavaScript" in out


def test_it_says_stop_trying_this_site(page):
    """The four wasted steps were all retries against the same host."""
    page.serve(EMPTY_SHELL)
    out = _extract(page)
    assert "DO NOT keep trying this site" in out
    assert "different source" in out


def test_unreachable_is_distinguished_from_empty(page):
    """The run reported Reuters as having no content. It had plenty."""
    page.serve(EMPTY_SHELL)
    out = _extract(page)
    assert "UNREACHABLE rather than as empty" in out


def test_the_character_count_is_reported(page):
    """83 characters is the evidence. Saying so lets the agent judge."""
    page.serve(CLOUDFLARE)
    out = _extract(page)
    assert "characters" in out
    assert "Just a moment" in out, "name the title that gave it away"


# ------------------------------------------------- the normal case

def test_a_real_page_is_untouched(page):
    """The guard must not fire on a page that simply is short-ish."""
    page.serve(REAL_ARTICLE)
    out = _extract(page)
    assert "ALMOST NO TEXT" not in out
    assert "traders weighed inflation" in out


def test_the_threshold_is_well_below_a_real_page():
    from backend.app.browser.task_flow import _MIN_REAL_PAGE_CHARS
    assert 100 <= _MIN_REAL_PAGE_CHARS <= 600


def test_known_interstitial_titles_are_recognised():
    """Verbatim from live runs: Cloudflare, Indeed, Naukri."""
    from backend.app.browser.task_flow import _looks_like_a_wall
    for title in ("Just a moment...", "Security Check - Indeed.com",
                  "Access Denied", "Attention Required! | Cloudflare"):
        assert _looks_like_a_wall(title, ""), title


def test_an_ordinary_title_is_not_a_wall():
    from backend.app.browser.task_flow import _looks_like_a_wall
    for title in ("Reuters World News", "Product Manager Jobs in India",
                  "Markets today"):
        assert not _looks_like_a_wall(title, ""), title
