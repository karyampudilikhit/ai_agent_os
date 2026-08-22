"""Landing on the page is not the same as having the answer.

TWO FAILURES, IN OPPOSITE DIRECTIONS, FROM THE SAME GAP.

Asked for the first five entries of a page's table of contents, a live
run had all of them at call two and then made TEN MORE. Nothing could
tell it the task was finished, because the only finish line this system
understood was "did we land on the pages the task named" -- and it had
landed on the one page there was.

Turn that around and it is worse. For the same task, a navigation-only
check says DONE the moment the page opens, BEFORE anything is read. "Go
to X and give me Y" is two requirements, and a completion check that
knows only about X would end the run without Y.

So a content requirement makes completion strictly HARDER, never easier.
Arriving counts for nothing until the asked-for content is in hand, and
the read has to have happened after the arrival.

Every count here is read from a line one of OUR OWN tools printed --
"12 entr(y/ies)", "showing 25", "DATA: 30 row(s)". Nothing counts words
on a page and nothing asks the model how much it got. The fixtures are
built by calling the code that writes those lines.
"""
from __future__ import annotations

import pytest

from backend.app.browser.observation import DATA_PREFIX
from backend.app.browser.policy import wrap_untrusted
from backend.app.orchestrator.goal_spec import from_task
from backend.app.orchestrator.goal_state import (
    check, content_entries, content_requirement, item_requirement,
)

PY_URL = "https://en.wikipedia.org/wiki/Python_(programming_language)"
PY_TITLE = "Python (programming language) - Wikipedia"

W4 = ("Go to the Python Wikipedia page and give me the first five items "
      "in the table of contents.")
W1 = ("Go to Wikipedia's Artificial Intelligence page and give me the "
      "first three sentences.")
INTERNSHIPS = (
    "Find 10 Product Manager internships in India posted within the last "
    "7 days, visit each job page, extract company, role, location, "
    "posting date and application URL, remove duplicates, and give me "
    "the top 10."
)


def view(url: str, title: str, rows: str = "(no data table on this page)") -> str:
    """A page view in the shape observation.render actually emits."""
    return "\n".join([f"URL: {url}", f"TITLE: {title}",
                      f"{DATA_PREFIX} {rows}", "",
                      "INTERACTIVE ELEMENTS:", '  e1  link "History"'])


def toc(url: str, n: int) -> str:
    """browser_extract_toc's real output for a page with n entries."""
    body = [f"[table of contents — {url}]",
            f"{n} entr(y/ies), in page order. Found by: role=doc-toc.",
            ""]
    body += [f"{i:>3}. Entry {i}   (#e{i})" for i in range(1, n + 1)]
    return wrap_untrusted("\n".join(body), url)


def table(url: str, rows: int) -> str:
    return wrap_untrusted(
        f"{DATA_PREFIX} {rows} row(s) | Ticker · Price · Chg%\n"
        f"SORTED: Chg% descending", url)


def call(tool: str, out: str, at: float, ok: bool = True):
    return {"tool": tool, "ok": ok, "at": at, "output": out}


# ------------------------------------------------ what the task asks for

def test_a_counted_content_goal_is_recognised():
    assert content_requirement(from_task(W4)) == 5


def test_three_sentences_is_one_answer_not_three():
    """goal_spec refuses sentence nouns as counts, so the spec is not
    confident and this module must behave exactly as it did before."""
    spec = from_task(W1)
    assert not spec.confident
    assert content_requirement(spec) == 0


def test_per_item_work_is_a_different_finish_line():
    spec = from_task(INTERNSHIPS)
    assert spec.per_item_work and spec.item_count == 10
    assert content_requirement(spec) == 0
    assert item_requirement(spec, 8) == 8


# ---------------------------------------------------------- W4, in order

def test_arriving_is_not_finishing():
    """The regression this whole file exists to prevent. A
    navigation-only check would say DONE here, one call before the
    contents were read."""
    calls = [call("action.browser_navigate", view(PY_URL, PY_TITLE), 1.0)]
    done, why = check(W4, calls, spec=from_task(W4))
    assert not done and why == ""


def test_reading_the_contents_finishes_it():
    calls = [call("action.browser_navigate", view(PY_URL, PY_TITLE), 1.0),
             call("action.browser_extract_toc", toc(PY_URL, 12), 2.0)]
    done, why = check(W4, calls, spec=from_task(W4))
    assert done
    assert "12 entr(y/ies)" in why
    assert "landed on all 1 page(s)" in why


def test_fewer_entries_than_asked_for_is_not_finished():
    calls = [call("action.browser_navigate", view(PY_URL, PY_TITLE), 1.0),
             call("action.browser_extract_toc", toc(PY_URL, 3), 2.0)]
    assert check(W4, calls, spec=from_task(W4))[0] is False


