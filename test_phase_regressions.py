"""Everything that worked before Phase 4A must still work.

A phase machine that refuses actions is the most dangerous thing added
to this codebase. Every other guard here can only make a run noisier;
this one can stop the single call that was going to work. So the
properties below matter more than the feature does, and the first four
are all the same property said four ways: SILENCE IS THE DEFAULT.

A task with no countable shape gets no phase, no refusals, and behaves
exactly as it did before this file existed.
"""
from __future__ import annotations

import pytest

from backend.app.browser.observation import DATA_PREFIX
from backend.app.browser.policy import wrap_untrusted
from backend.app.orchestrator import discovery_guard, discovery_state
from backend.app.orchestrator.goal_spec import from_task
from backend.app.orchestrator.item_state import derive
from backend.app.orchestrator.task_graph import build, phase_of

LIST = "https://jobs.test/search?q=pm+intern&l=india"
ITEM = "https://jobs.test/job/detail/role-{}-4455{}"


def view(url, title="Page"):
    return "\n".join([f"URL: {url}", f"TITLE: {title}",
                      f"{DATA_PREFIX} (no data table on this page)"])


def records(url, links, titles=None):
    body = [f"[records — {url}]",
            f"{len(links)} similar item(s) on the page, showing {len(links)}.", ""]
    for i, href in enumerate(links, 1):
        t = titles[i - 1] if titles else f"Product Manager Intern {i}"
        body += [f"--- record {i} ---", f"title: {t}", f"link: {href}", ""]
    return wrap_untrusted("\n".join(body), url)


def call(tool, out, at, ok=True, args=""):
    return {"tool": tool, "ok": ok, "at": at, "output": out, "args_text": args}


def nav(url, at):
    return call("action.browser_navigate", view(url), at,
                args=f'{{"url": "{url}"}}')


def phase_for(task, calls, budget=22):
    spec = from_task(task)
    plan = build(task, budget, spec=spec)
    prog = derive(calls, plan.feasible_items)
    return phase_of(plan, candidates=len(prog.discovered),
                    opened=len(prog.opened), verified=prog.done,
                    steps_left=budget - len(calls))


# ============================================ H. uncertain GoalSpec

@pytest.mark.parametrize("task", [
    "Research the Indian EV market and write it up.",
    "Research the company Supervity and give me its website and products.",
    "Go to the Python Wikipedia page and give me the first five contents entries.",
    "Give me the top 5 stocks by monthly performance.",
])
def test_a_task_without_per_item_work_gets_no_phase_at_all(task):
    """The load-bearing safety property. No phase means no refusal can
    ever be issued, whatever the run does."""
    ph = phase_for(task, [nav(LIST, 1.0)])
    assert not ph.active, f"{task!r} should have no phase, got {ph.name}"


def test_no_phase_means_no_refusal_ever():
    task = "Research the company Supervity and give me its website."
    ph = phase_for(task, [nav(LIST, 1.0)])
    disco = discovery_state.derive([nav(LIST, 1.0)])
    for tool in ("action.browser_extract_records", "action.web_search",
                 "action.browser_extract_table"):
        assert discovery_guard.verdict(tool, ph, disco, "jobs.test") is None


# ============================================ A. single-source task

def test_a_source_that_keeps_producing_is_never_declared_spent():
    """Three reads, new candidates every time. Nothing here should be
    told to go elsewhere."""
    calls = [nav(LIST, 1.0)]
    at = 2.0
    for page in range(3):
        links = [ITEM.format(i, page) for i in range(page * 5, page * 5 + 5)]
        calls.append(call("action.browser_extract_records",
                          records(LIST, links), at))
        at += 1.0
    disco = discovery_state.derive(calls)
    assert disco.sources["jobs.test"].status == "ACTIVE"
    assert disco.total_new == 15 and disco.total_duplicates == 0


def test_pagination_of_a_productive_query_is_one_strategy():
    """Page two is the same question continuing. If this read as a
    repeat, honest pagination would be blocked to stop useless
    re-searching — the case that must not be confused."""
    a = discovery_state.strategy_key(url=LIST + "&start=0")
    b = discovery_state.strategy_key(url=LIST + "&start=10")
    c = discovery_state.strategy_key(url=LIST + "&start=20")
    assert a == b == c


