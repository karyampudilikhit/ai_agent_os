"""Call the tool. Do not merely read its advertisement.

browser_extract_table declared `head` and `tail`, documented them in its
description, and never read them. The render call passed bare
`head=head, tail=tail` -- two names that exist nowhere in that function
-- so EVERY call carrying either one died with:

    (action failed: name 'head' is not defined)

It survived a full test suite because the test that covered the feature
asserted the SPEC declared the parameters and never invoked the handler:

    def test_extract_table_offers_head_and_tail():
        names = {p["name"] for p in spec.parameters}
        assert {"head", "tail"} <= names

That checks the menu, not the kitchen. A live screener run lost three of
its twelve steps to it, on a page that had already loaded the twenty rows
it was asking for.

So every test in this file EXECUTES the handler against a real page.
"""
from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.sync_api")

ROWS = [("WETO", "+64.20%", "17.40"), ("IPST", "+61.10%", "9.02"),
        ("BOXL", "+58.30%", "6.11"), ("XHLD", "+55.90%", "22.75"),
        ("FGI", "+52.40%", "8.30"), ("CAPR", "+49.80%", "13.09"),
        ("NBIL", "+47.20%", "5.44"), ("NEBX", "-38.10%", "7.90"),
        ("SNXX", "-41.60%", "11.25"), ("ARX", "-52.30%", "19.54")]


def _screener_html() -> str:
    body = "".join(
        f"<tr><td>{i}</td><td>{t}</td><td>Company {t}</td>"
        f"<td>{chg}</td><td>{px}</td></tr>"
        for i, (t, chg, px) in enumerate(ROWS, 1))
    return ("<html><head><title>Screener</title></head><body><table>"
            "<tr><th>No.</th><th>Ticker</th><th>Company</th>"
            "<th>Perf Week</th><th>Price</th></tr>"
            f"{body}</table></body></html>")


@pytest.fixture()
def page():
    with playwright.sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_page()
        p.context.route("https://fixture.test/**", lambda r: r.fulfill(
            status=200, content_type="text/html", body=_screener_html()))
        p.goto("https://fixture.test/screener")
        yield p
        b.close()


def _call(page, **args):
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
        return task_flow._browser_extract_table_impl({"session_token": "t", **args})
    finally:
        task_flow.get_manager = real


# ------------------------------------------ the call itself must work

def test_head_and_tail_do_not_crash_the_call(page):
    """The exact live failure: '(action failed: name head is not defined)'."""
    out = _call(page, head=20, tail=0)
    assert "not defined" not in out, out[:300]
    assert "action failed" not in out, out[:300]


def test_no_arguments_at_all_works(page):
    out = _call(page)
    assert "action failed" not in out
    assert "WETO" in out


def test_table_index_with_head_and_tail_works(page):
    """The shape the live run sent on its third attempt."""
    out = _call(page, table_index=0, head=12, tail=12)
    assert "action failed" not in out
    assert "WETO" in out and "ARX" in out


# ------------------------------------------ and the rows must be right

def _rendered_rows(out: str) -> str:
    """Only the table body.

    The header carries a DATA fingerprint that lists every row key on the
    page by design -- that is how a later step proves the rows moved. A
    naive substring search over the whole reply therefore finds every
    ticker no matter what head/tail did, which would make these two tests
    pass on a completely broken renderer."""
    marker = out.find("--- table")
    assert marker != -1, out[:300]
    return out[marker:]


def test_head_limits_from_the_top(page):
    body = _rendered_rows(_call(page, head=3, tail=0))
    assert "WETO" in body and "BOXL" in body
    assert "ARX" not in body, "tail=0 must not smuggle the bottom in"


def test_both_ends_survive(page):
    """The gainers are on top and the losers at the bottom."""
    body = _rendered_rows(_call(page, head=2, tail=2))
    assert "WETO" in body and "IPST" in body
    assert "SNXX" in body and "ARX" in body
    assert "FGI" not in body
    assert "omitted" in body, "the gap is stated, never silent"


def test_head_only_still_truncates(page):
    """The live call was {"head": 20, "tail": 0}. With `if head and tail`
    that returned every row of the table -- the very truncation the
    parameter exists to control."""
    body = _rendered_rows(_call(page, head=4, tail=0))
    assert "WETO" in body and "XHLD" in body
    assert "CAPR" not in body and "ARX" not in body
    assert "omitted" in body
    assert "FIRST 4" in body and "LAST" not in body


def test_tail_only_still_truncates(page):
    body = _rendered_rows(_call(page, head=0, tail=3))
    assert "ARX" in body and "SNXX" in body
    assert "WETO" not in body
    assert "LAST 3" in body and "FIRST" not in body


