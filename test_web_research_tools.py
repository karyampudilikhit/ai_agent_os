"""Two instruments, one rule for choosing between them.

Tavily had been in this codebase for months WITHOUT being a tool. It ran
as prompt enrichment before the loop started, fired by keyword match,
stuffing snippets into a prompt nobody asked for. The agentic loop could
never select it, so every task that needed a page went to the browser
whether the browser was the right instrument or not.

Of five live tests, THREE failed on bot detection rather than on
anything the loop did wrong -- Finviz's Cloudflare check, Reddit's "You've
been blocked by network security", Indeed's security check. None of those
tasks needed a browser. They needed the contents of a page.

The load-bearing property is in the last section: routing around the
browser must NOT route around the guards. The fabrication gate reads the
output of every successful tool call, so a search tool that returns its
real snippets is covered for free -- and a search tool that returned a
summary of its snippets would not be.
"""
from __future__ import annotations

import json

import httpx
import pytest

from backend.app.tools import web_research_specs as wr


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    yield


# ------------------------------------------------ registered as tools

def test_both_are_registered_so_the_loop_can_choose_them():
    """The whole point. Prompt enrichment cannot be selected."""
    from backend.app.actions.action_registry import get_registry
    names = set(get_registry().known_names())
    assert "web_search" in names
    assert "web_read" in names


def test_they_are_not_mutating():
    """Reading is not acting; neither should ever queue for approval."""
    for spec in wr.ALL_SPECS:
        assert spec.mutating is False, spec.name


def test_the_descriptions_carry_the_routing_rule():
    """The model picks from the description, so the rule has to be in
    it -- not only in a comment we can read and it cannot."""
    search = wr.WEB_SEARCH_SPEC.description
    read = wr.WEB_READ_SPEC.description
    assert "CANNOT click" in search and "sort" in search
    assert "browser tools" in search, "say what to use instead"
    assert "does not preserve table cells" in read
    assert "browser_extract_table" in read, "point at the structured reader"


def test_the_instrument_choice_reaches_the_FIRST_step():
    """Where this guidance lives decides whether it can work.

    It was written into BROWSER_RULES, which is only added to the prompt
    once a browser has ALREADY been detected -- so on the first live run
    it could not influence step 1 at all. The run opened Reuters in a
    browser, got 83 characters, and only then switched to web_read: the
    advice arrived two steps after the decision it was meant to inform.

    So the choice of instrument sits in the base prompt, which every step
    sees, including the first."""
    from backend.app.orchestrator.execution_loop import STEP_PROMPT
    assert "web_search" in STEP_PROMPT and "web_read" in STEP_PROMPT
    assert "must ACT" in STEP_PROMPT, "name the line between them"
    assert "before the first call" in STEP_PROMPT


def test_the_browser_rules_keep_only_what_they_can_influence():
    """A browser-detected block cannot shape the first call, but it can
    shape the recovery from a blocked one -- which is the thing the live
    run actually needed and did."""
    from backend.app.orchestrator.execution_loop import BROWSER_RULES
    assert "web_read" in BROWSER_RULES
    assert "CHANGE INSTRUMENT, NOT SITE" in BROWSER_RULES
    assert "do not conclude it has no content" in BROWSER_RULES


# ------------------------------------- an unset key is not a fact

def test_a_missing_key_is_reported_as_a_setting_not_as_an_empty_page():
    """The distinction this codebase keeps having to defend: an
    infrastructure gap must never present as a fact about the world."""
    out = wr._search_impl({"query": "top startup news"})
    assert "TAVILY_API_KEY is not set" in out
    assert "NOT a fact about the page" in out
    assert "browser" in out, "name the fallback"


def test_a_missing_key_on_read_says_the_same():
    out = wr._read_impl({"url": "https://example.com/a"})
    assert "TAVILY_API_KEY is not set" in out
    assert "browser_navigate" in out


def test_an_empty_query_fails_plainly():
    assert "no query given" in wr._search_impl({"query": "  "})


def test_an_empty_url_fails_plainly():
    assert "no url given" in wr._read_impl({"url": ""})


# ------------------------------------------------ search behaviour

def _fake_results(n=3):
    return [{"title": f"Headline {i}", "url": f"https://news.test/{i}",
             "snippet": f"Body text for story {i}."} for i in range(n)]


