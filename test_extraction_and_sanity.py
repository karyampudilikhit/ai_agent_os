"""The last mile: get the rows out, and refuse a ranking of junk.

Three failures, all measured on live runs:

  - browser_extract_table read 100 rows and THREE reached the model. The
    transcript capped a data result at the same 1500 characters it uses
    for "what is on this page", so the agent reported three gainers and
    wrote "unknown" for the rest, blaming the site. The rows were there.
  - a sorted table answers at BOTH ends -- top rows are the gainers,
    bottom rows are the losers -- and a flat character cut keeps only the
    first. The same run reported "the table truncates before the bottom 5
    appear".
  - job boards do not use tables. extract_table returned nothing on
    Wellfound and the run reported "zero listings captured", which was
    honest and useless.

And the gap underneath all three: nothing asked whether the answer made
SENSE. A correctly-sorted list of delisted sub-penny shells passed every
guard in the system.
"""
from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.sync_api")

JUNK_RANKING = (
    "| Ticker | Company | Weekly % | Price |\n"
    "|---|---|---|---|\n"
    "| VALV | Shengkai Innovations | +9,999,900.00% | 0.1000 USD |\n"
    "| GENNQ | Genesis Healthcare | +4,699,900.00% | 0.0470 USD |\n"
    "| TREVQ | Trevali Mining | +999,900.00% | 0.0100 USD |\n"
)
REAL_RANKING = (
    "| Ticker | Company | Weekly % | Price |\n"
    "|---|---|---|---|\n"
    "| NVDA | NVIDIA | +12.40% | 225.16 USD |\n"
    "| AAPL | Apple Inc. | +8.10% | 305.93 USD |\n"
    "| MSFT | Microsoft | -4.20% | 495.40 USD |\n"
)
RANK_TASK = "top 5 gainers and losers by weekly percentage change"


# ----------------------------------------------- the rows reach the model

def test_a_data_result_gets_far_more_room_than_a_page_description():
    """1500 characters is a sensible budget for "what is on this page"
    and a catastrophic one for "here are the rows"."""
    from backend.app.orchestrator.execution_loop import (
        MAX_DATA_OBSERVATION_CHARS, MAX_OBSERVATION_CHARS, _obs_cap,
    )
    assert MAX_DATA_OBSERVATION_CHARS >= 6 * MAX_OBSERVATION_CHARS
    assert _obs_cap("action.browser_extract_table") == MAX_DATA_OBSERVATION_CHARS
    assert _obs_cap("action.browser_extract_records") == MAX_DATA_OBSERVATION_CHARS
    assert _obs_cap("action.browser_observe") == MAX_OBSERVATION_CHARS


def test_both_ends_of_a_sorted_table_survive():
    """The gainers are at the top and the losers at the bottom. A flat
    truncation keeps only the gainers, and then the run reports that the
    site hid the losers."""
    from backend.app.browser.task_flow import _render_tables
    rows = [[f"ROW{i}", f"{i}%"] for i in range(1, 101)]
    out = _render_tables([{"index": 0, "rows": rows}], 14000, head=3, tail=3)
    assert "ROW1 |" in out and "ROW3 |" in out
    assert "ROW98 |" in out and "ROW100 |" in out
    assert "ROW50" not in out
    assert "middle row(s) omitted" in out, "the gap is stated, never silent"


def test_a_short_table_is_not_split():
    from backend.app.browser.task_flow import _render_tables
    rows = [[f"R{i}", str(i)] for i in range(1, 6)]
    out = _render_tables([{"index": 0, "rows": rows}], 14000, head=12, tail=12)
    assert "omitted" not in out
    for i in range(1, 6):
        assert f"R{i} |" in out


def test_extract_table_offers_head_and_tail():
    from backend.app.browser.task_flow import BROWSER_EXTRACT_TABLE_SPEC as spec
    names = {p["name"] for p in spec.parameters}
    assert {"head", "tail"} <= names
    assert "LAST" in spec.description, "the model has to know it gets both ends"


# ------------------------------------- records that are not in a table

