"""A site solved once should not have to be solved again.

The winning path through TradingView's screener is three moves: open the
Performance tab, find "Perf % 1W", click it. The agent found that path
ONCE in five attempts and forgot it every time -- every run started cold
and re-explored a site that had already been solved. A task that works
one time in five stays one in five forever.

So a run that PASSES THE GATE has its path kept, and the next run on a
task like it is handed the recipe.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from backend.app.browser.playbook import PlaybookStore, distil, domain_of

TASK = "top 5 gainers and losers by weekly percentage change from the stock screener"


def _call(tool, at, out="", ok=True, **args):
    return {"tool": f"action.{tool}", "at": at, "ok": ok,
            "args_text": json.dumps(args), "output": out}


WINNING_RUN = [
    _call("browser_navigate", 1, "Now on: \"Screener\" (https://www.tradingview.com/screener/)",
          url="https://www.tradingview.com/screener/"),
    _call("browser_observe", 2, "URL: https://www.tradingview.com/screener/"),
    _call("browser_find", 3, "SEARCHED 373 element(s)", query="weekly change column"),
    _call("browser_click_element", 4, 'Clicked e40 "Performance".', element_id="e40"),
    _call("browser_click_element", 5, 'Clicked e59 "Perf % 1W".', element_id="e59"),
    _call("browser_extract_table", 6, "[tables — ...]"),
]


@pytest.fixture()
def store():
    return PlaybookStore(Path(tempfile.mkdtemp()) / "pb.json")


def test_the_path_is_recorded_and_recalled(store):
    store.record(TASK, WINNING_RUN)
    got = store.recall(TASK)
    assert got is not None
    assert got["steps"] == [
        "go to https://www.tradingview.com/screener/",
        'search the page for "weekly change column"',
        'click "Performance"',
        'click "Perf % 1W"',
        "read the table",
    ]
    assert got["domains"] == ["tradingview.com"]


def test_element_ids_are_never_stored():
    """The single most important property. Ids are per-observation by
    design -- e59 means nothing an hour later, and a playbook full of
    them would be a machine for clicking the wrong thing with
    confidence. What is kept is what a person would write down."""
    recipe = distil(WINNING_RUN)
    joined = " ".join(recipe["steps"])
    assert "e40" not in joined and "e59" not in joined
    assert '"Performance"' in joined and '"Perf % 1W"' in joined


def test_typed_text_is_not_kept():
    """Whatever was typed is this run's data, often personal, and never
    the next run's."""
    recipe = distil([
        _call("browser_navigate", 1, "Now on: \"X\" (https://site.test/a)",
              url="https://site.test/a"),
        _call("browser_type", 2, "Typed into e3.", element_id="e3",
              text="karyampudi.likhit@example.com"),
        _call("browser_click_element", 3, 'Clicked e4 "Search".', element_id="e4"),
    ])
    joined = " ".join(recipe["steps"])
    assert "example.com" not in joined
    assert "type into the field" in joined


def test_an_unrelated_task_recalls_nothing(store):
    """A wrong recipe is worse than no recipe."""
    store.record(TASK, WINNING_RUN)
    assert store.recall("draft a cold email to investors") is None


def test_a_failed_click_is_not_learned(store):
    """Only succeeded calls. Learning from a failure teaches the agent
    to repeat it."""
    recipe = distil([
        _call("browser_navigate", 1, "Now on: \"X\" (https://site.test/a)",
              url="https://site.test/a"),
        _call("browser_click_element", 2, "(click on e9 failed: timeout)",
              ok=False, element_id="e9"),
        _call("browser_click_element", 3, 'Clicked e10 "Sort".', element_id="e10"),
    ])
    assert recipe["steps"] == ["go to https://site.test/a", 'click "Sort"']


def test_an_unnamed_click_is_skipped():
    """"click the third unnamed button" is not something the next run
    can act on."""
    recipe = distil([
        _call("browser_navigate", 1, "Now on: \"X\" (https://site.test/a)",
              url="https://site.test/a"),
        _call("browser_click_element", 2, "Clicked e9 \"\".", element_id="e9"),
        _call("browser_click_element", 3, 'Clicked e10 "Apply".', element_id="e10"),
    ])
    assert recipe["steps"] == ["go to https://site.test/a", 'click "Apply"']


