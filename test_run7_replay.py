"""Run 7, replayed: the orchestrator must stop looking and start checking.

THE RUN THIS IS BUILT FROM. Twenty Product Manager internships
requested. Twenty-one calls, every one of them on a single host. Six
different searches of it produced forty-nine candidate rows -- nineteen
genuinely new, thirty repeats of jobs already in hand. Two candidates
were opened. One was read. None was verified. The budget ran out.

Nothing was broken. The browser worked, the extractor worked, the site
answered every call. What did not exist was any object that could say
"nineteen candidates is enough, go and check them" or "this source has
stopped producing, try another".

WHAT THIS FILE ASSERTS, AND WHAT IT DELIBERATELY DOES NOT. It does not
assert "at call 13, leave Indeed" -- that would encode the trace instead
of testing the architecture, and the next trace would be different. It
asserts the RULES fire: that a phase transition happens on the
arithmetic, that a discovery call made after that transition is refused,
and that the refusal never names a replacement site.

The step number is read out of the replay, not written into it. If the
thresholds change, the number changes and these tests still hold.
"""
from __future__ import annotations

import json
import os

import pytest

FIXTURE = os.path.join(os.path.dirname(__file__), "test_fixtures",
                       "run7_indeed_trace.json")


@pytest.fixture(scope="module")
def trace():
    if not os.path.exists(FIXTURE):
        pytest.skip("run-7 fixture not present")
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def replay(trace):
    """Walk the real calls, applying the new rules at each one.

    Returns, for every call, what the orchestrator WOULD have decided
    with only the calls up to that point in hand.
    """
    from backend.app.orchestrator import discovery_guard, discovery_state
    from backend.app.orchestrator.goal_spec import from_task
    from backend.app.orchestrator.item_state import derive
    from backend.app.orchestrator.task_graph import build, phase_of

    task, calls = trace["task"], trace["calls"]
    spec = from_task(task)
    plan = build(task, 22, spec=spec)
    steps = []
    for i in range(len(calls)):
        prior = calls[:i]                     # what was known BEFORE call i+1
        tool = str(calls[i].get("tool") or "")
        disco = discovery_state.derive(prior)
        prog = derive(prior, plan.feasible_items)
        ph = phase_of(plan, candidates=len(prog.discovered),
                      opened=len(prog.opened), verified=prog.done,
                      steps_left=max(0, 22 - i))
        steps.append({
            "n": i + 1, "tool": tool, "phase": ph.name, "disco": disco,
            "candidates": len(prog.discovered), "opened": len(prog.opened),
            "refusal": discovery_guard.verdict(tool, ph, disco,
                                               disco.current_source),
        })
    return {"spec": spec, "plan": plan, "steps": steps, "task": task,
            "calls": calls}


# ------------------------------------------------ the run is understood

def test_the_goal_is_twenty_items_each_on_its_own_page(replay):
    spec = replay["spec"]
    assert spec.confident and spec.item_count == 20 and spec.per_item_work


def test_the_budget_only_ever_bought_eight(replay):
    assert replay["plan"].feasible_items == 8


# --------------------------------------- discovery state is now tracked

def test_only_one_source_was_ever_used(replay):
    disco = replay["steps"][-1]["disco"]
    assert disco.tried_sources == ["indeed.com"], disco.tried_sources


def test_the_repeats_are_visible_for_the_first_time(replay):
    """Forty-nine rows harvested, twenty-six of them new and twenty-three
    already in hand. Nothing in the system could see that before: every
    guard counted CALLS, and the calls all looked different.

    These numbers moved once during development, from 19/30, when a
    regression test caught the title matcher merging fifteen distinct
    postings that shared the words "Product Manager Intern". A title
    names a ROLE; only a title carrying the employer names a JOB. The
    figures here are the ones after that correction."""
    disco = replay["steps"][-1]["disco"]
    assert disco.total_new == 26
    assert disco.total_duplicates == 23


def test_several_distinct_strategies_were_used_on_that_one_source(replay):
    """SOURCE and STRATEGY are different things, and this run is why:
    six ways of searching one site. A check that only knew about sources
    would have called this one attempt; one that only knew about
    strategies would never have noticed the site was the constant."""
    disco = replay["steps"][-1]["disco"]
    assert len(disco.sources["indeed.com"].strategies) >= 5


def test_opening_an_item_is_not_recorded_as_a_search(replay):
    """Navigating to a job page is a navigation, not a way of searching.
    Counting those inflated this run to ten "strategies", four of which
    were individual jobs."""
    disco = replay["steps"][-1]["disco"]
    for key in disco.sources["indeed.com"].strategies:
        assert "/rc/clk" not in key, key


# ------------------------------------------- the transition that fires

def test_the_run_reaches_verify_on_its_own_arithmetic(replay):
    phases = [s["phase"] for s in replay["steps"]]
    assert "DISCOVER" in phases, phases
    assert "VERIFY" in phases, "the run must stop being in DISCOVER forever"


def test_the_transition_happens_once_candidates_cover_what_is_needed(replay):
    """Read out of the replay rather than written into it."""
    first = next(s for s in replay["steps"] if s["phase"] == "VERIFY")
    # Eight affordable, so eight unopened candidates is the trigger.
    assert first["candidates"] - first["opened"] >= 8
    assert first["n"] < 21, "it must transition well before the budget ends"


def test_discovery_is_refused_after_the_transition(replay):
    refused = [s for s in replay["steps"] if s["refusal"]]
    assert refused, "the run gathered 15 more times after having enough"
    assert all(s["phase"] in ("VERIFY", "ASSEMBLE") or s["refusal"]
               for s in refused)


