"""Do not ask an unchanged page the same question five times.

Measured on a live screener run. The agent needed to sort by one-month
performance, and searched the page for the control five separate times:

    "1 month performance column"
    "performance column header"
    "sort by month"
    "monthly change column"
    "sort control"

Every one came back NO MATCH -- correctly, because that view of the page
has no such control. Five of the run's calls went on re-asking a
question the page had already answered.

Three guards were watching and not one could see it. The repeat guard
fingerprints (tool, arguments) and all five argument strings differed.
Instrument memory keys on host x instrument, and the browser was reading
that host perfectly -- there was simply nothing to find. The consecutive
failure guard counts calls that ERRORED, and NO MATCH is a successful
call: it answers the question asked.

The fixtures below are built by calling the code that writes these
messages, not by typing out what it probably writes. Three bugs shipped
in this codebase passed hand-written fixtures and failed in production
because the real output had a different shape.
"""
from __future__ import annotations

import pytest

from backend.app.browser.observation import DATA_PREFIX, _EMPTY_TABLE, _NO_TABLE
from backend.app.browser.policy import wrap_untrusted
from backend.app.orchestrator import strategy_memory as sm

SCREENER = "https://www.tradingview.com/screener/"


def no_match(url: str, query: str, scanned: int = 412) -> str:
    """browser_find's real answer when nothing on the page matches.

    Mirrors observation.render_matches. The load-bearing line is the
    second one, and it is quoted from that function so a rewording there
    shows up here as a failing test rather than as a silent guard.
    """
    return wrap_untrusted("\n".join([
        f'SEARCHED {scanned} element(s) on {url} for "{query}".',
        "NO MATCH. Nothing on this page answers that description.",
        "",
        "Rewording is unlikely to help — this searched every element.",
    ]), url)


def matched(url: str, query: str) -> str:
    return wrap_untrusted("\n".join([
        f'SEARCHED 412 element(s) on {url} for "{query}".',
        "2 match(es):",
        '  e17  columnheader "Perf %"',
    ]), url)


def page_view(url: str, title: str, rows: int = 30, marker: str = "") -> str:
    """A page view in the shape observation.render actually emits."""
    data = f"{DATA_PREFIX} {rows} row(s) | Ticker · Price"
    return "\n".join([
        f"URL: {url}", f"TITLE: {title}", data, marker,
        "", "INTERACTIVE ELEMENTS:", '  e1  link "Screener"',
    ])


@pytest.fixture(autouse=True)
def _fresh():
    sm.reset()
    yield
    sm.reset()


# ------------------------------------------------------------ the facts

def test_a_no_match_is_recorded_as_finding_nothing():
    assert sm.looks_fruitless(no_match(SCREENER, "sort control"))


def test_a_real_match_is_not():
    assert not sm.looks_fruitless(matched(SCREENER, "perf column"))


@pytest.mark.parametrize("body", [
    "NO REPEATED RECORDS FOUND. This page does not show a list of similar items",
    '(browser_click: could not find anything matching "Sort" to click)',
    "(no headings found on https://example.com. The page may build its",
])
def test_the_other_tools_ways_of_saying_nothing_here(body):
    assert sm.looks_fruitless(body)


@pytest.mark.parametrize("body", [f"{DATA_PREFIX} {_NO_TABLE}",
                                  f"{DATA_PREFIX} {_EMPTY_TABLE}"])
def test_no_table_here_counts_only_from_the_tool_that_wanted_a_table(body):
    """This one is also part of the page FINGERPRINT, which every
    navigation, click and scroll carries. Counted unconditionally, an
    ordinary click on an ordinary page with no table would file a failed
    approach -- and three of those would refuse the fourth."""
    assert sm.looks_fruitless(body, "action.browser_extract_table")
    assert not sm.looks_fruitless(body, "action.browser_click")
    assert not sm.looks_fruitless(body)


