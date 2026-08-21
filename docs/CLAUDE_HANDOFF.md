# Vision AI — Session Handoff

**Written:** 2026-08-21 · **Branch:** `feat/phase-4-orchestration` · **Tests:** 863 passed, 2 skipped

This describes the repository **as it exists now**. Where something is planned
rather than built, it says so. Where something passed a test but has not been
proven in a live run, it says that too — and this session, for the first time,
several things were proven in live runs and are marked accordingly.

**Supersedes the 2026-08-20 handoff.** Read §11 before planning work.

---

## 1. WHAT CHANGED THIS SESSION

Two things happened: a full architecture audit, and V1 core work built on top of
what it found. Then five live runs against real websites with a real model.

### Built
| Module | Purpose | Status |
|---|---|---|
| `orchestrator/strategy_memory.py` | stop re-asking a dead question | ✅ **live-proven** |
| `orchestrator/relevance.py` | is this item what was asked for | ✅ built, unit-tested |
| `orchestrator/item_verification.py` | per-item states + the overclaim refusal | ✅ built, replay-proven |
| `orchestrator/run_report.py` | honest result counted from state | ✅ built, replay-proven |
| `employees/capability.py` | capability → reuse-or-hire employees | ✅ **live-proven** |
| `goal_state.py` broadened completion | content + item + field completion | ✅ **live-proven** |

### Fixed (all found by replaying real traces, not by inspection)
1. **P0-1 cause two — SOLVED.** `browser_extract` heads its output
   `[page text — <url>]`, a fourth address shape nothing was reading. An extract
   on an item page never registered as *reading* it. The 2026-08-20 handoff
   guessed a `browser_click` route with no landing URL; **that was wrong.**
2. **`routes.py` never defined `logger`.** `logger.warning` at line ~796 has
   always been a `NameError` swallowed by its own `except`.
3. **`tools: {allow, deny}`** was in the config schema, accepted by `validate()`,
   shown in the UI, and dropped by `resolve()` — a setting that changed nothing.
   Now carried through and enforced in `AgenticExecutor._scoped()`.
4. **Budget widened only for browser tools.** A run doing research through
   `web_search`/`web_read` never got the wider budget. Now keyed on
   `is_research_tool` (browser + web_read + web_search).
5. **`_FIELD_WORDS` missed `product`, `website`, `funding`, `pricing`.** A task
   asking for four fields parsed as one, so completion fired on entry count
   while two of four fields were absent.
6. **`web_search` was in no strategy-memory family.** Eleven reworded searches
   in one live run went completely unseen.

---

## 2. CURRENT ARCHITECTURE

```
FRONTEND    frontend/          — React scaffold, EVERY FILE 0 BYTES. Dead.
            frontend_mvp/app/index.html — 4,059-line single file. The real UI.
                │ HTTP (FastAPI, no app-level auth)
API         api/routes.py (2,900 lines, ~60 endpoints)
            _gate_deliverable() is the last-mile verification chokepoint
                │
        ┌───────┴────────┐
   CEOManager        EmployeeCoordinator.run_with_supervisor()
   (Company)         (Unit: Supervisor + Specialists)
                            │ per specialist
                     DynamicEmployee.run_task()
                       1. heuristic pre-flight (URL fetch / Tavily / browser_task)
                       2. AgenticExecutor.run()  ◄── THE tool loop
                       3. build_objective()  — flattens transcript to a string
                       4. Pipeline.run_objective() — SEPARATE LLM, NO TOOLS
```

**The load-bearing fact:** tool-gathering and answer-writing are **two separate
LLM calls with no feedback loop**. `AgenticExecutor` gathers evidence; everything
downstream (contract, agent DAG, synthesis, critique) is pure prose generation
over a flattened transcript. `agent_executor.py` has no tool dispatch at all.
If the writer needs one more fact, there is no way to go get it.

---

## 3. VERIFICATION MODEL