def test_a_different_question_is_a_different_strategy():
    a = discovery_state.strategy_key(url="https://jobs.test/search?q=pm+intern")
    b = discovery_state.strategy_key(url="https://jobs.test/search?q=data+intern")
    assert a != b


def test_tracking_parameters_do_not_invent_a_new_strategy():
    a = discovery_state.strategy_key(url=LIST)
    b = discovery_state.strategy_key(url=LIST + "&utm_source=x&bb=TRACKING")
    assert a == b


def test_a_single_source_run_that_verifies_enough_is_never_refused():
    """The whole task done from one site, properly. Rotation must not be
    forced on a run that is succeeding."""
    task = ("Find 4 Product Manager internships in India. Visit each job "
            "page to verify it.")
    links = [ITEM.format(i, 0) for i in range(4)]
    calls = [nav(LIST, 1.0),
             call("action.browser_extract_records", records(LIST, links), 2.0)]
    at = 3.0
    for href in links:
        calls += [nav(href, at),
                  call("action.browser_extract",
                       wrap_untrusted("Product Manager Intern role.", href),
                       at + 0.5)]
        at += 1.0
    ph = phase_for(task, calls)
    assert ph.name == "ASSEMBLE", ph.line()
    assert ph.verified == 4


# ============================================ exploration is protected

def test_the_first_search_of_a_new_source_is_never_refused():
    """Do not blindly refuse the first repeated search. A run must be
    free to look before it is told it has enough."""
    calls = [nav(LIST, 1.0),
             call("action.browser_extract_records",
                  records(LIST, [ITEM.format(1, 0)]), 2.0)]
    task = "Find 20 Product Manager internships in India. Verify each one."
    ph = phase_for(task, calls)
    disco = discovery_state.derive(calls)
    assert ph.name == "DISCOVER"
    assert discovery_guard.verdict("action.browser_extract_records", ph,
                                   disco, "jobs.test") is None


def test_a_run_short_of_candidates_keeps_discovering():
    task = "Find 20 Product Manager internships in India. Verify each one."
    calls = [nav(LIST, 1.0),
             call("action.browser_extract_records",
                  records(LIST, [ITEM.format(i, 0) for i in range(3)]), 2.0)]
    ph = phase_for(task, calls)
    assert ph.name == "DISCOVER", ph.line()


def test_two_candidates_are_too_few_to_enforce_on():
    """A floor under the arithmetic, so a tiny ask cannot trigger a
    refusal off one or two rows."""
    task = "Find 2 Product Manager internships in India. Visit each page."
    calls = [nav(LIST, 1.0),
             call("action.browser_extract_records",
                  records(LIST, [ITEM.format(1, 0), ITEM.format(2, 0)]), 2.0)]
    ph = phase_for(task, calls)
    disco = discovery_state.derive(calls)
    assert discovery_guard.verdict("action.browser_extract_records", ph,
                                   disco, "jobs.test") is None


# ============================================ cross-source dedupe

def test_the_same_job_from_two_sources_counts_once():
    other = "https://board2.test/search?q=pm+intern"
    title = "Product Manager Intern at Acme Corp"
    calls = [nav(LIST, 1.0),
             call("action.browser_extract_records",
                  records(LIST, ["https://jobs.test/job/detail/a-99881122"],
                          [title]), 2.0),
             nav(other, 3.0),
             call("action.browser_extract_records",
                  records(other, ["https://board2.test/listing/x-55443322"],
                          [title]), 4.0)]
    disco = discovery_state.derive(calls)
    assert disco.total_new == 1
    assert disco.total_duplicates == 1


def test_two_different_jobs_at_the_same_company_stay_separate():
    """The dangerous direction. An over-eager matcher merging these
    would silently under-report, and the founder would never see it."""
    calls = [nav(LIST, 1.0),
             call("action.browser_extract_records",
                  records(LIST,
                          ["https://jobs.test/job/detail/a-11112222",
                           "https://jobs.test/job/detail/b-33334444"],
                          ["Product Manager Intern at Acme",
                           "Data Science Intern at Acme"]), 2.0)]
    disco = discovery_state.derive(calls)
    assert disco.total_new == 2 and disco.total_duplicates == 0


# ============================================ F/G. verification intact

