# Vision AI — Session Handoff

**Written:** 2026-08-22 · **Branch:** `feat/phase-4-orchestration` · **Tests:** 976 passed, 2 skipped
**Supersedes** the 2026-08-21 handoff. Read §1 before anything else.

---

## 1. THE APP DOES NOT WORK. THE ORCHESTRATOR DOES.

**Symptom the founder sees:**

> Team completeness note (2 of 2 specialist(s) did not complete their assigned work):
> * Quantitative Researcher — produced no output at all
> * Trading Strategist — produced no output at all

**This is the single most important fact in this document.** Every
orchestration layer described below has been demonstrated working, live,
by a harness — and the product is nonetheless returning nothing to the
founder.

### Why it works in the harness and fails in the app

They execute different amounts of the system.

```
HARNESS (scratchpad/v1_live.py, and every live test reported this cycle)
    GoalSpec -> staffing -> feasibility -> AgenticExecutor / graph_runner
    -> print the transcript                                    <-- STOPS HERE

APP (DynamicEmployee.run_task, dynamic_employee.py:771)
    ...everything above...
    -> build_objective(task, web_context, teammates_context, task_brief)
    -> Pipeline.run_objective(objective)          <-- NEVER EXERCISED BY ANY
    -> _run_single_call -> ONE LLM CALL               HARNESS RUN THIS CYCLE
    -> snapshot()["synthesized_output"]
```

The harness verified the half that GATHERS evidence. The app also runs
the half that WRITES the answer, and that half is what fails. Every
"live-proven" claim in the previous handoff is therefore a claim about
the gathering half only. That distinction was not made, and it should
have been.

### The failing call, exactly

`pipeline_controller._run_single_call` (line ~219):

```python
prompt = SINGLE_CALL_PROMPT.format(objective=objective)
adapter.chat_completion(prompt, temperature=0.6,
                        max_tokens=self.single_call_max_tokens,   # 5000
                        format=None)
```

The model returns **empty content with `finish_reason="length"`**.
`deepseek-v4-flash` is a thinking model: it spends its completion budget
reasoning and never emits a visible token. The employee then has no
output, and the coordinator reports "produced no output at all".

**Reproduced deliberately, outside the app:**

| prompt | max_tokens | result |
|---|---|---|
| trivial ("list 3 papers") | 5000 | OK, 1,091 chars |
| 30k chars context, trivial ask | 5000 | OK, 270 chars |
| 197k chars context, trivial ask | 5000 | OK, 216 chars |
| **`SINGLE_CALL_PROMPT`, "thorough answer", 45k context** | **5000** | **EMPTY (length)** |
| same | 8000 | OK, 5,316 chars |
| same, larger context (live) | 8000 | **EMPTY (length)** |

**Input size is not the cause** — 197k characters answers fine. What
exhausts the budget is being asked for a *thorough, well-organised
deliverable* over a large gathered context. That is a generative task
whose reasoning scales with the material.

### The constraint

```python
# openai_adapter.py
EMPTY_RETRY_MULTIPLIER = 3
EMPTY_RETRY_CEILING = 8000      # <-- the binding constraint

# pipeline_controller.py
single_call_max_tokens = 5000   # <-- the starting budget
```

The writer starts at 5000, escalates 5000 -> 8000, and stops. **8000 is
an arbitrary constant, not a model limit.** DeepSeek supports far more
output than this. Live log from a real run:

```
13:22:50 retrying with max_tokens=2700
13:24:03 retrying with max_tokens=8000
13:26:00 retrying with max_tokens=8000
13:27:16 Single-call stage failed: empty response (finish_reason='length')
```

### Why the app's context is larger than anything tested

The objective handed to that one call stacks:

| source | size |
|---|---|
| graph transcript (2 nodes) | ~8,500 chars each |
| Tavily search results | 5 x ~600 |
| deep-read pages | up to 12,000 each (`MAX_PAGE_CHARS`) |
| `teammates_context` | **the previous specialist's entire output plus its `gathered_context`** |
| role, mandate, task brief, run record | small |

The *second* specialist therefore carries the first one's whole run. No
harness run this cycle reproduced that stacking.

### What to do about it — NOT YET DONE, deliberately

The founder stopped further changes pending this audit. Three options,
in the order I would try them:

1. **Raise `EMPTY_RETRY_CEILING` and `single_call_max_tokens`.** One
   constant each. The evidence says 8000 is simply too low for this
   model on this task. Cheapest test of the diagnosis.
2. **Shrink what the writer must read.** The graph already produces
   structured state; feeding the writer the run record and the verified
   items instead of raw transcripts attacks the cause rather than the
   symptom. This is the "structured output" direction (§6).
3. **Use a non-thinking model for the writing stage only.** The writer
   does not need to reason; it needs to transcribe from evidence.

