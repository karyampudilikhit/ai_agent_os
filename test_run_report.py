"""The final answer's numbers come from the record, not from memory.

THE FAILURE THIS ANSWERS. Asked for ten items, a run that verified two
would say "here are the internships I found" and list whatever it had.
The count, the confidence and the framing were all the model's, written
from a transcript it had partly forgotten. The system already KNEW —
item_state had the count, the ledger had the pages — and none of it
reached the page the founder read, because the last step of every run
was a language model writing prose about its own work.

Nothing in run_report asks a model for anything. Every number is counted
from ledger-derived state, and the sentences around them are fixed text
selected by which numbers came back. A model cannot make this say ten by
being confident.
"""
from __future__ import annotations

from backend.app.browser.observation import DATA_PREFIX
from backend.app.browser.policy import wrap_untrusted
from backend.app.orchestrator.goal_spec import from_task
from backend.app.orchestrator.item_state import derive
from backend.app.orchestrator.item_verification import verify
from backend.app.orchestrator.relevance import subject_of
from backend.app.orchestrator.run_report import build, for_run
from backend.app.orchestrator.task_graph import build as build_plan

INTERNSHIPS = ("Find 10 Product Manager internships in India posted within "
               "the last 7 days, visit each job page, extract company, role, "
               "location, posting date and application URL.")
LISTING = "https://internshala.com/internships/product-management-internship"
ITEM = "https://internshala.com/internship/detail/job-{}"


def view(url, title):
    return "\n".join([f"URL: {url}", f"TITLE: {title}",
                      f"{DATA_PREFIX} (no data table on this page)", "",
                      "INTERACTIVE ELEMENTS:", '  e1  link "Apply"'])


def records(url, links):
    body = [f"[records — {url}]",
            f"{len(links)} similar item(s) on the page, showing {len(links)}.", ""]
    for i, href in enumerate(links, 1):
        body += [f"--- record {i} ---", f"title: Intern {i}", f"link: {href}", ""]
    return wrap_untrusted("\n".join(body), url)


def call(tool, out, at, ok=True, args=""):
    return {"tool": tool, "ok": ok, "at": at, "output": out, "args_text": args}


def a_run(opened, body="Product Manager Intern. Own the product roadmap.",
          found=10, blocked_urls=()):
    links = [ITEM.format(i) for i in range(1, found + 1)]
    calls = [call("action.browser_navigate", view(LISTING, "PM internships"), 1.0),
             call("action.browser_extract_records", records(LISTING, links), 2.0)]
    at = 3.0
    for href in links[:opened]:
        calls.append(call("action.browser_navigate", view(href, "Intern"), at))
        calls.append(call("action.browser_extract", wrap_untrusted(body, href),
                          at + 0.5))
        at += 1.0
    for href in blocked_urls:
        calls.append(call("action.browser_navigate", "", at, ok=False,
                          args=f'{{"url": "{href}"}}'))
        at += 1.0
    return calls


def report_for(calls):
    spec = from_task(INTERNSHIPS)
    plan = build_plan(INTERNSHIPS, 22, spec=spec)
    prog = derive(calls, plan.feasible_items)
    verdicts = verify(prog, subject_of(INTERNSHIPS, spec))
    return build(INTERNSHIPS, spec=spec, plan=plan, prog=prog, verdicts=verdicts)


def test_it_states_the_real_count_not_the_asked_for_one():
    text = report_for(a_run(opened=2))
    assert "asked for : 10 items" in text
    assert "verified  : 2" in text
    assert "SHORTFALL: 8 of 10" in text


def test_a_complete_run_states_no_shortfall():
    text = report_for(a_run(opened=10))
    assert "verified  : 10" in text
    assert "SHORTFALL" not in text


def test_blocked_and_never_opened_are_different_answers():
    """"the site refused three of them" is a fact about the world;
    "I ran out of steps" is a fact about the budget."""
    text = report_for(a_run(opened=2, blocked_urls=[ITEM.format(9)]))
    assert "blocked   : 1" in text
    assert "not opened: 7" in text


def test_a_rejected_item_is_named_with_its_reason():
    """"3 rejected" with no reasons is a number the founder cannot
    check and cannot argue with."""
    text = report_for(a_run(opened=2, body="Fashion Merchandising Intern — "
                                           "assist the buying team."))
    assert "rejected  : 2" in text
    assert "REJECTED, and why:" in text
    assert "product" in text


def test_it_forbids_padding_the_difference():
    text = report_for(a_run(opened=3))
    assert "Do NOT" in text and "results list" in text


def test_the_budget_verdict_rides_along():
    spec = from_task(INTERNSHIPS)
    plan = build_plan(INTERNSHIPS, 10, spec=spec)      # 25 needed, 10 available
    assert plan.reason
    prog = derive(a_run(opened=1), plan.feasible_items)
    text = build(INTERNSHIPS, spec=spec, plan=plan, prog=prog,
                 verdicts=verify(prog, subject_of(INTERNSHIPS, spec)))
    assert "BUDGET" in text


def test_errors_are_reported_not_swallowed():
    spec = from_task(INTERNSHIPS)
    text = build(INTERNSHIPS, spec=spec, verdicts=[],
                 errors=["browser_navigate timed out on internshala.com"])
    assert "ERRORS ENCOUNTERED" in text and "timed out" in text


def test_an_open_ended_task_gets_no_report():
    """A report that says "0 of 0" is noise in a prompt."""
    task = "Research the Indian EV market and write it up."
    assert build(task, spec=from_task(task), verdicts=[]) == ""


def test_for_run_rebuilds_everything_from_the_ledger_alone():
    """The gate never held the loop's plan object. Task plus calls has
    to be enough."""
    text = for_run(INTERNSHIPS, a_run(opened=2))
    assert "verified  : 2" in text and "SHORTFALL: 8 of 10" in text


def test_for_run_is_silent_on_a_shapeless_task():
    assert for_run("Tell me about the Indian EV market.", a_run(opened=2)) == ""


def test_for_run_never_raises_on_junk():
    assert for_run(INTERNSHIPS, [{"nonsense": True}]) is not None
    assert for_run("", []) == ""


def test_a_blocked_run_still_tells_the_founder_what_it_got():
    """A refusal that reports nothing is worse than the overclaim it
    prevented — the founder loses both the answer and the work."""
    import inspect
    from backend.app.api import routes
    src = inspect.getsource(routes._gate_deliverable)
    assert "for_run(" in src
    assert "DeliverableBlocked" in src.partition("for_run(")[2]
