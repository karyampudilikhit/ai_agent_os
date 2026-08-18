"""Did the ANSWER change, or merely the page?

Every check that came before this compared page views - the element
list, the URL, the chrome, the text. None compared the DATA, and the
gap was not theoretical:

  - a run navigated to "?sort=Perf%20%25&order=desc&timeframe=1W", a
    parameter TradingView silently ignores. The page loaded, the address
    bar read as sorted, the rows were the defaults. Nothing flagged it.
  - a click on a toolbar FILTER opened a dropdown. The page changed. It
    counted as progress.
  - RANKED_RESULT was satisfied by "an interaction changed the page and a
    read followed", which proves the page was OPERATED, not SORTED. A run
    satisfied it and shipped the default market-cap list as top gainers.

Two design corrections in here were forced by measuring a live page
rather than reasoning about one, and both are load-bearing:

  1. Comparing "did the same rows come back reordered" fails on exactly
     the tables that matter - a 100-row table sampled 14 deep has every
     sampled row REPLACED by a sort, so a sort and a filter look
     identical. Monotonicity of the numbers is what a sort means.
  2. "Is some column sorted?" answers YES ON THE DEFAULT VIEW, because
     TradingView's screener arrives sorted by market cap. It would have
     passed the exact run it exists to reject. What proves a ranking was
     produced is that the ordering CHANGED.

Offline. A scripted adapter returns canned decisions and a stubbed
_execute returns canned pages.
"""
from __future__ import annotations

from test_browser_operation import (  # the shared offline harness
    BROWSE_TASK,
    DEFAULT_VIEW,
    SORTED_VIEW,
    _call,
    _executor,
)

SORTED_BY_CAP = (
    "URL: https://tradingview.com/screener\nTITLE: Screener\n\n"
    'INTERACTIVE ELEMENTS:\n  e1 button "Overview"\n\n'
    "DATA: 100 row(s) | NVDA · AAPL · GOOG · MSFT · AMZN\n"
    "SORTED: Mkt cap descending"
)
# Same rows, same order - a filter panel opened and nothing else.
FILTER_OPENED = (
    "URL: https://tradingview.com/screener\nTITLE: Screener\n\n"
    'INTERACTIVE ELEMENTS:\n  e1 button "Overview"\n  e9 button "Filter"\n'
    '  e10 combobox "Period"\n\n'
    "DATA: 100 row(s) | NVDA · AAPL · GOOG · MSFT · AMZN\n"
    "SORTED: Mkt cap descending"
)
# The invented URL parameter: different address, identical data.
FAKE_URL = (
    "URL: https://tradingview.com/screener?sort=Perf%20%25&order=desc\n"
    'TITLE: Screener\n\nINTERACTIVE ELEMENTS:\n  e1 button "Overview"\n\n'
    "DATA: 100 row(s) | NVDA · AAPL · GOOG · MSFT · AMZN\n"
    "SORTED: Mkt cap descending"
)
REALLY_SORTED = (
    "URL: https://tradingview.com/screener\nTITLE: Screener\n\n"
    'INTERACTIVE ELEMENTS:\n  e1 button "Overview"\n\n'
    "DATA: 100 row(s) | TREVQ · ETBI · IOBTQ · SHWZ · EKSN\n"
    "SORTED: Chg % descending"
)
NO_TABLE = (
    "URL: https://example.test/contact\nTITLE: Contact\n\n"
    'INTERACTIVE ELEMENTS:\n  e1 textbox "Email"\n\n'
    "DATA: (no data table on this page)"
)


# ------------------------------------------- stage 1: the fingerprint itself

def test_the_fingerprint_round_trips():
    from backend.app.browser.observation import parse_data_keys, parse_sorted_columns
    assert parse_data_keys(SORTED_BY_CAP) == ["NVDA", "AAPL", "GOOG", "MSFT", "AMZN"]
    assert parse_sorted_columns(SORTED_BY_CAP) == [
        {"column": "Mkt cap", "direction": "descending"}]


def test_missing_evidence_is_unknown_never_unchanged():
    """The property the whole design rests on.

    A page with no table, or a result recorded before the fingerprint
    existed, must answer UNKNOWN - never "the rows did not change".
    Treating missing evidence as passing evidence is the shape of every
    guard this codebase has had to rewrite.

    It earned itself on the first live run, which caught the page
    mid-load: the check said unknown instead of guessing."""
    from backend.app.browser.observation import rows_changed
    assert rows_changed(SORTED_BY_CAP, NO_TABLE) is None
    assert rows_changed(SORTED_BY_CAP, "no fingerprint at all") is None
    assert rows_changed(NO_TABLE, NO_TABLE) is None


