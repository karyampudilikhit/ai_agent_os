# Vision AI (repo: `ai_agent_os`)

**The vision, as of this pivot:** a person has a raw startup idea. Instead
of learning and juggling a dozen separate tools to validate it, build it,
market it, and run it, they spin up **AI employees** — persistent, named
agents with real roles — and manage their entire operation from one
terminal. The AI employees connect to and operate real tools on the
person's behalf (research, code, marketing channels, ops) instead of the
founder doing it by hand.

This is a sharper, more concrete version of the original idea ("AI
Workforce OS" — a multi-agent orchestration OS), not a different one. The
name changes; the underlying engine being built does not get thrown away
(see "What carries over" below). The GitHub repo is still `ai_agent_os`
for now — rename it separately if you want the URL to match the new name.

**Audience rollout: founders first, everyone later.** The MVP targets
founders specifically — narrow, high-pain, easy to define "done" for
(validate → build → market → run a startup). A later version opens this to
any person, same underlying concept (AI employees managing your work from
one terminal), just not restricted to startup-building tasks. This means
the Employee abstraction (Phase 7) must be designed **role-generic** from
day one — "a persistent agent with a role, scoped memory, and a task
inbox" — not hard-coded around startup-specific concepts like "raw idea"
or "validation verdict." V2 should be able to add new employee types
(personal admin, health, home, whatever) without rebuilding the core
abstraction that shipped for founders.

This file is the state-of-the-project snapshot. It gets rewritten every
time work is pushed, specifically so a new chat (no memory of this
session) can read it and pick up without re-deriving everything from the
diff.

## What carries over from "AI Workforce OS", and what's genuinely new

**Directly reusable — real, working infrastructure:**
- The core engine (contract → decompose → spawn agents → execute →
  synthesize → critique/refine) is exactly what an AI Employee needs
  internally every time it's given a task. An employee doesn't need a
  different execution engine — it needs *this* engine, called repeatedly,
  with memory attached.
- The critique/refine quality-control loop matters *more* under this
  vision, not less — an employee doing real ongoing work for someone's
  actual startup needs to be trustworthy, not just good enough for a demo.
- Ollama/multi-provider routing, config-driven limits, the safety-guardrail
  groundwork — all still exactly the right foundation.

**Genuinely new — not in the original plan, needs real design:**
- The **Employee abstraction** itself. Today the system spawns agents *for
  one objective* and they're done. Nothing models "a persistent role with
  its own memory and inbox that keeps existing across weeks." This sits on
  top of the engine, not inside the old Phase 7.
- The **MCP connector layer** — the original Phase 8 plan was a hand-rolled
  `tool_registry.py`/`api_tool_handler.py`. Building on the Model Context
  Protocol instead gets access to an existing ecosystem of tool
  integrations (Slack, GitHub, browsers, databases, etc.) instead of
  writing every integration from scratch.
- The **manager terminal** — the original Phase 9 was a generic
  task-submission dashboard. Now it's a roster view: what is each employee
  doing, approve/reject its actions, cost and output per employee.

## Where things actually stand

| Phase | Status | Notes |
|---|---|---|
| 1 — Scaffolding | ✅ committed | repo layout, `config.yaml` |
| 2 — Execution contracts | ✅ committed | `execution_contract.py`, `agent_schema.py` |
| 3 — Agent creation + Ollama | ✅ committed | `agent_factory.py`, `model_router.py`, `ollama_adapter.py` |
| 4 — Orchestration engine | ✅ committed, then extended uncommitted | the reusable engine every future AI employee calls |
| 5 — Safety & cost guardrails | ❌ not built | `constraints_enforcer.py`, `input_output_validator.py` are empty stubs — more urgent now that employees will eventually hold real tool permissions |
| 6 — Adaptive supervision & critique loop | 🟡 in progress, being validated | see "Benchmark" below — the fabrication-scrubbing bug is fixed and independently confirmed; the pairwise judge itself is not reliable yet |
| 7 — Employee abstraction (was "Memory & learning") | ❌ not started | redesigned: per-employee/per-startup persistent memory, not a global mistake repository |
| 8 — MCP connector layer (was "Tools, multi-provider, workflows") | ❌ not started | redesigned: adopt MCP instead of hand-rolled tool integrations |
| 9 — Manager terminal (was "API, frontend & ship") | ❌ not started | redesigned: roster/dashboard for directing employees, not a task-submission form |
| 10 — First flagship employee: Idea Validation | ❌ not started | raw idea in, market/competitor research + a clear validation verdict out. The narrowest end-to-end useful slice — build and prove this before Builder/Marketing/Ops employees |

### Known gap: Phase 6 work landed before Phase 5

The original plan has Phase 6 depending on Phase 5 — refinement/critique
loops need cost and spawn-rate limits enforced first, or they can spiral.
That didn't happen: `critique_agent.py` and the refinement loop got built
and benchmarked directly on top of Phase 4, with only a stopgap in place
(`max_agents_per_cycle` raised in `config.yaml`, throttle-instead-of-abort
semantics still marked "Phase 5" in a comment there). This hasn't bitten
us because Ollama is free — a spiral costs time/CPU, not money — but it's
still open, and it matters a lot more once employees hold real tool
permissions (Phase 8) and paid providers (OpenAI/Anthropic) enter the mix.

## What's beyond Phase 4 in the current code

On top of the committed Phase 4 orchestration engine, the following got
built and benchmarked (all real code):

- **`backend/app/contracts/clarification.py`** — `ClarificationEngine`.
  Before decomposing an objective, asks up to 5 clarifying questions if the
  objective is short/ambiguous, and enriches it with the answers.
- **`backend/app/orchestrator/synthesis.py`** — `SynthesisEngine`. Merges
  every sub-agent's output into one coherent deliverable.
- **`backend/app/critique/critique_agent.py`** — `CritiqueEngine`. Scores
  the synthesized output for completeness, flags over-engineering and
  fabricated claims, and can trigger refinement.
- **Context-sharing fix** — sub-agents now see earlier agents' results
  instead of running blind to each other.
- **Sharpened critique** — tightened scope-mismatch and fabricated-claim
  detection, which had been too lenient to be reliable.
- **Verified-refine fix (`pipeline_controller.py`)** — the critique/refine
  loop used to report the score of the *pre-refine* draft even after
  refinement changed the shipped output, so a fabrication-containing
  final answer could still carry a clean 0.9+ score. Now it re-critiques
  after every refine pass (up to `max_refinement_depth`) and reports
  against the text that actually ships.

## Benchmark: single LLM call vs. the full multi-agent product

Ongoing effort to answer "does the multi-agent pipeline actually produce a
better result than just asking the model directly?" — same 5 large
business-automation objectives each round, same model both arms
(`gpt-oss:120b-cloud` unless noted).

**Rounds 1–4** (phi3 invalid → first gpt-oss test → context-sharing fix →
sharpened critique): multi-agent token cost stabilized around 4-9x the
single-call baseline while self-assessed critique score went from
partially-failing to a reliable ~0.93 average — but this was the system
grading itself, unverified against anything external.

**Round 5** — fresh full regeneration of both arms. 5 calls / 23,250
tokens (single) vs. 70 calls / 219,218 tokens (multi-agent, ~9.4x).
Critique scores 0.90/0.95/0.93/0.85/0.95 (avg ≈0.92). Two of five tasks
didn't finish every spawned agent (11/12, 6/7) despite scoring well.

**Independent judge, attempt 1 (`judge_benchmark.py`)** — the first real
attempt to get a verdict from something other than the pipeline grading
itself. `deepseek-v3.1:671b-cloud` turned out to require a paid Ollama
subscription (not usable); switched to `qwen3-coder:480b-cloud` (different
model family from the generator, so not self-grading). Each task judged
twice with A/B swapped, to control for position bias. **Result: single_call
1, multiagent 0, tie 4** (4 of 5 position-inconsistent, i.e. the raw
verdict flipped depending on which side was shown first, so discarded).
The one clean, order-independent verdict went to single-call, specifically
because the multi-agent answer fabricated evidence ("tested at 3
locations") that the critique step was supposed to catch and didn't.

**Root cause found and fixed** — see "verified-refine fix" above. Traced
directly to `pipeline_controller.py` reporting a stale pre-refine critique
score instead of re-checking the shipped output.

**Round 6 (`rerun_verified_refine.py`, fix applied)** — multi-agent arm
regenerated, single-call baseline reused from Round 5. All 5 tasks now
self-report `fabricated_claims: []` post-refine (down from confirmed
leakage before the fix). Cost: 78 calls / 239,828 tokens (~10.3x single-
call, ~9% more than Round 5 due to the extra verification pass).

**Independent judge, attempt 2** — re-judged Round 6 with the same
position-swap methodology. **The judge saturated**: picked "Response A" in
10 of 10 passes regardless of content, so every winner verdict was
discarded as position-inconsistent (0/0/5). Pairwise judging with this
judge model is not reliable — next step is switching to independent
absolute scoring (rate each response alone, 0-10, never side-by-side) to
remove position bias structurally instead of just detecting it.

However, the **fabrication flags are still valid signal** — they're
independent per-response checks, not a forced pick. Cross-referenced
against which system produced which response: **multi-agent fabricated on
1 of 10 independent checks (and even that one was inconsistent between its
own two passes); single-call fabricated on 3 of 10, across 3 of the 5
tasks.** So the specific fix is confirmed working and multi-agent's
fabrication rate is now measurably lower than single-call's, which has no
check at all. The bigger "which is smarter overall" question is still open
pending a non-pairwise judge.

## Repo map (what's real vs. stub)

```
backend/app/
  contracts/        execution_contract.py ✅  agent_schema.py ✅  clarification.py ✅
  agents/            agent_factory.py ✅  agent_executor.py ✅  agent_validator.py ✅
  models/            model_router.py ✅  provider_adapters/ollama_adapter.py ✅
                      provider_adapters/openai_adapter.py ❌  anthropic_adapter.py ❌
  orchestrator/      execution_engine.py ✅  state_manager.py ✅  dependency_validator.py ✅
                      pipeline_controller.py ✅  synthesis.py ✅  adaptive_supervisor.py ❌
  critique/          critique_agent.py ✅  confidence_evaluator.py ❌
  safety/            recursion_guard.py ✅ (174 lines, ad hoc)  constraints_enforcer.py ❌
                      input_output_validator.py ❌ (Phase 5)
  memory/            all stubs ❌ (becomes the Employee abstraction's per-employee
                      memory, Phase 7 — not a global mistake repository anymore)
  tools/ workflows/  all stubs ❌ (becomes the MCP connector layer, Phase 8)
  api/               all stubs ❌ (becomes the manager terminal, Phase 9)
frontend/            all 27 files stubs ❌ (manager terminal, Phase 9)
```

Benchmark/experiment scripts at repo root (`benchmark_*.py`,
`build_blind_report_*.py`, `rerun_*.py`, `judge_benchmark.py`,
`clarification_simulation.py`, `test_verify_refine_fix.py`) are
throwaway-but-kept — each documents a specific hypothesis test. Their
outputs (`benchmark_result_*.json`, `blind_report_*.html`,
`judge_result_*.json`) are the actual data. Raw judge-splitting temp files
and crash-dump scratch (`judge_tmp*/`, `critique_raw_debug*.txt`, etc.) are
gitignored — regenerable, no lasting value.

## Phase plan

**Foundation (built):**
1. Scaffolding
2. Execution contracts
3. Agent creation + Ollama integration
4. Core orchestration engine — the reusable engine every AI employee calls
   internally when given a task

**Trust core (must be solid before employees get real tool access):**
5. Safety & cost guardrails — `max_total_cost` / `max_spawn_rate_per_minute`
   actually block execution; malformed I/O rejected before burning a call
6. Adaptive supervision & critique loop — in progress; next step is an
   independent absolute-scoring judge (not pairwise) to finally answer
   whether multi-agent reasons better than one call

**Vision AI layer (the pivot — new work, not in the original plan):**
7. Employee abstraction — a persistent role (e.g. "Growth Marketing
   Employee") with scoped memory of *this specific startup* (the idea,
   decisions already made, brand voice) and a task inbox instead of a
   one-shot contract
8. MCP connector layer — employees reach real tools (Slack, GitHub,
   Stripe, browsers, databases) through the Model Context Protocol instead
   of hand-rolled integrations
9. Manager terminal — a dashboard for directing a roster of employees:
   what each is doing, approve/reject its actions (ties into Phase 5's
   permission tiers), cost and output per employee

**First flagship employee (prove one narrow slice before building the
whole roster):**
10. Idea-Validation Employee — raw idea in, market/competitor research +
    a clear-eyed validation verdict out. Standalone-useful on its own,
    and the template for how every later employee gets built.

## Picking this back up

1. Read "Where things actually stand" above — it's the ground truth, more
   current than any phase-count in commit messages.
2. The core-engine question is still open: Round 6 fixed a real fabrication
   bug (verified independently), but the pairwise judge broke on position
   bias, so "is multi-agent actually smarter than one call" is unresolved.
   Next step: an absolute-scoring judge, not pairwise.
3. Decide Phase 5 (safety guardrails) timing — still just a stopgap. Gets
   materially more important once Phase 8 (MCP connectors) gives employees
   real tool access, and once paid providers enter the mix.
4. The Employee abstraction (Phase 7 redesign) is the next real design
   work — nothing today models a persistent role with memory and an
   inbox, only one-shot task decomposition.
5. `git status` — there is usually uncommitted work in progress; check
   before assuming the phase table above is fully reflected in the working
   tree.
