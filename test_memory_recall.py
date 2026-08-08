"""Phase 4 — memory. Tests that recall is by RELEVANCE, not recency.

The bug these exist to prevent is subtle, because the broken version
looks fine in a demo: ask a question, get an answer, ask a follow-up,
watch it resolve. That works today via the recency window. It breaks the
moment three unrelated runs happen in between — the deliverable is still
on disk in runs.json and the founder still gets "which pricing table?".

So the load-bearing test here is test_recall_survives_restart_and_gap:
old run, several newer unrelated ones, a fresh store loaded from disk
(a server restart), and recall must still find it.

The second theme is the threshold. Every test that asserts something is
NOT recalled is guarding the calibration claim in retrieval.
MIN_RELEVANCE, because a loose match is worse than no match — it hands
an employee plausible material for a question it does not answer.

Run:
    py -3 test_memory_recall.py
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from backend.app.chat.async_runs import RunStore
from backend.app.memory import retrieval
from backend.app.memory.memory_manager import MemoryManager
from backend.app.memory.mistake_repository import MistakeRepository


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

NOW = 1_700_000_000.0


def _fresh_store() -> RunStore:
    """Isolated RunStore so a test can never read or clobber the real
    runs.json — the same isolation the http_tools test uses."""
    tmp = Path(tempfile.mkdtemp()) / "runs.json"
    return RunStore(path=str(tmp))


def _add_run(
    store: RunStore,
    *,
    task: str,
    output: str,
    finished_at: float,
    status: str = "done",
    error: str = "",
) -> str:
    """Insert a FINISHED run directly. Goes through the store's own
    setters so persistence behaves exactly as it does in production,
    then back-dates finished_at — ordering is what these tests turn on."""
    run_id = store.create(intent="run_task_unit", task=task)
    store.set_running(run_id)
    if status == "done":
        store.set_done(run_id, output)
    else:
        store.set_failed(run_id, error or "boom")
    store._set(run_id, finished_at=finished_at)  # noqa: SLF001 — test fixture
    store._save()  # noqa: SLF001
    return run_id


# --------------------------------------------------------------------
# Ranker
# --------------------------------------------------------------------

def test_ranker_prefers_topic_over_position() -> None:
    """The whole point: position in the list must not decide the winner."""
    docs = [
        ("a", "Draft a launch tweet thread about our onboarding flow"),
        ("b", "Compare Notion and Linear pricing tiers for a 12-person team"),
        ("c", "Write three cold outbound emails to design agencies"),
    ]
    hits = retrieval.rank("add Asana to that Notion pricing comparison", docs)
    assert hits, "expected the pricing run to be recalled"
    assert hits[0][0] == "b", f"ranked wrong doc first: {hits}"


def test_ranker_rejects_incidental_vocabulary() -> None:
    """Guards MIN_RELEVANCE. These documents share ordinary business
    words with the query and nothing else; recalling one would be worse
    than recalling nothing."""
    docs = [
        ("a", "Write a blog post about remote team culture and hiring"),
        ("b", "Summarise the customer support backlog for last week"),
    ]
    hits = retrieval.rank("compare Notion and Linear pricing tiers", docs)
    assert hits == [], f"recalled an unrelated run: {hits}"


def test_tokenizer_keeps_identifiers_intact() -> None:
    """Splitting on '.' or '-' would shred exactly the tokens that carry
    the most signal."""
    toks = retrieval.tokenize("DOI 10.1016/j.jfineco see notion.so and gpt-4o")
    assert "10.1016" in toks, toks
    assert "notion.so" in toks, toks
    assert "gpt-4o" in toks, toks


def test_ranker_ignores_terms_absent_from_the_corpus() -> None:
    """Regression: "add X to that Y" is how founders refer back to work,
    and it always contains words the corpus has never seen. Those words
    used to carry maximum IDF into the denominator and sink the correct
    document below the threshold — a real match scored 0.094 against a
    0.15 cutoff."""
    docs = [
        ("a", "Draft a launch tweet thread about our onboarding flow"),
        ("b", "Compare Notion and Linear pricing tiers for a 12-person team"),
        ("c", "Write three cold outbound emails to design agencies"),
    ]
    with_unknowns = retrieval.rank("add Asana to that Notion pricing comparison", docs)
    without = retrieval.rank("Notion pricing", docs)
    assert with_unknowns and without
    assert with_unknowns[0][0] == without[0][0] == "b"
    # Padding the query with corpus-absent words must not move the score.
    assert abs(with_unknowns[0][1] - without[0][1]) < 1e-9, (
        f"unknown terms still affect scoring: {with_unknowns} vs {without}"
    )


def test_ranker_ignores_terms_common_to_most_of_the_corpus() -> None:
    """The other half of the prune. If every past run is a 'report',
    matching the word 'report' is not evidence of anything."""
    docs = [(str(i), f"Weekly report number {i} covering operations") for i in range(6)]
    assert retrieval.rank("produce a report", docs) == []
    # ...but a distinctive term in the same corpus still retrieves.
    docs.append(("x", "Weekly report covering Notion and Linear pricing tiers"))
    hits = retrieval.rank("report on Notion pricing", docs)
    assert hits and hits[0][0] == "x", f"distinctive term failed to retrieve: {hits}"


def test_one_shared_generic_word_is_not_a_match() -> None:
    """Found by probing 16 real runs, not by construction.

    Both queries below share exactly one ordinary word with the corpus
    ('table', 'write') while every distinctive word they contain —
    restaurant, Lisbon, poem, sea — is absent from it. Coverage alone
    rated these 0.32 and 0.55 and recalled the wrong deliverable.
    """
    docs = [
        ("papers", "Find the top 5 trading research papers to build a quant "
                   "model. Results table with DOI links for each paper."),
        ("email", "Write a 100-word cold email to a seed investor "
                  "introducing an AI agents startup."),
        ("python", "Go to python.org downloads and report the latest stable "
                   "Python version."),
        ("screener", "Write a two-sentence summary of what a stock screener is."),
    ]
    assert retrieval.rank("book me a table at a restaurant in Lisbon", docs) == []
    assert retrieval.rank("write me a poem about the sea", docs) == []
    # Naming the subject still works — this is the cost of the rule.
    hits = retrieval.rank("add two more trading papers to that quant list", docs)
    assert hits and hits[0][0] == "papers", hits


def test_ranker_empty_inputs_are_safe() -> None:
    assert retrieval.rank("", [("a", "text")]) == []
    assert retrieval.rank("query", []) == []
    assert retrieval.rank("query", [("a", "")]) == []


# --------------------------------------------------------------------
# MemoryManager — the acceptance test
# --------------------------------------------------------------------

def test_recall_survives_restart_and_gap() -> None:
    """The founder-visible bug, end to end.

    A pricing comparison, then three unrelated runs that push it out of
    the recency window, then a server restart. The follow-up must still
    resolve to it.
    """
    tmp = Path(tempfile.mkdtemp()) / "runs.json"
    store = RunStore(path=str(tmp))

    _add_run(
        store,
        task="Compare Notion and Linear pricing for a 12-person team",
        output=(
            "| Tool | Tier | Price/user/mo |\n"
            "| Notion | Business | $15 |\n"
            "| Linear | Business | $14 |\n"
            "Both bill annually; Linear has no free tier above 250 issues."
        ),
        finished_at=NOW,
    )
    for i, (task, out) in enumerate([
        ("Draft a launch tweet thread", "Thread: 1/ we shipped..."),
        ("Write cold outbound emails to agencies", "Subject: quick question..."),
        ("Summarise this week's support tickets", "42 tickets, 6 escalations..."),
    ]):
        _add_run(store, task=task, output=out, finished_at=NOW + 100 * (i + 1))

    # Restart: brand-new store object, same file on disk.
    reloaded = RunStore(path=str(tmp))
    assert len(reloaded.list_history()) == 4, "persistence broke before recall ran"

    # Recency alone cannot see the pricing run any more — this is the
    # feed that used to be the ONLY feed.
    recent = reloaded.list_recent_done(session_id=None, company_id=None, limit=2)
    assert all("pricing" not in (r.get("task") or "").lower() for r in recent), (
        "fixture is wrong: pricing run is still inside the recency window, "
        "so this test would pass without any recall at all"
    )

    # The follow-up names its subject ("Notion pricing"). A bare "add
    # Asana to that table" deliberately recalls nothing — see
    # retrieval.MIN_MATCHED_TERMS for why one shared word is not
    # treated as evidence.
    hits = MemoryManager(store=reloaded).recall("add Asana to that Notion pricing table")
    assert hits, "recall found nothing after restart — the amnesia bug is back"
    assert "Notion" in hits[0].task
    assert "$15" in hits[0].output


def test_recall_excludes_ids_already_shown() -> None:
    """The clarifier passes the recency feed's ids in so a deliverable
    never appears twice under two different headers."""
    store = _fresh_store()
    run_id = _add_run(
        store,
        task="Compare Notion and Linear pricing",
        output="Notion $15, Linear $14",
        finished_at=NOW,
    )
    mgr = MemoryManager(store=store)
    assert mgr.recall("notion linear pricing"), "sanity: should match unfiltered"
    assert mgr.recall("notion linear pricing", exclude_run_ids=[run_id]) == []


def test_recall_ignores_failed_runs() -> None:
    """A failed run's output is an error string. Feeding it back as
    source material is how an error message became a search query in the
    bug that started all of this."""
    store = _fresh_store()
    _add_run(
        store,
        task="Compare Notion and Linear pricing",
        output="",
        status="failed",
        error="OllamaAdapterError: HTTP 502",
        finished_at=NOW,
    )
    assert MemoryManager(store=store).recall("notion linear pricing") == []


def test_recall_returns_nothing_for_unrelated_task() -> None:
    store = _fresh_store()
    _add_run(
        store,
        task="Compare Notion and Linear pricing",
        output="Notion $15, Linear $14",
        finished_at=NOW,
    )
    hits = MemoryManager(store=store).recall("book me a flight to Lisbon in March")
    assert hits == [], f"recalled an irrelevant deliverable: {hits}"


def test_recall_degrades_to_empty_when_store_breaks() -> None:
    """Recall is additive. A broken store must cost relevance, never the
    founder's turn."""
    class Exploding:
        def list_history(self, limit: int = 500) -> List[Dict[str, Any]]:
            raise RuntimeError("disk on fire")

    assert MemoryManager(store=Exploding()).recall("anything") == []


