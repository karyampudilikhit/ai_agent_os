"""Do not re-learn a wall you hit thirty seconds ago.

Measured on a live Reddit run, 19 calls. web_read was tried on
old.reddit.com at step 1, came back "Failed to fetch url", and the agent
drew exactly the right conclusion: switch to a headed browser, which it
did at step 3, and which worked for every subreddit afterwards.

Then at steps 11, 12 and 19 it went BACK to web_read on the same host.
Five of nineteen calls spent re-establishing a fact the run had settled
in its first thirty seconds.

The existing repeat guard cannot see this. It fingerprints the exact
call, and every URL differed -- /r/Entrepreneur/top/,
/r/Entrepreneur/top/.json, /r/ycombinator/top/. Same doomed instrument,
same host, different path each time.

This is a NEW failure mode created by having two instruments. With only
a browser there was nothing to switch back and forth between.
"""
from __future__ import annotations

import pytest

from backend.app.orchestrator import instrument_memory as im

BLOCKED = ("(web_read got no content from https://old.reddit.com/r/startups/top/ "
           "(Failed to fetch url. The page may be login-walled... Report it as "
           "UNREACHABLE rather than empty, or try action.browser_navigate with "
           "interactive true.)")
BROWSER_BLOCKED = ("[page text - https://finviz.com/screener]\n"
                   "THIS PAGE RETURNED ALMOST NO TEXT (143 characters).")
WORKED = 'Now on: "Top posts" (https://old.reddit.com/r/startups/top/)'


@pytest.fixture(autouse=True)
def _fresh():
    im.reset()
    yield
    im.reset()


# ------------------------------------------------- the live sequence

def test_the_same_instrument_on_a_new_path_is_flagged():
    """The exact waste: web_read fails on /r/startups/top/, and the run
    tries it again on /r/Entrepreneur/top/.json."""
    m = im.get_memory()
    m.record("action.web_read", "https://old.reddit.com/r/startups/top/", BLOCKED)
    note = m.note_for("action.web_read",
                      "https://old.reddit.com/r/Entrepreneur/top/.json?t=day")
    assert note and "already failed to reach reddit.com" in note


def test_the_note_names_the_instrument_that_worked():
    """Telling it to stop is half an answer. The run already knows which
    tool got through."""
    m = im.get_memory()
    m.record("action.web_read", "https://old.reddit.com/r/startups/top/", BLOCKED)
    m.record("action.browser_navigate", "https://old.reddit.com/r/startups/top/", WORKED)
    note = m.note_for("action.web_read", "https://www.reddit.com/r/SaaS/top/")
    assert "the browser DID work" in note


def test_with_nothing_working_yet_it_asks_for_a_real_change():
    m = im.get_memory()
    m.record("action.web_read", "https://old.reddit.com/r/x/", BLOCKED)
    note = m.note_for("action.web_read", "https://old.reddit.com/r/y/")
    assert "No instrument has reached this host yet" in note
    assert "interactive" in note


# ---------------------------------------------- what it must NOT do

def test_a_different_host_is_untouched():
    m = im.get_memory()
    m.record("action.web_read", "https://old.reddit.com/r/x/", BLOCKED)
    assert m.note_for("action.web_read", "https://apnews.com/hub/business") is None


def test_the_other_instrument_is_untouched():
    """The whole point is to send it to the tool that works."""
    m = im.get_memory()
    m.record("action.web_read", "https://old.reddit.com/r/x/", BLOCKED)
    assert m.note_for("action.browser_navigate", "https://old.reddit.com/r/x/") is None


def test_a_successful_call_records_no_wall():
    m = im.get_memory()
    m.record("action.web_read", "https://apnews.com/hub/business", "[page text] real body")
    assert m.note_for("action.web_read", "https://apnews.com/hub/us-news") is None


def test_it_nudges_and_never_refuses():
    """A bot check expires, a login completes, a 502 clears. A guard that
    cannot recover is worse than the waste it prevents -- so this returns
    a NOTE, and there is no path here that blocks a call."""
    import inspect
    src = inspect.getsource(im.InstrumentMemory.note_for)
    assert "return None" in src
    assert "raise" not in src


# ------------------------------------------- browser side, same rule

def test_a_blocked_browser_page_is_recorded_too():
    """Finviz turned the browser away roughly two runs in three, and the
    agent kept trying view parameters against the same wall."""
    m = im.get_memory()
    m.record("action.browser_navigate",
             "https://finviz.com/screener.ashx?v=111", BROWSER_BLOCKED)
    note = m.note_for("action.browser_navigate",
                      "https://finviz.com/screener.ashx?v=141")
    assert note and "the browser already failed" in note


def test_browser_tools_share_one_verdict():
    """A host that turns away browser_navigate turns away
    browser_extract on the same page."""
    m = im.get_memory()
    m.record("action.browser_navigate", "https://finviz.com/screener", BROWSER_BLOCKED)
    assert m.note_for("action.browser_extract", "https://finviz.com/screener") is not None


# --------------------------------------------------------- plumbing

def test_subdomains_are_one_wall():
    """old.reddit.com and www.reddit.com are the same refusal."""
    assert im.host_of("https://old.reddit.com/r/x") == "reddit.com"
    assert im.host_of("https://www.reddit.com/r/x") == "reddit.com"
    assert im.host_of("https://apnews.com/hub") == "apnews.com"


def test_only_our_own_wording_counts_as_a_failure():
    """Matching a SITE's wording would be matching untrusted content: a
    page that happens to contain "could not fetch" is not a verdict on
    whether we reached it."""
    assert im.looks_unreachable("(web_read got no content from x)")
    assert im.looks_unreachable("THIS PAGE RETURNED ALMOST NO TEXT (143 characters).")
    assert not im.looks_unreachable(
        "Article: the minister said the report could not fetch enough support.")


def test_each_run_starts_with_no_verdicts():
    m = im.get_memory()
    m.record("action.web_read", "https://old.reddit.com/r/x/", BLOCKED)
    im.reset()
    assert im.get_memory().note_for("action.web_read",
                                    "https://old.reddit.com/r/y/") is None


def test_the_loop_records_and_warns():
    import inspect
    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    assert "note_for" in src, "the warning must be produced before the call"
    assert "get_instrument_memory().record" in src, "and the result recorded after"
    assert "reset_instrument_memory()" in src, "cleared per run"
