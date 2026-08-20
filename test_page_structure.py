"""Sections, TOCs and page shape, on synthetic HTML that is nobody's site.

Both tools exist because of one live failure and one live near-miss.

Asked for "the History section of the Artificial Intelligence article", a
run had the right page at step 2 and no instrument to slice it:
browser_extract returns the whole article, and a "#History" URL scrolls
the browser without changing a character of what gets read. Twenty calls,
675 seconds, bouncing between the anchor and a site API, out of budget
with the answer on screen.

Asked for a page's first five contents entries, another run escaped the
same way and succeeded only because that site published an API. Most do
not.

EVERY FIXTURE HERE IS GENERIC. A test written against one encyclopedia's
markup would prove we can read that encyclopedia. The markup below is
plain HTML5, a div-based component page, and an ARIA-annotated docs page
-- three different houses, one set of rules.
"""
from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.sync_api")

# --------------------------------------------------------------- fixtures

SEMANTIC = """
<html><head><title>Handbook</title></head><body>
<main>
  <h1>Widget Handbook</h1>
  <p>An overview of widgets.</p>
  <h2>History</h2>
  <p>Widgets began in the workshops of the north.</p>
  <p>The first factory opened in 1912.</p>
  <h3>Early years</h3>
  <p>Production was slow and entirely manual.</p>
  <h2>Design</h2>
  <p>A widget has a body and a flange.</p>
  <h3>Materials</h3>
  <p>Brass, then steel, then polymer.</p>
  <h2>Safety</h2>
  <p>Always wear gloves near a widget press.</p>
</main>
</body></html>
"""

# Same document, wrapped: headings and their prose live in sibling
# <section> elements. Sibling-walking from the <h2> never escapes the
# wrapper, which is why the extractor walks the document instead.
WRAPPED = """
<html><head><title>Handbook</title></head><body>
<article>
  <section><h1>Widget Handbook</h1><p>An overview of widgets.</p></section>
  <section><h2>History</h2><p>Widgets began in the workshops of the north.</p>
    <p>The first factory opened in 1912.</p></section>
  <section><h3>Early years</h3><p>Production was slow and entirely manual.</p></section>
  <section><h2>Design</h2><p>A widget has a body and a flange.</p></section>
  <section><h2>Safety</h2><p>Always wear gloves near a widget press.</p></section>
</article>
</body></html>
"""

# A component-framework page: no <h*> at all, ARIA roles instead.
ARIA = """
<html><head><title>Docs</title></head><body>
<div role="main">
  <div role="heading" aria-level="1">API Reference</div>
  <p>Everything the client exposes.</p>
  <div role="heading" aria-level="2">Authentication</div>
  <p>Send a bearer token on every request.</p>
  <div role="heading" aria-level="3">Rotating keys</div>
  <p>Keys rotate every ninety days.</p>
  <div role="heading" aria-level="2">Rate limits</div>
  <p>Sixty requests a minute.</p>
</div>
</body></html>
"""

# A real contents widget: a nav of same-page fragment links, nested.
TOC_NAV = """
<html><head><title>Guide</title></head><body>
<nav aria-label="Contents">
  <ul>
    <li><a href="#install">Installation</a>
      <ul><li><a href="#install-linux">On Linux</a></li>
          <li><a href="#install-mac">On macOS</a></li></ul></li>
    <li><a href="#usage">Usage</a></li>
    <li><a href="#faq">FAQ</a></li>
  </ul>
</nav>
<nav aria-label="Site">
  <ul><li><a href="/home">Home</a></li><li><a href="/pricing">Pricing</a></li>
      <li><a href="/blog">Blog</a></li><li><a href="/docs">Docs</a></li></ul>
</nav>
<main>
  <h2 id="install">Installation</h2><p>Run the installer.</p>
  <h3 id="install-linux">On Linux</h3><p>Use the package manager.</p>
  <h3 id="install-mac">On macOS</h3><p>Use the disk image.</p>
  <h2 id="usage">Usage</h2><p>Call the binary.</p>
  <h2 id="faq">FAQ</h2><p>Questions and answers.</p>
</main>
</body></html>
"""

