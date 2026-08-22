# Vision AI — Principal Architecture Audit

**Date:** 2026-08-21 · **Branch:** `feat/phase-4-orchestration` @ `5d26b28` + uncommitted working tree
**Scope:** AUDIT ONLY. No files modified, no commits made.

Working-tree state at time of audit (not yet committed):
```
 M backend/app/orchestrator/execution_loop.py
 M backend/app/orchestrator/goal_state.py
 M backend/app/orchestrator/item_state.py
 M test_goal_completion.py / test_item_state.py / trace_browser_run.py
?? backend/app/orchestrator/strategy_memory.py
?? test_broadened_completion.py / test_strategy_memory.py
```
This is today's session's own work (strategy memory + broadened completion +
P0-1 instrumentation). It is included below as **current tree state**, marked
separately from what is actually committed, because auditing the committed
snapshot alone would describe code the founder is about to ship past.

---

## A. CURRENT ARCHITECTURE

```
┌─────────────────────────────────────────────────────────────────────┐
│  FRONTEND                                                             │
│  frontend/         — React/Vite scaffold. Every source file is       │
│                       0 bytes. DEAD.                                  │
│  frontend_mvp/app/index.html — 4,059-line single-file HTML/JS app.   │
│                       Calls the REAL API (/api/sessions/...). This   │
│                       is the actual UI.                              │
└─────────────────────────────────────────────────────────────────────┘
                              │ HTTP (FastAPI, no auth)
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  API — backend/app/api/routes.py (2,857 lines, ~60 endpoints)        │
│  Company/Unit/session CRUD, /run, /chat, /companies/{id}/run,        │
│  connectors, employees, build info. _gate_deliverable() is the       │
│  last-mile verification chokepoint (§G).                             │
└─────────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┴─────────────────────┐
        ▼                                             ▼
┌──────────────────────┐                  ┌────────────────────────────┐
│ COMPANY LAYER          │                  │ UNIT LAYER (session)        │
│ CEOManager             │  plans across    │ Supervisor + Specialists    │
│ plan_company_delegation│─── Units ───────▶│ EmployeeCoordinator          │
│ synthesize_company     │                  │ .run_with_supervisor()      │
└──────────────────────┘                  └──────────────┬─────────────┘
                                                            │ per specialist
                                                            ▼
                                          ┌──────────────────────────────┐
                                          │ DynamicEmployee.run_task()    │
                                          │  1. heuristic pre-flight       │
                                          │     (URL fetch/browser_task/  │
                                          │      Tavily/Reddit — ad hoc)  │
                                          │  2. AgenticExecutor.run()  ◄──┼── THE tool loop
                                          │     → text transcript only    │
                                          │  3. build_objective()         │
                                          │     (flattens transcript into │
                                          │      one prompt string)       │
                                          │  4. Pipeline.run_objective()  │
                                          │     → SEPARATE LLM call(s),   │
                                          │       NO TOOL ACCESS          │
                                          └──────────────────────────────┘
```

**The load-bearing fact this diagram exists to show:** there are **two
orchestration systems, and they are sequential, not parallel or
integrated.** AgenticExecutor gathers evidence via tools. Everything
downstream of it — contract generation, agent factory, execution engine,
synthesis, critique — is pure LLM prose-writing over the flattened
transcript string. **None of the "agents" the contract/DAG system creates
can call a tool.** `agent_executor.py`'s own docstring: *"Runs a single
agent through an LLM adapter... knows how to render a prompt, call the
adapter, parse JSON, retry on failure"* — no tool dispatch anywhere in it.

This means the DAG/dependency machinery in `dependency_validator.py` /
`execution_engine.py` / `state_manager.py` (the system the 2026-08-20
handoff called "the other orchestration system") is real, wired, and runs
on every multi-agent-tier task — but it orchestrates **writers**, not
**doers**. `dependency_validator.py`'s own docstring: *"the factory
doesn't yet emit real dependencies... every plan today is one layer."*

---

## B. ACTUAL EXECUTION FLOW

Traced for: **"Find 10 PM internships in India posted within 7 days, visit
each job page, extract fields, give me the top 10."**