def test_no_verification_layer_was_weakened():
    """Phase 4A adds a guard; it must remove none."""
    import inspect

    from backend.app.api import routes
    src = inspect.getsource(routes._gate_deliverable)
    for check in ("detect_handback", "detect_contradicted_claims",
                  "unretrieved_urls", "missing_metric_values",
                  "untraceable_metric_values", "unbacked_row_labels",
                  "run_saw_a_data_table", "_plausible", "overclaim"):
        assert check in src, f"{check} disappeared from the deliverable gate"


def test_completion_is_still_code_owned():
    """The phase machine must not become a way for a model to declare
    itself finished."""
    import inspect

    from backend.app.orchestrator import task_graph
    src = inspect.getsource(task_graph.phase_of)
    assert "adapter" not in src and "chat_completion" not in src


def test_a_discovered_candidate_is_never_a_verified_item():
    task = "Find 20 Product Manager internships in India. Verify each one."
    calls = [nav(LIST, 1.0),
             call("action.browser_extract_records",
                  records(LIST, [ITEM.format(i, 0) for i in range(20)]), 2.0)]
    ph = phase_for(task, calls)
    assert ph.candidates == 20
    assert ph.verified == 0
    assert ph.name == "VERIFY", "20 candidates and 0 verified is not discovery"


# ============================================ E. capability layer intact

def test_dynamic_employee_selection_still_works():
    from backend.app.employees.capability import (
        DATA_EXTRACTION, WEB_RESEARCH, required_for,
    )
    task = "Find 20 Product Manager internships in India. Verify each one."
    caps = required_for(task, from_task(task))
    assert WEB_RESEARCH in caps and DATA_EXTRACTION in caps


# ====== a source must not be called unverifiable for succeeding
#
# The dangerous direction of the verification-capability check. A source
# whose items open and read fine must never be rotated away from, and a
# single failed page is a page, not a policy.

def item_run(opens_ok, fails=0):
    links = [f"https://jobs.test/job/detail/role-{i}-99{i}0011" for i in range(6)]
    calls = [nav(LIST, 1.0),
             call("action.browser_extract_records", records(LIST, links), 2.0)]
    at = 3.0
    for href in links[:opens_ok]:
        calls += [nav(href, at),
                  call("action.browser_extract",
                       wrap_untrusted("Product Manager Intern. Own the roadmap.",
                                      href), at + 0.5)]
        at += 1.0
    for href in links[opens_ok:opens_ok + fails]:
        calls.append(call("action.browser_navigate", "", at, ok=False,
                          args=f'{{"url": "{href}"}}'))
        at += 1.0
    return calls


def test_a_source_whose_items_open_and_read_stays_verifiable():
    disco = discovery_state.derive(item_run(opens_ok=3))
    src = disco.sources["jobs.test"]
    assert src.item_opens == 3 and src.item_failures == 0
    assert src.can_verify and src.status != "VERIFICATION_EXHAUSTED"


def test_one_failed_item_page_is_not_a_policy():
    """A page is a page. Two is a pattern."""
    disco = discovery_state.derive(item_run(opens_ok=2, fails=1))
    assert disco.sources["jobs.test"].can_verify


def test_two_failed_item_pages_is():
    disco = discovery_state.derive(item_run(opens_ok=0, fails=2))
    src = disco.sources["jobs.test"]
    assert not src.can_verify
    assert src.status == "VERIFICATION_EXHAUSTED"


def test_a_working_source_is_never_refused_for_opening_an_item():
    task = "Find 20 Product Manager internships in India. Verify each one."
    calls = item_run(opens_ok=3)
    ph = phase_for(task, calls)
    disco = discovery_state.derive(calls)
    assert discovery_guard.verdict_for_open(
        "https://jobs.test/job/detail/role-4-9940011", ph, disco) is None


def test_an_unshaped_goal_never_triggers_the_capability_refusal():
    """Silence is the default here too — a task with no countable shape
    gets no phase, and no phase means no refusal."""
    task = "Research the company Supervity and give me its website."
    calls = item_run(opens_ok=0, fails=3)
    ph = phase_for(task, calls)
    disco = discovery_state.derive(calls)
    assert not ph.active
    assert discovery_guard.verdict_for_open(
        "https://jobs.test/job/detail/role-1-9910011", ph, disco) is None
