"""The five fixes from the TradingView autopsy.

The run that motivated these: asked for the top 5 weekly gainers and
losers, Vision AI read the screener's DEFAULT market-cap view at step 2,
failed three attempts to sort it, and reported that default view as the
answer. Every price in it was real. Every row was wrong.

Nothing errored. That is what makes it worth testing carefully.
"""
from __future__ import annotations

from backend.app.orchestrator.output_contract import (
    RANKED_RESULT,
    REFUSAL_TEXT,
    satisfied_kinds,
    task_wants_ranking,
)
from backend.app.tools.tool_registry import _looks_failed
from backend.app.tools.tool_registry import get_registry as tool_registry


def _c(tool, at, ok=True, view=""):
    """`view` is the tool's captured output — what the page looked like
    after the call. Comparing views is how a click that sorted the table
    is told apart from a click the page ignored."""
    return {"tool": tool, "at": at, "ok": ok, "args_text": "{}", "output": view}


DEFAULT_VIEW = "URL: /screener\nAAPL 305.93 +0.22%\nNVDA 225.16 -0.06%\nMSFT 495.40 -0.30%"
SORTED_VIEW = "URL: /screener\nXYZ 12.40 +38.10%\nABC 3.02 +31.7%\nQRS 88.10 +27.4%"


# ------------------------------------- fix 1: failures are recorded

def test_a_failed_browser_action_is_recognised_as_failed():
    """Step 8's click failed and the ledger recorded it as ok=True,
    because success was inferred from 'no exception'. Handlers report
    failure as text, so it has to be read as text."""
    assert _looks_failed("(click on e40 failed: timeout)")
    assert _looks_failed("(refused: bank.com is outside this task's allowed domains)")
    assert _looks_failed("(that browser session no longer exists)")
    assert _looks_failed("(browser action failed: Page closed)")
    assert _looks_failed("Python exited with code 1. This code did NOT run")


def test_a_real_result_is_not_mistaken_for_a_failure():
    assert not _looks_failed("URL: https://x.com\nINTERACTIVE ELEMENTS:\n  e1 button")
    assert not _looks_failed("Clicked e17 \"Deploy\".")
    assert not _looks_failed("Python output:\nSharpe 0.74")


def test_browser_results_are_kept_whole_for_auditing():
    """A 200-char preview cut off before anything diagnostic. The click
    that failed left no recoverable reason at all."""
    from backend.app.tools.tool_call_ledger import ToolCallLedger
    led = ToolCallLedger()
    long_err = "(click on e40 failed: " + "x" * 500 + ")"
    led.record("action.browser_click_element", {"element_id": "e40"},
               ok=False, result_preview=long_err[:200], full_output=long_err)
    kept = [c for c in led.calls() if c.get("output")]
    assert kept and len(kept[0]["output"]) > 400


# --------------------------------- fix 2: the deliverable contract

def test_a_ranking_task_is_detected():
    for t in ("top 5 gainers and losers of the week",
              "which are the best performing sectors",
              "sort by highest revenue",
              "show me the biggest movers today",
              "worst performing funds this quarter"):
        assert task_wants_ranking(t), t


def test_an_ordinary_task_is_not_treated_as_a_ranking():
    for t in ("summarise the attached meeting notes",
              "draft an email to the investor",
              "what does this company do"):
        assert not task_wants_ranking(t), t


def test_the_exact_failed_run_would_now_be_refused():
    """The real trace. Note step 6's click SUCCEEDED — it simply landed
    on something that was not the sort control, so the page did not move.
    A contract that only asked 'did a click succeed' passes this run,
    which is exactly why the view comparison is the load-bearing part."""
    trace = [
        _c("action.browser_navigate", 1, view=DEFAULT_VIEW),
        _c("action.browser_extract_table", 2, view=DEFAULT_VIEW),
        _c("action.browser_observe", 5, view=DEFAULT_VIEW),
        _c("action.browser_click_element", 6, view=DEFAULT_VIEW),   # clicked, nothing moved
        _c("action.browser_extract", 7, view=DEFAULT_VIEW),
        _c("action.browser_click_element", 8, ok=False, view=""),   # failed outright
    ]
    assert RANKED_RESULT not in satisfied_kinds(set(), False, trace)


def test_a_click_the_page_ignored_does_not_count():
    """The distinction the whole check exists for."""
    trace = [_c("action.browser_observe", 1, view=DEFAULT_VIEW),
             _c("action.browser_click_element", 2, view=DEFAULT_VIEW),
             _c("action.browser_extract_table", 3, view=DEFAULT_VIEW)]
    assert RANKED_RESULT not in satisfied_kinds(set(), False, trace)


def test_reading_before_clicking_is_not_sorting():
    """The run's answer came from an extract taken BEFORE any click."""
    trace = [_c("action.browser_extract_table", 2, view=DEFAULT_VIEW),
             _c("action.browser_click_element", 9, view=SORTED_VIEW)]
    assert RANKED_RESULT not in satisfied_kinds(set(), False, trace)


def test_a_failed_click_cannot_satisfy_the_contract():
    trace = [_c("action.browser_observe", 4, view=DEFAULT_VIEW),
             _c("action.browser_click_element", 5, ok=False, view=SORTED_VIEW),
             _c("action.browser_extract_table", 6, view=SORTED_VIEW)]
    assert RANKED_RESULT not in satisfied_kinds(set(), False, trace)


