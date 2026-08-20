# Vision AI — Session Handoff

**Written:** 2026-08-20 · **Branch:** `feat/phase-4-orchestration` · **Tests:** 707 passed, 2 skipped

This describes the repository **as it exists now**. Where something is planned
rather than built, it says so. Where something passed a test but has not been
proven in a live run, it says that too — several things today looked finished
and were not.

---

## 1. PROJECT OVERVIEW

### What Vision AI is becoming
AI employees that do a founder's actual work — browse, extract, compute, draft,
act — rather than advise. The differentiator is not capability but **honesty**:
the system refuses to report what it cannot evidence.

### Current architecture (three layers, loosely coupled)

```
EMPLOYEES        ceo_manager, dynamic_employee, supervisor, company_store
   │             (who does the work, with what mandate)
   ▼
ORCHESTRATION    two disconnected systems — see §5
   │             (a) pipeline/contract path: ExecutionState, agent DAG
   │             (b) AgenticExecutor loop: ALL browser work, no shared state
   ▼
TOOLS            47 registered actions: browser (23), web_search/web_read,
                 run_python, documents, email/slack, github, arc_game
```

### Browser automation architecture
Playwright, one shared instance on one thread (`session_manager.py`), one
on-disk profile → one context. Semantic element ids (`data-vai-id`) stamped per
observation. Headed/headless is a per-session property with a one-way upgrade.

### Orchestration architecture
`AgenticExecutor.run()` in `execution_loop.py` (~1,380 lines) is the real loop:
THINK → ACT → OBSERVE, one tool per step, LLM picks the tool. Guards wrap it.
As of today it is preceded by a goal spec and a feasibility verdict (§5).

### Employees ↔ orchestration
`dynamic_employee.py:678` constructs `AgenticExecutor` per specialist and passes
`loop_model` from employee config. Employees own the mandate and standing rules;
the orchestrator owns execution. **Employees do not share state with the loop** —
they receive its transcript and write prose from it.

---

## 2. CURRENT IMPLEMENTATION

### Branch and commits
- Branch `feat/phase-4-orchestration`, **not merged**, **not pushed**
- Last 6 commits (all browser-layer, pre-today):
  `0b7a398` A ranking can be asked for in the URL
  `1020555` Pick the data table, not the biggest one
  `eb838c6` Read the listings, and stop calling a real link a fabrication
  `c908585` Get the rows out, and refuse a ranking of things that cannot be real
  `99c3fd6` Match a playbook on proportion, not on a count of shared words
  `e728f46` Stop blaming a run for a URL it really opened

### Uncommitted work — ALL of today is uncommitted
**16 modified:** `action_registry.py`, `builtin/run_python.py`, `api/routes.py`,
`browser/observation.py`, `browser/primitives.py`, `browser/session_manager.py`,
`browser/task_flow.py`, `models/adapter_pool.py`,
`models/provider_adapters/openai_adapter.py`, `orchestrator/execution_loop.py`,
`orchestrator/output_contract.py`, `orchestrator/step_outcome.py`,
`tools/tool_registry.py`, `test_browser_task.py`, `test_outcome_check.py`,
`trace_browser_run.py`

**7 new source files:** `browser/structure.py`, `orchestrator/goal_spec.py`,
`orchestrator/goal_state.py`, `orchestrator/instrument_memory.py`,
`orchestrator/item_state.py`, `orchestrator/task_graph.py`,
`tools/web_research_specs.py`

**15 new test files:** `test_blocked_is_not_empty`, `test_extract_table_actually_runs`,
`test_goal_completion`, `test_goal_spec_and_budget`, `test_headed_upgrade`,
`test_instrument_memory`, `test_item_state`, `test_model_routing`,
`test_page_structure`, `test_quota_not_a_retry`, `test_records_fields`,
`test_records_picks_content`, `test_run_python_network_block`,
`test_table_reachability`, `test_web_research_tools`