def test_a_click_on_a_table_less_page_is_not_a_failed_approach():
    """The same bug, through the front door."""
    mem = sm.get_memory()
    url = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    clicked = "\n".join([f"URL: {url}", "TITLE: Python - Wikipedia",
                         f"{DATA_PREFIX} {_NO_TABLE}", "",
                         "INTERACTIVE ELEMENTS:", '  e1  link "History"'])
    for target in ("History", "Syntax", "Libraries"):
        mem.record("action.browser_click", url, target, clicked, clicked)
    assert mem.note_for("action.browser_click", url, "Implementations") is None
    assert mem.refusal_for("action.browser_click", url, "Implementations",
                           clicked) is None


def test_a_page_cannot_file_a_verdict_about_our_tools():
    """Every marker carries punctuation or a tool name a site cannot write.

    instrument_memory learned this the hard way: a bare "could not fetch"
    matched an article sentence and recorded a wall from a page that had
    been read successfully.
    """
    prose = wrap_untrusted(
        "We could not find anything matching your search. No match was "
        "found for that query, and no data table on the page you wanted.",
        "https://example.com/help")
    assert not sm.looks_fruitless(prose)


# --------------------------------------------------------- what intent is

def test_wording_is_stripped_out():
    a = sm.subject_words("the date filter")
    b = sm.subject_words("filter by date posted")
    assert sm.same_intent(a, b)


def test_different_questions_stay_different():
    assert not sm.same_intent(sm.subject_words("date filter"),
                              sm.subject_words("next pagination button"))


def test_a_shared_word_alone_is_not_the_same_question():
    """"sort by date" and "date filter" share one word out of two, which
    is below the bar on purpose: silence is the safe default, and a
    wrongly-collapsed intent silences a search that should have run."""
    assert not sm.same_intent(sm.subject_words("sort by date"),
                              sm.subject_words("date filter"))


def test_plurals_are_one_word():
    assert sm.subject_words("column headers") == sm.subject_words("column header")


# ------------------------------------------------- the five-call screener

# The five queries the live run actually made, in order.
FIVE = ["1 month performance column", "performance column header",
        "sort by month", "monthly change column", "sort control"]


def replay(mem, queries, view):
    """Make each search in turn, recording what the page answered.

    Returns what the guard would have said BEFORE each call, so the test
    reads in the order the run happened.
    """
    verdicts = []
    for q in queries:
        verdicts.append((
            mem.refusal_for("action.browser_find", SCREENER, q, view),
            mem.note_for("action.browser_find", SCREENER, q),
        ))
        mem.record("action.browser_find", SCREENER, q,
                   no_match(SCREENER, q), view)
    return verdicts


def test_the_second_ask_gets_a_note():
    mem = sm.get_memory()
    view = page_view(SCREENER, "Screener")
    mem.record("action.browser_find", SCREENER, FIVE[0],
               no_match(SCREENER, FIVE[0]), view)
    note = mem.note_for("action.browser_find", SCREENER, FIVE[1])
    assert note and "already looked for this on this page" in note
    assert FIVE[0] in note


def test_asking_the_same_question_a_third_time_is_refused():
    mem = sm.get_memory()
    view = page_view(SCREENER, "Screener")
    for q in ("the date filter", "filter by date posted"):
        mem.record("action.browser_find", SCREENER, q,
                   no_match(SCREENER, q), view)
    refusal = mem.refusal_for("action.browser_find", SCREENER,
                              "date posted filter", view)
    assert refusal and refusal.startswith("(REFUSED:")
    assert "same thing 2 times" in refusal
    assert "has not changed" in refusal


def test_the_whole_screener_run_is_stopped_by_its_fourth_search():
    """The acceptance test for this module: the real five, in order.

    A note by the second, and the fourth refused -- so the run spends
    three calls learning the page has no sort control instead of five,
    and is told what to do instead rather than simply blocked.
    """
    mem = sm.get_memory()
    view = page_view(SCREENER, "Screener")
    verdicts = replay(mem, FIVE, view)

    assert verdicts[0] == (None, None)              # nothing known yet
    assert verdicts[1][1] is not None               # noted by the second
    assert verdicts[2][1] is not None               # and again by the third
    assert all(r is None for r, _ in verdicts[:3])  # none of them blocked
    assert verdicts[3][0] is not None               # the fourth is refused
    assert "3 times" in verdicts[3][0]