def test_a_real_sort_followed_by_a_read_satisfies_it():
    trace = [_c("action.browser_navigate", 1, view=DEFAULT_VIEW),
             _c("action.browser_observe", 2, view=DEFAULT_VIEW),
             _c("action.browser_click_element", 3, view=SORTED_VIEW),   # page moved
             _c("action.browser_extract_table", 4, view=SORTED_VIEW)]
    assert RANKED_RESULT in satisfied_kinds(set(), False, trace)


def test_selecting_a_filter_counts_when_it_changes_the_page():
    trace = [_c("action.browser_observe", 2, view=DEFAULT_VIEW),
             _c("action.browser_select", 3, view=SORTED_VIEW),
             _c("action.browser_extract_table", 4, view=SORTED_VIEW)]
    assert RANKED_RESULT in satisfied_kinds(set(), False, trace)


def test_the_refusal_names_what_went_wrong():
    text = REFUSAL_TEXT[RANKED_RESULT]
    assert "default view is not a ranking" in text
    assert "every number in it was real and every row was wrong" in text
    assert "browser_select" in text or "browser_click_element" in text


# ------------------------------------------- fix 3: tool ranking

def test_arc_tools_are_hidden_from_unrelated_tasks():
    """`arc_click` ranked FIRST for a TradingView task — BM25 matched
    'click' — while browser_click_element missed the top five."""
    names = [t["qualified_name"] for t in tool_registry().list_tools(
        for_planner=True, task_hint="open tradingview and find the top gainers")]
    assert "action.arc_click" not in names
    assert "action.arc_reset" not in names
    assert "action.browser_click_element" in names, "the real tool must survive"


def test_arc_tools_are_still_available_to_arc_tasks():
    """Hidden until mentioned — not removed. This is the difference
    between niche_keywords and planner_excluded."""
    names = [t["qualified_name"] for t in tool_registry().list_tools(
        for_planner=True, task_hint="play the arc-agi grid game vc33-5430563c")]
    assert "action.arc_click" in names


def test_no_hint_keeps_everything_visible():
    """An un-migrated caller must lose nothing."""
    names = [t["qualified_name"] for t in tool_registry().list_tools(for_planner=True)]
    assert "action.arc_click" in names


# ------------------------------------- fix 4: headed when operating

def test_navigate_accepts_an_interactive_flag():
    from backend.app.actions.builtin.browser_task import BROWSER_NAVIGATE_SPEC
    params = {p["name"] for p in BROWSER_NAVIGATE_SPEC.parameters}
    assert "interactive" in params
    desc = BROWSER_NAVIGATE_SPEC.description
    assert "visible window" in desc
    assert "sort" in desc, "the model needs to know when to set it"


# ------------------------------- fix 5: stop refining what cannot improve

def test_a_minimum_refinement_gain_is_enforced():
    from backend.app.orchestrator.pipeline_controller import MIN_REFINEMENT_GAIN
    assert 0 < MIN_REFINEMENT_GAIN <= 0.2, (
        "must be small enough to allow real progress and large enough to "
        "stop three passes of 0.20 -> 0.20")


# ---------------------------- fix 6: argument types the model sends

def test_json_natural_argument_types_are_accepted():
    """The re-run lost 2 of 8 steps arguing about spelling. The model
    sent `interactive: true` and `table_index: 0` — both exactly right,
    both rejected because the params were declared as strings."""
    from backend.app.actions.builtin.browser_task import (
        BROWSER_EXTRACT_TABLE_SPEC, BROWSER_NAVIGATE_SPEC,
    )
    types = {p["name"]: p["type"]
             for s in (BROWSER_NAVIGATE_SPEC, BROWSER_EXTRACT_TABLE_SPEC)
             for p in s.parameters}
    assert types["interactive"] == "boolean"
    assert types["table_index"] == "integer"


def test_a_bool_for_a_string_param_is_coerced_not_rejected():
    from backend.app.actions.action_registry import ActionSpec, _check_arg_types
    spec = ActionSpec(name="t", description="", handler=lambda a: "",
                      preview=lambda a: "",
                      parameters=[{"name": "flag", "type": "string",
                                   "description": "", "required": False}])
    args = {"flag": True}
    assert _check_arg_types(spec, args) is None
    assert args["flag"] == "true", "coerced in place so the handler sees a string"


def test_an_integer_for_a_string_param_is_still_rejected():
    """The original bug this validator exists for: a planner sent the
    INTEGER 0 where a game id like 'vc33-5430563c' belonged, eight times.
    Coercing that to '0' would hand the guess straight downstream."""
    from backend.app.actions.action_registry import ActionSpec, _check_arg_types
    spec = ActionSpec(name="t", description="", handler=lambda a: "",
                      preview=lambda a: "",
                      parameters=[{"name": "game_id", "type": "string",
                                   "description": "e.g. vc33-5430563c",
                                   "required": True}])
    assert _check_arg_types(spec, {"game_id": 0}) is not None


# ------------------- fix 7: the contract is enforced at the gate too

def test_the_gate_blocks_a_ranking_that_was_never_sorted():
    """The loop refused DONE, then ended on the repeat-guard instead —
    so the contract was never satisfied and the answer shipped anyway.
    A guard that only lives in the loop protects the loop."""
    import inspect
    from backend.app.api import routes
    src = inspect.getsource(routes._gate_deliverable)
    assert "task_wants_ranking" in src
    assert "RANKED_RESULT" in src
    assert "never actually sorted or filtered" in src