def test_content_read_before_arriving_does_not_count():
    """A table of contents from somewhere else is not this page's."""
    other = "https://www.google.com/search?q=python"
    calls = [call("action.browser_extract_toc", toc(other, 20), 1.0),
             call("action.browser_navigate", view(PY_URL, PY_TITLE), 2.0)]
    assert check(W4, calls, spec=from_task(W4))[0] is False


def test_a_failed_call_is_no_evidence():
    calls = [call("action.browser_navigate", view(PY_URL, PY_TITLE), 1.0),
             call("action.browser_extract_toc", toc(PY_URL, 12), 2.0, ok=False)]
    assert check(W4, calls, spec=from_task(W4))[0] is False


# ------------------------------------------- counting only a real read

def test_a_page_that_has_a_table_is_not_a_table_we_read():
    """DATA: rows is in the fingerprint of every navigation and click,
    where it means the page HAS a table -- not that we took one."""
    task = "Give me the top 5 stocks by monthly performance."
    calls = [call("action.browser_navigate",
                  view("https://finviz.com/screener.ashx", "Screener",
                       rows="30 row(s) | Ticker · Price"), 1.0)]
    assert content_entries(calls) == []
    assert check(task, calls, spec=from_task(task))[0] is False


def test_but_extracting_that_table_is():
    task = "Give me the top 5 stocks by monthly performance."
    url = "https://finviz.com/screener.ashx"
    calls = [call("action.browser_navigate", view(url, "Screener"), 1.0),
             call("action.browser_extract_table", table(url, 30), 2.0)]
    assert content_entries(calls) == [(2.0, 30)]
    done, why = check(task, calls, spec=from_task(task))
    assert done and "30 entr(y/ies)" in why


# ------------------------------------------------------ multi-item goals

LISTING = "https://internshala.com/internships/product-management-internship"


def records(url: str, links) -> str:
    body = [f"[records — {url}]",
            f"{len(links)} similar item(s) on the page, showing {len(links)}. "
            f"Each block is ONE record exactly as the page rendered it.", ""]
    for i, href in enumerate(links, 1):
        body += [f"--- record {i} ---", f"title: Product Manager Intern {i}",
                 f"link: {href}", "text: Mumbai · 2 days ago", ""]
    return wrap_untrusted("\n".join(body), url)


def item_ledger(n_opened: int, n_found: int = 10):
    """A run that listed n_found candidates and opened+read n_opened."""
    links = [f"https://internshala.com/internship/detail/{i}"
             for i in range(1, n_found + 1)]
    calls = [call("action.browser_navigate", view(LISTING, "PM internships"), 1.0),
             call("action.browser_extract_records", records(LISTING, links), 2.0)]
    at = 3.0
    for href in links[:n_opened]:
        calls.append(call("action.browser_navigate",
                          view(href, "Product Manager Intern"), at))
        calls.append(call("action.browser_extract",
                          wrap_untrusted("Company: Acme\nPosted: 2 days ago",
                                         href), at + 0.5))
        at += 1.0
    return calls


def test_listing_ten_candidates_finishes_nothing():
    """The internship run's actual failure: twenty calls of results
    pages, ten candidates in hand, none of them opened."""
    done, _ = check(INTERNSHIPS, item_ledger(0), spec=from_task(INTERNSHIPS),
                    target_items=10)
    assert not done


def test_opening_and_reading_them_all_finishes_it():
    done, why = check(INTERNSHIPS, item_ledger(10), spec=from_task(INTERNSHIPS),
                      target_items=10)
    assert done and "opened and read all 10 item(s)" in why


def test_a_descoped_run_finishes_at_the_number_it_was_told_to_do():
    """Told to do eight properly of ten, eight IS the finish line. The
    plan's figure, not the spec's."""
    calls = item_ledger(8)
    assert check(INTERNSHIPS, calls, spec=from_task(INTERNSHIPS),
                 target_items=8)[0] is True
    assert check(INTERNSHIPS, calls, spec=from_task(INTERNSHIPS),
                 target_items=10)[0] is False


# ----------------------------------------------- nothing else may change

W5_TASK = (
    "Do this navigation sequence, in order:\n"
    f"  1. Open {PY_URL}\n"
    "  2. From that page, follow a link to "
    "https://en.wikipedia.org/wiki/Programming_language\n"
    "  3. Go BACK to the Python page using the browser's back action."
)


@pytest.mark.parametrize("task,calls", [
    (W4, [call("action.browser_navigate", view(PY_URL, PY_TITLE), 1.0),
          call("action.browser_extract_toc", toc(PY_URL, 12), 2.0)]),
    (W5_TASK, [call("action.browser_navigate", view(PY_URL, PY_TITLE), 1.0)]),
    (W1, [call("action.browser_navigate", view(PY_URL, PY_TITLE), 1.0)]),
])
def test_without_a_spec_this_is_the_check_it_always_was(task, calls):
    """Every caller that has not been taught about goal shapes must get
    byte-identical behaviour -- which for W4 means the old, wrong
    answer, and that is the point: the change is opt-in."""
    from backend.app.orchestrator.goal_state import _nav_complete
    assert check(task, calls) == _nav_complete(task, calls)