def test_row_keys_are_compared_not_cell_values():
    """A screener's prices tick every second. Comparing values would
    report "changed" on every observation and silently disable everything
    built on top - the same way browser_find's output would have, had it
    been used as the page-comparison baseline."""
    from backend.app.browser.observation import rows_changed
    assert rows_changed(SORTED_BY_CAP, SORTED_BY_CAP) is False
    assert rows_changed(SORTED_BY_CAP, FAKE_URL) is False, "different URL, same rows"
    assert rows_changed(SORTED_BY_CAP, REALLY_SORTED) is True


def test_sort_must_have_CHANGED_not_merely_exist():
    """The correction that makes this worth anything. Measured live:
    TradingView's screener ARRIVES sorted by market cap descending, so
    "is some column in order?" answers yes on the very view this rejects."""
    from backend.app.browser.observation import is_sorted_somehow, sort_changed
    assert is_sorted_somehow(SORTED_BY_CAP), "the default view genuinely IS sorted"
    assert sort_changed(SORTED_BY_CAP, FAKE_URL) is False
    assert sort_changed(SORTED_BY_CAP, FILTER_OPENED) is False
    assert sort_changed(SORTED_BY_CAP, REALLY_SORTED) is True
    assert sort_changed(SORTED_BY_CAP, NO_TABLE) is None


# --------------------------------- stage 2: the loop says what did not move