MIXED = """
<html><head><title>Catalogue</title></head><body>
<main>
  <h1>Catalogue</h1>
  <table><tr><th>SKU</th><th>Price</th></tr>
    <tr><td>A1</td><td>10.00</td></tr><tr><td>B2</td><td>12.50</td></tr>
    <tr><td>C3</td><td>9.75</td></tr></table>
  <ul><li>Ships in 24 hours</li><li>Free returns</li><li>Two year warranty</li>
      <li>Made in the north</li></ul>
  <div class="results">
    <div class="card"><h3>Widget One</h3><a href="/w/1">View</a>
      <p>A dependable widget for everyday use in the workshop.</p></div>
    <div class="card"><h3>Widget Two</h3><a href="/w/2">View</a>
      <p>A heavier widget for industrial presses and heavy loads.</p></div>
    <div class="card"><h3>Widget Three</h3><a href="/w/3">View</a>
      <p>A compact widget for tight spaces and small assemblies.</p></div>
  </div>
</main>
</body></html>
"""


@pytest.fixture()
def page():
    with playwright.sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_page()
        p.context.route("https://fixture.test/**", lambda r: r.fulfill(
            status=200, content_type="text/html",
            body=getattr(p, "_html", SEMANTIC)))

        def serve(html=SEMANTIC):
            p._html = html
            p.goto("https://fixture.test/doc")

        p.serve = serve
        p.serve()
        yield p
        b.close()


def _call(fn, page, **args):
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
        return fn({"session_token": "t", **args})
    finally:
        task_flow.get_manager = real


def _section(page, **kw):
    from backend.app.browser.structure import _extract_section_impl
    return _call(_extract_section_impl, page, **kw)


# ------------------------------------------------------ section boundaries

def test_a_named_section_comes_back_alone(page):
    out = _section(page, heading="History")
    assert "workshops of the north" in out
    assert "first factory opened in 1912" in out
    assert "body and a flange" not in out, "Design leaked in"
    assert "wear gloves" not in out, "Safety leaked in"


def test_a_sibling_heading_ends_the_section(page):
    out = _section(page, heading="Design")
    assert "body and a flange" in out
    assert "wear gloves" not in out


def test_the_last_section_ends_at_the_document(page):
    """No following heading is not a reason to return nothing."""
    out = _section(page, heading="Safety")
    assert "wear gloves" in out


# --------------------------------------------------------- nested sections

def test_subsections_are_included_by_default(page):
    """A subsection belongs to its parent."""
    out = _section(page, heading="History")
    assert "slow and entirely manual" in out, "the h3 under History was cut"


def test_include_subsections_false_stops_at_the_next_heading_of_any_level(page):
    out = _section(page, heading="History", include_subsections=False)
    assert "first factory opened" in out
    assert "slow and entirely manual" not in out, "the h3 should have ended it"


def test_the_subsections_taken_are_named(page):
    out = _section(page, heading="History")
    assert "Subsections included: Early years" in out


def test_a_string_false_is_honoured(page):
    """Models send "false" as often as false."""
    out = _section(page, heading="History", include_subsections="false")
    assert "slow and entirely manual" not in out


# ------------------------------------------- structure the DOM, not the URL

def test_a_wrapped_section_is_still_extracted(page):
    """<section><h2>..</h2><p>..</p></section> — sibling-walking from the
    heading never escapes the wrapper."""
    page.serve(WRAPPED)
    out = _section(page, heading="History")
    assert "workshops of the north" in out
    assert "first factory opened" in out
    assert "wear gloves" not in out


def test_aria_headings_work_without_any_h_tags(page):
    """Component frameworks style a div as a heading and never emit h2."""
    page.serve(ARIA)
    out = _section(page, heading="Authentication")
    assert "bearer token" in out
    assert "Sixty requests" not in out, "ran past the next aria heading"


def test_aria_levels_nest(page):
    page.serve(ARIA)
    out = _section(page, heading="Authentication")
    assert "rotate every ninety days" in out, "the level-3 aria heading is a sub"


# ---------------------------------------------------------- heading lookup

def test_the_match_is_not_case_sensitive(page):
    assert "workshops of the north" in _section(page, heading="history")


def test_an_exact_match_beats_a_longer_one(page):
    page.serve(SEMANTIC.replace("<h2>Design</h2>",
                                "<h2>History of the trade</h2><p>Longer.</p>"
                                "<h2>Design</h2>"))
    out = _section(page, heading="History")
    assert "workshops of the north" in out
    assert "Longer." not in out