def test_an_unshaped_task_is_left_entirely_alone():
    task = "Research the state of the Indian EV market and write it up."
    calls = [call("action.browser_extract_toc",
                  toc("https://example.com", 40), 1.0)]
    assert check(task, calls, spec=from_task(task)) == (False, "")


# ------------------------------------- enough rows is not the answer
#
# A LIVE RUN, 2026-08-21. Asked for "company, funding, product, and
# website", it found ten entries on one Inc42 list page and stopped --
# complete by its own reckoning, having obtained company and funding and
# neither of the other two. Counting entries answers "how many"; it says
# nothing about "of what", and both were asked for.

STARTUPS = ("Research the top 10 AI startups in India and create a "
            "comparison containing company, funding, product, and website.")
INC42 = "https://inc42.com/lists/top-20-funded-ai-startups-in-india-2026"


def inc42_records(n=10, product=False, own_sites=False):
    """The shape Inc42's list actually returned: name, funding, founded,
    and every link pointing back at Inc42's own profile pages."""
    body = [f"[records — {INC42}]",
            f"{n} similar item(s) on the page, showing {n}.", ""]
    for i in range(1, n + 1):
        link = (f"https://startup{i}.ai" if own_sites
                else f"https://inc42.com/company/startup-{i}/?itm_medium=website")
        body += [f"--- record {i} ---", f"title: {i}. Startup {i}",
                 f"link: {link}",
                 f"text: Track Startup {i}, an AI company with $50.0M in funding. "
                 f"Sector AI Founded 2023 Total funding amount $50.00 Mn"
                 + (" Product: an AI agent platform." if product else ""), ""]
    return wrap_untrusted("\n".join(body), INC42)


def test_the_task_names_all_four_fields():
    """_FIELD_WORDS matched only "company" until a live run showed it."""
    assert from_task(STARTUPS).per_item_fields == [
        "company", "funding", "product", "website"]


def test_ten_rows_without_product_or_website_is_not_finished():
    """The exact run. Ten entries, two of four fields, reported done."""
    calls = [call("action.browser_extract_records", inc42_records(), 1.0)]
    done, _ = check(STARTUPS, calls, spec=from_task(STARTUPS))
    assert not done


def test_the_same_rows_carrying_every_field_are_finished():
    calls = [call("action.browser_extract_records",
                  inc42_records(product=True, own_sites=True), 1.0)]
    done, why = check(STARTUPS, calls, spec=from_task(STARTUPS))
    assert done and "every field asked for" in why


def test_a_tracking_parameter_is_not_a_website():
    """Every Inc42 link carries "itm_medium=website" in its query string.
    Matched against raw text, that made "website" look evidenced on a
    page that never showed one company's own address."""
    from backend.app.orchestrator.goal_state import fields_evidenced
    _, missing = fields_evidenced(["website"], inc42_records(), {"inc42.com"})
    assert "website" in missing


def test_a_link_off_the_source_site_is_a_website():
    from backend.app.orchestrator.goal_state import fields_evidenced
    present, _ = fields_evidenced(["website"], inc42_records(own_sites=True),
                                  {"inc42.com"})
    assert "website" in present


def test_field_evidence_comes_from_the_page_that_gave_the_rows():
    """Checked run-wide, a search snippet mentioning "product" vouched
    for per-company product data on a listing that had none. Observed on
    the real trace."""
    snippet = wrap_untrusted(
        "[web search — 'AI startups']\n3 result(s). Product roundup: "
        "see https://someblog.example/product-list", "https://tavily")
    calls = [call("action.web_search", snippet, 1.0),
             call("action.browser_extract_records", inc42_records(), 2.0)]
    assert check(STARTUPS, calls, spec=from_task(STARTUPS))[0] is False


def test_a_task_naming_no_fields_is_unaffected():
    task = "Give me the first five items in the table of contents."
    spec = from_task(task)
    assert not spec.per_item_fields
    calls = [call("action.browser_extract_toc", toc(PY_URL, 12), 1.0)]
    assert check(task, calls, spec=spec)[0] is True


def test_the_loop_hands_over_the_goal_shape():
    """Opt-in behaviour is only worth having if something opts in.
    item_state passed nineteen unit tests and never fired once live."""
    import inspect
    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    _, _, tail = src.partition("goal_check(")
    assert "spec=plan.spec" in tail[:300]
    assert "target_items=plan.feasible_items" in tail[:300]