def test_the_refusals_land_on_gathering_not_on_opening(replay):
    """Opening and reading must never be blocked by this guard — those
    are the actions the run is being pushed TOWARD."""
    for s in replay["steps"]:
        if s["refusal"]:
            assert any(t in s["tool"] for t in
                       ("extract_records", "extract_table", "web_search")), s["tool"]


def test_the_refusal_says_what_to_do_instead(replay):
    text = next(s["refusal"] for s in replay["steps"] if s["refusal"])
    assert "REFUSED" in text
    assert "open" in text.lower()


def test_no_replacement_source_is_ever_named(replay):
    """Code decides THAT a new way in is needed. Which one is world
    knowledge and stays with the model — a hardcoded successor would
    make this a job-board tool instead of an orchestrator."""
    import ast
    import inspect

    from backend.app.orchestrator import discovery_guard, discovery_state

    SITES = ("internshala", "naukri", "linkedin", "indeed", "glassdoor",
             "monster", "shine.com", "unstop")

    def _runtime_strings(mod):
        """Every string the CODE uses, docstrings excluded.

        Checked against the AST rather than the file text: the module
        docstrings describe the Indeed run these were built from, and
        that is documentation. What must contain no site name is the
        logic.
        """
        tree = ast.parse(inspect.getsource(mod))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        return [n.value.lower() for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value not in docstrings]

    for mod in (discovery_guard, discovery_state):
        for text in _runtime_strings(mod):
            for site in SITES:
                assert site not in text, f"{mod.__name__} hardcodes {site!r}"
    for s in replay["steps"]:
        if s["refusal"]:
            low = s["refusal"].lower()
            for site in ("internshala", "naukri", "linkedin", "glassdoor"):
                assert site not in low


def test_how_much_the_run_would_have_saved(replay):
    """The behavioural claim, in one number."""
    refused = [s["n"] for s in replay["steps"] if s["refusal"]]
    assert refused
    print(f"\n  first refusal at call {refused[0]} of 21 — "
          f"{len(refused)} discovery call(s) refused")
    assert len(refused) >= 3


# --------------------------------------------------- honest reporting

def test_the_counters_are_reported_and_none_of_them_is_verified(replay):
    from backend.app.orchestrator.item_state import derive
    from backend.app.orchestrator.item_verification import tally, verify
    from backend.app.orchestrator.relevance import subject_of
    prog = derive(replay["calls"], replay["plan"].feasible_items)
    t = tally(verify(prog, subject_of(replay["task"], replay["spec"])),
              wanted=20)
    assert t.verified == 0
    assert t.rejected == 1
    assert prog.done <= 1


def test_the_report_names_the_source_and_its_repeats(replay):
    from backend.app.orchestrator.run_report import for_run
    report = for_run(replay["task"], replay["calls"])
    assert "DISCOVERY" in report
    assert "indeed.com" in report
    assert "23 already seen" in report


# =========== a source that can search but cannot be verified from
#
# THE SECOND HALF OF RUN 7, and the more expensive half. Indeed answered
# every search with fifteen records and refused every job page behind
# them. The run tried seven times, because nothing distinguished "this
# source has no more candidates" from "this source has candidates it
# will not let you read".
#
# Those are opposite problems. The first says search it differently. The
# second says its search is irrelevant, because the task can never be
# satisfied from it however many candidates it hands over.

def test_the_source_is_recognised_as_unverifiable(replay):
    disco = replay["steps"][-1]["disco"]
    src = disco.sources["indeed.com"]
    assert src.item_failures >= 2
    assert src.item_reads == 0
    assert not src.can_verify
    assert src.status == "VERIFICATION_EXHAUSTED"


def test_discovery_working_is_not_mistaken_for_the_task_working(replay):
    """The distinction that did not exist. Its SEARCH was fine
    throughout — that is exactly why nothing noticed."""
    disco = replay["steps"][-1]["disco"]
    src = disco.sources["indeed.com"]
    assert src.new_candidates > 20, "search was productive"
    assert not src.can_verify, "and verification was impossible"


def test_opening_another_item_on_it_is_refused(replay):
    from backend.app.orchestrator import discovery_guard
    from backend.app.orchestrator.task_graph import build, phase_of
    disco = replay["steps"][-1]["disco"]
    ph = phase_of(replay["plan"], candidates=26, opened=2, verified=0,
                  steps_left=10)
    refusal = discovery_guard.verdict_for_open(
        "https://in.indeed.com/viewjob?jk=d1283bdbfde3e215", ph, disco)
    assert refusal and "REFUSED" in refusal
    assert "DIFFERENT source" in refusal


def test_that_refusal_still_names_no_replacement(replay):
    from backend.app.orchestrator import discovery_guard
    from backend.app.orchestrator.task_graph import phase_of
    disco = replay["steps"][-1]["disco"]
    ph = phase_of(replay["plan"], candidates=26, opened=2, verified=0,
                  steps_left=10)
    refusal = discovery_guard.verdict_for_open(
        "https://in.indeed.com/viewjob?jk=d1283bdbfde3e215", ph, disco).lower()
    for site in ("internshala", "naukri", "linkedin", "glassdoor", "unstop"):
        assert site not in refusal


def test_the_candidates_survive_as_discovered_but_unverified(replay):
    """A source being unverifiable must not delete what it found. The
    founder is owed 'we found 26 and could verify none of them, here is
    why', not silence."""
    from backend.app.orchestrator.item_state import derive
    from backend.app.orchestrator.item_verification import tally, verify
    from backend.app.orchestrator.relevance import subject_of
    prog = derive(replay["calls"], replay["plan"].feasible_items)
    t = tally(verify(prog, subject_of(replay["task"], replay["spec"])),
              wanted=20)
    assert t.discovered > 20
    assert t.verified == 0
