"""Two fields that were on the page and did not come back.

Both measured on a live Reddit run that otherwise succeeded -- every
post, score and permalink was real, so nothing looked broken.

  1. TITLES VANISHED ON LINK POSTS. old.reddit puts a link post's
     headline in <a class="title"> and its thumbnail FIRST in the DOM.
     The extractor took `querySelector('a[href]')` -- the thumbnail,
     whose text is empty -- so every image post in r/SaaS and
     r/ycombinator came back with no title at all. The headline was
     sitting in the text blob two nodes away.

  2. COMMENT COUNTS WERE UNREADABLE. old.reddit's action row is inline
     list items with no separating whitespace, so the parent's innerText
     reads:

         19 hours ago by bowtamer111 19 commentssharesavehidereport

     "19 comments" is welded to "sharesavehide". Each anchor's OWN text
     is clean; only the parent's is not.

Neither failed a run. Both silently dropped a field the task asked for,
which is the quieter and worse kind of wrong.
"""
from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.sync_api")

# old.reddit's real shape: thumbnail anchor first, headline in a.title,
# action row as inline <li> with no whitespace between them.
REDDIT = """
<html><body><div id="siteTable">
  <div class="thing"><a class="thumbnail" href="https://i.redd.it/a.jpg"></a>
    <p class="title"><a class="title" href="https://old.reddit.com/r/SaaS/comments/aaa/">
      6 failed startups later, finally hit $500 MRR</a></p>
    <div class="score">64</div>
    <ul class="flat-list buttons"><li class="first"><a class="comments"
      href="https://old.reddit.com/r/SaaS/comments/aaa/">33 comments</a></li><li><a
      class="share">share</a></li><li><a class="save">save</a></li><li><a
      class="hide">hide</a></li><li><a class="report">report</a></li></ul></div>
  <div class="thing"><a class="thumbnail" href="https://i.redd.it/b.jpg"></a>
    <p class="title"><a class="title" href="https://old.reddit.com/r/SaaS/comments/bbb/">
      I built a whole SaaS for myself</a></p>
    <div class="score">45</div>
    <ul class="flat-list buttons"><li class="first"><a class="comments"
      href="https://old.reddit.com/r/SaaS/comments/bbb/">26 comments</a></li><li><a
      class="share">share</a></li><li><a class="save">save</a></li></ul></div>
  <div class="thing"><a class="thumbnail" href="https://i.redd.it/c.jpg"></a>
    <p class="title"><a class="title" href="https://old.reddit.com/r/SaaS/comments/ccc/">
      quit my job 2 years ago with zero expectations</a></p>
    <div class="score">72</div>
    <ul class="flat-list buttons"><li class="first"><a class="comments"
      href="https://old.reddit.com/r/SaaS/comments/ccc/">22 comments</a></li><li><a
      class="share">share</a></li><li><a class="save">save</a></li></ul></div>
  <div class="thing"><a class="thumbnail" href="https://i.redd.it/d.jpg"></a>
    <p class="title"><a class="title" href="https://old.reddit.com/r/SaaS/comments/ddd/">
      After months of building, I got my first organic user</a></p>
    <div class="score">75</div>
    <ul class="flat-list buttons"><li class="first"><a class="comments"
      href="https://old.reddit.com/r/SaaS/comments/ddd/">18 comments</a></li><li><a
      class="share">share</a></li><li><a class="save">save</a></li></ul></div>
</div></body></html>
"""


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
            p.goto("https://fixture.test/r/SaaS/top/")

        p.serve = serve
        yield p
        b.close()


def _run(page, **args):
    from backend.app.browser import primitives
    from backend.app.browser.primitives import _extract_records_impl

    class S:
        pass

    s = S()
    s.page, s.token, s.touch = page, "t", lambda: None
    real = primitives._session
    primitives._session = lambda a: (s, None)
    try:
        return _extract_records_impl({"session_token": "t", **args})
    finally:
        primitives._session = real


def _titles(out: str):
    return [l.split("title:", 1)[1].strip()
            for l in out.splitlines() if l.startswith("title:")]


# ------------------------------------------------------------ titles

def test_a_link_post_title_is_returned_as_a_title(page):
    """It was in the blob and not in the field, which is the same as
    missing for anything that reads the field."""
    page.serve(REDDIT)
    titles = _titles(_run(page))
    assert "6 failed startups later, finally hit $500 MRR" in titles
    assert "I built a whole SaaS for myself" in titles


def test_the_thumbnail_is_not_mistaken_for_the_title(page):
    """The thumbnail anchor comes FIRST in the DOM and has no text."""
    page.serve(REDDIT)
    titles = _titles(_run(page))
    assert titles and all(t for t in titles), f"a blank title came back: {titles}"


def test_the_permalink_not_the_thumbnail_is_the_link(page):
    """A record whose href is the image file cannot be opened as the
    post, which is what the founder asked for."""
    page.serve(REDDIT)
    out = _run(page)
    links = [l.split("link:", 1)[1].strip()
             for l in out.splitlines() if l.startswith("link:")]
    assert links
    assert all("/comments/" in l for l in links), links
    assert not any(l.endswith(".jpg") for l in links), links


# ------------------------------------------------------------ counts

def test_a_comment_count_welded_to_the_action_row_is_recovered(page):
    """The parent's innerText reads "33 commentssharesavehidereport".
    The anchor's own text reads "33 comments"."""
    page.serve(REDDIT)
    out = _run(page)
    assert "counts:" in out
    assert "33 comments" in out
    assert "26 comments" in out


def test_the_counts_field_holds_only_counts(page):
    """"share", "save", "hide" are not counts and must not ride along."""
    page.serve(REDDIT)
    counts = [l.split("counts:", 1)[1].strip()
              for l in _run(page).splitlines() if l.startswith("counts:")]
    assert counts
    for c in counts:
        assert "share" not in c.lower()
        assert "hide" not in c.lower()
        assert "report" not in c.lower()


def test_a_page_with_no_counts_says_nothing_rather_than_guessing(page):
    page.serve("<html><body><main>" + "".join(
        f'<div class="card"><h3>Item {i}</h3>'
        f'<a href="https://fixture.test/i{i}">Open</a>'
        f'<p>Some description text that is long enough to count here.</p></div>'
        for i in range(4)) + "</main></body></html>")
    out = _run(page)
    assert "Item 0" in out
    assert "counts:" not in out


# ------------------------------------------- the cases that worked

def test_a_heading_still_wins_over_any_link(page):
    """Naukri and the job boards render a real <h3>; that path must not
    change."""
    page.serve("<html><body><main>" + "".join(
        f'<div class="card"><a href="https://fixture.test/x{i}">apply now here</a>'
        f'<h3>Senior Product Manager {i}</h3>'
        f'<p>Acme Corp - Bengaluru, India - Posted 2 days ago</p></div>'
        for i in range(4)) + "</main></body></html>")
    titles = _titles(_run(page))
    assert any(t.startswith("Senior Product Manager") for t in titles), titles