```
1. POST /sessions/{id}/run  { task: "..." }
     routes.py:318 run_task_on_team()
2. TeamStore(session_id).members()  — must be non-empty or 400
3. EmployeeSpawner.instantiate(supervisor_spec + specialist_specs)
     → per-employee config resolved from EmployeeRegistry (JSON file)
     → per-employee loop_model read here (employee_config.py)
4. EmployeeCoordinator.run_with_supervisor(prompt, supervisor, specialists)
     a. SupervisorPlanner.design_delegation() — ONE LLM call, JSON plan
        of {role, sub_task, task_brief} — CODE never overrides this;
        if it fails, every specialist gets the raw prompt (fallback).
     b. for each assignment: DynamicEmployee.run_task(sub_task, ...)
5. DynamicEmployee.run_task()  (dynamic_employee.py:410)
     a. heuristic_text = task + original_task  (substring scans, no LLM)
     b. extract_urls() → deep-crawl or flat-fetch mentioned URLs
     c. should_browser_automate() → maybe action.browser_task (blocking,
        interactive-login path)
     d. should_search() → maybe Tavily + fetch top-2 results
     e. AgenticExecutor(adapter, max_steps=config.max_steps, ...).run(task)
        — THIS is where GoalSpec/TaskGraph/ItemState/GoalState/
          StrategyMemory/InstrumentMemory all live (execution_loop.py)
        — Returns a STRING (self._wrap(steps)) — no structured object
6. web_context = "\n\n".join(all gathered text)
7. objective = build_objective(task, web_context, task_brief, ...)
8. Pipeline.run_objective(objective)  (pipeline_controller.py:140)
     a. AdaptiveSupervisor.classify(objective) → tier
        (single_call | single_call_critique | multi_agent)
     b. multi_agent: ExecutionContractGenerator → AgentFactory →
        ExecutionEngine.run() → SynthesisEngine → CritiqueEngine
        — ALL PURE TEXT, no tool calls, operating over the objective
          string built in step 7
9. Coordinator formats final_output from supervisor synthesis
10. _gate_deliverable(final_output, task, since=run_start)  (§G)
      — raises DeliverableBlocked, OR returns text unchanged
11. RunStore.set_done() / progress_store.mark_complete()
12. Response → frontend polls /sessions/{id}/progress
```

**Step 5e is the only place tools are ever called.** It runs exactly
once per specialist per task. If the writer (step 8) realizes mid-synthesis
that it's missing a fact, there is no mechanism to go back and fetch it —
the tool-gathering phase is already over.

---

## C. COMPONENT STATUS TABLE