def test_but_a_page_that_moved_is_a_new_question():
    """A tab opened, a menu expanded, a scroll loaded more rows -- and
    the same words now ask something the page has not answered.

    This is the escape hatch that keeps a hard stop honest, and it is
    the same test the repeat guard already uses.
    """
    mem = sm.get_memory()
    before = page_view(SCREENER, "Screener")
    replay(mem, FIVE[:3], before)
    after = page_view(SCREENER, "Screener",
                      marker="  e88  button 'Performance' [expanded]")
    assert mem.refusal_for("action.browser_find", SCREENER, FIVE[3], after) is None
    assert mem.note_for("action.browser_find", SCREENER, FIVE[3]) is not None


def test_an_unknown_view_is_never_refused():
    """No evidence the page is unchanged means no refusal. A guard with
    no record to read stays quiet."""
    mem = sm.get_memory()
    replay(mem, FIVE[:3], "")
    assert mem.refusal_for("action.browser_find", SCREENER, FIVE[3], "") is None


# ------------------------------------------------------- staying quiet

def test_a_different_question_is_noted_but_never_refused_on_its_own():
    """The page-level rule is about the PAGE, so it reaches a genuinely
    new question too -- as a note. Only the page's own count of empty
    answers can refuse one, and two is not enough."""
    mem = sm.get_memory()
    view = page_view(SCREENER, "Screener")
    replay(mem, FIVE[:2], view)
    note = mem.note_for("action.browser_find", SCREENER, "export to csv button")
    assert note and "answered NOTHING FOUND every time" in note
    assert "already looked for this" not in note
    assert mem.refusal_for("action.browser_find", SCREENER,
                           "export to csv button", view) is None


def test_the_same_question_on_a_different_page_is_untouched():
    mem = sm.get_memory()
    view = page_view(SCREENER, "Screener")
    for q in FIVE[:2]:
        mem.record("action.browser_find", SCREENER, q,
                   no_match(SCREENER, q), view)
    other = "https://finviz.com/screener.ashx"
    assert mem.note_for("action.browser_find", other, FIVE[2]) is None


def test_a_query_string_makes_a_different_page():
    """Putting the sort in the URL is the move the note recommends. If
    that counted as the same page, this memory would refuse the one
    attempt that was going to work."""
    mem = sm.get_memory()
    view = page_view(SCREENER, "Screener")
    for q in FIVE[:2]:
        mem.record("action.browser_find", SCREENER, q,
                   no_match(SCREENER, q), view)
    sorted_view = SCREENER + "?sort=perf_1m"
    assert mem.note_for("action.browser_find", sorted_view, FIVE[2]) is None


def test_verdicts_do_not_cross_families():
    """A control that is not on the page says nothing about whether the
    page can be READ."""
    mem = sm.get_memory()
    view = page_view(SCREENER, "Screener")
    for q in FIVE[:2]:
        mem.record("action.browser_find", SCREENER, q,
                   no_match(SCREENER, q), view)
    assert mem.note_for("action.browser_extract_records", SCREENER,
                        "monthly performance column") is None


def test_a_search_that_worked_is_never_a_failure():
    mem = sm.get_memory()
    view = page_view(SCREENER, "Screener")
    mem.record("action.browser_find", SCREENER, "perf column",
               matched(SCREENER, "perf column"), view)
    assert mem.note_for("action.browser_find", SCREENER,
                        "performance column") is None


