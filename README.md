# AI Workforce OS (`ai_agent_os`)

A multi-agent orchestration OS: a task ("execution contract") gets decomposed,
spawned out to sub-agents with configurable traits, executed against an LLM,
critiqued, and synthesized into one answer — instead of a single raw model
call. MVP LLM is Ollama (local server, free tier + Ollama's cloud-hosted
models like `gpt-oss:120b-cloud` through the same local API, no extra key).
`config.yaml` is already wired for OpenAI/Anthropic/Gemini for later.

This file is the state-of-the-project snapshot. It gets rewritten every time
work is pushed, specifically so a new chat (no memory of this session) can
read it and pick up without re-deriving everything from the diff.

## Where things actually stand

The project is a 9-phase build plan (see "Phase plan" below). **Phases 1-4
are committed.** Beyond that, phases have blurred: Phase 6 (critique loop)
material got written early, during benchmarking, ahead of Phase 5 (safety
guardrails) — see "Known gap" below.

| Phase | Status | Notes |
|---|---|---|
| 1 — Scaffolding | ✅ committed | repo layout, `config.yaml` |
| 2 — Execution contracts | ✅ committed | `execution_contract.py`, `agent_schema.py` |
| 3 — Agent creation + Ollama | ✅ committed | `agent_factory.py`, `model_router.py`, `ollama_adapter.py` |
| 4 — Orchestration engine | ✅ committed, then extended uncommitted | see below |
| 5 — Safety & cost guardrails | ❌ not built | `constraints_enforcer.py`, `input_output_validator.py` are empty stubs |
| 6 — Adaptive supervision & critique loop | 🟡 partially built | `critique_agent.py` has real code (188 lines); `adaptive_supervisor.py`, `confidence_evaluator.py` still empty |
| 7 — Memory & learning | ❌ not started | |
| 8 — Tools, multi-provider, workflows | ❌ not started | |
| 9 — API, frontend & ship | ❌ not started | |

### Known gap: Phase 6 work landed before Phase 5

The original plan has Phase 6 depending on Phase 5 — refinement/critique
loops need cost and spawn-rate limits enforced first, or they can spiral.
That didn't happen: `critique_agent.py` and the refinement loop got built
and benchmarked directly on top of Phase 4, with only a stopgap in place
(`max_agents_per_cycle` raised in `config.yaml`, throttle-instead-of-abort
semantics still marked "Phase 5" in a comment there). This hasn't bitten
us because Ollama is free — a spiral costs time/CPU, not money — but it's
still open. Decide before Phase 8 brings in paid providers (OpenAI/Anthropic),
where an uncapped refinement loop *does* cost real money.

## What's beyond Phase 4 in the current code

On top of the committed Phase 4 orchestration engine, the following got
built and benchmarked (all real code, some pushed as Phase 4/6 extensions,
some as standalone experiment scripts):