def test_format_for_prompt_marks_recall_as_stale() -> None:
    """Framing is load-bearing — unlabelled, a weak model treats recalled
    text as current fact or as instructions."""
    store = _fresh_store()
    _add_run(
        store,
        task="Compare Notion and Linear pricing",
        output="Notion $15",
        finished_at=NOW,
    )
    hits = MemoryManager(store=store).recall("notion linear pricing")
    block = MemoryManager(store=store).format_for_prompt(hits)
    assert "EARLIER" in block
    assert "re-verify" in block.lower()
    assert MemoryManager(store=store).format_for_prompt([]) == ""


# --------------------------------------------------------------------
# MistakeRepository
# --------------------------------------------------------------------

def test_failures_respect_retention_window() -> None:
    store = _fresh_store()
    now = time.time()
    _add_run(
        store,
        task="Scrape competitor pricing pages",
        output="",
        status="failed",
        error="TimeoutError",
        finished_at=now - (60 * 86400),  # 60 days old
    )
    repo = MistakeRepository(store=store, retention_days=30)
    assert repo.similar_failures("scrape competitor pricing pages", now=now) == []

    repo_long = MistakeRepository(store=store, retention_days=90)
    assert repo_long.similar_failures("scrape competitor pricing pages", now=now)


def test_failures_rank_on_task_not_error_text() -> None:
    """Two failures with identical error strings but different tasks —
    only the matching TASK should come back. If the ranker were reading
    the error text, both would."""
    store = _fresh_store()
    now = time.time()
    _add_run(
        store, task="Scrape competitor pricing pages", output="",
        status="failed", error="TimeoutError: navigation timed out",
        finished_at=now - 3600,
    )
    _add_run(
        store, task="Generate quarterly board deck", output="",
        status="failed", error="TimeoutError: navigation timed out",
        finished_at=now - 1800,
    )
    hits = MistakeRepository(store=store).similar_failures(
        "scrape competitor pricing pages again", now=now
    )
    assert len(hits) == 1, f"expected exactly one match, got {[h.task for h in hits]}"
    assert "Scrape competitor" in hits[0].task