| Layer | Question | Where | Status |
|---|---|---|---|
| Provenance | did a tool run? | `tool_call_ledger`, `source_ledger` | ✅ |
| Substance | did we get data? | `run_saw_a_data_table`, extractors | ✅ |
| Correctness | is the data sane? | `plausibility.py`, `compute_gate` | ✅ |
| Outcome | did state change? | `step_outcome.judge_step` | ✅ |
| Fabrication (figures) | was it computed? | `compute_gate.untraceable_metric_values` | ✅ |
| Fabrication (rows) | was the row seen? | `compute_gate.unbacked_row_labels` | ⚠️ **see §5** |
| Fabrication (URLs) | was it fetched? | `source_ledger.unretrieved_urls` | ✅ **live-proven** |
| Goal completion | is it done? | `goal_state.check` | ✅ nav + content + item + fields |
| Item verification | N items really opened? | `item_verification.overclaim` | ✅ **wired to the gate** |
| Relevance | does it match the ask? | `relevance.judge` | ⚠️ informs, does not block |

**The audit's #1 finding is closed.** `item_verification.overclaim` is now called
from `_gate_deliverable`. Before, every per-item guard lived inside the loop and
nothing connected them to the decision the founder sees.

---

## 4. LIVE RUN RESULTS (2026-08-21, DeepSeek v4 + Tavily)

| Test | Calls | Result | Fabrication |
|---|---|---|---|
| AI startups run 1 (no Tavily) | 13 (3 fail) | nothing produced | ✅ none |
| AI startups run 2 | 9 | **10 startups w/ funding**, 2 of 4 fields | ✅ none |
| AI startups run 3 | 8 | nothing — budget never widened | ✅ none |
| AI startups run 4 | 16 | nothing — 11 reworded searches | ✅ none |
| AI startups run 5 | 19 | table produced | ❌ **6 invented websites** |
| Vellum competitors | 20 | real pricing analysis, 1 competitor | ✅ none |
| **OpenAI research** | 15 | **complete, 4/4 fields** | ✅ **0 of 13 claims unbacked** |
| **Supervity research** | **9** | **complete, 4/4 fields** | ✅ **0 of 14 claims unbacked** |

**The pattern, stated plainly:** single-entity research off an authoritative site
works reliably. Multi-item aggregation across many sources is where every
failure has been. Five runs of the same multi-item task produced five different
outcomes — variance is the dominant problem, not any single guard.

**Run 5's fabrication is the important one.** It produced a 10-row table and
invented six company websites and four product descriptions from model memory.
`source_ledger.unretrieved_urls` catches the URLs (6 ≥ 2 → blocked). Nothing
catches the invented product text.

---

## 5. KNOWN BUGS / RISKS

### P0
1. **`reported_row_labels` is blind to ranked tables.** It reads the FIRST cell
   of each markdown row. When that cell is a rank number (`| 1 | Ola Krutrim |`)
   it rejects it as not identifier-shaped and returns `[]` — so
   `unbacked_row_labels` passes **vacuously**. Verified on run 5's real
   deliverable. One of the six verification layers is currently doing nothing on
   any table with a `#` column. **Fix this first.**
2. **Nothing checks cell VALUES.** `Hyperautomation platform` was pure invention
   with no URL attached and no gate looks at it. Row-backing checks labels only.
3. **Multi-item variance.** Same task, same code, same model: 5 runs, 1 verified,
   1 fabricated, 3 nothing. No fix has yet moved the success rate, because what
   varies is which source the model reaches for and nothing steers that.

### P1
4. **Snippet evidence is treated as page evidence.** Both single-entity runs took
   the HQ from a `web_search` snippet, not a fetched page. Correct both times,
   but the system cannot tell the two apart.
5. **Tool-gathering and writing are disconnected** (§2). A gap the loop leaves is
   a hard failure, not a recoverable one.
6. **Relevance informs, does not block.** Deliberate — a wrong rejection fails
   honest work — but it means an irrelevant item still reaches the deliverable.
7. **Task graph prices work, doesn't schedule it.** No task IDs, dependencies, or
   per-task accountable employee. The Supervisor LLM still assigns.
8. **No replan on failure.** Memories nudge and refuse; nothing re-plans.

### P2
9. No persistence (JSON files, no DB anywhere in the tree). 10. No app-level auth
— `auth/` is OAuth for third-party tools only. 11. `frontend/` is dead code.
12. `safety/constraints_enforcer.py` and `input_output_validator.py` have zero
references anywhere. 13. `execution_loop.run()` is one ~800-line function.

