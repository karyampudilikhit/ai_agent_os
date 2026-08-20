"""A finished ACTION is not a finished TASK.

Asked to open a page, follow a link and go back, a live run did exactly
that in its first three calls -- and then made TWELVE MORE. It found
"See also", scrolled, searched the page, clicked around, and ended where
it had already been at call three. Every one of those calls SUCCEEDED.
Not one advanced the task.

The loop could tell that an action had worked. Nothing could tell that
the goal had been reached, so success kept reading as "carry on".

What counts as evidence here is never the model announcing DONE. It is
the LEDGER: the pages the task named were actually landed on, and a
required back-navigation was actually performed by the browser.
"""
from __future__ import annotations

from backend.app.orchestrator.goal_state import (
    check, requirements, urls_in, visited_urls, went_back,
)

PY_URL = "https://en.wikipedia.org/wiki/Python_(programming_language)"
PL_URL = "https://en.wikipedia.org/wiki/Programming_language"

W5_TASK = (
    "Do this navigation sequence, in order:\n"
    f"  1. Open {PY_URL}\n"
    f"  2. From that page, follow a link to {PL_URL}\n"
    "  3. Go BACK to the Python page using the browser's back action.\n"
    "Tell me the page title and URL at each of the three stages."
)


def _view(url: str, tool: str = "action.browser_navigate", at: float = 1.0,
          ok: bool = True) -> dict:
    return {"tool": tool, "at": at, "ok": ok,
            "output": f'Now on: "A page" ({url})\nURL: {url}\nDATA: (none)'}


def _back(url: str, at: float = 3.0) -> dict:
    return {"tool": "action.browser_back", "at": at, "ok": True,
            "output": f"Went back to {url}.\n\nURL: {url}\nTITLE: A page"}


# ------------------------------------------------ reading the finish line

def test_urls_in_the_task_are_the_requirements():
    req = requirements(W5_TASK)
    assert len(req["urls"]) == 2
    assert req["needs_back"] is True


def test_a_task_with_no_stated_pages_states_no_finish_line():
    """The common case, and why this is safe to run on everything:
    open-ended work is left entirely to the existing DONE handling."""
    req = requirements("Research the competitive landscape and write it up.")
    assert req["urls"] == []
    assert check("Research the landscape and write it up.",
                 [_view("https://example.com/a")]) == (False, "")


def test_urls_compare_as_pages_not_as_strings():
    a = urls_in("see https://WWW.Example.com/Page/ and http://example.com/page")
    assert len(a) == 1, a


# --------------------------------------- what the run actually observed

def test_visits_are_read_from_output_not_from_arguments():
    """Asking for a URL is not arriving at one, and the difference is the
    whole point of checking."""
    calls = [{"tool": "action.browser_navigate", "at": 1, "ok": True,
              "args_text": '{"url": "https://example.com/wanted"}',
              "output": 'Now on: "Blocked" (https://example.com/denied)'}]
    assert visited_urls(calls) == ["example.com/denied"]


def test_a_failed_call_is_not_a_visit():
    calls = [_view("https://example.com/a", ok=False)]
    assert visited_urls(calls) == []


def test_going_back_is_read_from_the_browsers_own_report():
    assert went_back([_back(PY_URL)]) is True
    assert went_back([_view(PY_URL)]) is False


def test_a_claimed_back_without_the_tool_does_not_count():
    """The model saying it went back is not the browser going back."""
    calls = [{"tool": "action.browser_extract", "at": 1, "ok": True,
              "output": "Went back to " + PY_URL}]
    assert went_back(calls) is False


# ------------------------------------------------------- the W5 sequence

def test_the_w5_sequence_completes_at_the_third_call():
    """Exactly the run that then made twelve more calls."""
    calls = [_view(PY_URL, at=1), _view(PL_URL, at=2), _back(PY_URL, at=3)]
    done, why = check(W5_TASK, calls)
    assert done is True
    assert "back-navigation" in why


def test_it_is_not_complete_before_the_back():
    calls = [_view(PY_URL, at=1), _view(PL_URL, at=2)]
    assert check(W5_TASK, calls)[0] is False


def test_it_is_not_complete_with_a_page_unvisited():
    calls = [_view(PY_URL, at=1), _back(PY_URL, at=2)]
    assert check(W5_TASK, calls)[0] is False


def test_a_round_trip_must_END_on_the_starting_page():
    """Going back and then wandering off is not a completed round trip."""
    calls = [_view(PY_URL, at=1), _view(PL_URL, at=2), _back(PY_URL, at=3),
             _view("https://en.wikipedia.org/wiki/See_also", at=4)]
    assert check(W5_TASK, calls)[0] is False


# ------------------------------------------- a single-page task finishes

def test_a_one_page_task_completes_on_arrival():
    task = f"Open {PY_URL} and tell me the first paragraph."
    assert check(task, [_view(PY_URL)])[0] is True