def test_the_note_points_at_what_did_work():
    mem = sm.get_memory()
    view = page_view(SCREENER, "Screener")
    mem.record("action.browser_find", SCREENER, "ticker column",
               matched(SCREENER, "ticker column"), view)
    mem.record("action.browser_find", SCREENER, FIVE[0],
               no_match(SCREENER, FIVE[0]), view)
    note = mem.note_for("action.browser_find", SCREENER, FIVE[1])
    assert note and "ticker column" in note and "DID return something" in note


# ------------------------------------------------------------ never raise

@pytest.mark.parametrize("tool,page,query", [
    ("action.browser_find", "", "sort control"),
    ("action.browser_find", SCREENER, ""),
    ("action.run_python", SCREENER, "sort control"),
    ("", "", ""),
])
def test_nothing_to_key_on_means_silence(tool, page, query):
    mem = sm.get_memory()
    mem.record(tool, page, query, no_match(SCREENER, query or "x"), "v")
    assert mem.note_for(tool, page, query) is None
    assert mem.refusal_for(tool, page, query, "v") is None


# --------------------------------------------------- reading a page view

def test_page_in_reads_all_three_shapes_a_view_uses():
    """URL:, Now on: and Source:. All three, because item_state shipped
    reading only two and its guard never fired once in four live runs --
    every extractor wraps its output with a Source: header."""
    assert sm.page_in(page_view(SCREENER, "Screener")) == SCREENER
    assert sm.page_in(f'Now on: "Screener" ({SCREENER})') == SCREENER
    assert sm.page_in(no_match(SCREENER, "x")) == SCREENER


def test_query_in_finds_the_description_whatever_it_is_called():
    assert sm.query_in({"query": "sort control"}) == "sort control"
    assert sm.query_in({"token": "t", "heading": "History"}) == "History"
    assert sm.query_in({"token": "t", "url": "https://x.com"}) == ""


# ---------------------------------------------- asking the web twice
#
# MEASURED ON A LIVE RUN, 2026-08-21. Asked to research ten AI startups,
# the loop made ELEVEN web_search calls, every one successful, every one
# a rewording of "top 10 AI startups in India funding product website".
# Twenty-two steps spent and not one source opened.
#
# This module saw none of it: web_search was in no family, so family_of
# returned "" and the memory returned before doing anything. The repeat
# guard saw eleven different argument strings, as it always does.
#
# A search differs from every other attempt here in two ways, and both
# had to be built in rather than assumed away:
#   it is not scoped to a page  -- the web is the scope
#   a SUCCESSFUL one still repeats -- the corpus does not change between
#                                     two identical questions

RUN4 = [
    "top 10 AI startups in India funding product website",
    "top 10 AI startups in India 2025",
    "top 10 AI startups in India 2025 list",
    "top 10 AI startups India 2025 Inc42",
    "top 10 AI startups in India 2025 funding product",
    "top AI startups India 2025 list funding product Inc42 OR YourStory",
]
RESULTS = ("[web search — 'x']\n3 result(s). These are SNIPPETS a search "
           "engine returned.\n--- result 1 ---\ntitle: Something\n")


def test_web_search_is_a_family_now():
    assert sm.family_of("action.web_search") == sm.SEARCH


def test_a_search_is_not_scoped_to_whatever_page_was_open():
    """Run 4's first searches happened before any browser page existed.
    Keyed on the page, they recorded nothing at all."""
    mem = sm.get_memory()
    mem.record("action.web_search", "", RUN4[0], RESULTS, "")
    assert mem.note_for("action.web_search", "", RUN4[1]) is not None
    # And the same question from a page is still the same question.
    assert mem.note_for("action.web_search", SCREENER, RUN4[1]) is not None


def test_a_search_that_returned_results_still_counts_as_a_repeat():
    """Every other family remembers only what came back empty. All
    eleven of run 4's searches succeeded."""
    mem = sm.get_memory()
    assert not sm.looks_fruitless(RESULTS)
    mem.record("action.web_search", "", RUN4[0], RESULTS, "")
    note = mem.note_for("action.web_search", "", RUN4[1])
    assert note and "already searched the web" in note


