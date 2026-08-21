"""An item has to be the thing that was asked for, and the answer has to
claim no more items than the run opened pages.

TWO FAILURES, ONE FILE, BECAUSE THEY ARE THE TWO HALVES OF ONE HOLE.

First: asked for ten Product Manager internships, a run reported
"Fashion Merchandising Intern" among them. The provenance ledger agreed
the page was fetched. The row-backing check agreed the title appeared in
text the run really read. Plausibility found nothing implausible. Every
guard was right and none of them was asking whether a fashion
merchandising internship is a product manager internship.

Second, and larger: every per-item guard in this system lives inside the
execution loop, and the check that decides what the founder is TOLD
lives in _gate_deliverable. Nothing joined them. A run could list ten
titles off one results page, open none of them, write ten rows, and pass
-- because the listing page is text the run genuinely read, so every row
was "backed".

The fixtures below are built by calling the code that writes these
shapes. Three bugs in this codebase passed hand-written fixtures and
failed live because the real output had a different shape.
"""
from __future__ import annotations

import pytest

from backend.app.browser.observation import DATA_PREFIX
from backend.app.browser.policy import wrap_untrusted
from backend.app.orchestrator.goal_spec import from_task
from backend.app.orchestrator.item_state import derive
from backend.app.orchestrator.item_verification import (
    BLOCKED, DISCOVERED, OPENED, REJECTED, VERIFIED,
    claimed_items, distinct_pages_visited, overclaim, tally, verify,
)
from backend.app.orchestrator.relevance import judge, subject_of

INTERNSHIPS = (
    "Find 10 Product Manager internships in India posted within the last "
    "7 days, visit each job page, extract company, role, location, "
    "posting date and application URL, remove duplicates, and give me "
    "the top 10."
)
LISTING = "https://internshala.com/internships/product-management-internship"
ITEM = "https://internshala.com/internship/detail/job-{}"


# ------------------------------------------------- reading the subject

def test_the_subject_is_read_out_of_the_founders_own_words():
    s = subject_of(INTERNSHIPS, from_task(INTERNSHIPS))
    assert s.enforceable
    assert "product" in s.distinctive
    assert "manag" in s.distinctive          # manager / management, one stem
    assert s.location.lower().startswith("india")
    # "posted within the last 7 days" is a different clause about the
    # items, not part of what they ARE.
    assert "post" not in s.terms and "day" not in s.terms


def test_a_generic_ask_is_never_enforceable():
    """"10 companies" is not a specification. A check that rejected on
    the single word "company" would fail every page that said "firm"."""
    task = "Find 10 companies in India and collect their pricing."
    s = subject_of(task, from_task(task))
    assert not s.enforceable


def test_an_uncounted_task_has_no_subject_to_enforce():
    task = "Research the Indian EV market and write it up."
    assert not subject_of(task, from_task(task)).enforceable


def test_management_and_manager_are_one_ask():
    s = subject_of(INTERNSHIPS, from_task(INTERNSHIPS))
    ok, _ = judge("Associate Product Management Intern. You will own the "
                  "roadmap for our payments product.", s)
    assert ok


def test_the_fashion_merchandising_intern_is_rejected():
    """The exact item that shipped as a Product Manager internship."""
    s = subject_of(INTERNSHIPS, from_task(INTERNSHIPS))
    ok, why = judge("Fashion Merchandising Intern — assist the buying team "
                    "with seasonal range planning and vendor coordination.", s)
    assert not ok
    assert "product" in why and "manag" in why


def test_an_item_with_nothing_read_from_it_is_not_verified():
    s = subject_of(INTERNSHIPS, from_task(INTERNSHIPS))
    ok, why = judge("", s)
    assert not ok and "own page" in why


def test_an_unenforceable_subject_accepts_everything():
    """Silence is the default. Behaviour without a readable subject is
    exactly what it was before this module existed."""
    task = "Find 10 companies in India and collect their pricing."
    s = subject_of(task, from_task(task))
    assert judge("literally anything at all", s) == (True, "")


# ------------------------------------------------------- item verdicts

def view(url, title):
    return "\n".join([f"URL: {url}", f"TITLE: {title}",
                      f"{DATA_PREFIX} (no data table on this page)", "",
                      "INTERACTIVE ELEMENTS:", '  e1  link "Apply"'])