### Tests
`707 passed, 2 skipped`. The 2 skips are in `test_browser_task.py:65` — live-LLM
tests that skip when no model is reachable. **They currently skip because the
DeepSeek key returns HTTP 401** (rotated/expired). They fail loudly if a model
answers wrongly; only unreachability skips.

### Model configuration
Default is `deepseek-v4-flash` (routes.py, task_flow.py). Bare `deepseek-*`
names route to `api.deepseek.com`; `vendor/model` names route to OpenRouter.
Ollama free tier exhausted; OpenRouter key expired; **DeepSeek key now 401**.
A working key must be supplied before any live run.

---

## 3. PHASE STATUS

### Browser hardening (today, complete)
Built: section/TOC/structure extraction, UTF-8 subprocess fix, network-block
fix, headed upgrade, instrument memory, table selection, head/tail.
Proven live: W1–W5, screener runs. See §8.

### Phase 1 — GoalSpec ✅ built, ✅ live
`goal_spec.py`. Rules-based extraction of item_count, per_item_work,
per_item_fields, recency, ranking. `confident=False` → caller behaves exactly as
before. `propose_refinement()` (LLM) exists but **is not wired in**.
Live: logged correctly on internship runs 3 and 4.

### Phase 2 — TaskGraph + feasibility ✅ built, ⚠️ partially live
`task_graph.py`. Sub-goal expansion, coarse cost estimate, verdict
RUN/DESCOPE/REFUSE. Recomputed when the browser budget widens.
Live: verdict fired and recomputed correctly (`25 of 10 → DESCOPE 2`, then
`25 of 22 → DESCOPE 8`).
**NOT validated:** that the DESCOPE note changes behaviour. It did not.

### Phase 3 — Item state ⚠️ half live
`item_state.py`. Derives discovered/opened/read from the ledger.
- `shortfall_note` — ✅ **proven live** (reported "2 of 8", then "1 of 8")
- `progress_note` (mid-run nudge) — ❌ **never fired in 4 live runs**

### Planned, not built
Strategy memory (page,intent), relevance verification, broadened completion for
content/item goals, persistence/resumption.

---

## 4. BROWSER AUTOMATION

### Stack
`session_manager.py` — Playwright lifecycle, one instance/one thread, session
registry, headed/headless with `headless` recorded on `BrowserSession` and a
**one-way** headless→headed upgrade (`task_flow.py`, `_browser_navigate_impl`).

### Capabilities (47 actions total; 23 browser)
| Area | Tool | Notes |
|---|---|---|
| Navigate | `browser_navigate` | session reuse; `interactive:true` upgrades to headed |
| Observe | `browser_observe` | semantic ids, iframes (≤8, `f1e3` ids) |
| Find | `browser_find` | locate control by description |
| Click | `browser_click`, `browser_click_element` | columnheader → inner-control path first |
| Type/Select | `browser_type`, `browser_select`, `browser_press` | never types passwords |
| Extraction | `browser_extract` | whole-page text; **blocked-page detection** |
| Tables | `browser_extract_table` | scored data-table default; `head`/`tail`; header excluded from counts |
| Records | `browser_extract_records` | card lists; homogeneity+boundedness scoring; `counts:` field |
| Sections | `browser_extract_section` | TreeWalker; `include_subsections`; h1–h6 + `role=heading` |
| TOC | `browser_extract_toc` | real widget detection; falls back to heading outline |
| Outline | `browser_outline` | all headings + levels |
| Structure | `browser_page_structure` | sections/tables/lists/cards + which reader to use |
| Login | `browser_await_login`, `browser_login_wait` | 180s, watches for password field to vanish |
| Misc | `browser_back`, `browser_scroll`, `browser_wait`, `browser_upload`, `browser_download`, `browser_dismiss_overlay` | |