def test_the_note_says_to_open_a_result_not_to_reword():
    mem = sm.get_memory()
    mem.record("action.web_search", "", RUN4[0], RESULTS, "")
    note = mem.note_for("action.web_search", "", RUN4[1])
    assert "browser_navigate" in note or "web_read" in note
    assert "Searching again is not progress" in note


def test_run_four_is_stopped_at_its_fifth_search():
    """The acceptance test: the real queries, in the order they ran."""
    mem = sm.get_memory()
    stopped = None
    for i, q in enumerate(RUN4, 1):
        if mem.refusal_for("action.web_search", "", q, ""):
            stopped = i
            break
        mem.record("action.web_search", "", q, RESULTS, "")
    assert stopped == 5, f"refused at {stopped}, expected the 5th"


def test_a_genuinely_different_search_is_untouched():
    mem = sm.get_memory()
    for q in RUN4[:5]:
        mem.record("action.web_search", "", q, RESULTS, "")
    other = "Vellum AI pricing tiers"
    assert mem.note_for("action.web_search", "", other) is None
    assert mem.refusal_for("action.web_search", "", other, "") is None


def test_searching_does_not_silence_the_browser():
    """Families never cross. Four searches must not stop a page read."""
    mem = sm.get_memory()
    for q in RUN4[:5]:
        mem.record("action.web_search", "", q, RESULTS, "")
    assert mem.note_for("action.browser_find", SCREENER,
                        "top 10 AI startups list") is None


def test_a_browser_search_still_needs_an_unchanged_page_to_refuse():
    """The page-unchanged escape hatch is untouched for page families —
    only SEARCH, which has no page, bypasses it."""
    mem = sm.get_memory()
    before = page_view(SCREENER, "Screener")
    for q in FIVE[:2]:
        mem.record("action.browser_find", SCREENER, q,
                   no_match(SCREENER, q), before)
    moved = page_view(SCREENER, "Screener", marker="  e9 button 'x'")
    assert mem.refusal_for("action.browser_find", SCREENER, FIVE[2], moved) is None


# ------------------------------------------------------ wired into the loop
#
# item_state passed nineteen unit tests and never fired once across four
# live runs. Tests that a guard is REACHABLE are the cheap half of
# stopping that happening again.

def _loop_source() -> str:
    import inspect
    from backend.app.orchestrator import execution_loop
    return inspect.getsource(execution_loop.AgenticExecutor.run)


def test_the_refusal_is_decided_before_the_call_is_made():
    """A guard that runs after the call has already paid for it."""
    src = _loop_source()
    assert src.index("refusal_for(") < src.index("result = self._execute(action, args)")


def test_what_the_page_answered_is_recorded_after_it():
    src = _loop_source()
    assert src.index("result = self._execute(action, args)") < \
        src.index("get_strategy_memory().record(")


def test_the_refusal_actually_skips_the_call():
    src = _loop_source()
    _, _, tail = src.partition("if _refusal:")
    assert "continue" in tail[:800], "a refusal must not fall through and execute"


def test_the_note_reaches_the_model():
    """It has to be joined onto the RESULT the model reads, not merely
    computed and dropped."""
    src = _loop_source()
    _, _, tail = src.partition('"\\n".join(x for x in (_instrument_note')
    assert tail, "the observation is no longer assembled where expected"
    assert "_strategy_note" in tail[:200]


def test_the_loop_does_not_require_a_page_to_consult_the_memory():
    """A web search has no page. The loop gated the whole guard on one,
    so every search skipped it — the memory change alone would have
    changed nothing live, which is this codebase's oldest failure shape.
    """
    src = _loop_source()
    assert "if _query and _page:" not in src, (
        "gating on a page skips every web_search")
    _, _, tail = src.partition("_strategy_note = None")
    assert tail.lstrip().startswith("#") or "if _query:" in tail[:600]


def test_a_new_run_starts_with_no_verdicts():
    """A page's layout is a fact about right now, and this executor is
    reused across runs."""
    assert "reset_strategy_memory()" in _loop_source()