def test_a_missing_heading_returns_the_headings_that_exist(page):
    """The failure this replaces is a run concluding a section is absent."""
    out = _section(page, heading="Controversies")
    assert "no heading matching" in out
    assert "not about the page" in out
    assert "History" in out and "Safety" in out


# --------------------------------------------------------------------- TOC

def test_a_contents_nav_is_found_and_the_site_nav_is_not(page):
    """Both are <nav> with links. Only one is mostly same-page anchors."""
    page.serve(TOC_NAV)
    from backend.app.browser.structure import _extract_toc_impl
    out = _call(_extract_toc_impl, page)
    assert "Installation" in out and "Usage" in out and "FAQ" in out
    assert "Pricing" not in out, "the site nav was picked instead"
    assert "Blog" not in out


def test_toc_order_and_nesting_are_preserved(page):
    page.serve(TOC_NAV)
    from backend.app.browser.structure import _extract_toc_impl
    import re
    out = _call(_extract_toc_impl, page)
    items = [m.group(1) for m in
             (re.match(r"^\s*\d+\.\s(.*?)\s+\(#", l) for l in out.splitlines()) if m]
    assert items[0].strip() == "Installation"
    assert items[1].strip() == "On Linux"
    assert items[1].startswith("  "), "a nested entry must be indented"
    assert items[3].strip() == "Usage"
    assert not items[3].startswith("  ")


def test_toc_entries_carry_their_anchors(page):
    page.serve(TOC_NAV)
    from backend.app.browser.structure import _extract_toc_impl
    out = _call(_extract_toc_impl, page)
    assert "(#install)" in out and "(#faq)" in out


def test_a_page_with_no_toc_widget_falls_back_to_its_headings(page):
    """Plenty of pages have sections and publish no contents list."""
    from backend.app.browser.structure import _extract_toc_impl
    out = _call(_extract_toc_impl, page)
    assert "no contents widget" in out
    assert "History" in out and "Design" in out


def test_the_toc_never_uses_a_site_api():
    import inspect
    from backend.app.browser import structure
    src = inspect.getsource(structure)
    assert "api.php" not in src
    assert "wikipedia" not in src.lower().replace("encyclopedia", "")


# ------------------------------------------------------- page structure

def test_structure_reports_tables_lists_and_cards(page):
    page.serve(MIXED)
    from backend.app.browser.structure import _structure_impl
    out = _call(_structure_impl, page)
    assert "TABLES: 1" in out
    assert "SKU" in out
    assert "LISTS:" in out and "Free returns" in out
    assert "REPEATED CARD GROUPS" in out


def test_structure_points_at_the_right_reader(page):
    page.serve(MIXED)
    from backend.app.browser.structure import _structure_impl
    out = _call(_structure_impl, page)
    assert "browser_extract_table" in out
    assert "browser_extract_records" in out
    assert "browser_extract_section" in out


def test_structure_on_plain_prose_says_so(page):
    page.serve("<html><body><p>Just a paragraph of prose and nothing else at "
               "all on this page.</p></body></html>")
    from backend.app.browser.structure import _structure_impl
    out = _call(_structure_impl, page)
    assert "plain prose" in out
    assert "browser_extract" in out


# ------------------------------------------------------------- outline

def test_the_outline_is_in_page_order_with_nesting(page):
    from backend.app.browser.structure import _outline_impl
    import re
    out = _call(_outline_impl, page)
    texts = [m.group(1) for m in
             (re.match(r"^\s*\d+\.\s(.*)$", l) for l in out.splitlines()) if m]
    assert texts[0].strip() == "Widget Handbook"
    assert texts[1].strip() == "History"
    assert texts[2].strip() == "Early years"
    assert texts[2].startswith("  "), "the h3 should sit under its h2"


def test_a_page_with_no_headings_says_what_to_use_instead(page):
    page.serve("<html><body><div>Text with no headings at all.</div></body></html>")
    from backend.app.browser.structure import _outline_impl
    out = _call(_outline_impl, page)
    assert "no headings found" in out
    assert "browser_extract" in out


# ------------------------------------------------------------- plumbing