def test_a_trivial_path_is_not_worth_keeping(store):
    """"we opened a URL" is true and not worth recalling."""
    assert store.record(TASK, [
        _call("browser_navigate", 1, "Now on: \"X\" (https://site.test/a)",
              url="https://site.test/a"),
    ]) is None


def test_a_newer_verified_path_replaces_the_old_one(store):
    """A site that changed is better described by the run that just
    worked than by the one that worked last month."""
    store.record(TASK, WINNING_RUN)
    store.record(TASK, [
        _call("browser_navigate", 1, "Now on: \"X\" (https://www.tradingview.com/screener/)",
              url="https://www.tradingview.com/screener/"),
        _call("browser_click_element", 2, 'Clicked e7 "Movers".', element_id="e7"),
        _call("browser_extract_table", 3, "[tables]"),
    ])
    assert len(store.all()) == 1
    assert 'click "Movers"' in store.recall(TASK)["steps"]


def test_the_hint_says_it_may_be_stale(monkeypatch, store):
    """It is prior knowledge, not a script. An agent that follows a dead
    recipe and reports success is the failure the rest of this layer
    exists to refuse."""
    from backend.app.browser import playbook
    monkeypatch.setattr(playbook, "get_store", lambda: store)
    store.record(TASK, WINNING_RUN)
    hint = playbook.hint_for(TASK)
    assert 'click "Perf % 1W"' in hint
    assert "not a script" in hint
    assert "may have changed" in hint
    assert "confirm each step did what you expected" in hint


def test_no_playbook_means_no_block(monkeypatch, store):
    from backend.app.browser import playbook
    monkeypatch.setattr(playbook, "get_store", lambda: store)
    assert playbook.hint_for("something never done before") == ""


def test_domains_are_normalised():
    assert domain_of("https://www.TradingView.com/screener/") == "tradingview.com"
    assert domain_of("https://in.indeed.com/jobs?q=x") == "in.indeed.com"
    assert domain_of("not a url") == ""


def test_it_is_learned_only_from_a_run_that_passed_the_gate():
    import inspect
    from backend.app.api import routes
    src = inspect.getsource(routes._gate_deliverable)
    before, _, after = src.partition("if not problems:")
    assert "playbook" in after, "recorded only on the success path"
    assert "playbook" not in before, "never recorded from a refused run"


def test_the_loop_asks_for_a_playbook():
    import inspect
    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    assert "hint_for" in src
    assert "{playbook}" in execution_loop.STEP_PROMPT


# ------------------- what a live run taught about matching

def test_an_unrelated_task_never_gets_this_recipe(monkeypatch, store):
    """Caught live: a TradingView screener path was recalled for "find 5
    recent Product Manager job listings in India", on little more than a
    shared "5" and "find". Handing a job search a stock-screener path is
    worse than handing it nothing."""
    from backend.app.browser import playbook
    monkeypatch.setattr(playbook, "get_store", lambda: store)
    store.record(TASK, WINNING_RUN)
    assert playbook.hint_for(
        "Find 5 RECENT job listings for Product Manager roles in India "
        "from any public job platform") == ""
    assert playbook.hint_for("Draft a cold email to seed investors") == ""


def test_the_same_task_still_recalls_its_own_path(monkeypatch, store):
    from backend.app.browser import playbook
    monkeypatch.setattr(playbook, "get_store", lambda: store)
    store.record(TASK, WINNING_RUN)
    assert playbook.hint_for(TASK) != ""


def test_the_decision_does_not_rest_on_the_bm25_score():
    """The measurement that settled the design. Against the real store
    the screener task scored 0.479 against its OWN path while the job
    search scored 0.486 against that same path -- the unrelated task
    scored HIGHER than the identical one. No threshold separates those,
    so subject-term overlap is the gate and relevance only ranks."""
    from backend.app.browser.playbook import MIN_RELEVANCE, MIN_SHARED_TERMS
    assert MIN_RELEVANCE <= 0.1, "relevance ranks candidates, it does not judge them"
    assert MIN_SHARED_TERMS >= 2


def test_scaffolding_words_are_not_subject_words():
    """"top", "find", "5" and "page" appear in every task and are exactly
    what let two unrelated ones match."""
    from backend.app.browser.playbook import _subject_terms
    terms = _subject_terms("Find the top 5 listings on any page and report them")
    assert "listings" in terms
    for noise in ("find", "top", "five", "page", "report", "any"):
        assert noise not in terms, noise
