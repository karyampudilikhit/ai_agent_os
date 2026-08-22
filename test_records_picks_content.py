"""Pick the articles, not the menu that contains them.

Measured on livemint.com/market and cnbc.com section pages during a live
news run. browser_extract_records returned SEVEN "records" that were the
site's navigation sections -- "Markets News", "US Market", "IPO" -- each
one carrying every headline beneath it crushed into a single blob:

    --- record 1 ---
    title: Markets News
    text: All Market Dashboard Mark To Market US Market Stock Market News
          IPO Commodities Bonds Mutual Funds Premium Stories Markets News
          Lalithaa Jewellery Mart IPO Day 2: GMP, subscription to...

Every real headline was in there and none of them was reachable. The run
spent six of its twenty-two steps trying other URLs to get at articles it
had already downloaded.

What hid it was the `avg` cap at 300: a 40-character headline and a
4,000-character section blob both score 300, so the container level and
the record level looked identical to the ranking.
"""
from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.sync_api")

HEADLINES = [
    ("Tata Sons divided over reconsideration of Chair's resignation", "/tata"),
    ("Lalithaa Jewellery Mart IPO Day 2: GMP, subscription to review", "/ipo"),
    ("Low PE stock Felix Industries jumps 8% on new order win", "/felix"),
    ("Nasdaq plans 23-hour trading sessions from December 2026", "/nasdaq"),
    ("Rupee slips 12 paise against the dollar in early trade", "/rupee"),
    ("Sensex ends 340 points higher led by banking and IT counters", "/sensex"),
]
SECTIONS = ["Markets News", "US Market", "Stock Market News", "IPO", "Commodities"]


def _news_page() -> str:
    """A section-nav wrapper whose items each swallow the whole article
    list -- the shape livemint.com/market actually serves."""
    cards = "".join(
        f'<div class="card"><h3>{t}</h3><a href="https://fixture.test{h}">Read</a>'
        f'<p>Published today by the markets desk. '
        f'{"Detail sentence. " * 4}</p></div>'
        for t, h in HEADLINES
    )
    navs = "".join(
        f'<section class="navsec"><a href="https://fixture.test/sec/{i}">{name}</a>'
        f'<div class="inner">{cards}</div></section>'
        for i, name in enumerate(SECTIONS)
    )
    return f"<html><body><main><nav>{navs}</nav><div class='feed'>{cards}</div></main></body></html>"


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
            p.goto("https://fixture.test/market")

        p.serve = serve
        yield p
        b.close()


def _run(page):
    from backend.app.browser import primitives
    from backend.app.browser.primitives import _extract_records_impl

    class S:
        pass

    s = S()
    s.page, s.token, s.touch = page, "t", lambda: None
    real = primitives._session
    primitives._session = lambda a: (s, None)
    try:
        return _extract_records_impl({"session_token": "t"})
    finally:
        primitives._session = real


def test_the_articles_come_back_not_the_sections(page):
    page.serve(_news_page())
    out = _run(page)
    titles = [ln.split("title:", 1)[1].strip()
              for ln in out.splitlines() if ln.startswith("title:")]
    assert titles, out
    # Every returned record should be an article, not a nav section.
    for name in SECTIONS:
        assert name not in titles, f"returned the {name!r} nav section as a record"


def test_each_headline_is_its_own_record(page):
    """The failure was not that the text was missing -- it was all
    there, inside one item. Separate records are the whole point."""
    page.serve(_news_page())
    out = _run(page)
    got = sum(1 for t, _ in HEADLINES if t in out)
    assert got >= 4, f"only {got} of {len(HEADLINES)} headlines surfaced\n{out[:900]}"
    assert out.count("--- record") >= 4


def test_a_group_of_giant_blobs_is_labelled_as_sections(page):
    """Past a point an item is not a row in a list, it is the list.

    It is still HANDED OVER -- the text really is on the page, and
    returning nothing sends the agent hunting for data it already
    downloaded, which is the six wasted steps this file is about. What
    changes is that it is labelled, so the agent looks closer instead of
    concluding the page was empty."""
    blob = "Sentence about the market. " * 90   # ~2.4k chars each
    html = ("<html><body><main>" + "".join(
        f'<div class="sec"><a href="https://fixture.test/s{i}">Section {i}</a>'
        f'<p>{blob}</p></div>' for i in range(5)) + "</main></body></html>")
    page.serve(html)
    out = _run(page)
    assert "probably the SECTIONS that contain" in out
    assert "Do not report these blocks as if each were one record" in out


def test_a_genuine_card_list_still_works(page):
    """The fix must not cost the case that already worked -- Naukri and
    Wellfound both render exactly this."""
    cards = "".join(
        f'<div class="card"><h3>{t}</h3><a href="https://fixture.test{h}">View</a>'
        f'<p>Acme Corp - Bengaluru, India - Posted 2 days ago</p></div>'
        for t, h in HEADLINES)
    page.serve(f"<html><body><main><div class='results'>{cards}</div></main></body></html>")
    out = _run(page)
    assert out.count("--- record") >= 5
    for t, _ in HEADLINES[:5]:
        assert t in out