def test_search_returns_the_real_snippets(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")

    class Tool:
        def search(self, q, max_results=5):
            return _fake_results(3)

    monkeypatch.setattr("backend.app.tools.web_search.TavilySearchTool", Tool)
    out = wr._search_impl({"query": "startup news"})
    for i in range(3):
        assert f"Headline {i}" in out
        assert f"https://news.test/{i}" in out
        assert f"Body text for story {i}." in out


def test_search_says_these_are_snippets_not_pages(monkeypatch):
    """A model handed snippets will happily write a report as if it read
    the articles."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")

    class Tool:
        def search(self, q, max_results=5):
            return _fake_results(2)

    monkeypatch.setattr("backend.app.tools.web_search.TavilySearchTool", Tool)
    out = wr._search_impl({"query": "x"})
    assert "SNIPPETS" in out
    assert "web_read" in out, "say how to get the full page"


def test_search_results_are_wrapped_as_untrusted(monkeypatch):
    """Page text is data. Routing around the browser must not route
    around that either."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")

    class Tool:
        def search(self, q, max_results=5):
            return _fake_results(1)

    monkeypatch.setattr("backend.app.tools.web_search.TavilySearchTool", Tool)
    out = wr._search_impl({"query": "x"})
    assert "UNTRUSTED WEB CONTENT" in out


