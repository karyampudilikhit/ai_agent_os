"""How many of the asked-for items are actually done.

Phase 2 handed the model a verdict -- "ten items each opened individually
needs about twenty-five steps and you have twenty-two, so do eight
PROPERLY and say so". It was advice, and the live re-run ignored it:
seventeen of twenty-two steps went on results pages before the first job
page opened. Three items, not eight.

That is the pattern this codebase keeps rediscovering. A note in a prompt
is not enforcement. So progress is now DERIVED FROM THE LEDGER -- what
was found, what was landed on, what was read -- and the run is told at
the moment it reaches for another list, not fifteen steps later.
"""
from __future__ import annotations

from backend.app.orchestrator.item_state import (
    derive, progress_note, shortfall_note,
)

LIST_URL = "https://site.test/internships/product-management"
ITEM = "https://site.test/internship/detail/job-{}"


def _listing(n, at):
    body = "\n".join(f"link: {ITEM.format(i)}" for i in range(1, n + 1))
    return {"tool": "action.browser_extract_records", "at": at, "ok": True,
            "output": f"URL: {LIST_URL}\n[records]\n{body}"}


def _nav(url, at):
    return {"tool": "action.browser_navigate", "at": at, "ok": True,
            "output": f'Now on: "Job" ({url})\nURL: {url}'}


def _read(url, at):
    return {"tool": "action.browser_extract", "at": at, "ok": True,
            "output": f"URL: {url}\n[page text] the body of the posting"}


# --------------------------------------------- derived from the ledger

def test_candidates_are_discovered_from_a_records_read():
    p = derive([_nav(LIST_URL, 1), _listing(5, 2)], wanted=8)
    assert len(p.discovered) == 5
    assert p.done == 0


def test_the_listing_page_is_not_one_of_its_own_items():
    p = derive([_nav(LIST_URL, 1), _listing(5, 2)], wanted=8)
    assert all("detail" in d for d in p.discovered), p.discovered


def test_opening_and_reading_one_item_counts_it():
    calls = [_nav(LIST_URL, 1), _listing(5, 2),
             _nav(ITEM.format(1), 3), _read(ITEM.format(1), 4)]
    assert derive(calls, wanted=8).done == 1


def test_opening_without_reading_does_not_count():
    """Arriving is not extracting. The task asked for fields off the
    page, and a navigation alone produced none of them."""
    calls = [_nav(LIST_URL, 1), _listing(5, 2), _nav(ITEM.format(1), 3)]
    assert derive(calls, wanted=8).done == 0


def test_a_failed_navigation_is_not_an_open():
    calls = [_nav(LIST_URL, 1), _listing(5, 2),
             dict(_nav(ITEM.format(1), 3), ok=False)]
    assert derive(calls, wanted=8).done == 0


# ------------------------------------------------ the note that fires

def test_gathering_another_list_with_nothing_opened_is_called_out():
    """The exact live behaviour: five candidates in hand, none opened,
    and the next call gathers more."""
    p = derive([_nav(LIST_URL, 1), _listing(5, 2)], wanted=8)
    note = progress_note(p, "action.browser_extract_records", budget_left=18)
    assert note and "opened NONE of them" in note
    assert "Open one of the candidates you already have" in note


def test_the_note_states_how_many_items_the_budget_still_buys():
    p = derive([_nav(LIST_URL, 1), _listing(5, 2)], wanted=8)
    note = progress_note(p, "action.browser_extract_records", budget_left=10)
    assert "5 item(s) worth" in note


def test_the_note_stops_once_work_has_started():
    """One opened item is proof the run has moved on. Nagging past that
    is the failure mode on the other side of this guard."""
    calls = [_nav(LIST_URL, 1), _listing(5, 2),
             _nav(ITEM.format(1), 3), _read(ITEM.format(1), 4)]
    p = derive(calls, wanted=8)
    assert progress_note(p, "action.browser_extract_records", 16) is None