### Verification / guards in the browser path
- **Data fingerprint** (`observation.py`) — row KEYS not values; `DATA:`/`SORTED:` lines
- **Outcome check** (`step_outcome.judge_step`) — before/after page state; read tools return UNKNOWN
- **Repeat guard** — `(tool, args)` fingerprint; allowed if page moved; 2nd repeat stops
- **Consecutive failure guard** — per tool, any args, resets on success
- **URL verification** — `source_ledger` records fetched vs seen
- **Instrument memory** — host × instrument failure, nudge not block
- **Item state** — see §5

### Known browser limitations
- **Bot walls are the real ceiling.** Finviz ~2-in-3 headless; Reddit blocks all
  headless routes and blocks Tavily entirely; Indeed Cloudflare.
- One profile → one context; no parallel sessions.
- No CAPTCHA policy — reports and moves on.
- No infinite-scroll pagination driver.
- Tavily cannot read JS-rendered tables (Finviz returns chrome, no rows).

---

## 5. ORCHESTRATION

### AgenticExecutor (`execution_loop.py`)
The only path browser work takes. Per-run state is **local variables**:
`seen_calls`, `repeat_counts`, `consecutive_failures`, `step_retries`,
`successful_calls`, `computed`, `done_refusals`, `goal_done`, `last_page_view`,
`plan`, `plan_note`. Nothing persists.

Budget: `MAX_STEPS=10`/240s default; widens to 22 steps/600s/3000 tokens on
first browser tool, **only if the budget is default** (an explicit founder
budget is never overridden).

### What exists
| Component | File | Status |
|---|---|---|
| GoalSpec | `goal_spec.py` | ✅ live |
| TaskGraph + feasibility | `task_graph.py` | ✅ verdict live; enforcement not |
| Item state | `item_state.py` | ⚠️ shortfall live; nudge never fires |
| Completion gate | `goal_state.py` | ✅ live (navigation goals only) |
| Instrument memory | `instrument_memory.py` | ✅ built, effect unmeasured |
| Output contract | `output_contract.py` | ✅ live |
| Step outcome | `step_outcome.py` | ✅ live |
| Plausibility | `plausibility.py` | ✅ live |
| Playbook | `browser/playbook.py` | ✅ live, cross-run |

### What is planned only
Strategy memory; relevance checking; content/item completion; persistence;
`GoalSpec.propose_refinement` wiring; per-item enforcement (refusal).

### The other orchestration system
`pipeline_controller` → `dependency_validator` → `execution_engine` →
`state_manager.ExecutionState`. Has real state (agent DAG, layering, status,
cost). **`execution_loop.py` references none of it** (grep: 0 matches).
`dependency_validator`'s own docstring notes the factory emits no real
dependencies, so every plan today is one layer.

---

## 6. EMPLOYEES

**Implemented:** `employee.py`, `dynamic_employee.py`, `employee_config.py`,
`employee_registry.py`, `employee_spawner.py`, `employee_coordinator.py`,
`ceo_manager.py`, `supervisor.py`, `hierarchy_designer.py`, `company_store.py`,
`team_store.py`, `memory_store.py`, `progress_store.py`, `playbooks.py`.

**Relationship:** employee config carries `model.loop_model` (per-employee model)
and standing rules, which reach the loop. `dynamic_employee` runs pre-flight
Tavily enrichment, dispatches `browser_task` directly for founder-present work,
and constructs `AgenticExecutor` for agentic work.

**Architectural proposal, not built:** employees owning a GoalSpec, or sharing
task/item state with the orchestrator.

---

## 7. VERIFICATION MODEL