# --------------------------------------------------------------------
# Per-employee store
# --------------------------------------------------------------------

def test_employee_memory_prefers_relevance_then_falls_back() -> None:
    from backend.app.employees.memory_store import EmployeeMemoryStore

    tmpdir = tempfile.mkdtemp()
    mem = EmployeeMemoryStore("test-employee", memory_dir=tmpdir)
    mem.record("Compare Notion and Linear pricing", {"output": "Notion $15, Linear $14"})
    for task in ("Draft a tweet thread", "Write outbound emails", "Summarise tickets"):
        mem.record(task, {"output": f"done: {task}"})

    # Relevance wins over recency...
    ctx = mem.relevant_context("what did we find on Notion pricing")
    assert "Notion" in ctx, f"relevance lookup missed: {ctx}"

    # ...and with no match, the old recency behaviour still applies
    # rather than the block vanishing.
    ctx2 = mem.relevant_context("book a flight to Lisbon")
    assert ctx2.strip(), "fallback produced an empty block (regression)"
    assert "Summarise tickets" in ctx2, ctx2


# --------------------------------------------------------------------
# Clarifier rendering
# --------------------------------------------------------------------

def test_clarifier_block_separates_recalled_from_recent() -> None:
    """Recent and recalled deliverables share one list but must not
    share a header — 'that list' means the recent one."""
    from backend.app.chat.clarifier import _build_context_block

    block = _build_context_block({
        "recent_deliverables": [
            {"task": "Draft a tweet thread", "snippet": "1/ we shipped", "kind": "recent"},
            {"task": "Notion vs Linear pricing", "snippet": "Notion $15", "kind": "recalled"},
        ],
    })
    assert "Recent deliverables in this session" in block
    assert "Earlier work that matches this request" in block
    assert block.index("Draft a tweet thread") < block.index("Notion vs Linear pricing")


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(list(globals().items())):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
