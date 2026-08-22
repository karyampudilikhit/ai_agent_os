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


def test_one_opened_item_does_not_stand_down_the_guard():
    """MEASURED, 2026-08-21. This used to assert the opposite -- that a
    single opened item proved the run had moved on -- and the clause it
    pinned read `if prog.opened: return None`, a boolean where the
    question is a ratio.

    A live run opened its first item at call 11 and the guard went
    silent for calls 13, 15, 17 and 19, which harvested twenty-seven
    more candidates nobody ever opened. One of eight done is not "work
    has started, stand down"; it is seven still to do.
    """
    calls = [_nav(LIST_URL, 1), _listing(5, 2),
             _nav(ITEM.format(1), 3), _read(ITEM.format(1), 4)]
    p = derive(calls, wanted=8)
    assert p.done == 1
    assert progress_note(p, "action.browser_extract_records", 16) is not None


def test_a_run_that_is_keeping_up_is_left_alone():
    """The other side of it. As many candidates opened as items still
    needed leaves nothing to redirect, and nagging then is the failure
    mode this guard must not become."""
    calls = [_nav(LIST_URL, 1), _listing(5, 2)]
    at = 3
    for i in range(1, 5):
        calls += [_nav(ITEM.format(i), at), _read(ITEM.format(i), at + 1)]
        at += 2
    p = derive(calls, wanted=5)
    assert progress_note(p, "action.browser_extract_records", 10) is None


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


# ------------------------------------ saying WHICH clause stayed silent
#
# Four live runs, no nudge, nineteen passing tests. Every one of those
# runs reported the same thing -- nothing -- because all six ways to
# return None look identical from outside, and three fixes were proposed
# for the wrong one. The clauses are unchanged; they are now named, so a
# run says which of them decided.

def test_every_way_of_staying_silent_names_itself():
    from backend.app.orchestrator.item_state import (
        ALREADY_OPENED, ENOUGH_DONE, FIRED, NOTHING_DISCOVERED,
        NOT_A_LIST_CALL, NO_ITEM_GOAL, TOO_FEW_CANDIDATES, verdict,
    )
    LIST = "action.browser_extract_records"

    assert verdict(derive([_wrapped_listing(10, 1)], wanted=1), LIST) \
        == NO_ITEM_GOAL
    assert verdict(derive([_nav(LIST_URL, 1)], wanted=8), LIST) \
        == NOTHING_DISCOVERED
    assert verdict(derive([_wrapped_listing(10, 1)], wanted=8),
                   "action.browser_navigate") == NOT_A_LIST_CALL
    assert verdict(derive([_wrapped_listing(2, 1)], wanted=8), LIST) \
        == TOO_FEW_CANDIDATES

    # ALREADY_OPENED is now a RATIO, not "has anything been opened".
    # Enough candidates open to cover what is still needed.
    # ALREADY_OPENED is now a RATIO, not "has anything been opened".
    # Eight candidates OPEN and none read yet: there is nothing left to
    # redirect toward, so the guard stands down without ENOUGH_DONE
    # (which needs them read) having fired.
    keeping_up = [_wrapped_listing(10, 1)]
    for i in range(1, 9):
        keeping_up.append(_nav(ITEM.format(i), 1 + i))
    p8 = derive(keeping_up, wanted=8)
    assert p8.done == 0 and len(p8.opened) == 8
    assert verdict(p8, LIST) == ALREADY_OPENED

    calls = [_wrapped_listing(10, 1)]
    at = 2
    for i in range(1, 9):
        calls += [_nav(ITEM.format(i), at), _read(ITEM.format(i), at + 1)]
        at += 2
    assert verdict(derive(calls, wanted=8), LIST) == ENOUGH_DONE

    assert verdict(derive([_wrapped_listing(10, 1)], wanted=8), LIST) == FIRED


def test_the_reason_and_the_behaviour_cannot_drift_apart():
    """progress_note reads its answer from verdict() rather than
    repeating the conditions, so the reason logged is the one acted on."""
    from backend.app.orchestrator.item_state import FIRED, verdict
    p = derive([_wrapped_listing(10, 1)], wanted=8)
    for action in ("action.browser_extract_records", "action.browser_navigate"):
        fired = progress_note(p, action, budget_left=13) is not None
        assert fired is (verdict(p, action) == FIRED)


def test_the_derivation_reports_what_it_actually_saw():
    from backend.app.orchestrator.item_state import diagnose
    p = derive([_wrapped_listing(10, 1), _nav(ITEM.format(1), 2)], wanted=8)
    line = diagnose(p)
    assert "discovered=10" in line and "opened=1" in line and "done=0/8" in line
    assert "OPENED BUT NEVER READ" in line


def test_a_browser_call_with_no_page_address_is_recorded():
    """The leading suspect for the second cause. An arrival this cannot
    see is an item it cannot count as opened, so a run has to say when
    one goes unseen rather than silently dropping it."""
    from backend.app.orchestrator.item_state import diagnose
    blind = {"tool": "action.browser_click", "at": 2, "ok": True,
             "output": "clicked the first listing"}
    p = derive([_wrapped_listing(10, 1), blind], wanted=8)
    assert p.unlocated == ["browser_click"]
    assert "no-address=browser_click" in diagnose(p)


def test_a_non_browser_call_without_an_address_is_not_suspicious():
    from backend.app.orchestrator.item_state import diagnose
    calc = {"tool": "action.run_python", "at": 2, "ok": True, "output": "42"}
    p = derive([_wrapped_listing(10, 1), calc], wanted=8)
    assert p.unlocated == []
    assert "no-address" not in diagnose(p)


def test_the_loop_says_whether_the_guard_was_eligible_at_all():
    """Never evaluated and evaluated-but-silent look identical in a
    transcript. One line at the top of the run separates them."""
    import inspect
    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    assert src.count("_log_item_gate(") == 2, \
        "the gate must be logged when the plan is built AND when it is rebuilt"
    gate = inspect.getsource(execution_loop._log_item_gate)
    assert "per_item_work" in gate and "feasible_items" in gate