| Layer | Question | Where | Status |
|---|---|---|---|
| Provenance | did a tool run? | `tool_call_ledger`, `source_ledger` | ✅ |
| Substance | did we get data? | `run_saw_a_data_table`, extractors | ✅ |
| Correctness | is the data sane? | `plausibility.py`, `compute_gate` | ✅ domain-limited |
| Outcome | did the state change? | `step_outcome.judge_step` | ✅ |
| Fabrication | is every row backed? | `compute_gate.unbacked_row_labels` via `_gate_deliverable` | ✅ |
| Goal completion | is the task done? | `goal_state.check` | ⚠️ navigation goals only |
| **Relevance** | does this match the ask? | — | ❌ **planned** |
| **Artifact detection** | is +320%/−95% a reverse split? | — | ❌ **planned** |

The gate reads the output of **every** successful tool call, not only browser
ones — which is why `web_read`/`web_search` are covered. That only holds because
`tool_registry.py` keeps FULL output for retrieval tools (`is_retrieval`), not
just a 200-char preview.

---

## 8. RECENT TEST RESULTS

| Test | Result | Worked | Failed | Root cause | Fixed | Live-validated |
|---|---|---|---|---|---|---|
| W1 AI page | PASS 3 calls | — | 1 redundant call | — | n/a | ✅ |
| W2 search | PASS 5 calls | typed in real search box; earlier run recovered click→Enter | — | — | n/a | ✅ |
| W3 History section | PASS 7 calls, **0 API** | `browser_extract_section` | 3 redundant calls | no section tool existed | ✅ | ✅ |
| W4 Python TOC | PASS 12 calls, **0 API** | `browser_extract_toc` | 10 calls after answer | no completion for content goals | ❌ | — |
| W5 back-nav | PASS **3 calls** (was 16) | `goal_state` hard stop | — | no completion concept | ✅ | ✅ |
| TOC repeat | FAIL→PASS **2 calls** | — | returned `0 entries` | `innerText` empty in collapsed container; box non-zero so visibility passed | ✅ `textOf` fallback | ✅ |
| Screener 1-month | PASS 2 calls | table selection, sort verified | — | — | n/a | ✅ (route was given) |
| Worldwide gainers/losers | PARTIAL 10 calls | cross-view join; all 3 head/tail modes | no losers' company names | Overview read sorted descending | ❌ | — |
| **Internship ×4** | **FAIL / 0,3,2,1 items** | shortfall note reports honest count | nudge never fires; budget over-committed | see §9 P0-1 | ⚠️ partial | ❌ |

---

## 9. KNOWN BUGS / RISKS

### P0
1. **`progress_note` never fires in production** (`item_state.py`). 4 live runs,
   0 firings, 19 unit tests pass. Cause 1 (`Source:` vs `URL:` header) fixed and
   replay-verified. **Cause 2 open** — run 4 verified an item the detector didn't
   see, likely a `browser_click` route with no landing URL. **Do not attempt
   another blind fix; add logging first.**
2. **Advice ≠ enforcement.** Every *note* built today failed to change behaviour;
   every *hard stop* worked. Open decision: should `progress_note` refuse the
   call rather than annotate it (recommend: refuse on the 2nd redundant list call).
3. **Two disconnected state systems** — browser path uses neither
   `ExecutionState` nor per-item state beyond the derived view.

### P1
4. **Evaluation unreliable** — same task, same code: 0/3/2/1 items. Variance
   exceeds signal. Needs n≥3 runs or a deterministic replay harness.
5. **Item detection is navigation-shaped** — misses click-through, SPA routes,
   new tabs.
6. **No strategy memory** — 5 rephrased `browser_find` calls survived every guard.
7. **No relevance check** — accepted "Fashion Merchandising" as a PM internship.
8. **Completion is navigation-only** — W1/W3/W4 get no signal; W4 wasted 10 calls.

### P2
9. No persistence/resumption. 10. No parallel sessions. 11. `run_python` with
stderr and no stdout discards stderr. 12. `execution_loop.run()` is one ~780-line
function. 13. Plausibility misses reverse-split artefacts (+320% month/−95% year).

---

## 10. ARCHITECTURAL DECISIONS