def test_both_zero_means_everything(page):
    body = _rendered_rows(_call(page, head=0, tail=0))
    for t, _, _ in ROWS:
        assert t in body
    assert "omitted" not in body


def test_a_string_row_count_is_honoured(page):
    """Models send "3" and 3 for the same intent, and a row count has no
    reading in which "3" is not three."""
    assert _call(page, head="3", tail="0") == _call(page, head=3, tail=0)


def test_a_nonsense_row_count_falls_back_instead_of_failing(page):
    """Losing a whole page read over a malformed count is the worse
    outcome."""
    out = _call(page, head="lots")
    assert "action failed" not in out
    assert "WETO" in out


def test_the_default_shows_both_ends(page):
    """A caller that names neither still gets the losers."""
    out = _call(page)
    assert "WETO" in out and "ARX" in out


def test_the_declared_parameters_are_the_ones_that_are_read():
    """The advertisement and the kitchen must agree -- checked by
    running the handler with each declared name, not by reading the
    spec."""
    import inspect
    from backend.app.browser.task_flow import (
        BROWSER_EXTRACT_TABLE_SPEC as spec, _browser_extract_table_impl,
    )
    src = inspect.getsource(_browser_extract_table_impl)
    for p in spec.parameters:
        name = p["name"]
        if name == "session_token":
            continue
        assert f'args.get("{name}")' in src, (
            f"{name} is advertised in the spec and never read by the handler")


# ------------------- no index means "the data one", not "the first one"

FINVIZ_SHAPE = """
<html><head><title>Screener</title></head><body>
  <table id="spacer"><tr><td></td><td></td></tr><tr><td></td><td></td></tr>
    <tr><td></td><td></td></tr></table>
  <table id="nav"><tr><td>Home</td><td>News</td><td>Screener</td></tr>
    <tr><td>Charts</td><td>Maps</td><td>Groups</td></tr>
    <tr><td>Portfolio</td><td>Insider</td><td>Futures</td></tr></table>
  <table id="results">
    <tr><th>No.</th><th>Ticker</th><th>Perf Week</th><th>Price</th></tr>
    <tr><td>1</td><td>IPST</td><td>61.10</td><td>9.02</td></tr>
    <tr><td>2</td><td>BOXL</td><td>58.30</td><td>6.11</td></tr>
    <tr><td>3</td><td>XHLD</td><td>55.90</td><td>22.75</td></tr>
    <tr><td>4</td><td>CAPR</td><td>49.80</td><td>13.09</td></tr>
  </table>
</body></html>
"""


@pytest.fixture()
def finviz_page():
    with playwright.sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_page()
        p.context.route("https://fixture.test/**", lambda r: r.fulfill(
            status=200, content_type="text/html", body=FINVIZ_SHAPE))
        p.goto("https://fixture.test/screener")
        yield p
        b.close()


def test_the_data_table_comes_first_without_an_index(finviz_page):
    """Four live screener runs died here. The DATA fingerprint found the
    results grid every time -- "DATA: 20 row(s) | IPST · BOXL · XHLD" --
    while this tool returned tables in DOM order and made the agent guess
    an index. Finviz's table 0 is a layout spacer, so the run guessed
    0, 1, 2, 0, 1 and printed five rows of (blank) from a page whose real
    rows were already known one line above."""
    out = _call(finviz_page)
    body = _rendered_rows(out)
    first_block = body.split("--- table")[1]
    assert "IPST" in first_block, f"the data table is not first:\n{body[:500]}"
    assert "BOXL" in first_block and "CAPR" in first_block


def test_the_other_tables_are_still_there(finviz_page):
    """Ranked, not filtered. A page whose data lives somewhere
    unexpected must stay fully readable."""
    out = _call(finviz_page)
    assert "Home" in out, "the nav table was dropped rather than deprioritised"


def test_an_explicit_index_still_wins(finviz_page):
    """The caller who knows which table they want must keep getting it."""
    body = _rendered_rows(_call(finviz_page, table_index=1))
    assert "Home" in body
    assert "IPST" not in body.split("--- table")[1]


def test_the_two_implementations_ask_the_same_question():
    """One rule, two files, is a bug with a schedule. Both score a table
    by numeric density and consistent row width."""
    from backend.app.browser.observation import _DATA_FN_JS
    from backend.app.browser.task_flow import _EXTRACT_TABLES_JS
    for src in (_DATA_FN_JS, _EXTRACT_TABLES_JS):
        assert "numeric" in src and "regular" in src
        assert "0.15" in src and "0.6" in src