JOB_BOARD = """
<html><body><main>
  <div class="results">
    <div class="card"><h3>Senior Product Manager</h3>
      <a href="https://fixture.test/job/1">View</a>
      <p>Acme Corp - Bengaluru, India - Posted 2 days ago</p></div>
    <div class="card"><h3>Product Manager, Growth</h3>
      <a href="https://fixture.test/job/2">View</a>
      <p>Globex - Mumbai, India - Posted 3 days ago</p></div>
    <div class="card"><h3>Associate Product Manager</h3>
      <a href="https://fixture.test/job/3">View</a>
      <p>Initech - Remote, India - Posted 5 days ago</p></div>
    <div class="card"><h3>Group Product Manager</h3>
      <a href="https://fixture.test/job/4">View</a>
      <p>Hooli - Hyderabad, India - Posted 1 week ago</p></div>
  </div>
</main></body></html>
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
            p.goto("https://fixture.test/board")

        p.serve = serve
        yield p
        b.close()


def _run(fn, page, args):
    """_extract_records_impl resolves its session through the manager, so
    the lookup is stubbed rather than a real session registered."""
    from backend.app.browser import primitives

    class Sess:
        pass

    s = Sess()
    s.page = page
    s.token = "t"
    s.touch = lambda: None

    real = primitives._session
    primitives._session = lambda a: (s, None)
    try:
        return fn(args)
    finally:
        primitives._session = real


def test_repeated_cards_are_extracted(page):
    """Wellfound rendered its listings this way, extract_table returned
    nothing, and the run reported zero listings captured."""
    from backend.app.browser.primitives import _extract_records_impl
    page.serve(JOB_BOARD)
    out = _run(_extract_records_impl, page, {"session_token": "t"})
    assert "Senior Product Manager" in out
    assert "Group Product Manager" in out
    assert "fixture.test/job/1" in out
    assert "Bengaluru" in out


def test_a_page_with_no_records_says_so(page):
    from backend.app.browser.primitives import _extract_records_impl
    page.serve("<html><body><h1>Just a heading</h1><p>one line</p></body></html>")
    out = _run(_extract_records_impl, page, {"session_token": "t"})
    assert "NO REPEATED RECORDS FOUND" in out


def test_records_is_registered_and_says_when_to_use_it():
    from backend.app.browser.primitives import ALL_SPECS
    spec = next(s for s in ALL_SPECS if s.name == "browser_extract_records")
    assert "does NOT use a table" in spec.description
    assert "browser_extract_table returns nothing" in spec.description


# ------------------------------------------------- is the answer sane

def test_a_ranking_of_impossible_moves_is_rejected():
    from backend.app.orchestrator.plausibility import check
    problem = check(RANK_TASK, JUNK_RANKING)
    assert problem
    assert "not a market move" in problem
    assert "filter" in problem


def test_a_real_ranking_passes():
    from backend.app.orchestrator.plausibility import check
    assert check(RANK_TASK, REAL_RANKING) is None


def test_only_rankings_are_judged():
    """An outlier is a curiosity in prose and the whole answer in a
    ranking, because sorting by a broken figure puts the broken rows on
    top by construction."""
    from backend.app.orchestrator.plausibility import check
    assert check("summarise this page for me", JUNK_RANKING) is None


def test_sub_penny_rows_are_caught_even_without_wild_percentages():
    from backend.app.orchestrator.plausibility import untradeable_rows
    rows = ("| AAAA | Shell One | +40.00% | 0.0004 USD |\n"
            "| BBBB | Shell Two | +38.00% | 0.0009 USD |")
    assert untradeable_rows(rows) == ["AAAA", "BBBB"]


def test_a_normal_price_is_not_flagged():
    from backend.app.orchestrator.plausibility import untradeable_rows
    assert untradeable_rows("| NVDA | NVIDIA | +12.40% | 225.16 USD |") == []


def test_the_loop_can_correct_it_and_the_gate_can_block_it():
    """At the gate this only fails the run; in the loop it can still be
    fixed by filtering the page."""
    import inspect
    from backend.app.api import routes
    from backend.app.orchestrator import execution_loop
    assert "plausibility" in inspect.getsource(routes._gate_deliverable)
    assert "plausibility" in inspect.getsource(execution_loop.AgenticExecutor.run)


def test_the_retry_note_says_filter_not_delete():
    """Dropping the bad rows just promotes the next ones, which are the
    same kind of thing."""
    from backend.app.orchestrator.plausibility import RETRY_NOTE
    assert "FILTER THE PAGE FIRST" in RETRY_NOTE
    assert "Do not simply delete" in RETRY_NOTE