**Do not** treat this as a retry-policy problem. It was misdiagnosed
that way once already this cycle (§7).

---

## 2. V1 ROADMAP — WHERE WE ACTUALLY ARE

Against the founder's own phase list.

| Layer | Status | Evidence |
|---|---|---|
| **L0** GoalSpec | ✅ complete | live |
| **L1** Task Graph | ✅ **built this session** | `task_node.py`, `task_graph.build_graph`, 19 tests |
| **L2** Feasibility | ✅ complete | `45 of 22 -> DESCOPE 8`, live |
| **L3** Execution | ✅ complete | `AgenticExecutor` |
| **L4** Task State | ✅ complete | per-item states + counters |
| **L5** Strategy Memory | ✅ complete | source/strategy hierarchy, verification exhaustion |
| **L6** Verification | ⚠️ 2 known holes | §5 |
| **L7** Completion | ✅ complete | nav + content + item + field |
| **L8** Persistence | ⛔ not started | deliberate |
| **E1** Capability system | ✅ complete | `capability.py` |
| **E2** Execution runtime | ✅ **built this session** | `graph_runner.py`, 17 tests, live |
| **E3** Dynamic creation | ✅ complete | reuse-or-hire, live |
| **E4** Employee memory | ✅ complete | relevance recall |
| **E5** Tool permissions | ✅ complete | `_scoped()`, enforced |
| **R1** Failure detection | ✅ strong | streaks, exhaustion, blocked markers |
| **R2** Failure classification | ❌ **missing** | every failure is "a failed call" |
| **R3** Recovery / replanning | ❌ **missing** | grep: zero hits |
| **R4** Retry / fallback | ⚠️ partial | uncoordinated: step retries, outage retries, session reopen, adapter retry |
| **R5** Human escalation | ⚠️ approvals only | `approval_queue`, not failure escalation |

**Approved build order, position reached:**

1. ✅ L1 TaskNode + DAG executor — **done**
2. ⏸️ R2 failure classification — **next**
3. ✅ E2 task→employee binding — **done**
4. ⏸️ R3 recovery / replanning
5. ⏸️ L6 structured deliverable — **now also the §1 fix**
6. ⏸️ E2E tests 1–7

**Nothing is committed.** Everything in §3 is uncommitted working tree.

---

## 3. WHAT WAS BUILT THIS SESSION

**New modules:** `task_node.py` (TaskNode, Graph, 9 code-owned statuses) ·
`graph_runner.py` (node execution, one employee per node) ·
`discovery_state.py` · `discovery_guard.py` · `relevance.py` ·
`item_verification.py` · `run_report.py` · `capability.py` ·
`strategy_memory.py`

**L1 — the graph.** Six fields: `id, kind, dependencies, capability,
status, verification`. Reuses `dependency_validator` (cycle, dangling,
depth, Kahn layering) — the caller it had been waiting for since Phase 4.
The distinction the design rests on: **COMPLETED means the work ran,
VERIFIED means the evidence held**, and only VERIFIED releases
downstream.

**E2 — nodes get owners.** A node names a *capability*; staffing turns
that into a reused or newly hired employee with its own model, tool scope
and memory. Demonstrated live:

```
discover      VERIFIED  Job Listings Researcher     3 steps   46s  15 candidate(s)
verify_items  REJECTED  Data Extraction Specialist  3 steps  178s  0 verified of 6
assemble      BLOCKED   (no report written about work that failed)
```

---

## 4. THE APP PATH, END TO END

```
POST /api/sessions/{id}/run
  -> EmployeeCoordinator.run_with_supervisor
     -> SupervisorPlanner.design_delegation      (LLM, one call)
     -> for each specialist: DynamicEmployee.run_task
        -> heuristic pre-flight (URL fetch / Tavily / deep-read)
        -> graph_runner.run   IF the goal is countable AND not delegated
           else AgenticExecutor.run              (single loop)
        -> build_objective(...)                  <-- everything becomes ONE string
        -> Pipeline.run_objective(...)           <-- FAILS HERE (§1)
     -> Supervisor synthesis
  -> _gate_deliverable  (routes.py:2145)
```

**Note the graph rarely runs in the app.** It is skipped for delegated
sub-tasks (correctly — see §7), and the Supervisor delegates almost
everything. A goal reaching an employee *undelegated* is the uncommon
case. **The graph is built and tested but barely load-bearing in
production**, which is a scope fact worth stating plainly.

---

## 5. KNOWN VERIFICATION HOLES (unchanged, still open)

1. **`reported_row_labels` is blind to ranked tables.** First cell of
   `| 1 | Ola Krutrim |` is `1`, rejected as not identifier-shaped,
   returns `[]` — so `unbacked_row_labels` passes vacuously on any table
   with a rank column. One of six verification layers inert.
