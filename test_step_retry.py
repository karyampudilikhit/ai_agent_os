"""A step that misses its target is redone, not abandoned.

THE ARITHMETIC THIS EXISTS FOR. Per-step accuracy measured on live sites
is about 79%. Over six steps that is 25%, which matches what a screener
sort actually achieved. Over twenty steps it is 0.8%, which is why no
message flow or ad wizard has ever completed.

Failures MULTIPLIED, because nothing sent the agent back to the step that
went wrong. It wandered to a different approach two screens further on,
with the original goal quietly abandoned, and one bad decision at minute
fifteen wasted everything after it.

So the model now declares what should be TRUE after each action, that
claim is checked against the page, and a miss returns the agent to the
same step with the failure named.

Offline: a scripted adapter returns canned decisions and a stubbed
_execute returns canned pages.
"""
from __future__ import annotations

from test_browser_operation import BROWSE_TASK, _executor

BEFORE = (
    "URL: https://site.test/screener\nTITLE: Screener\n"
    "DATA: 100 row(s) | NVDA · AAPL · GOOG\n"
    "SORTED: Mkt cap descending\n\n"
    'INTERACTIVE ELEMENTS:\n  e1 button "Filter"\n  e2 columnheader "Chg %"'
)
# A filter panel opened: the page moved, the data did not.
PANEL_OPEN = (
    "URL: https://site.test/screener\nTITLE: Screener\n"
    "DATA: 100 row(s) | NVDA · AAPL · GOOG\n"
    "SORTED: Mkt cap descending\n\n"
    'INTERACTIVE ELEMENTS:\n  e1 button "Filter"\n  e2 columnheader "Chg %"\n'
    '  e7 combobox "Period"\n  e8 button "Apply"'
)
SORTED_VIEW = (
    "URL: https://site.test/screener\nTITLE: Screener\n"
    "DATA: 100 row(s) | TREVQ · ETBI · IOBTQ\n"
    "SORTED: Chg % descending\n\n"
    'INTERACTIVE ELEMENTS:\n  e1 button "Filter"\n  e2 columnheader "Chg %"'
)


def _act(tool, expect, **args):
    return {"thought": "next", "action": tool,
            "arguments": args or {"session_token": "t"}, "expect": expect}


def test_a_missed_step_is_retried_not_abandoned():
    """The core behaviour. The first click opens a panel instead of
    sorting -- the page moves, the rows do not -- so the agent is sent
    back to the same step rather than carrying on."""
    ex = _executor(
        [_act("action.browser_observe", "I can see the table"),
         _act("action.browser_click_element", "the table is sorted by weekly change",
              session_token="t", element_id="e1"),
         _act("action.browser_click_element", "the table is sorted by weekly change",
              session_token="t", element_id="e2"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": BEFORE,
         "action.browser_click_element": [PANEL_OPEN, SORTED_VIEW]},
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert "THAT STEP DID NOT DO WHAT YOU EXPECTED" in out
    assert "the table is sorted by weekly change" in out, "it quotes the claim back"
    assert "Do NOT move on to a different part of the task" in out


def test_a_step_that_lands_is_not_retried():
    ex = _executor(
        [_act("action.browser_observe", "I can see the table"),
         _act("action.browser_click_element", "the table is sorted by weekly change",
              session_token="t", element_id="e2"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": BEFORE,
         "action.browser_click_element": SORTED_VIEW},
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert "THAT STEP DID NOT DO" not in out


def test_opening_a_panel_counts_when_that_was_the_point():
    """The mirror of the first test, and the reason the judge reads the
    expectation rather than always demanding the data move. Opening a
    filter panel SHOULD leave the rows alone -- judging it on the data
    would fail a step that did exactly what it meant to."""
    ex = _executor(
        [_act("action.browser_observe", "I can see the table"),
         _act("action.browser_click_element", "the period dropdown is open",
              session_token="t", element_id="e1"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": BEFORE,
         "action.browser_click_element": PANEL_OPEN},
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert "THAT STEP DID NOT DO" not in out


def test_retries_are_bounded():
    """A specialist whose control genuinely does not exist must not be
    trapped repeating one step until the budget is gone."""
    from backend.app.orchestrator.step_outcome import MAX_STEP_RETRIES
    assert 1 <= MAX_STEP_RETRIES <= 3
    ex = _executor(
        [_act("action.browser_observe", "I can see the table")]
        + [_act("action.browser_click_element", "the table is sorted by weekly change",
                session_token="t", element_id=f"e{i}") for i in range(1, 8)]
        + [{"thought": "done", "action": "DONE"}],
        {"action.browser_observe": BEFORE,
         "action.browser_click_element": PANEL_OPEN},
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert out.count("THAT STEP DID NOT DO WHAT YOU EXPECTED") <= MAX_STEP_RETRIES


def test_an_unmeasurable_expectation_never_blocks():
    """"the results show Product Manager roles" is a claim about content
    that no mechanical check settles. UNKNOWN must carry on -- a check
    that guessed here would stall honest runs on every page it could not
    read, which is worse than the failure it set out to fix."""
    ex = _executor(
        [_act("action.browser_observe", "I can see the page"),
         _act("action.browser_click_element", "the results show Product Manager roles",
              session_token="t", element_id="e1"),
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": BEFORE,
         "action.browser_click_element": PANEL_OPEN},
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert "THAT STEP DID NOT DO" not in out


def test_a_step_with_no_expectation_is_left_alone():
    """Back-compat: an older caller, or a model that omits the field,
    must behave exactly as before."""
    ex = _executor(
        [{"thought": "look", "action": "action.browser_observe",
          "arguments": {"session_token": "t"}},
         {"thought": "click", "action": "action.browser_click_element",
          "arguments": {"session_token": "t", "element_id": "e1"}},
         {"thought": "done", "action": "DONE"}],
        {"action.browser_observe": BEFORE,
         "action.browser_click_element": PANEL_OPEN},
    )
    out = ex.run(BROWSE_TASK, role="Web Operator") or ""
    assert "THAT STEP DID NOT DO" not in out


def test_the_model_is_asked_for_an_expectation():
    from backend.app.orchestrator.execution_loop import STEP_PROMPT
    assert '"expect"' in STEP_PROMPT
    assert "SAY WHAT SHOULD BE TRUE AFTERWARDS" in STEP_PROMPT
