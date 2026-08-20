"""Know what was asked for, and whether it fits, before spending.

THE FAILURE THIS ANSWERS. A run was asked for ten internships, each
verified on its own page -- roughly three calls to find a source plus two
per item, about twenty-five against a browser budget of twenty-two. It
was unwinnable before its first step. It spent all twenty calls on
results pages, opened none of the ten, and produced nothing.

Twenty steps of silence is the worst available answer. "This needs about
twenty-five steps and I have twenty-two, so I will verify eight properly
and say so" is a good one, and it costs one step to decide.

THE SAFETY PROPERTY, checked hardest below: a task whose shape is NOT
clearly stated must behave exactly as it did before any of this existed.
A wrong plan is worse than no plan -- it would execute confidently in the
wrong direction, where today the system merely wanders.
"""
from __future__ import annotations

from backend.app.orchestrator.goal_spec import from_task
from backend.app.orchestrator.task_graph import (
    DESCOPE, REFUSE, RUN, build,
)

INTERNSHIPS = (
    "Find 10 Product Manager internships in India posted within the last 7 "
    "days, visit each job page, extract company, role, location, posting "
    "date and application URL, remove duplicates, and give me the top 10."
)


# ------------------------------------------------- reading the shape

def test_the_item_count_is_read():
    assert from_task(INTERNSHIPS).item_count == 10


def test_per_item_work_is_recognised():
    """"visit each job page" is the difference between reading one list
    and opening ten pages -- a factor of twenty in cost."""
    assert from_task(INTERNSHIPS).per_item_work is True


def test_a_recency_window_is_read():
    assert from_task(INTERNSHIPS).recency_days == 7


def test_fields_are_read():
    fields = from_task(INTERNSHIPS).per_item_fields
    for f in ("company", "role", "location", "date"):
        assert f in fields, fields


def test_a_time_window_is_not_an_item_count():
    """"within the last 7 days" must not read as seven items."""
    spec = from_task("Find 3 articles posted in the last 30 days")
    assert spec.item_count == 3


def test_sentences_are_not_items():
    """"the first three sentences" is ONE answer, not three items --
    and treating it as three would budget for three page visits."""
    spec = from_task("Go to the AI page and give me the first three sentences.")
    assert spec.item_count == 1
    assert spec.confident is False


# ------------------------------------- silence when the shape is unclear

def test_an_open_ended_task_states_no_shape():
    spec = from_task("Research the competitive landscape and write it up.")
    assert spec.confident is False


def test_an_unclear_task_is_never_refused():
    """The safety property. An unrecognised goal runs exactly as before."""
    plan = build("Research the landscape and write it up.", budget=5)
    assert plan.verdict == RUN
    assert plan.est_calls == 0
    assert plan.note() == ""


def test_a_single_page_read_is_comfortable():
    plan = build("Go to the AI page and give me the first three sentences.",
                 budget=22)
    assert plan.verdict == RUN
    assert plan.note() == ""


# ------------------------------------------------- the budget verdict

def test_the_internship_task_is_descoped_not_attempted_whole():
    """The exact run that returned nothing."""
    plan = build(INTERNSHIPS, budget=22)
    assert plan.verdict == DESCOPE
    assert plan.est_calls > 22
    assert 1 <= plan.feasible_items < 10


def test_the_descope_note_forbids_padding():
    """Eight verified is worth more than ten unverified. The one thing
    that must not happen is quietly filling the rest from the list."""
    note = build(INTERNSHIPS, budget=22).note()
    assert "PROPERLY" in note
    assert "Do NOT pad" in note
    assert "unverified item is not one" in note


def test_an_impossible_task_is_refused_before_starting():
    plan = build(INTERNSHIPS, budget=4)
    assert plan.verdict == REFUSE
    assert plan.feasible_items == 0
    assert "do not begin" in plan.note()


def test_a_bigger_budget_makes_the_same_task_fit():
    """The verdict is about the pair, not the task -- which is why the
    loop recomputes it when the browser budget widens."""
    assert build(INTERNSHIPS, budget=60).verdict == RUN


def test_five_items_with_no_per_item_work_is_cheap():
    """Five rows off one sorted table is not five page visits."""
    plan = build("Find the top 5 gainers by weekly percentage change.",
                 budget=22)
    assert plan.verdict == RUN
    assert plan.est_calls <= 10


# --------------------------------------------------- wired to the loop

def test_the_loop_builds_a_plan_before_it_starts():
    import inspect
    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    assert "build_plan(task, max_steps" in src
    # The loop is a while with a manual counter, not a for-range.
    assert src.index("build_plan(task, max_steps") < src.index("step_i = -1")


def test_the_plan_is_recomputed_when_the_browser_budget_widens():
    """The same goal is affordable at 22 steps and not at 10."""
    import inspect
    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    assert src.count("build_plan(task, max_steps") >= 2


def test_a_founders_explicit_budget_is_never_overridden():
    """Refusing a task because OUR estimate calls it tight would override
    a decision the founder made deliberately."""
    import inspect
    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    assert "plan.verdict == REFUSE and self._budget_is_default" in src


def test_the_verdict_reaches_the_model():
    import inspect
    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    assert "plan_note" in src
    assert "standing_rules=(" in src