---

## 6. ARCHITECTURAL DECISIONS

- **The model decides HOW; code decides WHETHER, WHAT'S NEXT, and WHEN DONE.**
- **Model assertions are never trusted without evidence.** No guard asks the
  model whether its own work succeeded.
- **Verification is ledger-based.** Guards that query a RECORD hold; guards that
  match TEXT fail.
- **Nudge, don't block — except where a hard stop is provably safe.**
  `goal_state` stops hard. `strategy_memory` refuses only on an unchanged page
  (or, for searches, after four identical questions — the web does not change
  between two of them).
- **Silence is the default for new guards.** `confident=False`, `verdict=RUN`,
  `check()→False` all mean "behave exactly as before".
- **A founder's explicit budget is never overridden.**
- **Fixtures come from real traces.** Every bug fixed this session was found by
  replaying a real trace; none was found by inspection or synthetic fixtures.
- **A guard nothing calls does not exist.** Checked explicitly now — see the
  wiring tests in `test_strategy_memory.py` and `test_relevance_and_items.py`.

---

## 7. TESTS

`863 passed, 2 skipped`. The 2 skips are live-LLM tests in `test_browser_task.py`
that skip when no model is reachable.

New this session:
`test_strategy_memory.py` (44) · `test_relevance_and_items.py` (27) ·
`test_capability_staffing.py` (25) · `test_broadened_completion.py` (25) ·
`test_run_report.py` (12) · `test_v1_replay.py` (12, **real 21-call trace**)

`test_v1_replay.py` replays a genuine recorded run through the whole V1 chain.
Two bugs were found while writing it that no synthetic fixture could have caught.

---

## 8. RUNNING IT

```bash
python -m pytest -q                     # expect 863 passed, 2 skipped
python trace_browser_run.py --task-file <file>   # one live loop run + trace
```

Live runs need `DEEPSEEK_API_KEY` and `TAVILY_API_KEY` as environment variables.
**Without Tavily, `web_search` AND `web_read` are both dead** and research tasks
fall back to driving search engines in the browser, where Bing returns generic
results and DuckDuckGo serves a CAPTCHA. This was measured, not assumed.

Keys are never written to a file. Rotate any key that appears in a transcript.

---

## 9. DO NOT TOUCH

- The **verification layers** — do not weaken, merge, or make any model-attested.
- `source_ledger` / `tool_call_ledger`, including `is_retrieval` in
  `tool_registry.py`. Note `output_contract._RESEARCH_MARKERS` must stay in sync
  with it; a test pins them together.
- `goal_state`'s navigation stop — live-proven, W5 16 calls → 3.
- The browser tool layer — the strongest part of the codebase; **stop adding to it.**
- LLM ownership of tool choice, query wording, site selection.

---

## 10. RISKY FILES

- `orchestrator/execution_loop.py` — ~1,600 lines, one ~800-line function, eight
  guards interleaved. Verify `plan`/`steps`/`view_before` scope before inserting.
- `api/routes.py` — 2,900 lines. `_gate_deliverable` is the chokepoint; every
  check there decides whether a founder is told "done".
- `orchestrator/goal_state.py` — now carries navigation, content, item and field
  completion. The navigation path is live-proven and was deliberately left
  untouched while the others were added.

---

## 11. NEXT SESSION — WORK ON FIRST

1. **Fix `reported_row_labels` to look past a rank column** (P0-1). One of six
   verification layers is currently inert on any ranked table. Smallest,
   highest-value fix available.
2. **Decide whether cell values need backing** (P0-2). Row labels are checked;
   the cells beside them are not. This is a design decision, not a bug fix.
3. **Run the internship task ×3.** It is the only task that exercises
   `item_verification.overclaim`, and it has still never been run live since that
   gate was built. n≥3 because variance exceeds signal below that.
4. **Stop tuning the AI-startups task.** Five runs, one clean. The next useful
   evidence is on a different task shape.

### Principles that must not be violated
1. Never trust a model's claim about its own success.
2. Every guard reads a record, never text the model or a site wrote.
3. A new guard's default must be silence.
4. Fixtures come from real traces.
5. Do not report a fix as validated unless a **live run** proves it.
6. Do not widen a budget or weaken a check to make a test pass.
