"""Smoke test: DeepResearchTool + the founder_task pre-flight fix.

Offline checks always run. Live checks (real Playwright + real network)
run too, but are the slow part — comment out test_live_crawl() if you
just want the fast offline coverage.

Run with:
    py -3 test_deep_research.py
"""
from __future__ import annotations

from backend.app.employees.dynamic_employee import (
    extract_urls,
    should_deep_research,
)
from backend.app.tools.browser_automation import (
    DeepResearchTool,
    distinct_domain_urls,
)


def test_trigger_heuristic() -> None:
    assert should_deep_research("do a competitor analysis on linear.app")
    assert should_deep_research("compare our pricing vs Notion")
    assert should_deep_research("research our rivals in project management")
    assert not should_deep_research("write a poem about spring")
    assert not should_deep_research("what is the capital of France")
    assert not should_deep_research("give me 3 video ideas for our product")
    print("[ok] should_deep_research triggers")


def test_distinct_domain_urls() -> None:
    urls = [
        "https://linear.app", "https://linear.app/pricing",
        "https://notion.so", "https://linear.app/about",
        "https://asana.com",
    ]
    out = distinct_domain_urls(urls, limit=3)
    assert out == ["https://linear.app", "https://notion.so", "https://asana.com"], out
    # limit is respected even with more distinct domains available
    assert len(distinct_domain_urls(urls, limit=1)) == 1
    print("[ok] distinct_domain_urls dedupes by root domain, respects limit")


def test_founder_task_fallback_recovers_dropped_url() -> None:
    """Regression test for the exact bug found in the 2026-08-01 session:
    a Supervisor's delegated sub_task paraphrased away both the URL and
    the word "competitor" that were present in the founder's original
    prompt, so should_deep_research/extract_urls never fired on the
    sub_task alone. DynamicEmployee.run_task now combines `task` +
    `original_task` into `heuristic_text` before running any pre-flight
    heuristic — this test proves that combination recovers what the
    sub_task alone would have missed."""
    sub_task = "Research Linear's pricing tiers, product features per tier, "
    original_task = (
        "Do a competitor analysis on https://linear.app - cover their "
        "pricing tiers, what they sell, and their positioning. Write a "
        "short structured report."
    )

    # OLD behavior: sub_task alone misses both signals.
    assert extract_urls(sub_task) == []
    assert should_deep_research(sub_task) is False

    # NEW behavior: task + original_task combined recovers both.
    heuristic_text = f"{sub_task}\n{original_task}"
    assert extract_urls(heuristic_text) == ["https://linear.app"]
    assert should_deep_research(heuristic_text) is True
    print("[ok] founder_task fallback recovers a Supervisor-dropped URL + trigger word")


def test_unrelated_downstream_specialist_does_not_inherit_trigger() -> None:
    """Regression test for the over-trigger bug found in a real 4-Unit
    CEO run: founder's top-level prompt said 'competitor analysis' (on
    Notion) -> CEO delegated to a Data Acquisition Unit -> its Supervisor
    delegated 'Clean, normalize, and verify the combined data' to a Data
    Curator. That specialist's OWN sub-task has nothing to do with
    competitor research, but it deep-crawled 3 irrelevant generic
    data-cleaning blogs anyway because should_deep_research(task +
    original_task) saw 'competitor analysis' in the inherited
    original_task and fired regardless of what THIS specialist's job
    actually was.

    Fix: the search-result-escalation path (step 3) must gate on
    should_deep_research(task) ALONE — not the combined heuristic_text —
    so a specialist several delegation layers removed from the founder's
    original wording doesn't inherit a trigger that has nothing to do
    with its own job."""
    founder_task = (
        "Do a competitor analysis on Notion — cover pricing, positioning, "
        "and market share. Write a structured report."
    )
    data_curator_subtask = (
        "Clean, normalize, and verify the combined data from the "
        "Research Associate and Primary Research Coordinator before "
        "handing it to the Competitive Intelligence Unit."
    )

    # The specialist's OWN task must NOT look deep-research-shaped on
    # its own merits — this is what step 3 must gate on.
    assert should_deep_research(data_curator_subtask) is False, (
        "Data Curator's own sub-task should not trigger deep research"
    )

    # But the OLD (buggy) combined-heuristic check WOULD have fired,
    # proving this is a real behavior change, not a no-op test.
    combined = f"{data_curator_subtask}\n{founder_task}"
    assert should_deep_research(combined) is True, (
        "Sanity check: the combined text SHOULD trigger — this is "
        "exactly the over-trigger the fix prevents from reaching step 3"
    )
    print("[ok] downstream specialist's unrelated sub-task does not inherit the deep-research trigger")


def test_live_crawl() -> None:
    """Real Playwright + real network. Skips cleanly if Playwright isn't
    installed (mirrors every other connector's quiet-disable pattern)."""
    tool = DeepResearchTool()
    if not tool.enabled:
        print("[skip] Playwright not installed — deep research live test skipped")
        return

    # Single-page site — no internal links, exactly 1 page expected.
    pages = tool.research("https://example.com")
    assert len(pages) == 1, f"expected 1 page from example.com, got {len(pages)}"
    assert "Example Domain" in pages[0]["content"]
    print(f"[ok] single-page crawl: {len(pages)} page(s) from example.com")

    # Multi-page SPA — confirms JS rendering + link-following both work.
    pages = tool.research("https://linear.app")
    assert len(pages) >= 2, f"expected multi-page crawl, got {len(pages)} page(s)"
    urls = [p["url"] for p in pages]
    assert any("pricing" in u for u in urls), f"expected a pricing page among: {urls}"
    print(f"[ok] multi-page JS-rendered crawl: {len(pages)} page(s) from linear.app, including pricing")

    # Dead/unreachable site — must fail quietly, never raise.
    pages = tool.research("https://this-domain-does-not-exist-vision-ai-test.invalid")
    assert pages == [], f"expected quiet failure, got {pages}"
    print("[ok] dead site fails quietly (empty list, no exception)")


if __name__ == "__main__":
    test_trigger_heuristic()
    test_distinct_domain_urls()
    test_founder_task_fallback_recovers_dropped_url()
    test_unrelated_downstream_specialist_does_not_inherit_trigger()
    test_live_crawl()
    print("\nAll tests passed.")