def test_no_results_is_not_reported_as_no_such_thing(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")

    class Tool:
        def search(self, q, max_results=5):
            return []

    monkeypatch.setattr("backend.app.tools.web_search.TavilySearchTool", Tool)
    out = wr._search_impl({"query": "obscure thing"})
    assert "returned nothing" in out
    assert "browser_navigate" in out, "offer the other instrument"


def test_a_string_result_count_is_honoured(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    seen = {}

    class Tool:
        def search(self, q, max_results=5):
            seen["n"] = max_results
            return _fake_results(1)

    monkeypatch.setattr("backend.app.tools.web_search.TavilySearchTool", Tool)
    wr._search_impl({"query": "x", "max_results": "8"})
    assert seen["n"] == 8


# -------------------------------------------------- read behaviour

def _stub_post(monkeypatch, payload, status=200):
    def fake(url, **kwargs):
        return httpx.Response(status, json=payload,
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(wr.httpx, "post", fake)


def test_read_returns_the_page_text(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    _stub_post(monkeypatch, {"results": [
        {"url": "https://news.test/a", "raw_content": "The full article body."}]})
    out = wr._read_impl({"url": "https://news.test/a"})
    assert "The full article body." in out
    assert "UNTRUSTED WEB CONTENT" in out


def test_read_records_the_url_as_fetched(monkeypatch):
    """A citation of this page must be verifiable against the network,
    exactly as it is when the browser opens it."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    _stub_post(monkeypatch, {"results": [
        {"url": "https://news.test/b", "raw_content": "Body."}]})
    recorded = []
    import backend.app.tools.source_ledger as sl
    monkeypatch.setattr(sl.get_ledger(), "record_fetched",
                        lambda u: recorded.append(u))
    wr._read_impl({"url": "https://news.test/b"})
    assert recorded == ["https://news.test/b"]


def test_a_blocked_page_is_unreachable_not_empty(monkeypatch):
    """The same distinction browser_extract had to learn."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    _stub_post(monkeypatch, {"results": [],
                             "failed_results": [{"error": "403 Forbidden"}]})
    out = wr._read_impl({"url": "https://blocked.test/x"})
    assert "UNREACHABLE rather than empty" in out
    assert "403 Forbidden" in out
    assert "interactive true" in out, "offer the headed browser"


def test_a_transport_failure_is_not_a_fact_about_the_page(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")

    def boom(url, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(wr.httpx, "post", boom)
    out = wr._read_impl({"url": "https://news.test/c"})
    assert "retrieval failure, not a statement about the page" in out


def test_a_bare_host_is_given_a_scheme(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    asked = {}

    def fake(url, **kwargs):
        asked["urls"] = kwargs.get("json", {}).get("urls")
        return httpx.Response(200, json={"results": [
            {"url": "https://news.test", "raw_content": "Body."}]},
            request=httpx.Request("POST", url))

    monkeypatch.setattr(wr.httpx, "post", fake)
    wr._read_impl({"url": "news.test"})
    assert asked["urls"] == ["https://news.test"]


def test_a_long_page_says_it_was_truncated(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    _stub_post(monkeypatch, {"results": [
        {"url": "https://news.test/d", "raw_content": "x" * 40000}]})
    out = wr._read_impl({"url": "https://news.test/d"})
    assert "TRUNCATED" in out
    assert "NOT seeing the whole page" in out


def test_read_obeys_the_same_url_policy_as_the_browser(monkeypatch):
    """A tool that fetches a URL can reach an internal address. Routing
    around the browser must not route around its scope."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    out = wr._read_impl({"url": "http://169.254.169.254/latest/meta-data/"})
    assert "refused" in out.lower() or "blocked" in out.lower(), out[:200]


# ------------------------------------------- the guards still apply

def test_the_fabrication_gate_reads_every_tool_not_only_browser_ones():
    """THE LOAD-BEARING PROPERTY. Adding a second way to fetch a page is
    only safe because the gate checks reported rows against the output of
    every successful call. If it filtered on browser tools, the search
    path would have been a hole straight through it."""
    import inspect
    import re
    from backend.app.api import routes
    src = inspect.getsource(routes._gate_deliverable)
    block = re.search(r"captured = (.+?)unbacked_row_labels", src, re.S)
    assert block, "the fabrication gate no longer builds a captured-output join"
    captured = block.group(1)
    assert "browser" not in captured, (
        "the captured-output join must not filter to browser tools")
    assert 'get("ok")' in captured, "successful calls only, any tool"


def test_search_output_can_back_a_reported_row():
    """A row that came from web_search is as backed as one that came off
    a page, because the gate sees both as tool output."""
    from backend.app.critique.compute_gate import unbacked_row_labels
    captured = ("[web search — 'weekly movers']\n"
                "title: NVDA leads the week\nsnippet: AAPL and MSFT followed.\n")
    text = ("| NVDA | NVIDIA | +12.40% |\n| AAPL | Apple | +8.10% |\n"
            "| MSFT | Microsoft | -4.20% |")
    assert unbacked_row_labels(text, captured) == []


def test_a_row_from_nowhere_still_fails_on_the_search_path():
    """The hole this would have been: retrieve one row by search, report
    three. Only what was actually retrieved may be reported."""
    from backend.app.critique.compute_gate import unbacked_row_labels
    captured = "[web search — 'weekly movers']\ntitle: NVDA leads the week\n"
    text = ("| NVDA | NVIDIA | +12.40% |\n| AAPL | Apple | +8.10% |\n"
            "| MSFT | Microsoft | -4.20% |")
    assert set(unbacked_row_labels(text, captured)) == {"AAPL", "MSFT"}


# ---------------------- the evidence has to SURVIVE, not just be read

def test_a_web_read_keeps_its_whole_page_in_the_ledger(monkeypatch):
    """The hole this file originally missed.

    An earlier test here asserted the fabrication gate reads every tool's
    output, which was true and not sufficient. The ledger only kept the
    FULL output of compute tools and anything whose name contained
    ".browser_" -- everything else was clipped to a 200-character
    preview. web_read's first 200 characters are the untrusted-content
    banner, so on the first live run after these tools shipped, six
    web_read calls stored the banner and none of the page.

    A gate that reads all tools but sees 200 characters of each is a gate
    that checks nothing: every fact correctly read off a page would have
    looked unbacked.
    """
    import time

    from backend.app.tools.tool_call_ledger import get_call_ledger
    from backend.app.tools.tool_registry import get_registry

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    body = "Senate passes short-term funding bill. " * 200
    monkeypatch.setattr(wr.httpx, "post", lambda url, **k: httpx.Response(
        200, json={"results": [{"url": "https://news.test/a", "raw_content": body}]},
        request=httpx.Request("POST", url)))

    started = time.time()
    get_registry().call("action.web_read", {"url": "https://news.test/a"})
    entry = next(c for c in get_call_ledger().calls(since=started)
                 if "web_read" in str(c.get("tool")))
    kept = str(entry.get("output") or "")
    assert len(kept) > 2000, f"only {len(kept)} chars kept — the page was clipped"
    assert "Senate passes short-term funding bill" in kept


def test_the_ledger_keeps_evidence_by_what_a_tool_does():
    """Named by behaviour, not by subsystem, so the next retrieval tool
    inherits this instead of silently losing its evidence."""
    import inspect
    from backend.app.tools import tool_registry
    src = inspect.getsource(tool_registry.ToolRegistry.call)
    assert "is_retrieval" in src
    assert ".web_read" in src and ".browser_" in src


def test_a_page_read_by_search_can_back_a_row_end_to_end(monkeypatch):
    """The whole point, joined up: read a page with web_read, and the
    rows on it are backed for the gate."""
    import time

    from backend.app.critique.compute_gate import unbacked_row_labels
    from backend.app.tools.tool_call_ledger import get_call_ledger
    from backend.app.tools.tool_registry import get_registry

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    page = ("Weekly movers: NVDA gained 12.4 percent, AAPL gained 8.1 percent "
            "and MSFT fell 4.2 percent. " * 40)
    monkeypatch.setattr(wr.httpx, "post", lambda url, **k: httpx.Response(
        200, json={"results": [{"url": "https://news.test/m", "raw_content": page}]},
        request=httpx.Request("POST", url)))

    started = time.time()
    get_registry().call("action.web_read", {"url": "https://news.test/m"})
    captured = "\n".join(
        str(c.get("output") or c.get("result_preview") or "")
        for c in get_call_ledger().calls(since=started) if c.get("ok"))

    honest = "| NVDA | NVIDIA |\n| AAPL | Apple |\n| MSFT | Microsoft |"
    assert unbacked_row_labels(honest, captured) == []

    invented = "| NVDA | NVIDIA |\n| TSLA | Tesla |\n| AMZN | Amazon |"
    assert set(unbacked_row_labels(invented, captured)) == {"TSLA", "AMZN"}


# ------------------------------------- the budget follows the WORK
#
# MEASURED ON A LIVE RUN, 2026-08-21. Asked to research ten AI startups,
# the loop did everything through web_search and web_read, never touched
# a browser, and ran out at the default ten steps having found the right
# source and not yet opened it. A second run of the same task happened
# to reach for the browser on step nine, widened to twenty-two, and got
# the data.
#
# Same task, same model, opposite outcomes -- decided by which
# instrument the model picked, which is not a thing a budget should
# depend on.

def test_research_covers_the_browser_and_the_web_tools():
    from backend.app.orchestrator.output_contract import is_research_tool
    for name in ("action.browser_navigate", "action.browser_extract",
                 "action.web_read", "action.web_search"):
        assert is_research_tool(name), name


def test_research_does_not_cover_ordinary_tools():
    """Widening for everything would make the default budget a fiction."""
    from backend.app.orchestrator.output_contract import is_research_tool
    for name in ("action.write_file", "action.run_python", "action.send_email",
                 "action.create_pptx", ""):
        assert not is_research_tool(name), name


def test_the_budget_rule_and_the_evidence_rule_name_the_same_tools():
    """tool_registry decides whose output is kept in FULL from the same
    three markers. A run whose evidence is worth storing is a run whose
    work is worth budgeting for, and the two lists drifting apart would
    silently reintroduce this bug on whichever side was not updated."""
    import inspect

    from backend.app.orchestrator.output_contract import _RESEARCH_MARKERS
    from backend.app.tools import tool_registry

    src = inspect.getsource(tool_registry)
    for marker in _RESEARCH_MARKERS:
        assert marker in src, (
            f"{marker!r} is budgeted for as research but tool_registry no "
            f"longer keeps its full output")


def test_a_web_only_run_gets_the_wider_budget():
    """The regression this whole section exists for."""
    import inspect

    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    _, _, tail = src.partition("if is_research_tool(action):")
    assert tail, "the budget must widen on research, not on the browser alone"
    assert "_budget_widened = True" in tail[:400]
    assert "BROWSER_MAX_STEPS" in tail[:400]


def test_the_browser_rules_still_need_an_actual_browser():
    """Only the BUDGET widened. Handing browser instructions to a run
    that never opened one would be noise in the prompt."""
    import inspect

    from backend.app.orchestrator import execution_loop
    src = inspect.getsource(execution_loop.AgenticExecutor.run)
    head, _, _ = src.partition("if is_research_tool(action):")
    assert "browser_rules_block = BROWSER_RULES" in head
    assert "if is_browser_tool(action):" in head
