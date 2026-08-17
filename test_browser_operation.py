"""The five changes that separate "correctly blocked" from "actually works".

Four runs of the same TradingView task ended the same way: the screener
was never sorted. The trust layer caught it -- the fourth run was refused
outright rather than reporting the default view as the answer -- but
being reliably wrong is not the goal.

The autopsy found five distinct reasons, and none of them was the model
choosing badly between good options:

  1. it clicked, got NO feedback, and assumed it worked. The comparison
     that could have told it otherwise ran only after the turn ended.
  2. nothing ever suggested the URL, which encodes the sort directly.
  3. the sort control was below the observation cap, so it was never on
     the list the model was choosing from.
  4. 10 steps and 700 tokens per step -- three attempts at a job that
     costs three steps each, on a thinking model that spends tokens
     before it answers.
  5. no way to put a stronger model on the loop without moving the whole
     company onto it.

Offline throughout. A scripted adapter returns canned decisions and a
stubbed _execute returns canned pages, so these test the LOOP rather than
an LLM's judgement.
"""
from __future__ import annotations

import json

from backend.app.orchestrator.execution_loop import (
    BROWSER_DEADLINE_SECONDS,
    BROWSER_MAX_STEPS,
    BROWSER_RULES,
    BROWSER_STEP_MAX_TOKENS,
    DEADLINE_SECONDS,
    MAX_INEFFECTIVE_INTERACTIONS,
    MAX_STEPS,
    STEP_MAX_TOKENS,
    AgenticExecutor,
)
from backend.app.orchestrator.output_contract import (
    is_browser_tool,
    is_interaction_tool,
    is_page_view_tool,
    page_view_changed,
)

# Two views of the same screener. The only honest signal that a click
# worked is that these differ; every other trace they leave is identical.
DEFAULT_VIEW = (
    "URL: https://tradingview.com/screener\nTITLE: Stock Screener\n\n"
    "INTERACTIVE ELEMENTS:\n  e1 button \"Overview\"\n  e2 button \"Performance\"\n\n"
    "PAGE TEXT:\nAAPL 305.93 +0.22%\nNVDA 225.16 -0.06%\nMSFT 495.40 -0.30%"
)
SORTED_VIEW = (
    "URL: https://tradingview.com/screener?sort=change&order=desc\n"
    "TITLE: Stock Screener\n\nINTERACTIVE ELEMENTS:\n"
    "  e1 button \"Overview\"\n  e2 button \"Performance\"\n\n"
    "PAGE TEXT:\nXYZ 12.40 +38.10%\nABC 3.02 +31.70%\nQRS 88.10 +27.40%"
)
FIND_OUTPUT = (
    'SEARCHED 412 element(s) on https://tradingview.com/screener for "change column".\n'
    "BEST 2 MATCH(ES), most likely first:\n"
    '  e211 columnheader "Change %"\n  e214 columnheader "Change"'
)

BROWSE_TASK = "open the screener and give me the top 5 gainers of the week"
PLAIN_TASK = "summarise the attached meeting notes"


class ScriptedAdapter:
    def __init__(self, decisions):
        self.decisions = [json.dumps(d) if isinstance(d, dict) else d
                          for d in decisions]
        self.prompts = []
        self.max_tokens_seen = []

    def chat_completion(self, prompt, **kw):
        self.prompts.append(prompt)
        self.max_tokens_seen.append(kw.get("max_tokens"))
        if not self.decisions:
            return json.dumps({"thought": "out of script", "action": "DONE"})
        return self.decisions.pop(0)


def _executor(decisions, results, **kw):
    """`results` maps a tool name to what calling it returns, so a trace
    can script the PAGE as well as the decisions."""
    ex = AgenticExecutor(ScriptedAdapter(decisions), **kw)
    ex._available_tools = lambda: [
        {"qualified_name": "action.browser_navigate", "description": "Open a URL",
         "input_schema": {"properties": {"url": {}}}},
        {"qualified_name": "action.browser_observe", "description": "See the page",
         "input_schema": {"properties": {"session_token": {}}}},
        {"qualified_name": "action.browser_find", "description": "Search the page",
         "input_schema": {"properties": {"session_token": {}, "query": {}}}},
        {"qualified_name": "action.browser_click_element", "description": "Click",
         "input_schema": {"properties": {"session_token": {}, "element_id": {}}}},
        {"qualified_name": "action.write_file", "description": "Write a file",
         "input_schema": {"properties": {"path": {}}}},
    ]
    seq = dict(results)

    def _execute(qname, args):
        got = seq.get(qname, "(ok)")
        return got.pop(0) if isinstance(got, list) else got

    ex._execute = _execute
    return ex