def records(url, links):
    body = [f"[records — {url}]",
            f"{len(links)} similar item(s) on the page, showing {len(links)}.", ""]
    for i, href in enumerate(links, 1):
        body += [f"--- record {i} ---", f"title: Intern {i}",
                 f"link: {href}", ""]
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
        calls.append(call("action.browser_extract",
                          wrap_untrusted(body, href), at + 0.5))
        at += 1.0
    for href in blocked_urls:
        calls.append(call("action.browser_navigate", "", at, ok=False,
                          args=f'{{"url": "{href}"}}'))
        at += 1.0
    return calls


def test_each_item_gets_the_state_the_ledger_supports():
    calls = a_run(opened=2, blocked_urls=[ITEM.format(9)])
    prog = derive(calls, wanted=10)
    subj = subject_of(INTERNSHIPS, from_task(INTERNSHIPS))
    verdicts = {v.url: v.state for v in verify(prog, subj)}

    from backend.app.orchestrator.item_state import _norm
    assert verdicts[_norm(ITEM.format(1))] == VERIFIED
    assert verdicts[_norm(ITEM.format(2))] == VERIFIED
    assert verdicts[_norm(ITEM.format(9))] == BLOCKED
    assert verdicts[_norm(ITEM.format(5))] == DISCOVERED


def test_an_irrelevant_item_is_rejected_not_counted():
    calls = a_run(opened=2, body="Fashion Merchandising Intern — assist the "
                                 "buying team with seasonal range planning.")
    prog = derive(calls, wanted=10)
    subj = subject_of(INTERNSHIPS, from_task(INTERNSHIPS))
    t = tally(verify(prog, subj), wanted=10)
    assert t.verified == 0 and t.rejected == 2


def test_without_a_subject_a_read_item_still_counts():
    """Relevance is additive. Removing it returns the old behaviour."""
    calls = a_run(opened=3)
    t = tally(verify(derive(calls, wanted=10), None), wanted=10)
    assert t.verified == 3


def test_the_tally_reads_like_an_honest_answer():
    calls = a_run(opened=2, blocked_urls=[ITEM.format(9)])
    prog = derive(calls, wanted=10)
    t = tally(verify(prog, subject_of(INTERNSHIPS, from_task(INTERNSHIPS))),
              wanted=10)
    line = t.line()
    assert "2 verified" in line and "1 blocked" in line and "of 10" in line
    assert t.shortfall() == 8


# ------------------------------------------------- counting the claim

@pytest.mark.parametrize("body,expected", [
    ("| Company | Role |\n|---|---|\n| Acme | PM |\n| Beta | PM |", 2),
    ("1. Acme — PM intern\n2. Beta — PM intern\n3. Gamma — PM intern", 3),
    ("### Acme\ntext\n### Beta\ntext", 2),
    ("Just a paragraph about internships with no items in it.", 0),
])
def test_an_answer_is_counted_however_it_enumerates(body, expected):
    assert claimed_items(body) == expected


def test_a_header_row_is_not_an_item():
    body = "| Company | Role | Location |\n|---|---|---|\n| Acme | PM | Pune |"
    assert claimed_items(body) == 1


# ---------------------------------------- the refusal, which is arithmetic

TEN_ROWS = "\n".join(
    ["| Company | Role | URL |", "|---|---|---|"]
    + [f"| Firm{i} | Product Manager Intern | {ITEM.format(i)} |"
       for i in range(1, 11)])


def test_ten_rows_off_one_results_page_is_refused():
    """The exact shape that passed every other check in the system."""
    calls = a_run(opened=0)
    problem = overclaim(TEN_ROWS, INTERNSHIPS, from_task(INTERNSHIPS), calls)
    assert problem, "ten rows off one listing page must not be reportable"
    assert "only 0 item page(s)" in problem
    assert "presents 10 of them" in problem


def test_a_run_that_really_opened_them_is_not_refused():
    calls = a_run(opened=10)
    assert overclaim(TEN_ROWS, INTERNSHIPS, from_task(INTERNSHIPS), calls) is None