def test_a_first_listing_is_never_second_guessed():
    """Discovery is the correct first move. The note must not fire
    before there is anything to open."""
    p = derive([_nav(LIST_URL, 1)], wanted=8)
    assert progress_note(p, "action.browser_extract_records", 20) is None


def test_opening_an_item_is_never_second_guessed():
    p = derive([_nav(LIST_URL, 1), _listing(5, 2)], wanted=8)
    assert progress_note(p, "action.browser_navigate", 18) is None


def test_a_single_item_task_is_untouched():
    """The safety property: a goal with no item count behaves exactly as
    it did before this module existed."""
    p = derive([_nav(LIST_URL, 1), _listing(5, 2)], wanted=1)
    assert progress_note(p, "action.browser_extract_records", 18) is None


# ------------------------------------------------- finishing short

def test_a_short_run_is_told_to_report_the_real_number():
    calls = [_nav(LIST_URL, 1), _listing(10, 2),
             _nav(ITEM.format(1), 3), _read(ITEM.format(1), 4)]
    note = shortfall_note(derive(calls, wanted=8))
    assert "1 of 8" in note
    assert "Do NOT fill the remainder from the results list" in note


def test_a_complete_run_says_nothing():
    calls = [_nav(LIST_URL, 1), _listing(3, 2)]
    for i in (1, 2, 3):
        calls += [_nav(ITEM.format(i), 2 + i), _read(ITEM.format(i), 2.5 + i)]
    assert shortfall_note(derive(calls, wanted=3)) is None


# ------------------------------------------------- wired to the loop

def test_the_loop_derives_progress_before_each_call():
    import inspect
    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    assert "item_progress_note(" in src
    assert "derive_items(_ledger_calls()" in src


def test_the_note_only_applies_to_per_item_goals():
    import inspect
    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    assert "plan.spec.per_item_work and plan.feasible_items > 1" in src


def test_the_loop_reports_the_shortfall_at_the_end():
    """The mid-run nudge redirects; this makes the final count reach the
    writer. Without it a run that opened three items can present ten."""
    import inspect
    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    assert "item_shortfall_note(" in src
    head, _, tail = src.partition("item_shortfall_note(")
    assert "self._wrap(steps)" in tail, "must run BEFORE the transcript is built"


# ------------- the shape the extractors ACTUALLY emit

def _wrapped_listing(n, at):
    """browser_extract_records wraps its output with wrap_untrusted, whose
    header says "Source: <url>" — not "URL:" and not "Now on:".

    Reading only the other two meant the page a records read happened on
    was never recorded, so no candidate could be judged "below a known
    listing" and the nudge never fired on the live run it was built for.
    It passed every synthetic test, because the fixtures used the URL:
    shape. This one uses the real one."""
    body = "\n".join(f"link: {ITEM.format(i)}" for i in range(1, n + 1))
    return {"tool": "action.browser_extract_records", "at": at, "ok": True,
            "output": ("----- BEGIN UNTRUSTED WEB CONTENT -----\n"
                       f"Source: {LIST_URL}\n\n[records]\n{body}")}


def test_candidates_are_found_from_a_wrapped_records_read():
    p = derive([_wrapped_listing(10, 1)], wanted=8)
    assert len(p.discovered) == 10, p.discovered


def test_the_nudge_fires_on_the_real_output_shape():
    """The exact live sequence: records at step 6, records again at
    step 9 with nothing opened."""
    p = derive([_wrapped_listing(10, 1)], wanted=8)
    note = progress_note(p, "action.browser_extract_records", budget_left=13)
    assert note and "opened NONE of them" in note


def test_a_source_header_alone_still_tracks_opening_and_reading():
    calls = [_wrapped_listing(10, 1),
             _nav(ITEM.format(1), 2), _read(ITEM.format(1), 3)]
    assert derive(calls, wanted=8).done == 1