| Component | Does | Owns state | Decision-maker | Evidence used | On failure | Status |
|---|---|---|---|---|---|---|
| `AgenticExecutor` (execution_loop.py) | THINK→ACT→OBSERVE tool loop | local vars only, never persisted | LLM picks tool; code decides repeat/stop/budget | tool_call_ledger, page-view diffing | logs, injects note, continues or breaks | **A** (single-step), **B** (multi-item, see §D) |
| `GoalSpec` (goal_spec.py) | Parses task → structured shape | none (pure function) | rules; LLM refine exists, unwired | task text | `confident=False` → no-op | **A** |
| `TaskGraph` (task_graph.py) | Cost estimate, RUN/DESCOPE/REFUSE | none | code (coarse arithmetic) | GoalSpec + budget | fails open (RUN) | **A** for the verdict; **C** — nothing enforces it beyond a prompt note |
| `ItemState` (item_state.py) | Derives discovered/opened/read from ledger | none, recomputed every call | code | tool_call_ledger text patterns | never fires → silently under-tracks | **B**. Passes 19+ unit tests; confirmed in this session to have **never fired live in 4/4 runs** for reasons that are structural (see §H-1), not incidental |
| `GoalState` (goal_state.py) | Hard-stops the loop on verified completion | none | code, ledger-only | URL/title/count lines in ledger | silent — falls through to model DONE handling | **A** for navigation goals (proven live, 16→3 calls); **D** for content/item goals — built today, zero live runs |
| `StrategyMemory` (strategy_memory.py) | Refuses/notes repeated dead-end searches | per-run singleton | code | tool output text markers | silent if markers don't match | **D** — built and unit-tested today, **zero live validation** |
| `InstrumentMemory` (instrument_memory.py) | Nudges off a known-dead host×tool | per-run singleton | code | tool output text markers | silent | **B** — live-validated once (Reddit trace), effect on later runs unmeasured |
| `Pipeline` / contract-agent DAG (pipeline_controller.py + agents/) | Tiered multi-agent text synthesis | `StateManager` (in-memory, one run) | code picks tier; LLM writes | none — pure generation | falls back to single_call | **A** as a *text generator*; **F** as an *executor* — it has no tools |
| `_gate_deliverable` (routes.py:2145) | Last-mile fabrication/plausibility gate | none | code, ledger-backed | tool_call_ledger, source_ledger | raises `DeliverableBlocked`, run reported as failed | **A** — the strongest single component in the system |
| `EmployeeRegistry` / `EmployeeSpawner` | Dynamic employee identity + creation | JSON files, one per employee | LLM designs, code instantiates | — | falls back to solo Generalist | **A** |
| `CEOManager` / Company→Unit hierarchy | Cross-Unit delegation + synthesis | JSON (`company_store.py`) | LLM plans, code dispatches | — | falls back to flat per-Unit plan | **A**, reachable via `/companies/{id}/run` |
| `HierarchyDesigner` | LLM-proposes Company org structure | — | LLM proposes, founder must apply | — | — | **C** — proposal exists; "apply" step is a separate explicit action, thin test coverage (see §E) |
| Browser layer (browser/*.py, 5,700 lines) | Playwright automation, 23 actions | one shared Playwright instance/thread | LLM picks action; code verifies outcome | page-diff, DATA:/SORTED: fingerprint | reports failure text, tool returns to model | **A** — the most mature subsystem in the codebase |
| Tool registry / action registry | Unified tool surface (MCP+HTTP+builtins) | — | — | — | — | **A** |
| `adapter_pool.py` / `model_router.py` | Per-employee model routing | — | code (name-based routing) | — | falls back to Ollama/default | **A** (per this session's earlier fix) |
| `RecursionGuard` (safety/) | Spawn-limit enforcement | — | code | — | — | **B** — wired into the *old* pipeline path (`execution_engine.py`, `state_manager.py`); no evidence it bounds the Company→Unit→Specialist hierarchy depth |
| `constraints_enforcer.py`, `input_output_validator.py` (safety/) | (by name) input/output validation | — | — | — | — | **E — dead code.** Zero references outside their own files, anywhere in the tree |
| App-level auth | — | — | — | — | — | **F — does not exist.** `auth/` is OAuth for *third-party tool connections* (GitHub etc.), not for authenticating a user of Vision AI itself. Comment in `api/main.py`: *"Wide open for local MVP use"* |
| Persistence | — | JSON files under `data/` | — | — | — | **C.** No database anywhere in the tree (`grep` for sqlite/sqlalchemy/psycopg/redis/pymongo: zero hits). Works for single-user local; will not survive concurrent users or process restart mid-run |
| `frontend/` (React scaffold) | — | — | — | — | — | **E — dead.** Every source file is 0 bytes |
| `frontend_mvp/app/index.html` | Real UI, calls real API | browser localStorage/DOM state | — | — | — | **B** — functional, single 4,059-line file, no build step, no tests |

---

## D. ORCHESTRATION AUDIT

**Can Vision AI currently take "Find 10 PM internships in India posted
within 7 days and give me verified application links" and reliably:**

| # | Step | Verdict | Evidence |
|---|---|---|---|
| 1 | Understand the request | **YES** | `goal_spec.from_task()` — confirmed in this session to correctly extract `item_count=10, per_item_work=True, recency_days=7` from this exact wording (test fixtures use near-identical text) |
| 2 | Create a structured objective | **YES** | `GoalSpec` dataclass, deterministic extraction |
| 3 | Determine constraints | **PARTIAL** | Recency (`posted within N days`) is extracted into `spec.constraints`. Role/location/type constraints ("PM", "India") are **not** structurally extracted — they ride along in the free-text objective and are enforced nowhere. §H-6 |
| 4 | Plan subtasks | **YES** | `task_graph.build()` — discover(3) + per_item(2×N) + assemble(2), verdict RUN/DESCOPE/REFUSE, cost-estimated before spending |
| 5 | Discover candidates | **YES** | `browser_extract_records` — live-proven (screener, gainers/losers tests) |
| 6 | Open individual pages | **PARTIAL** | The *capability* exists (`browser_navigate`) and is used correctly when the model chooses to use it. What's missing is *forced* progression — see #10 |
| 7 | Verify each candidate | **PARTIAL** | `item_state.derive()` computes `opened & read` from the ledger — real, ledger-based, not model-attested. But it feeds only a **prompt note** inside the loop (§H-1/H-2), and **the final deliverable gate never checks it** (§G) — a run can pass `_gate_deliverable` while having verified 0 of 10 items, provided the candidate titles it reports were genuinely present on the LISTING page (which `unbacked_row_labels` accepts as "backing") |
| 8 | Maintain per-item state | **PARTIAL** | `ItemProgress` is recomputed fresh from the ledger on every evaluation — correct in design, silent in 4/4 live runs for reasons the session diagnosed today (§H-1) |
| 9 | Avoid duplicates | **YES** (design-only) | `_norm()` in `item_state.py`, `dict.fromkeys()` patterns in `goal_state.py` — dedup logic exists and is unit-tested; not separately live-validated for this specific task |
| 10 | Recover from failures | **PARTIAL** | Instrument memory nudges off dead hosts; strategy memory (new today, unvalidated live) nudges/refuses off dead-end searches; consecutive-failure and repeat-call guards exist. None of these *force* the model onto a different tool — every guard in this system is advisory except `goal_state`'s hard stop and (as of today, unvalidated) `strategy_memory`'s refusal |
| 11 | Stop when the goal is satisfied | **PARTIAL** | Navigation goals: **YES**, live-proven (W5: 16→3 calls). Item-count goals: **built today** (`goal_state.check(spec=..., target_items=...)`), unit-tested, **zero live runs** |
| 12 | Report partial completion honestly | **YES** | `shortfall_note()` — the one piece of this whole chain that IS live-proven: reported "2 of 8" then "1 of 8" on real runs, per the 2026-08-20 handoff |

**Net verdict:** the *machinery* for reliable multi-item execution is now
structurally complete for the first time (`GoalSpec → TaskGraph →
ItemState → GoalState`, all ledger-based, all wired into the loop). But
**every live run on record with this task shape (4/4) produced 0-3 items
of 10**, and the newest layer of enforcement (`strategy_memory`,
broadened `goal_state`, item-count completion) has run against **zero
live traffic**. The gap between "the code is correct" and "the run
succeeds" is exactly the gap this whole audit exists to surface — see
§H-1.

---

## E. EMPLOYEE SYSTEM AUDIT

| Question | Answer | Evidence |
|---|---|---|
| Can employees be created dynamically? | **YES** | `EmployeeSpawner.design_team()` — one LLM call → `[{role, mandate}]`, falls back to solo Generalist on failure |
| Is an employee just a prompt/persona? | **NO — more than that, less than full autonomy** | `DynamicEmployee` carries `role`, `mandate`, `config` (model, budgets, standing_rules, required_outputs), a memory store, AND its own `AgenticExecutor` instance per task. Not a bare persona; not an independent agent either — it's a configured invocation of the shared pipeline |
| Does an employee have tools? | **YES, indirectly** | Every employee's `run_task()` constructs an `AgenticExecutor` with the full tool surface (MCP + HTTP + built-in actions). No per-employee tool restriction mechanism found — every employee sees every connected tool |
| Does it have persistent state? | **YES** | `EmployeeMemoryStore` — JSON file per employee, keyed by `employee_id`, survives across task runs and Units (since 2026-08 the registry makes identity stable) |
| Can employees delegate? | **NO — only the Supervisor/CEO can** | `grep` for `delegate`/`send_message`/`terminate` methods on `Employee`/`DynamicEmployee`: zero hits. All delegation is Supervisor→Specialist (`SupervisorPlanner.design_delegation`) or CEO→Unit (`CEOManager.plan_company_delegation`) — top-down, code-orchestrated, never employee-initiated |
| Can employees communicate? | **ONE-WAY ONLY** | `teammates_context` — a formatted string of prior specialists' outputs, handed forward in delegation order. No employee can read a later teammate's work, ask a question, or receive a reply |
| Can employees execute independently? | **NO** | Every employee run is synchronous, coordinator-driven, inside one HTTP request (or one `async_runs.py` background task). Nothing runs unattended or on a trigger |
| Can employees have different models? | **YES** | `config.loop_model` flows from `EmployeeRegistry` → `employee_config.resolve()` → `AgenticExecutor(loop_model=...)`. Confirmed wired end-to-end |
| Can employees be spawned based on task requirements? | **YES** | `design_team()` reads the prompt and proposes a role list; `HierarchyDesigner` does the same at Company scale |
| Can an employee be terminated? | **PARTIAL** | `TeamStore.remove_member()` / registry deletion exist for **configuration removal**. There is no mechanism to interrupt an employee **mid-run** — `AgenticExecutor.run()` has a deadline/step budget but no external kill switch |
| Can employees reuse knowledge? | **YES, narrowly** | `EmployeeMemoryStore.record()`/recall by relevance (`test_memory_recall.py`, "recall is by RELEVANCE, not recency" — Phase 4 of the original project, already shipped). Scoped to one employee's own history, not shared across employees |
| Is there an employee registry? | **YES** | `employee_registry.py` — first-class UUIDs, one JSON file each, explicitly built to fix "same role in two Units = two different employees" |
| Is there a manager/CEO/orchestrator? | **YES, two layers** | Supervisor (Unit-level) and CEOManager (Company-level), both LLM-planned/code-dispatched, both reachable from the API |
| Can multiple employees work on one objective? | **YES** | This is the system's actual working mode — Supervisor→N specialists sequentially, teammates_context threading their outputs forward, Supervisor synthesizes |

**Implemented vs. design-only, summarized:** the entire *creation and
coordination* half of "AI employees" is implemented and reachable from
the API. The *autonomy* half — employees that decide their own next
action outside a request/response cycle, communicate bidirectionally, or
persist a run across a server restart — does not exist and is not
partially built; there is no code for it to audit.

---

## F. BROWSER AUDIT

| Capability | Works? | Evidence |
|---|---|---|
| Navigate | YES | `browser_navigate`, session reuse, one-way headless→headed upgrade |
| Click / type / press | YES | `browser_click`, `browser_click_element`, `browser_type`, `browser_press`; hover-then-force fallback for hover-revealed controls (TradingView sort arrows) |
| Observe | YES | `browser_observe`, semantic `data-vai-id` stamping, iframe support (≤8) |
| Extract text | YES | `browser_extract`, blocked-page detection (`_looks_like_a_wall`) |
| Extract sections | YES | `browser_extract_section` — TreeWalker-based, built and live-proven this project cycle (W3: 0 API calls) |
| Extract tables | YES | `browser_extract_table` — scored selection (not "biggest table"), head/tail, header excluded from row count |
| Extract records | YES | `browser_extract_records` — homogeneity+boundedness scoring to reject nav-menu false positives |
| Find elements | YES | `browser_find` — scans past the `MAX_ELEMENTS` cap that hides deep controls from `observe` |
| Follow links / go back | YES | `browser_back`, ledger-verified (`Went back to <url>`, not model-claimed) |
| Handle dynamic pages | PARTIAL | Retry-with-wait exists in `_extract_records_impl` (look, wait 2.5s, look again). No infinite-scroll driver. No generic "wait for network idle" primitive found |
| Recover from failed actions | YES | Hover-then-force click fallback; repeat-call guard nudges before stopping; consecutive-failure guard resets on success |
| Verify an action actually happened | YES | `step_outcome.judge_step` — before/after page-state comparison, not model self-report |
| Detect wrong outcomes | YES | Same + `plausibility.py` (catches e.g. +9,999,900% weekly gain on a delisted shell) |
| Maintain browser state | YES | One Playwright instance/one thread/one profile→one context (deliberate, documented constraint) |
| Work across multiple pages | YES | Session token persists across calls within a run |
| Work across multi-step workflows | PARTIAL | Within one `AgenticExecutor.run()` — yes. Across separate task runs / employee handoffs — no browser session is preserved |

**Known limitations, and whose fault they are:**

| Limitation | Category |
|---|---|
| Reddit, Indeed (Cloudflare), ~1/3 of Finviz headless attempts blocked outright | **CAPABILITY** — no CAPTCHA-solving policy, by design (this codebase declines to build one) |
| No infinite-scroll pagination | **CAPABILITY** — gap, not yet built |
| One profile → no parallel sessions | **CAPABILITY** — architectural choice, documented tradeoff |
| Tavily can't read JS-rendered tables (Finviz returns chrome, no rows) | **CAPABILITY** — third-party tool limit, not this codebase's |
| Item-level per-page verification (visited-but-not-individually-confirmed) | **ORCHESTRATION** — the browser correctly reports what happened; nothing above it enforces that enough pages were visited (§D, §H) |
| Five rephrased searches of one page before giving up | **LLM** (the model kept trying) mitigated by **ORCHESTRATION** as of today (`strategy_memory.py`, unvalidated live) |
| Reporting all 10 "verified" internships from a listing page never individually opened | **ORCHESTRATION**, not browser — the browser did exactly what it was told; nothing forced the individual opens |

**Conclusion:** the browser layer is the single most mature, most
battle-tested subsystem in the repository. It is not the reason
multi-item tasks fail. The orchestration layer above it is.

---

## G. VERIFICATION AUDIT

**Claim under test:** *"Nothing the model asserts is trusted. Everything
is checked against evidence."*

**Where this holds, concretely:**
- `tool_call_ledger.py` records every tool call, `since=` scoped so an
  old run's calls can't backstop a new one's claims (a real bug this
  fixed: `_unused_compute_capability` once asked the unscoped question)
- `source_ledger.unretrieved_urls()` — citations to URLs never fetched
  are caught (`_gate_deliverable`, routes.py:2210)
- `compute_gate.missing_metric_values` / `untraceable_metric_values` —
  a number must both be **stated** and **traceable to what the code
  actually printed** (full stdout kept for compute tools specifically,
  `MAX_COMPUTE_OUTPUT_CHARS=12000`, because a 200-char preview once hid
  the fabrication)
- `compute_gate.unbacked_row_labels` — every reported row must have
  appeared in something the run actually read
- `output_contract.run_saw_a_data_table` + ranking check — a "top N"
  claim over a table that was never actually sorted is refused
- `plausibility.check` — sense-checks the number itself (catches
  reverse-split artefacts, delisted-shell percentages)
- `goal_state.check` / `item_state.derive` — completion is read from
  ledger evidence, never from the model announcing DONE

**Where it does NOT hold — found in this audit, not previously
documented:**

1. **The final gate has no per-item count check.** `_gate_deliverable`
   verifies that reported rows are individually *backed by text the run
   read* (`unbacked_row_labels`), but that check is satisfied by a
   candidate's title appearing on the **listing** page — it does not
   require the item's own page to have been opened. `item_state.py`
   computes exactly that distinction (`opened & read`), but **nothing
   outside `execution_loop.py` imports `item_state`** (confirmed by
   grep). A run that lists 10 titles from one results page and writes
   plausible-sounding fields for each can pass every check in
   `_gate_deliverable` while `item_state.derive()` — had anyone asked it
   — would report `done=0/10`. This is the precise failure mode Phase
   1–4 orchestration work was built to prevent, and the strongest
   verification gate in the system cannot see it.

2. **The writer LLM (Pipeline.run_objective) never re-checks its own
   text against the loop's item-state before emitting it.** The
   loop's `shortfall_note` is folded into the transcript as prose the
   writer *may* read, not a structural constraint the writer *must*
   satisfy. Enforcement, where it exists at all for this class of
   claim, happens entirely after the fact, in `_gate_deliverable` — and
   per point 1, that gate doesn't check this particular claim.

3. **The contract/DAG agents (agent_executor.py) trust their own JSON
   parse.** `AgentResult` scores "confidence" heuristically from the
   agent's own output shape — this is model self-assessment, not
   ledger evidence, though it's a soft/informational score, not a gate
   that decides pass/fail on its own (it feeds `min_completeness_threshold`
   routing, not the final `_gate_deliverable`).

4. **`RecursionGuard` is real but scoped to the old pipeline path only**
   (`execution_engine.py`, `state_manager.py`). No evidence it bounds
   Company→Unit→Specialist nesting depth or the number of Units a CEO
   plan can spin up in one call.

**Net: the law is upheld everywhere it has been built to apply, and one
significant, previously-undocumented gap exists exactly where the
newest work (item-level verification) meets the oldest gate
(`_gate_deliverable`).** This is not a contradiction of the law — it's
an incompleteness: the check `item_state` performs was never plumbed
into the one place that decides pass/fail for the founder.

---

## H. TOP 10 PROBLEMS

Ranked by severity — what's actually stopping V1 reliability today.

### 1. Item-level enforcement doesn't reach the deliverable gate (SEVERITY: CRITICAL)
- **Evidence:** §G-1 above. `item_state.py`, `goal_state.py`, `strategy_memory.py`, `task_graph.py` are imported by exactly one file outside their own tests: `execution_loop.py`.
- **Why it matters:** the entire session's worth of Phase 1–4 work constrains the *loop*. It has zero effect on what the *founder is told is done*.
- **What breaks:** a run can under-deliver (0 of 10 items truly verified) and still pass every check that decides success/failure, provided the surface text is self-consistent with SOMETHING the run read (the listing page counts).
- **Fix:** thread `item_state.derive()` (and its `wanted` count from the plan) into `_gate_deliverable`, comparing the deliverable's claimed item count against `done`. Moderate complexity — the pieces exist; this is wiring, not new logic.

### 2. Four consecutive live runs of the flagship multi-item task produced 0–3 of 10 (SEVERITY: CRITICAL)
- **Evidence:** 2026-08-20 handoff, §8 test log: internship task ×4, results 0/3/2/1 items.
- **Why it matters:** this is the exact task V1's own target objective names as the benchmark ("Find 10 PM internships..."). It has never once succeeded.
- **What breaks:** V1's headline capability is unproven on its own headline example.
- **Fix:** the diagnostic instrumentation for this (P0-1, today's session) is now in the tree but has **zero live runs against it** — the very next action should be one live run with a working API key, reading the new `PER-ITEM PROGRESS` block in `trace_browser_run.py`'s output.

### 3. Tool-gathering and answer-writing are two separate LLM calls with no feedback loop (SEVERITY: HIGH)
- **Evidence:** §A, §B step 5e→8. `AgenticExecutor` returns a flat string; `Pipeline.run_objective` never calls a tool.
- **Why it matters:** if the writer realizes mid-synthesis that a fact is missing or contradictory, there is no mechanism to go fetch it. The only recourse is `_gate_deliverable` blocking the whole run after the fact and asking for a full re-run.
- **What breaks:** every gap the loop leaves is a hard failure, not a recoverable one, at the point where recovery would be cheapest.
- **Fix:** larger effort — either give the writer a bounded tool-call budget of its own, or make the critique/refine loop re-invoke `AgenticExecutor` with a targeted follow-up task when `_gate_deliverable` finds a specific, nameable gap (e.g. "missing metric X" → one more `run_python` call). High complexity; V2 candidate, not V1.

### 4. No live validation of ANY of today's new orchestration code (SEVERITY: HIGH)
- **Evidence:** `strategy_memory.py`, broadened `goal_state.check`, item-count completion — 27+53+ unit tests passing, `python trace_browser_run.py` never run this session (DeepSeek key returned 401 last confirmed).
- **Why it matters:** this session's own prior work found genuine defects in fixture-tested code before it ever ran live (the `DATA: (no data table)` false-positive in `strategy_memory.py`, caught only because a second, harder-nosed test was written). Unvalidated code of this shape has a real base rate of shipping-a-new-bug.
- **What breaks:** confidently reporting "Phase 4 is built" without a live run risks repeating the exact mistake `item_state.py`'s own docstring warns about — 19 passing unit tests, zero live firings, for a cause nobody could name for four runs.
- **Fix:** one live run, cheap, immediate — just needs a working API key.

### 5. Constraint fields ("PM", "India", "internship" vs "job") are not structurally verified (SEVERITY: HIGH)
- **Evidence:** §D-3, and the 2026-08-20 handoff's own P1 list: *"accepted 'Fashion Merchandising' as a PM internship."* `goal_spec.py`'s `_FIELD_WORDS`/`constraints` list captures recency and field *names*, not per-item constraint *values*.
- **Why it matters:** even a run that opens and reads all 10 pages can report 10 things that are not what was asked for, and nothing checks role/location relevance against evidence.
- **What breaks:** honest-looking, fully "verified" (by item_state's opened+read standard) deliverables that are wrong on the merits.
- **Fix:** this is exactly the "Relevance verification" item on the handoff's own roadmap (L6 addition, never built). Medium complexity — needs a real per-item constraint check against extracted fields, ledger-based, not model self-report.

### 6. No mechanism to interrupt a running employee (SEVERITY: MEDIUM)
- **Evidence:** §E. `AgenticExecutor` has an internal deadline/step budget; nothing external can stop it once started short of the HTTP request timing out.
- **Why it matters:** a founder who sees a run going wrong (via `/progress` polling) has no "stop" action — only wait it out.
- **What breaks:** wasted model spend on runs already known to be unproductive; no responsive UX for long tasks.
- **Fix:** medium complexity — a cancellation token threaded through the loop, checked once per step.

### 7. Persistence is JSON-files-on-disk, no transactionality, no concurrency safety (SEVERITY: MEDIUM, growing)
- **Evidence:** §C, zero DB library hits anywhere in the tree; `state_manager.py`'s own docstring: *"persistence is a Phase 9 concern... Phase 4 assumes single-run in-process usage."*
- **Why it matters:** fine for one founder, one browser tab, today. A second concurrent session, a server restart mid-run, or two employees writing the same registry file at once are all unguarded.
- **What breaks:** silent data loss or corruption under concurrent use — not observed yet because usage is single-user, but this is a ticking constraint, not a bug waiting to be found.
- **Fix:** not a V1 blocker at current usage. Flag for the moment multi-user or background scheduling (§I) becomes real.

### 8. The dead-code React frontend and two safety modules create false signal about system maturity (SEVERITY: LOW-MEDIUM)
- **Evidence:** §C — `frontend/` is 0 bytes throughout; `constraints_enforcer.py`, `input_output_validator.py` have zero external references.
- **Why it matters:** anyone (including a future audit, including this one on a first pass) can mistake "there is a file for it" for "there is a capability." The audit's own governing instruction — *do not confuse code existing with code working* — exists because this is a live risk in this exact repo.
- **What breaks:** wasted future effort extending or "fixing" code nobody calls; misleading repo size/maturity signal.
- **Fix:** trivial — delete or explicitly mark dead. See §I.

### 9. Employee-to-employee communication is one-way and code-mediated only (SEVERITY: LOW for V1, notable for the vision)
- **Evidence:** §E — no `delegate`/`message`/reply mechanism found anywhere.
- **Why it matters:** the long-term vision language ("employees... communicate") is not close to true today; it's Supervisor-mediated sequential handoff. Not a defect — a scope gap worth being honest about.
- **What breaks:** nothing today (V1 doesn't need this). Matters if V1's marketing/positioning claims employee-to-employee collaboration.
- **Fix:** none needed for V1. Flag as a naming/expectation issue, not a code issue.

### 10. `RecursionGuard` doesn't cover the Company→Unit→Specialist hierarchy (SEVERITY: LOW, latent)
- **Evidence:** §C, §G-4 — only wired into `execution_engine.py`/`state_manager.py` (the old single-Unit contract path).
- **Why it matters:** a CEO plan that names many Units, each running a Supervisor plan naming many specialists, has no code-enforced ceiling found in this audit on total fan-out per Company run.
- **What breaks:** in principle, a runaway-cost single API call. Not observed; not stress-tested either.
- **Fix:** low complexity — extend the existing guard's scope check to the Company dispatch path, or confirm (in a follow-up, not this audit) that an implicit ceiling already exists via `MAX_TEAM_SIZE`/`MAX_UNITS`-style constants and simply document it.

---

## I. WHAT TO STOP BUILDING

**Freeze immediately:**
- **The browser tool layer.** It is the strongest subsystem in the repo (§F). Every new browser primitive built this project cycle solved a real, live-traced failure — but the marginal next primitive is now competing with the orchestration layer for the same attention, and orchestration is where every live failure has been for the last three sessions running. The 2026-08-20 handoff already says this; this audit confirms it independently.
- **The DAG/dependency machinery** (`dependency_validator.py`, `execution_engine.py`'s layering logic) beyond what it already does. Its own docstring admits it's never had a real multi-layer dependency to schedule. Building it out further before a single real use case exists is exactly the "premature abstraction" this audit was told to hunt for.
- **`HierarchyDesigner`'s scope.** It works (LLM proposes an org chart) but has thin test coverage relative to its blast radius (it can restructure a whole Company). Don't add capability here; if anything, add tests before touching it again.

**Delete or explicitly mark dead (not a priority, but stop treating as live):**
- `frontend/` — 0 bytes throughout. Either delete it or add a one-line README saying "abandoned, see `frontend_mvp/app/index.html`." Its mere presence risks a future session "fixing" it under the impression it's the real frontend.
- `backend/app/safety/constraints_enforcer.py`, `backend/app/safety/input_output_validator.py` — zero references anywhere. Confirm with the founder whether these were meant for something not yet wired, or are leftover scaffolding; don't extend them either way until that's answered.

**Do not start (V2/V3, correctly out of scope per the founder's own framing):**
- Persistence/durable run state beyond JSON files (§H-7) — not needed until concurrent usage is real.
- Bidirectional employee messaging (§H-9) — the vision language implies it; V1 does not need it.
- CAPTCHA-solving or bot-wall circumvention for the browser layer — already correctly declined; nothing suggests revisiting this.
- Scheduling / autonomous unattended runs — no code for it exists, and nothing in the V1 target objective requires it.
- Any further multi-agent "tier" sophistication in `AdaptiveSupervisor`/`Pipeline` classification — the tiering already works; more tiers would be complexity without addressing any of the top 10 problems.

**The single highest-leverage thing to STOP doing:** shipping new
orchestration modules (GoalSpec → TaskGraph → ItemState → GoalState →
StrategyMemory, five modules across four work sessions) without a live
run validating the previous one first. Every module in that chain was
built cleanly, unit-tested thoroughly, and **the chain still has not
produced one successful live run of its own target task.** The
discipline that's needed now is fewer new modules, more live-run
evidence per module already built.

---

## J. WHAT V1 SHOULD ACTUALLY BE

Restating the founder's own scoping, because the audit confirms it's the
right cut:

**V1 = one founder, one meaningful business task at a time, executed
honestly.** Concretely, V1 is the path already traced in §B, working
reliably:

1. Founder states a goal in a Unit.
2. `GoalSpec` understands its shape (item count, per-item work, recency).
3. `TaskGraph` prices it against the budget and says RUN/DESCOPE/REFUSE
   *before* spending, honestly.
4. The Supervisor delegates to the right specialist(s) — already works.
5. `AgenticExecutor` gathers real evidence via real tools — already the
   strongest part of the system.
6. `ItemState`/`GoalState` track and enforce what was actually verified
   — built, needs live proof AND needs to reach the deliverable gate
   (§H-1, the actual next step).
7. `_gate_deliverable` refuses to report success on a deliverable the
   evidence doesn't support — already the strongest gate in the system,
   needs exactly one extension (§H-1).
8. The founder gets either a complete, verified answer, or an honest
   partial with a stated reason — `shortfall_note` already proves this
   works when it fires.

**V1 does NOT need**, and the audit found no evidence anyone is
currently trying to build: a full autonomous company, a universal
browser agent, autonomous ad platforms, an autonomous quant firm, or an
autonomous sales org. The repository's own trajectory over the last five
sessions (per commit log) has been narrowing toward exactly this V1, not
away from it — which is the right direction and worth stating plainly.

---

## K. V1 GAP ANALYSIS

| V1 requirement | Status | Gap |
|---|---|---|
| Understand the goal | DONE | none |
| Structured requirements | MOSTLY DONE | constraint *values* (not just presence) aren't verified (§H-5) |
| Determine what work needs to happen | DONE | `TaskGraph` |
| Choose/create the right employee | DONE | `EmployeeSpawner`/`SupervisorPlanner` |
| Create a plan | DONE | Supervisor delegation + `TaskGraph` sub-goals |
| Execute tools | DONE | browser layer, the system's strongest part |
| Verify results | GAP | verification exists (`item_state`) but doesn't reach the pass/fail gate (§H-1) — **this is THE gap** |
| Maintain state | MOSTLY DONE | per-run, ledger-based; not persisted across a server restart (acceptable for V1 per §I) |
| Recover from reasonable failures | MOSTLY DONE | instrument/strategy memory nudge and (new, unvalidated) sometimes refuse; no hard interrupt mechanism (§H-6, low priority) |
| Stop when satisfied | GAP-CLOSING | navigation goals: done. Item/content goals: built today, unvalidated live (§H-2, §H-4) |
| Report honestly | MOSTLY DONE | `shortfall_note` + `_gate_deliverable` prove the mechanism works; the one blind spot is §H-1 |

**The gap analysis and the top-10 list point at the same place from two
angles: item-level verification exists but is not connected to the
decision that actually reaches the founder.** Close that one wire and
V1's hardest remaining problem — reliable multi-item tasks — has a real
shot at working on the very next live run.

---

## L. RECOMMENDED BUILD ORDER

1. **Run the P0-1 diagnostic** (already instrumented, zero new code
   needed) — one live run of the internship task, read `PER-ITEM
   PROGRESS` in `trace_browser_run.py`'s output. This answers whether
   today's code changes anything before any more code is written.
2. **Wire `item_state`/`goal_state` into `_gate_deliverable`** (§H-1).
   Smallest, highest-leverage fix in this entire audit — the pieces
   exist, this is composition.
3. **Re-run the internship task** against the wired gate. This is the
   first point at which "V1 handles multi-item tasks" becomes an
   honestly testable claim rather than an aspiration.
4. **Only then**, decide on advice-vs-enforcement for `progress_note`
   (the open P0-2 question) — with live evidence in hand instead of a
   hypothesis.
5. **Relevance verification** (§H-5) — the next real gap once item-count
   verification is proven, not before (sequencing matters: no point
   verifying that 10 pages were opened if whether they were opened
   still isn't reliably known).
6. **Everything else in this list is lower priority than getting one
   full, honest, live pass on the flagship task.** Resist adding new
   orchestration modules until that happens.

---

## M. RISKS

- **Compounding unvalidated layers.** Five orchestration modules now sit
  on top of each other with a combined history of zero full live
  successes on their target task. Each additional module before a live
  proof point increases the search space for "which layer is actually
  silent" — exactly the diagnostic problem P0-1 spent a session solving
  for `item_state` alone.
- **False confidence from test count.** 766 passing unit tests is real
  signal about code correctness in isolation and close to zero signal
  about live reliability — proven by this exact codebase's own history
  (`item_state.py`: 19/19 passing, 0/4 live firings). Any future status
  report should state live-run counts alongside test counts, not in
  place of them.
- **Persistence ceiling** (§H-7) is currently invisible because usage is
  single-user. It will not announce itself gracefully when it becomes
  real; it will look like corrupted state under concurrent access.
- **Dead code as attractive nuisance** (§H-8) — the empty `frontend/`
  and the two unwired safety modules are cheap to accidentally build on
  top of in a future session that doesn't know to check.

---

## N. FINAL VERDICT

Vision AI's architecture is **closer to V1 than its live-run track
record suggests**, and the gap between those two facts is itself the
most important finding of this audit. The hard infrastructure — tool
execution, ledger-based verification, honest-partial reporting, dynamic
employee creation, a real Company/Unit hierarchy — is genuinely built
and, where it's been live-tested, genuinely works. The newest layer
(item-level goal tracking) is correctly designed, cleanly separated,
thoroughly unit-tested, and **has not yet been proven on a single live
run of its own target task** — and the one place it needs to reach
(the final deliverable gate) is a piece of wiring that doesn't exist
yet.

**This system does not need a new idea. It needs its most recent idea
proven once, live, and then connected to the gate that already exists
to enforce it.** That is a small amount of code and one API key away
from being true, which is a different — and better — kind of problem
than the audit was commissioned to find.