def test_a_page_that_moved_without_the_rows_is_called_out():
    """The filter-dropdown case. The page genuinely changed, so every
    page-view check reads it as progress. The rows say otherwise."""
    ex = _executor(
        [_call("action.browser_observe"),
         _call("action.browser_click_element", session_token="t", element_id="e9"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": SORTED_BY_CAP,
         "action.browser_click_element": FILTER_OPENED},
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert "THE ROWS DID NOT" in out
    assert "default view" in out


def test_a_url_the_site_ignored_is_called_out():
    """The most misleading result seen live. A navigation was not treated
    as acting on the page at all, so nothing checked what it produced."""
    ex = _executor(
        [_call("action.browser_observe"),
         _call("action.browser_navigate", url="https://x.test/?sort=perf&order=desc"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": SORTED_BY_CAP,
         "action.browser_navigate": FAKE_URL},
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert "THE ROWS DID NOT" in out


def test_a_real_sort_is_not_second_guessed():
    ex = _executor(
        [_call("action.browser_observe"),
         _call("action.browser_click_element", session_token="t", element_id="e2"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": SORTED_BY_CAP,
         "action.browser_click_element": REALLY_SORTED},
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert "ROWS DID NOT" not in out
    assert "byte-for-byte" not in out


def test_a_page_with_no_table_falls_back_to_the_page_comparison():
    """A form or a dashboard has no rows. The data check must go quiet
    rather than nag, and the older page check still stands."""
    ex = _executor(
        [_call("action.browser_observe"),
         _call("action.browser_click_element", session_token="t", element_id="e1"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": NO_TABLE,
         "action.browser_click_element": NO_TABLE},
    )
    out = ex.run("open the contact page and find the best email field",
                 role="Web Operator") or ""
    assert "ROWS DID NOT" not in out, "there are no rows to comment on"
    assert "byte-for-byte the same" in out, "the page check still applies"


# ------------------------- stage 3: the contract requires a PRODUCED ranking

def _c(tool, at, view, ok=True):
    return {"tool": tool, "at": at, "ok": ok, "args_text": "{}", "output": view}


def test_reading_a_page_that_arrived_sorted_is_not_a_ranking():
    """The whole point. Every call succeeded, the page changed twice, and
    the table is in exactly the order the site served it in."""
    from backend.app.orchestrator.output_contract import RANKED_RESULT, satisfied_kinds
    trace = [
        _c("action.browser_navigate", 1, SORTED_BY_CAP),
        _c("action.browser_observe", 2, SORTED_BY_CAP),
        _c("action.browser_click_element", 3, FILTER_OPENED),
        _c("action.browser_navigate", 4, FAKE_URL),
        _c("action.browser_extract_table", 5, FAKE_URL),
    ]
    assert RANKED_RESULT not in satisfied_kinds(set(), False, trace)


def test_changing_the_order_and_then_reading_is_a_ranking():
    from backend.app.orchestrator.output_contract import RANKED_RESULT, satisfied_kinds
    trace = [
        _c("action.browser_navigate", 1, SORTED_BY_CAP),
        _c("action.browser_observe", 2, SORTED_BY_CAP),
        _c("action.browser_click_element", 3, REALLY_SORTED),
        _c("action.browser_extract_table", 4, REALLY_SORTED),
    ]
    assert RANKED_RESULT in satisfied_kinds(set(), False, trace)


def test_sorting_after_reading_is_not_a_ranking():
    """The reported figures in the failed run came from an extract taken
    before anything was touched."""
    from backend.app.orchestrator.output_contract import RANKED_RESULT, satisfied_kinds
    trace = [
        _c("action.browser_observe", 1, SORTED_BY_CAP),
        _c("action.browser_extract_table", 2, SORTED_BY_CAP),
        _c("action.browser_click_element", 9, REALLY_SORTED),
    ]
    assert RANKED_RESULT not in satisfied_kinds(set(), False, trace)


def test_a_run_with_no_data_table_is_never_asked_for_a_ranking():
    """"write the best headline" and "summarise the top findings" both
    match task_wants_ranking. A gate that fails honest work is worse than
    the bug it prevents."""
    from backend.app.orchestrator.output_contract import run_saw_a_data_table
    assert not run_saw_a_data_table([_c("action.browser_navigate", 1, NO_TABLE)])
    assert not run_saw_a_data_table([])
    assert run_saw_a_data_table([_c("action.browser_navigate", 1, SORTED_BY_CAP)])


def test_a_trace_without_measurements_keeps_the_old_behaviour():
    """Strictly additive. A transcript recorded before the fingerprint
    existed must not start failing for want of a field that did not
    exist when it ran."""
    from backend.app.orchestrator.output_contract import RANKED_RESULT, satisfied_kinds
    trace = [
        _c("action.browser_observe", 1, DEFAULT_VIEW),
        _c("action.browser_click_element", 2, SORTED_VIEW),
        _c("action.browser_extract_table", 3, SORTED_VIEW),
    ]
    assert RANKED_RESULT in satisfied_kinds(set(), False, trace)


# ------------------------------------- stage 4: rows must come from somewhere

def test_a_row_that_was_never_on_any_page_is_caught():
    """Number provenance proves a FIGURE was computed. This proves a ROW
    was seen. A live run reported ten tickers where every ticker and price
    was real and five percentages had been altered to read as gainers -
    the figures were checkable, the records were not."""
    from backend.app.critique.compute_gate import unbacked_row_labels
    deliverable = (
        "| Ticker | Company | Weekly % | Price |\n|---|---|---|---|\n"
        "| TREVQ | Trevali Mining | +38.10% | 12.40 |\n"
        "| ETBI | Eastgate Bio | +31.70% | 3.02 |\n"
        "| FAKEX | Not Real Corp | +29.00% | 8.10 |\n"
        "| ALSOFAKE | Also Invented | +22.00% | 1.10 |\n"
    )
    captured = "DATA: 100 row(s) | TREVQ · ETBI · IOBTQ · SHWZ"
    assert unbacked_row_labels(deliverable, captured) == ["FAKEX", "ALSOFAKE"]


def test_rows_that_were_all_read_are_not_flagged():
    from backend.app.critique.compute_gate import unbacked_row_labels
    deliverable = "| Ticker | % |\n|---|---|\n| TREVQ | +38.1 |\n| ETBI | +31.7 |\n"
    assert unbacked_row_labels(deliverable, "TREVQ Trevali · ETBI Eastgate") == []


def test_prose_tables_are_left_alone():
    """It must not fire on a table of findings, recommendations, or any
    other writing that happens to use markdown pipes."""
    from backend.app.critique.compute_gate import reported_row_labels
    prose = (
        "| Finding | Detail |\n|---|---|\n"
        "| The market is crowded | many entrants |\n"
        "| Pricing is unclear | no public tiers |\n"
    )
    assert reported_row_labels(prose) == []


def test_a_header_row_is_not_a_data_row():
    from backend.app.critique.compute_gate import reported_row_labels
    assert reported_row_labels("| TICKER | X |\n|---|---|\n| AAPL | 1 |") == ["AAPL"]


def test_nothing_captured_means_no_accusation():
    """No evidence is not evidence of fabrication."""
    from backend.app.critique.compute_gate import unbacked_row_labels
    assert unbacked_row_labels("| AAPL | 1 |", "") == []


def test_the_gate_wires_both_new_checks():
    import inspect
    from backend.app.api import routes
    src = inspect.getsource(routes._gate_deliverable)
    assert "unbacked_row_labels" in src
    assert "run_saw_a_data_table" in src, (
        "must not demand a ranking of a run that never saw a table")


# ------------------- the four defects the design review found, measured

def test_the_fingerprint_is_visible_to_the_model():
    """Defect 1. It was emitted LAST, and on a 120-element page that put
    it at character 7068 -- while the model is only ever shown the first
    MAX_OBSERVATION_CHARS of a tool result. The loop would have told a
    model "the rows did not move" with the evidence truncated away, and
    the check would have worked on sparse pages and silently stopped on
    dense ones."""
    from backend.app.browser.observation import Observation
    from backend.app.orchestrator.execution_loop import MAX_OBSERVATION_CHARS
    els = [{"id": f"e{i}", "role": "button",
            "name": f"Market capitalization {i} - Sort ascending"}
           for i in range(1, 121)]
    rendered = Observation({"url": "https://x.test", "title": "Screener",
                            "elements": els, "text": "",
                            "data_keys": ["NVDA", "AAPL"], "data_total": 100,
                            "sorted_columns": []}, 1).render(include_text=False)
    offset = rendered.find("DATA:")
    assert 0 < offset < MAX_OBSERVATION_CHARS, (
        f"the fingerprint sits at {offset}, past the {MAX_OBSERVATION_CHARS}"
        " the model is shown")


def test_an_ordinal_column_can_never_be_the_row_key():
    """Defect 2. A rank column is maximally DISTINCT, so the old rule
    never rejected it -- and it reads 1,2,3... whatever order the rows
    are in, so the fingerprint reported "no change" across a genuine
    re-sort. Verified in headless Chromium over five table shapes; see
    verify_fingerprint_shapes.py."""
    from backend.app.browser.observation import _DATA_FN_JS
    assert "isOrdinal" in _DATA_FN_JS
    assert "if (isOrdinal(cells)) continue;" in _DATA_FN_JS


def test_an_ordinal_column_is_never_reported_as_sorted():
    """Defect 3. The same rank column is perfectly monotonic, so the
    sortedness scan reported "SORTED: # ascending" on an untouched table
    -- which would have passed the default view all over again, the exact
    bug the sort_changed correction already fixed once."""
    from backend.app.browser.observation import _DATA_FN_JS
    body = _DATA_FN_JS[_DATA_FN_JS.index("const sorted = []"):]
    assert "if (c === keyCol) continue;" in body
    assert "isOrdinal(cells)" in body, "one definition of 'this is a position'"


def test_every_page_view_tool_carries_the_fingerprint():
    """Defect 4. browser_navigate, browser_extract, browser_extract_table
    and browser_click build their own output and never call observe_page,
    so they carried no fingerprint at all. It mattered most for navigate:
    going to a sort parameter the site ignores happens entirely inside
    that tool, so the check was blind to its own motivating case."""
    import inspect
    from backend.app.browser import task_flow
    src = inspect.getsource(task_flow)
    assert src.count("_fp(session.page)") == 4, (
        "navigate, extract, extract_table and click must all carry it")
    for fn in ("_browser_navigate_impl", "_browser_extract_impl",
               "_browser_extract_table_impl", "_browser_click_impl"):
        assert "_fp(session.page)" in inspect.getsource(getattr(task_flow, fn)), fn


def test_the_fingerprint_has_exactly_one_definition():
    """Two copies would drift, and this codebase has been bitten by one
    list living in two files before."""
    from backend.app.browser.observation import _DATA_FN_JS, _DATA_JS, _OBSERVE_JS
    assert "__DATA_FN__" not in _OBSERVE_JS, "placeholder must be interpolated"
    assert _DATA_FN_JS in _OBSERVE_JS, "the observer uses the shared function"
    assert _DATA_FN_JS in _DATA_JS, "so does the standalone form"


# ---------------- a citation must not be broken by markdown formatting

def test_a_url_in_a_code_span_is_extracted_cleanly():
    """Caught live, and it was a FALSE ACCUSATION -- the worst kind.

    A run opened https://in.indeed.com/jobs?q=product+manager&l=India&sort=date
    and wrote it into the deliverable inside a markdown code span. The
    extractor kept the closing backtick, the URL never matched the one
    the run had really fetched, and the run was blocked for citing a page
    it had genuinely visited."""
    from backend.app.tools.source_ledger import extract_urls
    md = ("See `https://in.indeed.com/jobs?q=product+manager&l=India&sort=date` "
          "for the listings.")
    assert extract_urls(md) == [
        "https://in.indeed.com/jobs?q=product+manager&l=India&sort=date"]


def test_other_markdown_punctuation_is_stripped_too():
    from backend.app.tools.source_ledger import extract_urls
    assert extract_urls("**https://example.com/a**") == ["https://example.com/a"]
    assert extract_urls("ends here https://example.com/b.") == ["https://example.com/b"]
    assert extract_urls("“https://example.com/c”") == ["https://example.com/c"]
    assert extract_urls("[label](https://example.com/d)") == ["https://example.com/d"]


def test_a_genuinely_visited_url_is_not_reported_as_fabricated():
    from backend.app.tools.source_ledger import SourceLedger
    led = SourceLedger()
    led.record_fetched("https://in.indeed.com/jobs?q=product+manager&l=India&sort=date")
    text = ("Source: `https://in.indeed.com/jobs?q=product+manager&l=India&sort=date`")
    assert led.unretrieved_urls(text) == []