def test_all_four_are_registered():
    from backend.app.actions.action_registry import get_registry
    names = set(get_registry().known_names())
    for n in ("browser_extract_section", "browser_extract_toc",
              "browser_outline", "browser_page_structure"):
        assert n in names, n


def test_none_are_mutating():
    from backend.app.browser.structure import ALL_SPECS
    for spec in ALL_SPECS:
        assert spec.mutating is False, spec.name


def test_the_description_warns_a_fragment_url_does_not_narrow():
    """The misconception that cost the live run: #History looks like it
    worked."""
    from backend.app.browser.structure import BROWSER_EXTRACT_SECTION_SPEC as spec
    assert "only SCROLLS" in spec.description
    assert "does not change what action.browser_extract returns" in spec.description


def test_include_subsections_is_declared(page):
    from backend.app.browser.structure import BROWSER_EXTRACT_SECTION_SPEC as spec
    names = {p["name"] for p in spec.parameters}
    assert "include_subsections" in names


def test_section_output_is_wrapped_as_untrusted(page):
    out = _section(page, heading="History")
    assert "UNTRUSTED WEB CONTENT" in out


def test_the_loop_is_told_which_reader_to_use():
    from backend.app.orchestrator.execution_loop import BROWSER_RULES
    for tool in ("browser_extract_section", "browser_extract_toc",
                 "browser_extract_table", "browser_extract_records",
                 "browser_page_structure"):
        assert tool in BROWSER_RULES, tool
    # Whitespace-normalised: the rule is wrapped across lines in the
    # prompt, and reflowing prose to suit a substring test would be
    # letting the test dictate the product.
    flat = " ".join(BROWSER_RULES.split())
    assert "a DIFFERENT reader, not the same one again" in flat


# ------------------- text that is present but not rendered

# A contents sidebar collapsed by default. The links have real bounding
# boxes, so the visibility check passes -- and innerText returns "" for
# every one of them.
COLLAPSED_TOC = """
<html><head><title>Guide</title></head><body>
<nav aria-label="Contents"><div style="visibility:hidden">
  <ul><li><a href="#install">Installation</a>
        <ul><li><a href="#install-linux">On Linux</a></li></ul></li>
      <li><a href="#usage">Usage</a></li>
      <li><a href="#faq">FAQ</a></li></ul>
</div></nav>
<main><h2 id="install">Installation</h2><p>Run it.</p>
      <h2 id="usage">Usage</h2><p>Use it.</p></main>
</body></html>
"""


def test_a_collapsed_contents_list_still_yields_its_entries(page):
    """Measured live, and the tool said so itself: "0 entr(y/ies), in
    page order. Found by: aria-label names a contents list." It had
    located exactly the right container and returned nothing from it.

    innerText reads the RENDER and comes back empty inside a
    visibility:hidden container, while getBoundingClientRect still
    reports a real box — so the links passed the visibility check and
    were then all dropped for having no text."""
    page.serve(COLLAPSED_TOC)
    from backend.app.browser.structure import _extract_toc_impl
    out = _call(_extract_toc_impl, page)
    assert "0 entr" not in out, out[:400]
    assert "Installation" in out and "Usage" in out and "FAQ" in out


def test_a_collapsed_list_keeps_its_nesting(page):
    page.serve(COLLAPSED_TOC)
    from backend.app.browser.structure import _extract_toc_impl
    import re
    out = _call(_extract_toc_impl, page)
    items = [m.group(1) for m in
             (re.match(r"^\s*\d+\.\s(.*?)\s+\(#", l) for l in out.splitlines()) if m]
    assert items[0].strip() == "Installation"
    assert items[1].strip() == "On Linux"
    assert items[1].startswith("  "), "nesting must survive the fallback"


def test_the_fallback_reads_the_dom_not_the_render():
    """textContent rather than innerText, and only as a FALLBACK — a
    rendered page must keep using innerText, which respects layout."""
    import inspect
    from backend.app.browser import structure
    src = inspect.getsource(structure)
    assert "textContent" in src
    assert "const t = clean(el.innerText);" in src, "innerText is still first"


def test_a_rendered_page_is_unaffected(page):
    """The fallback must not change the answer where innerText works."""
    page.serve(TOC_NAV)
    from backend.app.browser.structure import _extract_toc_impl
    out = _call(_extract_toc_impl, page)
    assert "Installation" in out and "Pricing" not in out