- **The model decides HOW; code decides WHETHER, WHAT'S NEXT, and WHEN DONE.**
  The model is genuinely good at tool choice, query wording and source selection
  (it chose Internshala over suggested boards, recovered a failed click by
  pressing Enter). It is not trusted with stopping, budgeting, or completion.
- **Model assertions are never trusted without evidence.** No guard asks the
  model whether its own work succeeded. `goal_state` reads the ledger;
  `item_state` reads the ledger; `judge_step` compares page state.
- **Verification is ledger-based.** Guards that query a RECORD hold; guards that
  match TEXT fail. Repeatedly proven — the false-accusation bugs all came from
  text matching.
- **Nudge, don't block — except where a hard stop is provably needed.**
  `instrument_memory` nudges because walls come down mid-run. `goal_state` stops
  hard because a finished task is finished. Today's evidence suggests item
  progress may need to move to the stop side.
- **Silence is the default for new guards.** `confident=False`, `verdict=RUN`,
  `check()→False` all mean "behave exactly as before". A wrong plan is worse
  than no plan.
- **A founder's explicit budget is never overridden.**
- **Employees sit above orchestration**; the browser is a tool, not the
  intelligence layer.
- **Fixtures must come from real traces.** Three bugs today passed unit tests
  while failing in production because the fixture used a shape the system does
  not emit (`head`/`tail`, `table_index`, `Source:`).

---

## 11. NEXT STEPS (recommended order)

1. **Instrument `progress_note`** — log every evaluation and which clause
   returned None. One diagnostic run beats three speculative fixes.
2. **Decide advice vs enforcement** for item progress; if enforcement, refuse the
   2nd redundant list call.
3. **Build a replay harness** — feed a recorded `browser_trace.json` back through
   the loop's decision logic with a stubbed adapter. Makes evaluation possible.
4. **Broaden `goal_state`** to content and item-count goals (fixes W4's 10
   wasted calls).
5. **Strategy memory** — (page, intent) → outcome, extending `instrument_memory`.
6. **Relevance verification** against `GoalSpec.constraints`.

Do not start persistence (P2) — nothing in 1–6 needs it.

---

## NEXT SESSION INSTRUCTIONS

### Work on first
`progress_note` diagnosis (P0-1), then the advice-vs-enforcement decision (P0-2).
Nothing else in the orchestration layer is worth touching until item progress
actually fires.

### Do NOT touch
- The **six verification layers** — do not weaken, merge, or make any of them
  model-attested. This is the product's differentiator.
- `source_ledger` / `tool_call_ledger`, including `is_retrieval` in
  `tool_registry.py` (removing it silently blinds the fabrication gate).
- `goal_state`'s hard stop — proven live, W5 16 calls → 3.
- `playbook.py` subject-overlap matching.
- The browser tool layer — it is the strongest part; **stop adding to it**.
- LLM ownership of tool choice, query wording, site selection.

### Run these tests first
```bash
python -m pytest -q                    # expect 707 passed, 2 skipped
python -m pytest test_item_state.py test_goal_spec_and_budget.py -q
```
Live runs need a working `DEEPSEEK_API_KEY` — **the current key returns HTTP 401**.

### Risky files right now
- `orchestrator/execution_loop.py` — ~1,380 lines, one ~780-line function, six
  guards interleaved. Two patches today landed in the wrong scope and broke 43
  tests each time. Verify `plan`/`steps` scope before inserting anything.
- `orchestrator/item_state.py` — the module that doesn't work in production.
- `browser/structure.py` — three JS blocks sharing `_JS_HELPERS`; `stripEl` is
  defined before `textOf` (works via closure, fragile to reordering).

### Principles that must not be violated
1. Never trust a model's claim about its own success.
2. Every guard reads a record, never text the model or a site wrote.
3. A new guard's default must be silence.
4. Fixtures come from real traces.
5. Do not report a fix as validated unless a **live run** proves it.
