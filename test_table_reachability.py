"""A correct default nobody can reach is not a fix.

Two defects, both found by watching a live screener run rather than by
reading the code.

ONE. browser_extract_table was taught to score the page's tables and
return the DATA one first -- the same test the DATA fingerprint uses, so
a screener's results grid wins over its filter panel. It never once ran.
The scoring only applies when `table_index` is omitted, the parameter
existed, and the model supplied one on EVERY call: 0, 0, 1, 0, 0, 0. Each
returned fifteen rows of (blank) while the line directly above read
"DATA: 20 row(s) | WETO · IPST · BOXL".

The description was steering it there: "Which table, if the page has
several. Omit for all." Omitting sounded like asking for noise, so
supplying an index looked like the careful thing to do. A parameter's
existence is an instruction to use it, and the wording has to say
plainly when NOT to.

TWO. Finviz puts a one-character badge in the ticker cell, so innerText
reads "N NBIL", "S SNDU", "E EROC". Every row key built across four runs
was a ticker glued to a badge -- identifiers matching nothing on the page
and nothing in any deliverable, which is what the sort-changed check
compares.
"""
from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.sync_api")


# ------------------------------------------------ the default is reachable

def test_the_description_says_to_omit_the_index():
    """It said "Omit for all", which reads as "omitting gives you
    everything" -- so the model always passed one."""
    from backend.app.browser.task_flow import BROWSER_EXTRACT_TABLE_SPEC as spec
    param = next(p for p in spec.parameters if p["name"] == "table_index")
    text = param["description"]
    assert "OMIT THIS" in text
    assert "not the data" in text, "say what passing 0 actually gets"
    assert "for all" not in text.lower(), "the wording that caused it"


def test_the_tool_description_says_the_data_table_is_found_for_you():
    """The model reads the tool description before it reads a parameter."""
    from backend.app.browser.task_flow import BROWSER_EXTRACT_TABLE_SPEC as spec
    assert "JUST THE SESSION TOKEN" in spec.description
    assert "do not go hunting through table_index" in spec.description


# --------------------------------------------- a badge is not a ticker

@pytest.mark.parametrize("cell,expected", [
    ("N NBIL", "NBIL"),          # the live Finviz shape
    ("S SNDU", "SNDU"),
    ("E EROC", "EROC"),
    ("NVDA", "NVDA"),            # untouched
    ("US Steel", "US Steel"),    # not a repeat of its own first letter
    ("JP Morgan", "JP Morgan"),
    ("A Aardvark", "Aardvark"),  # is a repeat, so it goes
    ("", ""),
])
def test_a_leading_badge_letter_is_dropped(cell, expected):
    """Only when a lone leading letter repeats the token after it. That
    is what a badge looks like and what a two-word name does not."""
    with playwright.sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_page()
        from backend.app.browser.observation import _DATA_FN_JS
        body = _DATA_FN_JS[_DATA_FN_JS.index("const key ="):]
        body = body[:body.index("\n  //", 40)] if "\n  //" in body[40:] else body
        got = page.evaluate(
            "(s) => { " + _DATA_FN_JS[_DATA_FN_JS.index("const key ="):
                                      _DATA_FN_JS.index("};", _DATA_FN_JS.index("const key =")) + 2]
            + " return key(s); }", cell)
        b.close()
    assert got == expected


def test_the_row_keys_of_a_badged_table_are_real_tickers():
    """End to end on the shape Finviz actually serves."""
    html = ("<html><body><table>"
            "<tr><th>No.</th><th>Ticker</th><th>Perf Week</th><th>Price</th></tr>"
            "<tr><td>1</td><td>N NBIL</td><td>61.10</td><td>9.02</td></tr>"
            "<tr><td>2</td><td>S SNDU</td><td>58.30</td><td>6.11</td></tr>"
            "<tr><td>3</td><td>EROC</td><td>55.90</td><td>22.75</td></tr>"
            "<tr><td>4</td><td>CAPR</td><td>49.80</td><td>13.09</td></tr>"
            "</table></body></html>")
    with playwright.sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_page()
        page.set_content(html)
        from backend.app.browser.observation import _DATA_JS
        data = page.evaluate(_DATA_JS)
        b.close()
    keys = (data or {}).get("keys") or []
    assert "NBIL" in keys and "SNDU" in keys, keys
    assert not any(k.startswith("N N") or k.startswith("S S") for k in keys), keys