def test_an_honest_short_answer_passes():
    """Two rows after opening two pages is the behaviour being asked for,
    and must never be what gets blocked."""
    body = "\n".join(["| Company | Role |", "|---|---|",
                      "| Firm1 | PM Intern |", "| Firm2 | PM Intern |"])
    calls = a_run(opened=2)
    assert overclaim(body, INTERNSHIPS, from_task(INTERNSHIPS), calls) is None


def test_it_stays_silent_on_tasks_that_are_not_per_item():
    task = "Give me the top 5 stocks by monthly performance."
    calls = a_run(opened=0)
    assert overclaim(TEN_ROWS, task, from_task(task), calls) is None


def test_it_stays_silent_when_the_goal_shape_is_unknown():
    task = "Research the Indian EV market and write it up."
    calls = a_run(opened=0)
    assert overclaim(TEN_ROWS, task, from_task(task), calls) is None


def test_it_stays_silent_with_no_ledger():
    assert overclaim(TEN_ROWS, INTERNSHIPS, from_task(INTERNSHIPS), []) is None


def test_the_page_count_ignores_candidate_harvesting_entirely():
    """The refusal must not depend on item_state recognising arrivals.

    If it did, every arrival the deriver misses -- a click route with no
    address, an SPA that never changes URL -- would become a false
    accusation against a run that did the work.
    """
    calls = [call("action.browser_navigate", view(ITEM.format(i), "Intern"), float(i))
             for i in range(1, 11)]
    prog = derive(calls, wanted=10)
    assert not prog.discovered            # nothing was ever harvested
    assert distinct_pages_visited(calls) == 10
    assert overclaim(TEN_ROWS, INTERNSHIPS, from_task(INTERNSHIPS), calls) is None


EXTRACT_HEADER = "[page text — {url}]\nDATA: (no data table on this page)\nBody text."


def test_a_browser_extract_header_counts_as_reading_that_page():
    """P0-1, CAUSE TWO — found by replaying the real 21-call internship
    trace, not by guessing.

    browser_extract heads its output "[page text — <url>]", a fourth
    address shape that URL:, Now on: and Source: all miss. So an extract
    on an item page never registered as READING it, and every such item
    sat in "opened but never read" forever. The previous handoff guessed
    a browser_click route with no landing URL; it was this.
    """
    from backend.app.orchestrator.item_state import _norm
    url = ITEM.format(1)
    calls = [call("action.browser_navigate", view(LISTING, "PM"), 1.0),
             call("action.browser_extract_records", records(LISTING, [url]), 2.0),
             call("action.browser_navigate", view(url, "Intern"), 3.0),
             call("action.browser_extract", EXTRACT_HEADER.format(url=url), 4.0)]
    prog = derive(calls, wanted=5)
    assert _norm(url) in prog.read, prog.unlocated
    assert prog.done == 1


def test_search_pages_are_not_item_pages():
    """The real trace reached thirteen distinct pages -- Indeed, Naukri,
    LinkedIn and a handful of search URLs, not thirteen job postings.
    Counting pages flat let the exact failure this gate exists for walk
    straight through it.
    """
    from backend.app.orchestrator.item_verification import item_pages_read
    calls = [call("action.browser_navigate", view(LISTING, "PM"), 1.0),
             call("action.browser_extract_records",
                  records(LISTING, [ITEM.format(1)]), 2.0),
             call("action.browser_navigate", view(ITEM.format(1), "Intern"), 3.0),
             call("action.browser_navigate",
                  view("https://in.indeed.com/jobs?q=pm", "Indeed"), 4.0),
             call("action.browser_navigate",
                  view("https://www.naukri.com/pm-jobs", "Naukri"), 5.0)]
    assert distinct_pages_visited(calls) >= 4
    assert item_pages_read(calls) == 1


def test_with_no_list_read_there_is_no_basis_to_judge_item_pages():
    """Zero would accuse a run that navigated straight to items."""
    from backend.app.orchestrator.item_verification import item_pages_read
    calls = [call("action.browser_navigate", view(ITEM.format(i), "Intern"), float(i))
             for i in range(1, 4)]
    assert item_pages_read(calls) is None


def test_the_gate_actually_calls_it():
    """item_state passed nineteen unit tests and never fired live. A
    guard nothing calls is a guard that does not exist."""
    import inspect
    from backend.app.api import routes
    src = inspect.getsource(routes._gate_deliverable)
    assert "overclaim(" in src
    assert "problems.append(_over)" in src