def _call(tool, **args):
    return {"thought": "next", "action": tool, "arguments": args or {"session_token": "t"}}


# ------------------------------- fix 1: the loop says when nothing moved

def test_a_click_that_changes_nothing_is_reported_at_the_next_step():
    """THE missing feedback. The click succeeds, returns a full page, and
    is indistinguishable in the call log from one that sorted the table.
    Only comparing the page before and after tells them apart -- and that
    comparison has to happen while the model can still act on it."""
    ex = _executor(
        [_call("action.browser_observe"),
         _call("action.browser_click_element", session_token="t", element_id="e2"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": DEFAULT_VIEW,
         "action.browser_click_element": DEFAULT_VIEW},   # page never moved
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert "the page is byte-for-byte the same" in out
    assert "was not the control" in out


def test_a_click_that_really_sorts_is_not_second_guessed():
    """The other side of the check. Nagging a model that IS making
    progress is the failure mode opposite the one being fixed."""
    ex = _executor(
        [_call("action.browser_observe"),
         _call("action.browser_click_element", session_token="t", element_id="e2"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": DEFAULT_VIEW,
         "action.browser_click_element": SORTED_VIEW},
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert "byte-for-byte the same" not in out


def test_repeated_ineffective_clicks_escalate_to_changing_tactics():
    """A second identical outcome is not a mis-click to retry. The
    escalation names the two things that would actually work, and says
    plainly that what has been read so far is not the answer -- which is
    exactly the claim the failed run shipped."""
    ex = _executor(
        [_call("action.browser_observe"),
         _call("action.browser_click_element", session_token="t", element_id="e1"),
         _call("action.browser_click_element", session_token="t", element_id="e2"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": DEFAULT_VIEW,
         "action.browser_click_element": DEFAULT_VIEW},
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert "THE PAGE IS STILL UNCHANGED" in out
    assert "browser_find" in out
    assert "URL" in out
    assert "default view, not the" in out


def test_browser_find_output_is_not_used_as_a_page_baseline():
    """The regression that would silently switch the whole check off.

    browser_find returns matched controls in its own format and scans far
    deeper than an observation, so a click compared against it looks
    'changed' every time. Comparing only true page renders is what keeps
    the check honest."""
    assert not is_page_view_tool("action.browser_find")
    assert is_browser_tool("action.browser_find"), "still a browser tool"

    ex = _executor(
        [_call("action.browser_observe"),
         _call("action.browser_find", session_token="t", query="change column"),
         _call("action.browser_click_element", session_token="t", element_id="e211"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": DEFAULT_VIEW,
         "action.browser_find": FIND_OUTPUT,
         "action.browser_click_element": DEFAULT_VIEW},   # still did nothing
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert "byte-for-byte the same" in out, (
        "the click is compared against the last real page, not against the "
        "search results that happened to come between them")


def test_observing_a_page_that_moved_is_not_a_repeated_call():
    """The guard that made the instructions impossible to follow.

    browser_observe takes only a session token, so looking at a page
    AFTER changing it is a byte-identical call. The repeat guard killed a
    live run for exactly that -- 'stopped: repeated the same
    action.browser_observe call' -- on a page that had just been
    navigated somewhere new. The observe -> act -> verify cycle the
    browser rules instruct could never complete.
    """
    ex = _executor(
        [_call("action.browser_observe"),
         _call("action.browser_click_element", session_token="t", element_id="e2"),
         _call("action.browser_observe"),          # identical to step 1
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": [DEFAULT_VIEW, SORTED_VIEW],
         "action.browser_click_element": SORTED_VIEW},
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert "repeated the same" not in out
    assert out.count("action.browser_observe") >= 2, "the second look really ran"


def test_observing_an_unchanged_page_twice_is_still_stopped():
    """The other half. Allowing repeats unconditionally would trade one
    stuck loop for another."""
    ex = _executor(
        [_call("action.browser_observe"),
         _call("action.browser_observe"),
         _call("action.browser_observe"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": DEFAULT_VIEW},   # nothing ever moves
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert "you already made this exact" in out or "repeated the same" in out


def test_a_non_browser_repeat_is_unaffected():
    ex = _executor(
        [_call("action.write_file", path="a.md"),
         _call("action.write_file", path="a.md"),
         {"thought": "done", "action": "DONE"}],
        {"action.write_file": "wrote a.md"},
    )
    out = ex.run(PLAIN_TASK, role="Report Producer") or ""
    assert "you already made this exact" in out


def test_the_loop_and_the_gate_agree_on_what_changed_means():
    """One definition, shared. Two would drift, and the loop would
    encourage exactly what the gate refuses."""
    assert page_view_changed(DEFAULT_VIEW, SORTED_VIEW)
    assert not page_view_changed(DEFAULT_VIEW, DEFAULT_VIEW)
    assert is_interaction_tool("action.browser_click_element")
    assert not is_interaction_tool("action.browser_observe")


# ---------------------------------------- fix 2: prefer the URL over the UI

def test_the_browser_rules_lead_with_the_url():
    rules = BROWSER_RULES.lower()
    assert "prefer a url over clicking" in rules
    assert "browser_find" in rules
    assert "check the page actually changed" in rules
    assert "read the rows last" in rules


def test_browser_rules_appear_once_a_browser_tool_actually_works():
    """Triggered by evidence, not by guessing from the task's wording --
    the same rule every other check in this loop follows."""
    ex = _executor(
        [_call("action.browser_observe"),
         _call("action.browser_observe", session_token="u"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": DEFAULT_VIEW},
    )
    ex.run(BROWSE_TASK, role="Web Operator")
    prompts = ex.adapter.prompts
    assert "PREFER A URL OVER CLICKING" not in prompts[0], "not before any evidence"
    assert "PREFER A URL OVER CLICKING" in prompts[1], "present once the browser is in play"


def test_a_non_browser_task_never_pays_for_browser_rules():
    ex = _executor(
        [_call("action.write_file", path="notes.md"),
         {"thought": "done", "action": "DONE"}],
        {"action.write_file": "wrote notes.md"},
    )
    ex.run(PLAIN_TASK, role="Report Producer")
    assert all("PREFER A URL" not in p for p in ex.adapter.prompts)


# ------------------------------------------------- fix 3: search, don't scan

def test_find_ranks_the_control_above_the_noise():
    """Caught a real bug on first run: 'Change password' outranked
    'Change %' for a query about the change column, because the role
    synonyms for 'column' and 'header' credited a plain button exactly as
    much as an actual columnheader. A wrong element reached through a
    genuine match -- which is the kind that reads as working."""
    from backend.app.tools.browser_observation import Observation, find_elements
    obs = Observation({"url": "x", "elements": [
        {"id": "e1", "role": "link", "name": "Exchange rates"},
        {"id": "e2", "role": "button", "name": "Change password"},
        {"id": "e3", "role": "columnheader", "name": "Change %"},
        {"id": "e4", "role": "button", "name": "Overview"},
    ]}, 1)
    hits = find_elements(obs, "weekly change column header")
    assert hits and hits[0]["id"] == "e3"


def test_the_primary_meaning_of_a_role_word_beats_its_fallbacks():
    """The weighting that fixed the case above. 'dropdown' means a
    combobox first and a menuitem only if there is no combobox."""
    from backend.app.tools.browser_observation import _role_weights
    w = _role_weights(["dropdown"])
    assert w["combobox"] > w["menuitem"] > 0


def test_a_sort_control_is_named_by_its_column():
    """THE root cause of six failed TradingView runs, found by reading
    the live DOM rather than the deliverable.

    Its sort controls are icon-only buttons inside each <th>, and all
    twelve carry the identical aria-label "Sort descending". An
    observation therefore offered the model twelve indistinguishable
    buttons, so it clicked the TOOLBAR's filter buttons instead -- those
    have distinct names -- and the table never sorted. Six runs of
    'it cannot operate the screener' were one missing label.
    """
    from backend.app.tools.browser_observation import _OBSERVE_JS
    assert "closest('th, td')" in _OBSERVE_JS, "cell text must reach the name"
    assert "cellLabel(el, clean(aria))" in _OBSERVE_JS, (
        "an aria-label alone is not enough — it is the part that was "
        "identical across every column")
    assert "if (el.closest('th')) return 'columnheader'" in _OBSERVE_JS


def test_column_header_cells_are_addressable():
    """The last mile, measured on the live page.

    TradingView's screener has 14 sort buttons inside <th> of which only
    3 are visible -- the rest are visibility:hidden until hovered -- and
    13 header CELLS, all visible and cleanly labelled. Zero of the cells
    matched the observer's selector, so across six runs the agent was
    never once offered the control that sorts the table."""
    from backend.app.tools.browser_observation import _OBSERVE_JS
    assert "'th', '[role=\"columnheader\"]'" in _OBSERVE_JS


def test_a_hover_revealed_control_is_still_clickable():
    """Playwright runs actionability checks BEFORE it hovers, so a
    control the page reveals under the pointer fails them and the click
    never happens. The ordinary path is unchanged; this is the fallback.

    Verified live: with it, clicking the Chg % header takes the screener
    from NVDA/AAPL/GOOG to TREVQ/ETBI/IOBTQ -- the actual movers."""
    from backend.app.actions.builtin import browser_primitives as bp

    calls = []

    class Inner:
        def count(self):
            return 1

        def click(self, **kw):
            calls.append(("inner_click", kw.get("force")))

    class Loc:
        def __init__(self):
            self.attempts = 0

        def click(self, **kw):
            self.attempts += 1
            if self.attempts == 1 and not kw.get("force"):
                raise RuntimeError("element is not visible")
            calls.append(("outer_click", kw.get("force")))

        def scroll_into_view_if_needed(self, **kw):
            calls.append(("scroll", None))

        def hover(self, **kw):
            calls.append(("hover", kw.get("force")))

        def locator(self, sel):
            calls.append(("locator", sel))
            return type("X", (), {"first": Inner()})()

    class Sess:
        page = type("P", (), {"wait_for_timeout": staticmethod(lambda ms: None)})()

    bp._click_locator(Sess(), Loc())
    kinds = [c[0] for c in calls]
    assert kinds == ["scroll", "hover", "locator", "inner_click"]
    assert calls[-1][1] is True, "the revealed control is clicked with force"


def test_an_ordinary_click_does_not_take_the_fallback():
    """The happy path must stay exactly as it was — no extra hover, no
    force, on every click on every page."""
    from backend.app.actions.builtin import browser_primitives as bp
    calls = []

    class Loc:
        def click(self, **kw):
            calls.append(kw.get("force"))

        def hover(self, **kw):
            raise AssertionError("must not hover when the click worked")

    bp._click_locator(object(), Loc())
    assert calls == [None], "clicked once, without force"


def test_a_column_header_outranks_a_toolbar_button_of_the_same_name():
    """These really do collide on the real page: a 'Chg %' filter button
    in the toolbar and a 'Chg %' column header that sorts. They score
    identically on name, and document order handed it to the filter --
    which is what the agent clicked, six runs running."""
    from backend.app.tools.browser_observation import Observation, find_elements
    obs = Observation({"url": "x", "elements": [
        {"id": "e22", "role": "button", "name": "Chg %"},          # toolbar filter, earlier
        {"id": "e57", "role": "columnheader", "name": "Chg %"},    # the sort control
    ]}, 1)
    assert find_elements(obs, "Chg %")[0]["id"] == "e57"
    assert find_elements(obs, "change % column header to sort by")[0]["id"] == "e57"


def test_a_generic_action_word_does_not_become_part_of_a_column_name():
    """My own first version of the cell-label fix caused this: appending
    the control's label produced 'Mkt cap — Change sort', and a search
    for 'Change %' ranked THAT above 'Chg %' -- the word 'change' was
    sitting in a suffix that says nothing about which column it is."""
    from backend.app.tools.browser_observation import _OBSERVE_JS
    assert "GENERIC_ACTION" in _OBSERVE_JS
    assert "GENERIC_ACTION.test(own)" in _OBSERVE_JS

    from backend.app.tools.browser_observation import Observation, find_elements
    obs = Observation({"url": "x", "elements": [
        {"id": "e61", "role": "columnheader", "name": "Mkt cap"},
        {"id": "e57", "role": "columnheader", "name": "Chg %"},
    ]}, 1)
    hits = find_elements(obs, "Change %")
    assert hits and hits[0]["id"] == "e57", "the market-cap column is not a change column"


def test_a_substring_does_not_beat_a_whole_word():
    from backend.app.tools.browser_observation import Observation, find_elements
    obs = Observation({"url": "x", "elements": [
        {"id": "e1", "role": "button", "name": "Exchange"},
        {"id": "e2", "role": "button", "name": "Change"},
    ]}, 1)
    assert find_elements(obs, "change")[0]["id"] == "e2"


def test_find_scans_deeper_than_an_observation_shows():
    """The whole point. The cap on browser_observe exists so its output
    stays readable; a search has no such constraint, and the control that
    was never listed is the one that stopped four runs."""
    from backend.app.tools.browser_observation import FIND_SCAN_LIMIT, MAX_ELEMENTS
    assert FIND_SCAN_LIMIT > MAX_ELEMENTS * 3


def test_observe_page_honours_a_deeper_limit():
    from backend.app.tools.browser_observation import ElementMap, observe_page

    class FakePage:
        url = "https://x.test"

        def evaluate(self, _js, arg):
            FakePage.seen = arg["limit"]
            return {"url": self.url, "elements": [], "text": ""}

    observe_page(FakePage(), ElementMap(), limit=600)
    assert FakePage.seen == 600


def test_a_no_match_points_somewhere_useful():
    """'Not found' has to say what to do next, or the model just rewords
    the query until the step budget is gone."""
    from backend.app.tools.browser_observation import Observation, render_matches
    text = render_matches(Observation({"url": "https://x.test", "elements": []}, 1),
                          "sort control", [])
    assert "NO MATCH" in text
    assert "Scroll" in text and "URL" in text
    # Order matters. Leading with the URL sent a live run to a query
    # parameter TradingView ignores -- the page loaded, the address bar
    # showed the sort, and the rows were the default ones. Switching the
    # view is what finds a missing column.
    assert text.index("Switch views") < text.index("Only then, the URL")
    assert "looks right and shows the default data" in text.replace("\n", " ")


def test_find_reports_how_many_elements_it_considered():
    """'searched 412 elements' is what tells a model the control is
    genuinely absent, rather than that it phrased the query badly."""
    from backend.app.tools.browser_observation import Observation, render_matches
    obs = Observation({"url": "https://x.test", "elements": [
        {"id": f"e{i}", "role": "button", "name": f"b{i}"} for i in range(1, 31)
    ]}, 1)
    assert "SEARCHED 30 element(s)" in render_matches(obs, "b7", [obs.elements[6]])


def test_find_is_registered_as_a_real_tool():
    from backend.app.actions.builtin.browser_primitives import ALL_SPECS, FIND_SPEC
    assert FIND_SPEC in ALL_SPECS
    assert FIND_SPEC.name == "browser_find"
    assert {p["name"] for p in FIND_SPEC.parameters} == {"session_token", "query"}
    assert FIND_SPEC.mutating is False
    # The description has to say WHY to reach for it over observe, or it
    # is just another tool in a list of thirty.
    assert "INSTEAD OF browser_observe" in FIND_SPEC.description


# ------------------------------------------------------- fix 4: the budgets

def test_browser_budgets_are_bigger_where_it_matters():
    assert BROWSER_MAX_STEPS >= 2 * MAX_STEPS
    assert BROWSER_DEADLINE_SECONDS >= 2 * DEADLINE_SECONDS
    # The one with recorded evidence: the SAME model failed at 700 tokens
    # per step and succeeded at 3000.
    assert BROWSER_STEP_MAX_TOKENS >= 3000 > STEP_MAX_TOKENS


def test_the_budget_widens_once_a_browser_is_really_in_use():
    ex = _executor(
        [_call("action.browser_observe"),
         _call("action.browser_observe", session_token="u"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": DEFAULT_VIEW},
    )
    ex.run(BROWSE_TASK, role="Web Operator")
    assert ex._budget_widened
    assert f"of {BROWSER_MAX_STEPS} steps" in ex.adapter.prompts[1]
    assert ex.adapter.max_tokens_seen[0] == STEP_MAX_TOKENS
    assert ex.adapter.max_tokens_seen[1] == BROWSER_STEP_MAX_TOKENS


def test_a_founder_who_set_a_budget_keeps_it():
    """A settings page that gets silently overridden is a suggestion box.
    Someone who set max_steps=4 meant 4, browser or not."""
    ex = _executor(
        [_call("action.browser_observe"),
         _call("action.browser_observe", session_token="u"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": DEFAULT_VIEW},
        max_steps=4, deadline_seconds=30.0, step_max_tokens=500,
    )
    ex.run(BROWSE_TASK, role="Web Operator")
    assert not ex._budget_widened
    assert "of 4 steps" in ex.adapter.prompts[1]
    assert set(ex.adapter.max_tokens_seen) == {500}


def test_a_non_browser_run_keeps_the_ordinary_budget():
    ex = _executor(
        [_call("action.write_file", path="notes.md"),
         {"thought": "done", "action": "DONE"}],
        {"action.write_file": "wrote notes.md"},
    )
    ex.run(PLAIN_TASK, role="Report Producer")
    assert not ex._budget_widened
    assert set(ex.adapter.max_tokens_seen) == {STEP_MAX_TOKENS}


def test_widening_does_not_leak_into_the_next_run():
    """Budgets are per-run. An executor that drove a browser once must not
    hand a doubled budget to an unrelated task afterwards."""
    ex = _executor(
        [_call("action.browser_observe"), {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": DEFAULT_VIEW},
    )
    ex.run(BROWSE_TASK, role="Web Operator")
    assert ex._budget_widened
    ex.adapter.decisions = [json.dumps({"thought": "done", "action": "DONE"})]
    ex.run(PLAIN_TASK, role="Report Producer")
    assert not ex._budget_widened
    assert ex.max_steps == MAX_STEPS, "the configured budget is untouched"


def test_ineffective_click_allowance_is_small():
    assert 1 <= MAX_INEFFECTIVE_INTERACTIONS <= 3, (
        "enough to rule out one mis-click, not enough to spend the budget "
        "proving the same control does nothing")


# ------------- found by the live trace: the URL shortcut could not work

def test_a_failed_navigation_is_not_recorded_as_a_success():
    """The live trace caught this on the one step that mattered. The
    agent tried the URL shortcut, the launch was refused because the
    profile was already held, and the ledger recorded ok=True -- so
    nothing downstream could tell that the strategy had been blocked
    rather than tried and found wanting.

    Same bug class as the last two: a hand-maintained list of failure
    strings that this wording was not in. Matched by SHAPE now."""
    from backend.app.tools.tool_registry import _looks_failed
    assert _looks_failed(
        "(browser_navigate failed: could not open a browser session — "
        "the profile is already in use by another context)")
    assert _looks_failed("(browser_navigate failed: could not read the page — Timeout)")
    assert _looks_failed("(screenshot unavailable: display not found)")
    assert _looks_failed("(export was not permitted on this page)")


def test_real_browser_output_is_not_mistaken_for_a_failure():
    """The shape rule has to stay narrow. Page text and table dumps open
    with parentheses all the time."""
    from backend.app.tools.tool_registry import _looks_failed
    for ok in (
        "URL: https://x.com\nINTERACTIVE ELEMENTS:\n  e1 button",
        'Clicked e17 "Deploy".',
        "(Cells are pipe-separated and empty cells are shown as (blank).)",
        'SEARCHED 412 element(s) on https://x for "change".',
        "Python output:\nSharpe 0.74",
    ):
        assert not _looks_failed(ok), ok


def test_navigating_again_reuses_the_open_window():
    """The blocker under the blocker.

    One on-disk profile can be held by one context, so launching a second
    browser cannot succeed while the first is alive. Every 'navigate to
    the sorted URL' was therefore doomed from the moment step 1 opened a
    page -- the agent picked the right strategy and the plumbing refused
    it, then dropped it into a fresh session that had none of its state.
    """
    from backend.app.actions.builtin import browser_task

    class FakePage:
        url = "https://site.test/screener?sort=change&order=desc"

        def goto(self, url, **kw):
            FakePage.went_to = url

    class FakeSession:
        token = "bsess_existing"
        page = FakePage()

        def touch(self):
            pass

        def set_status(self, *a):
            pass

    class FakeMgr:
        launched = 0

        def sweep_idle(self):
            return 0

        def get(self, token):
            return FakeSession() if token == "bsess_existing" else None

        def current(self):
            return FakeSession()

        def goto(self, session, url):
            session.page.goto(url)

        def create(self, url, prefer_headless=False):
            FakeMgr.launched += 1
            raise AssertionError("must not launch a second browser")

    real_mgr = browser_task.get_manager
    real_snap = browser_task._snapshot_page
    browser_task.get_manager = lambda: FakeMgr()
    browser_task._snapshot_page = lambda page: {"url": page.url, "title": "Screener",
                                                "text": "rows"}
    try:
        out = browser_task._browser_navigate_impl(
            {"url": "https://site.test/screener?sort=change&order=desc"})
    finally:
        browser_task.get_manager = real_mgr
        browser_task._snapshot_page = real_snap

    assert FakeMgr.launched == 0, "reused the open window instead of launching"
    assert FakePage.went_to == "https://site.test/screener?sort=change&order=desc"
    assert "bsess_existing" in out, "the caller keeps the same session"


def test_navigate_advertises_that_a_second_call_moves_the_same_window():
    """The model has to know reuse is available, or it will keep treating
    a new URL as a new browser."""
    from backend.app.actions.builtin.browser_task import BROWSER_NAVIGATE_SPEC
    desc = BROWSER_NAVIGATE_SPEC.description
    assert "same window" in desc.lower()
    assert "session_token" in {p["name"] for p in BROWSER_NAVIGATE_SPEC.parameters}


def test_newest_returns_the_live_resource():
    from backend.app.actions.live_session_manager import LiveSessionManager

    class R:
        def __init__(self, n):
            self.n = n

        def touch(self):
            pass

    m = LiveSessionManager(idle_timeout_seconds=999, closer=lambda r: None)
    assert m.newest() is None
    a, b = R(1), R(2)
    m.register("a", a)
    m.register("b", b)
    assert m.newest() is b
    m.close("b")
    assert m.newest() is a


# -------------------------------------------- fix 5: a stronger loop model

def test_adapters_are_built_once_per_model():
    """This now runs per employee per run rather than once per process.
    Rebuilding each time would add a construction -- and, on the start-up
    path this copies, a live health probe -- to every specialist."""
    from backend.app.models import adapter_pool
    adapter_pool.reset()
    try:
        first = adapter_pool.get_adapter("nemotron-3-super:cloud", default="fallback")
        second = adapter_pool.get_adapter("nemotron-3-super:cloud", default="fallback")
        assert first is second
        assert adapter_pool.pooled_names() == ("nemotron-3-super:cloud",)
    finally:
        adapter_pool.reset()


def test_an_empty_model_name_means_inherit():
    from backend.app.models import adapter_pool
    assert adapter_pool.get_adapter("", default="pipeline") == "pipeline"
    assert adapter_pool.get_adapter(None, default="pipeline") == "pipeline"


def test_a_broken_model_name_falls_back_instead_of_failing_the_run():
    """A typo in a settings field must not cost a founder their run."""
    import backend.app.models.provider_adapters.ollama_adapter as oa
    from backend.app.models import adapter_pool
    adapter_pool.reset()
    original = oa.OllamaAdapter

    def _boom(**kw):
        raise RuntimeError("no such model")

    oa.OllamaAdapter = _boom
    try:
        assert adapter_pool.get_adapter("typo:cloud", default="pipeline") == "pipeline"
        # Cached as failed, so it degrades quietly rather than logging on
        # every step of every run.
        assert adapter_pool.get_adapter("typo:cloud", default="pipeline") == "pipeline"
        assert adapter_pool.pooled_names() == ()
    finally:
        oa.OllamaAdapter = original
        adapter_pool.reset()


def test_the_env_override_still_outranks_per_employee_config(monkeypatch):
    """A process-wide diagnostic knob has to beat configuration, or
    changing one variable to answer a question means auditing every
    employee first."""
    from backend.app.orchestrator import execution_loop
    from backend.app.models import adapter_pool
    adapter_pool.reset()
    seen = []

    def _fake(name, default=None):
        seen.append(name)
        return f"adapter:{name}"

    monkeypatch.setattr(adapter_pool, "get_adapter", _fake)
    monkeypatch.setenv("AGENT_LOOP_MODEL", "from-env:cloud")
    assert execution_loop._loop_adapter("pipeline", "from-config:cloud") == "adapter:from-env:cloud"

    monkeypatch.delenv("AGENT_LOOP_MODEL", raising=False)
    assert execution_loop._loop_adapter("pipeline", "from-config:cloud") == "adapter:from-config:cloud"
    assert execution_loop._loop_adapter("pipeline", None) == "pipeline"
    assert seen == ["from-env:cloud", "from-config:cloud"]


def test_config_carries_the_loop_model_into_the_executor():
    """The seam. `model.loop_model` has existed in the config schema and
    reached nothing."""
    import inspect
    from backend.app.employees import dynamic_employee
    src = inspect.getsource(dynamic_employee.DynamicEmployee.run_task)
    assert "loop_model=self.config.loop_model" in src