- **`backend/app/contracts/clarification.py`** — `ClarificationEngine`.
  Before decomposing an objective, asks up to 5 clarifying questions if the
  objective is short/ambiguous, and enriches it with the answers. Built to
  fix a specific failure mode: vague objectives (e.g. "build an app for
  students") produced generic, unfocused multi-agent output.
- **`backend/app/orchestrator/synthesis.py`** — `SynthesisEngine`. Merges
  every sub-agent's output into one coherent deliverable instead of
  returning N disconnected agent outputs concatenated together.
- **`backend/app/critique/critique_agent.py`** — `CritiqueEngine`. Scores
  the synthesized output for completeness (`completeness_score` 0-1),
  flags over-engineering and fabricated claims, and can trigger one
  refinement pass. This is the Phase 6 material referenced above.
- **Context-sharing fix** — sub-agents were originally running blind to
  each other's output; a fix was added so later agents see earlier agents'
  results (see `benchmark_result_large_ctxfix.json` — the "ctxfix" round).
- **Sharpened critique** — the critique agent's own scoring was too
  lenient/inconsistent (scope-mismatch and fabricated-claim detection were
  weak); tightened in the "sharpcrit" round.

## Benchmark: single LLM call vs. the full multi-agent product

Ongoing effort to answer "does the multi-agent pipeline actually produce a
better result than just asking the model directly?" — same 5 large business-
automation objectives each round (retail ops, hiring pipeline, property
management, restaurant back-office, subscription box), same model both arms.

**Round 1 (phi3, local, small model)** — invalid. Both arms produced
incoherent output at this task size; the run measured phi3's coherence
ceiling, not the architecture. Superseded.

**Round 2 (gpt-oss:120b-cloud, first real test)** —
`benchmark_result_large_gptoss.json`. 5 calls / 22,325 tokens (single) vs.
51 calls / 96,470 tokens (multi-agent), ~4.3x tokens. Critique scores
0.90, 0.85, 0.85, then two `None` (pipeline fallback/error on those two).

**Round 3 (+ context-sharing fix)** —
`benchmark_result_large_ctxfix.json`. 67 calls / 192,278 tokens multi-agent
(~8.6x). Critique scores 0.85, `None`, 0.85, `None`, `None` — fix addressed
context blindness but critique scoring itself was still unreliable (3/5
runs didn't produce a score).

**Round 4 (+ sharpened critique)** —
`benchmark_result_large_sharpcrit.json`, most recent completed round.
58 calls / 190,137 tokens multi-agent (~8.5x tokens, ~11.6x calls vs.
single-call's 5/22,325). **All 5 tasks now produce a critique score:
0.85, 0.93, 0.95, 0.95, 0.95 (avg ≈ 0.93)** — the scoring itself became
reliable, and the self-assessed completeness trended up.

**Round 5 (in progress / just re-run)** — a fresh full regeneration of
both arms (not reusing old single-call responses) on `gpt-oss:120b-cloud`,
via `benchmark_large_scale_gptoss.py` → `rerun_fresh_gptoss.log` +
`benchmark_result_large_gptoss.json`. Check that log for the outcome if
it wasn't done by the time this was pushed.

**Open question, unresolved across all 5 rounds:** every round produces a
`blind_report*.html` — a page meant for a *human* to read both answers
side-by-side and vote which is more useful. **No round has actually been
voted on and tallied.** The critique scores above are the multi-agent
system grading *itself*; there is still no independent judgment of
whether the multi-agent output is actually better than one raw call, only
that it costs ~4-12x more tokens to produce. That vote is the next real
signal to get before trusting this architecture.

## Repo map (what's real vs. stub)

```
backend/app/
  contracts/        execution_contract.py ✅  agent_schema.py ✅  clarification.py ✅
  agents/            agent_factory.py ✅  agent_executor.py ✅  agent_validator.py ✅
  models/            model_router.py ✅  provider_adapters/ollama_adapter.py ✅
                      provider_adapters/openai_adapter.py ❌  anthropic_adapter.py ❌ (Phase 8)
  orchestrator/      execution_engine.py ✅  state_manager.py ✅  dependency_validator.py ✅
                      pipeline_controller.py ✅  synthesis.py ✅  adaptive_supervisor.py ❌ (Phase 6)
  critique/          critique_agent.py ✅  confidence_evaluator.py ❌ (Phase 6)
  safety/            recursion_guard.py ✅ (174 lines, ad hoc)  constraints_enforcer.py ❌
                      input_output_validator.py ❌ (Phase 5)
  memory/            all stubs ❌ (Phase 7 — config.yaml defaults to Pinecone,
                      paid; swap for local Chroma/FAISS to stay free-tier consistent)
  tools/ workflows/  all stubs ❌ (Phase 8)
  api/               all stubs ❌ (Phase 9)
frontend/            all 27 files stubs ❌ (Phase 9)
```

Benchmark/experiment scripts at repo root (`benchmark_*.py`,
`build_blind_report_*.py`, `rerun_*.py`, `clarification_simulation.py`) are
throwaway-but-kept — each one documents a specific hypothesis test. Their
outputs (`benchmark_result_*.json`, `blind_report_*.html`) are the actual
data. Raw judge-splitting temp files and crash-dump scratch (`judge_tmp*/`,
`critique_raw_debug*.txt`, etc.) are gitignored — regenerable, no lasting
value.

## Phase plan (unchanged from original)

1. Scaffolding
2. Execution contracts
3. Agent creation + Ollama integration
4. Core orchestration engine — done when a contract runs end-to-end and
   prints a real result, no HTTP layer needed
5. Safety & cost guardrails — `max_total_cost` / `max_spawn_rate_per_minute`
   actually block execution; malformed I/O rejected before burning a call
6. Adaptive supervision & critique loop — low-confidence results
   auto-trigger critique + refinement, capped at `max_refinement_depth`
7. Memory & learning — a failed-before task surfaces the past mistake
8. Tools, multi-provider, workflows — agents can call real external APIs;
   complex tasks can route to GPT-4o/Claude instead of only Ollama
9. API, frontend & ship — dashboard, submit a task, watch it run live,
   `docker compose up`

Fastest path to a demo if needed before Phase 9 is fully done: a thin
FastAPI route + one React page can sit on top of Phase 4 alone and expand
as later phases land, rather than building the full frontend at the end.

## Picking this back up

1. Read the "Where things actually stand" table above — it's the ground
   truth, more current than any phase-count in commit messages.
2. Decide the open question from the last session: build Phase 5 (safety
   guardrails) before going further into Phase 6, or keep accepting the
   spiral risk while Ollama is free. This gets more important once Phase 8
   adds paid providers.
3. Get an actual human (or LLM-judge) verdict on one of the
   `blind_report_*.html` pages — right now "is multi-agent worth 4-12x the
   tokens" is still an open question, not a validated result.
4. `git status` — there is usually uncommitted work in progress; check
   before assuming the phase table above is fully reflected in the working
   tree.