2. **Nothing checks cell VALUES.** A run invented six company websites
   and four product descriptions; `source_ledger` caught the URLs
   because they were URLs. `Hyperautomation platform` had no URL and no
   gate looks at it.

---

## 6. ARCHITECTURAL DECISIONS

- **LLM decides HOW. Code decides WHETHER, WHAT'S NEXT, WHAT IS ALLOWED,
  and WHETHER IT IS DONE.** No path exists from model output to a node
  status.
- **Nothing the model asserts is trusted.** Every verdict reads the
  ledger.
- **Silence is the default.** An unshaped goal gets no graph, no phase,
  no refusals — it runs exactly as it did before any of this existed.
- **Code decides THAT a source is spent; the model decides which to try
  next.** No site name appears in any runtime string; an AST test
  enforces it.
- **Fixtures come from real traces.** Every bug fixed this cycle was
  found by replaying a real run, none by inspection.
- **A layer nothing calls does not exist.** Wiring is asserted as hard
  as behaviour.

---

## 7. BUGS FOUND AND FIXED THIS SESSION

Found by the founder running the app, not by 976 tests:

1. **App routed DeepSeek to Ollama.** `main._build_adapter` tested only
   for a vendor prefix (`"/" in model`), so the bare name
   `deepseek-v4-flash` fell to the local daemon, got "model not found",
   and dropped the app onto MockAdapter — telling the founder to run
   `ollama serve` with a valid key in the environment. `adapter_pool`
   had routed it correctly all along; two routing rules for one
   question.
2. **Nested graphs.** Every specialist parsed its Supervisor-delegated
   sub-task as a countable goal and spawned its own 3-node graph, all
   staffed with the same two employees. Now skipped when
   `original_task` differs from the task in hand.
3. **False shortfall.** `run_report` demanded per-item page visits for a
   goal whose `per_item_work` was `False` ("give me 10 trading research
   papers"), reported "0 of 10 verified", instructed the writer to say
   so, the writer handed the work back, and the gate refused it with a
   422. A shortfall against a bar nobody set.
4. **A retry that was removed and should not have been.** The adapter
   was changed to skip retrying at the ceiling on the reasoning that an
   identical request cannot give a different answer. Empty-at-length is
   stochastic; `_remember_floor` is process-global, so the writing stage
   began at the ceiling and raised instead of retrying. Restored,
   bounded by count.
5. **A node re-planned itself.** `AgenticExecutor` re-derived a GoalSpec
   from each node's *brief*, priced it against that node's slice of the
   budget, and REFUSED — telling the model "do not begin". Fixed with
   `subtask=True`.
6. **Two candidate harvesters disagreed.** `discovery_state` counts only
   record blocks; `item_state` also harvests links from page text. The
   node judge used the narrow one.

**The pattern worth carrying forward:** items 2–6 were all introduced
*this session*, and all were caught by running the product rather than
by the test suite. The tests verified that each mechanism worked; they
did not verify that it applied to the right goals.

---

## 8. RUNNING IT

```bash
python -m pytest -q                 # expect 976 passed, 2 skipped
python -m uvicorn backend.app.api.main:app --port 8000
# UI at http://127.0.0.1:8000/app/
```

Requires `DEEPSEEK_API_KEY` and `TAVILY_API_KEY` in the environment.
Without Tavily, `web_search` AND `web_read` are both dead and research
tasks fall back to driving search engines in the browser, where Bing
returns generic results and DuckDuckGo serves a CAPTCHA.

Keys are never written to a file. Rotate any key that appears in a
transcript.

---

## 9. DO NOT TOUCH

- The six verification layers and `_gate_deliverable`
- `source_ledger` / `tool_call_ledger`, incl. `is_retrieval`
- `goal_state`'s navigation stop — live-proven, 16 calls -> 3
- The browser layer — the strongest part of the codebase
- LLM ownership of tool choice, query wording, site selection

---

## 10. NEXT SESSION — WORK ON FIRST

1. **Fix §1.** The product returns nothing to the founder. Start with
   option 1 (raise the two constants) because it tests the diagnosis in
   minutes, then do option 2 (shrink the writer's input) because it is
   the actual architecture.
2. **Then R2 — failure classification.** It is the next item in the
   approved order and it is independently buildable.
3. **Do not add another orchestration layer until §1 is closed.** Five
   were added this cycle and the founder still cannot get an answer out
   of the product.

### Principles that must not be violated
1. Never trust a model's claim about its own success.
2. Every guard reads a record, never text a model or a site wrote.
3. A new guard's default must be silence.
4. Fixtures come from real traces.
5. **Do not report a fix as validated unless the APP was exercised,
   end to end, through `Pipeline.run_objective`.** A harness that stops
   at the tool loop proves half the system.