def test_arriving_somewhere_else_is_not_arriving():
    task = f"Open {PY_URL} and tell me the first paragraph."
    assert check(task, [_view("https://example.com/elsewhere")])[0] is False


# ------------------------------------------------------ wired into the loop

def test_the_loop_stops_on_verified_completion():
    """The hard stop lives in the code, not in the prompt."""
    import inspect
    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    assert "goal_check(task, _ledger_calls())" in src
    head, _, tail = src.partition("goal_check(task, _ledger_calls())")
    assert "break" in tail[:600], "completion must actually end the loop"
    assert "TASK COMPLETE" in tail[:600]


def test_completion_is_checked_after_the_call_not_before():
    """It rests on what the run OBSERVED, so it can only be judged once
    the observation exists."""
    import inspect
    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    assert src.index("result = self._execute(action, args)") < \
        src.index("goal_check(task, _ledger_calls())")


def test_recovery_is_not_broken_by_the_stop():
    """W2's recovery — a click failed, so it pressed Enter instead — must
    survive. A failed call reaches no completion state, so the guard
    cannot fire on it."""
    calls = [_view(PY_URL, at=1, ok=False)]
    assert check(f"Open {PY_URL} and read it.", calls)[0] is False


# ------------------------- pages named in words, not as URLs

W5_WORDS = (
    "Do this navigation sequence on Wikipedia, in order:\n"
    "  1. Open the page for the Python programming language.\n"
    '  2. From that page, follow a link to the "Programming language" article.\n'
    "  3. Go BACK to the Python page using the browser's back action.\n"
    "Tell me the page title and URL at each of the three stages, and confirm "
    "that stage 3 returned you to the same page as stage 1."
)
PY_TITLE = "Python (programming language) - Wikipedia"
GP_TITLE = "General-purpose programming language - Wikipedia"


def _titled(title: str, at: float = 1.0, tool="action.browser_navigate") -> dict:
    return {"tool": tool, "at": at, "ok": True, "output": f"TITLE: {title}"}


def _back_titled(title: str, at: float = 3.0) -> dict:
    return {"tool": "action.browser_back", "at": at, "ok": True,
            "output": f"Went back to https://x/y.\n\nTITLE: {title}"}


def test_pages_named_in_words_are_requirements_too():
    """Real tasks say "open the Python page", not "open https://...".
    A check that only understood URLs stayed silent on four of five
    acceptance tests."""
    from backend.app.orchestrator.goal_state import named_pages
    names = named_pages(W5_WORDS)
    assert "python programming language" in names
    assert "programming language" in names


def test_deictic_words_are_not_page_names():
    """"returned you to the SAME page as stage 1" yielded the page name
    "same", which no title can match — and since every named page must
    be reached, one bogus name blocks the whole check."""
    from backend.app.orchestrator.goal_state import named_pages
    assert "same" not in named_pages(W5_WORDS)
    assert "stage" not in named_pages(W5_WORDS)


def test_a_word_prefix_is_the_same_page_named_twice():
    """"the page for the Python programming language" and "the Python
    page" are one page. "Programming language" merely ENDS with the same
    words and is a different article."""
    from backend.app.orchestrator.goal_state import named_pages
    names = named_pages(W5_WORDS)
    assert "python" not in names, "a word-prefix is the same page again"
    assert "programming language" in names, "a suffix is a different page"


def test_the_full_sequence_completes():
    calls = [_titled(PY_TITLE, 1), _titled(GP_TITLE, 2), _back_titled(PY_TITLE, 3)]
    done, why = check(W5_WORDS, calls)
    assert done is True
    assert "back-navigation" in why


def test_two_named_pages_need_two_distinct_pages():
    """"Programming language" is a word-subset of the title "Python
    (programming language)", so a loose match let ONE page satisfy BOTH
    names and the run finished without opening the second article."""
    assert check(W5_WORDS, [_titled(PY_TITLE, 1)])[0] is False


def test_a_back_navigation_is_not_a_second_page():
    """browser_back reports the starting page's title again. Counting
    that as a second arrival finished the run one page short."""
    calls = [_titled(PY_TITLE, 1), _back_titled(PY_TITLE, 2)]
    assert check(W5_WORDS, calls)[0] is False


def test_it_is_incomplete_without_the_back():
    calls = [_titled(PY_TITLE, 1), _titled(GP_TITLE, 2)]
    assert check(W5_WORDS, calls)[0] is False


def test_a_url_in_the_task_still_wins_over_names():
    """When the founder gives a URL it is the exact requirement, and
    guessing at names alongside it can only add noise."""
    from backend.app.orchestrator.goal_state import requirements
    req = requirements(f"Open the Python page at {PY_URL}")
    assert req["urls"] and not req["names"]
