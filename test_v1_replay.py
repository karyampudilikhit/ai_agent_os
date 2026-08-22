"""The whole V1 chain, replayed against a run that really happened.

WHY A REPLAY TEST AND NOT MORE UNIT TESTS. Every component below passed
its own unit tests before this file existed, and the run they were built
for still shipped eleven internships of which one had been opened. Unit
tests prove a part works on the input you imagined. This proves the
parts compose, on the input the world actually produced.

THE FIXTURE IS A REAL TRACE — twenty-one calls, six hundred and
twenty-seven seconds, the 2026-08-20 internship run, captured by
trace_browser_run.py and trimmed only in the length of each tool output.
Two bugs found while writing this file were invisible to every synthetic
fixture in the repo:

  browser_extract heads its output "[page text — <url>]", a fourth
  address shape nothing was reading, so an extract on an item page never
  counted as reading it. That is P0-1's second cause, open across four
  live runs and guessed wrong in the handoff.

  Counting distinct pages conflated search pages with item pages. The
  run reached thirteen — Indeed, Naukri, LinkedIn, Internshala and
  several search URLs — and a check that read that as "thirteen items is
  plausible" let the exact failure it exists for walk through.

If this file ever passes because the numbers were adjusted to match, it
has stopped doing its job.
"""
from __future__ import annotations

import json
import os

import pytest

FIXTURE = os.path.join(os.path.dirname(__file__), "test_fixtures",
                       "internship_run_trace.json")


@pytest.fixture(scope="module")
def trace():
    if not os.path.exists(FIXTURE):
        pytest.skip("real-trace fixture not present")
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def chain(trace):
    """Everything the V1 path derives from that one run."""
    from backend.app.orchestrator.goal_spec import from_task
    from backend.app.orchestrator.item_state import derive
    from backend.app.orchestrator.item_verification import tally, verify
    from backend.app.orchestrator.relevance import subject_of
    from backend.app.orchestrator.task_graph import build as build_plan

    task, calls = trace["task"], trace["calls"]
    spec = from_task(task)
    plan = build_plan(task, 22, spec=spec)
    prog = derive(calls, plan.feasible_items)
    subj = subject_of(task, spec)
    verdicts = verify(prog, subj)
    return {"task": task, "calls": calls, "spec": spec, "plan": plan,
            "prog": prog, "subj": subj, "verdicts": verdicts,
            "tally": tally(verdicts, wanted=spec.item_count),
            "transcript": trace["transcript"]}


# ------------------------------------------------ the goal was understood

def test_the_goal_shape_is_read_correctly(chain):
    spec = chain["spec"]
    assert spec.confident
    assert spec.item_count == 10
    assert spec.per_item_work
    assert spec.recency_days == 7


def test_the_subject_is_product_manager_internships(chain):
    subj = chain["subj"]
    assert subj.enforceable
    assert {"product", "manag", "intern"} <= subj.distinctive


def test_the_run_was_priced_as_unaffordable_before_it_started(chain):
    plan = chain["plan"]
    assert plan.est_calls == 25 and plan.budget == 22
    assert plan.verdict == "descope" and plan.feasible_items == 8


# --------------------------------------------- what the run actually did

def test_the_candidates_were_found(chain):
    """Nineteen links harvested off two Internshala listing pages. This
    only works because item_state reads the Source: header the records
    extractor writes — the first cause of P0-1, fixed and pinned here
    against a real output rather than a synthetic one."""
    prog = chain["prog"]
    assert len(prog.discovered) == 20
    assert len(prog.listings) == 2


def test_the_run_opened_four_of_them(chain):
    """FOUR, and this number went UP when the deriver got more honest.

    It read 1 until candidates were matched by IDENTITY rather than by
    address. This run opened four LinkedIn job pages whose harvested
    hrefs carried tracking parameters the landing URLs did not, so three
    real item pages were invisible -- the deriver was under-reporting
    the run's actual work by four times.

    Under-counting is the safe direction (it can only produce an honest
    shortfall, never an overclaim), which is exactly why it survived
    this long without anything failing.
    """
    assert chain["prog"].done == 4


def test_the_extract_header_no_longer_hides_a_read(chain):
    """P0-1 cause two. browser_extract is the tool whose output shape
    was unreadable; it must not appear as an address-less call now."""
    assert "browser_extract" not in chain["prog"].unlocated


# --------------------------------------------------- and what it claimed

def test_the_deliverable_claimed_far_more_than_it_verified(chain):
    from backend.app.orchestrator.item_verification import claimed_items
    claimed = claimed_items(chain["transcript"])
    assert claimed >= 10
    assert claimed > chain["tally"].verified


def test_search_pages_were_not_counted_as_item_pages(chain):
    """Thirteen distinct pages, five of them below a listing. A flat
    page count would have read this run as plausible."""
    from backend.app.orchestrator.item_verification import (
        distinct_pages_visited, item_pages_read,
    )
    assert distinct_pages_visited(chain["calls"]) > 10
    reached = item_pages_read(chain["calls"])
    assert reached is not None and reached < 10


# ------------------------------------------------------ so it is refused

def test_this_run_would_now_be_refused(chain):
    """The whole point. This exact deliverable shipped."""
    from backend.app.orchestrator.item_verification import overclaim
    problem = overclaim(chain["transcript"], chain["task"], chain["spec"],
                        chain["calls"])
    assert problem, "the run that shipped eleven unverified items must not pass"
    assert "item page(s)" in problem


def test_the_run_is_not_reported_as_complete(chain):
    from backend.app.orchestrator.goal_state import check
    done, _ = check(chain["task"], chain["calls"], spec=chain["spec"],
                    target_items=chain["plan"].feasible_items)
    assert not done


def test_the_founder_is_told_the_real_numbers(chain):
    from backend.app.orchestrator.run_report import for_run
    report = for_run(chain["task"], chain["calls"])
    assert "asked for : 10 items" in report
    assert "verified  : 4" in report
    assert "SHORTFALL: 6 of 10" in report


# ------------------------------------------- and it would be staffed for

def test_the_goal_asks_for_research_extraction_and_writing(chain):
    from backend.app.employees.capability import (
        DATA_EXTRACTION, REPORT_WRITING, WEB_RESEARCH, required_for,
    )
    caps = required_for(chain["task"], chain["spec"])
    assert WEB_RESEARCH in caps
    assert DATA_EXTRACTION in caps
    assert REPORT_WRITING in caps
