# Vision AI — Session Handoff

> **Purpose:** This file lets a new chat session pick up exactly where the last
> one ended. The README is the evergreen snapshot of the whole project; this is
> the fresh-in-my-head "here's what we were mid-doing" companion.

---

## What Vision AI is

Vision AI (repo still named `ai_agent_os`) is **a team of AI employees that
actually does your work** — research, drafts, real actions (send email, create a
repo, fill a form) — verified against sources, with founder approval on every
irreversible action.

**Positioning:** *"AI employees that actually do your work."* Founders are the
starting wedge, not the definition.

**Naming — DECIDED 2026-08-09: the product is Vision AI.** The waitlist site
(https://vistron-ai.vercel.app/) still says "Vistron AI" and is the one asset
left to rebrand. Everything else already says Vision AI.

**Locked vocabulary:** Employee → Team → Unit → Company.

**The hard rule:** if the AI hands work back to the user, it has failed.

---

## Quick-start for a new session

```bash
cd "C:\Users\KARYAM~1\AppData\Local\Temp\claude\D--quant\34a3eb1d-a204-44dc-ac02-0da452892d77\scratchpad\repos\ai_agent_os"
py -3 -m pytest test_phase6_fixes.py test_execution_loop.py test_phase5_coverage.py test_memory_recall.py -q   # 77 pass
py -3 run_test_model.py --model gpt-oss:120b-cloud --label baseline               # the diagnostic
```

⚠️ Don't trust `/api/tags` for model liveness — retired models are listed and
return HTTP 410 on first use. Check with a real generate call.

⚠️ Run the server yourself in a terminal if you need it. Background-started
servers get reaped between turns and a stale one on :8000 will silently serve
the WRONG code. `run_test_model.py` runs in-process and avoids this entirely.

---

## State of the project

| | |
|---|---|
| **Branch** | `feat/phase-4-orchestration` |
| **Tests** | 77 pass |
| **Deploy** | ❌ still local only. `fly.toml` names `neutron-ai`, never created |
| **Design partners / paying** | ❌ Zero / Zero |
| **Demo video** | ❌ Not recorded |
| **Browser automation** | ✅ works, incl. inside the agentic loop |
| **Compute + market data** | ✅ works end to end — real Sharpe reaches the deliverable, in 2 of 4 valid runs |
| **Models** | `gpt-oss:120b-cloud` ✅, `nemotron-3-super:cloud` ✅ (free tier). `qwen3-coder:480b` / `deepseek-v3.1:671b` retired (410). Current top-tier cloud models are 403/paid. |
| **Nearest competitor** | Vellum (vellum.ai) — direct, shipping, $30-200/mo |

---

## What got done this session

**The previous session's blocker is gone, and its diagnosis was wrong.**

1. **Model blocker cleared, no billing needed.** Probed the whole current
   Ollama cloud catalog against this account: `kimi-k2.7-code`,
   `deepseek-v4-pro`, `glm-5.2`, `minimax-m2.7` are all **403 / paid**.
   **`nemotron-3-super:cloud` is free and works** — a second, different-family
   model, pulled and ready. An API key was the wrong path anyway: the
   `AGENT_LOOP_MODEL` builder hardcodes `OllamaAdapter`, and the
   anthropic/openai/gemini adapter files are **0 bytes**.

2. **Built `run_test_model.py`** — HANDOFF documented `run_test_model.sh` as the
   one command that would unblock everything; it did not exist.

3. **Fixed a real product bug** (`ollama_adapter.py`): Ollama spends
   `num_predict` on `thinking` before `response`, so a reasoning model that
   outgrows the budget returns **HTTP 200 with `response: ""`** — and the
   adapter passed that empty string downstream as content. Now raises, naming
   the budget. This is the same class the HTTP branch already guarded.

4. **Ran the diagnostic properly and overturned the conclusion** — see below.

---

## THE FINDING — it was never tool selection

The old framing ("the planner refuses to call `run_python`; scaffolding or
model?") is **falsified**. 11 runs, 7 invalidated by Ollama 500s, 4 valid:

| Loop model | Step budget | `run_python` | Outcome |
|---|---|---|---|
| `nemotron-3-super:cloud` | 3000 | **1** | real **Sharpe 0.9329**, CAGR 11.26%, MaxDD −18.76% ✅ |
| `gpt-oss:120b-cloud` | 3000 | **5** | real **Sharpe ≈0.70** ✅ |
| `gpt-oss:120b-cloud` | 700 | 0 | DONE at step 2 ❌ |
| `gpt-oss:120b-cloud` | 3000 | 0 | DONE at step 2 ❌ |

- **The success criterion was met, twice.** `run_python` is called and a real
  computed Sharpe reaches the deliverable, cited to the execution step. The
  prior "0 of 4" belonged to the old harness, not to the planner.
- **Not the model** — `gpt-oss`, the model previously blamed, made the most
  tool calls of any run (5).
- **Not the token budget** — 3000 appears in both a success and a failure.
- **It's premature DONE.** Both failures have the identical trace: *step 1
  fetch data → step 2 declare DONE → loop ends*, having computed nothing. The
  DONE-challenge fires, challenges **once**, then concedes.

Three separate times today a measurement nearly manufactured a confident wrong
answer, always the same way — an infrastructure or harness failure presenting
as a behavioural result:

- The first runner called `Pipeline.run_objective`, where `AgenticExecutor` is
  never constructed — it would have reported `run_python: 0` for a run in which
  no tool *could* be called.
- Four replication runs died on Ollama 500s and reported `run_python: 0`.
- The Sharpe detector matched `{sharpe:.4f}` inside a `print()` and scored an
  unformatted placeholder as a delivered number — this project's own failure
  mode, committed by its measuring instrument.

All three are now guarded: the runner drives a `DynamicEmployee`, marks
infra-killed runs INVALID (exit 3), and ignores fenced code.

---

## Next session — start here

### The one change that matters

**Build the required-outputs gate on the agentic loop.** The objective asks for
Sharpe/CAGR/drawdown; `ToolCallLedger` already records whether a compute tool
ran; so DONE must be **refused** while that recorded fact is missing — not
challenged once and conceded. This is the codebase's own lesson: guards that
query a record hold, and a single advisory challenge is a prompt rule wearing a
guard's clothes.

Success criterion: `run_python` called in **4 of 4** valid runs, not 2.

### Also queued

- Put the tool name INTO the sub-task text (`supervisor.design_delegation`) —
  still worth doing, now as reinforcement rather than as the primary fix.
- Rebrand the Vercel waitlist site Vistron → **Vision AI**.
- Register the GitHub OAuth App (~5 min; endpoints built, app never registered,
  so the connect flow is dead code).
- Deploy: `fly auth login`, unique app name, `fly volumes create data`, secrets,
  `fly deploy`. Raise `memory` 512mb → **1gb** in the same commit (headless
  Chromium OOMs below that; presents as the machine restarting mid-run).

### Known risk, unchanged

The product depends on Ollama cloud, which **500s frequently under repeated
runs** and retires models without warning. Two free models work today. There is
no fallback provider — the three non-Ollama adapters are empty files.

---

## Competitor: Vellum (vellum.ai) — direct, and they ship

Checked 9 Aug 2026. Appears to have pivoted from an LLMOps platform to a
personal AI assistant (treat the pivot as inference, not confirmed fact).

- Pricing: $30 / $100 / $200 per month, metered by vCPU + GiB + credits
- Has: persistent memory, self-improving skills, real actions incl. **code
  execution**, background runs, its own email/GitHub/Slack identity, permission
  tiers

**Four confirmed gaps → our advantage features**

1. **No output verification.** Their trust story is entirely SECURITY (Keychain,
   no training on your data) — nothing about citations, fabrication or accuracy.
   Ours is built and proven. Surface it as a trust receipt per deliverable.
   Hardest for them to copy: it needs the recording substrate, not a flag.
2. **Single-user only.** No shared workspaces, no audit logs; credentials live in
   one person's macOS Keychain. Our Employee→Team→Unit→Company model is a
   different product shape.
3. **macOS + iPhone only** — Android/Windows "on the roadmap". We're a web app.
   India is overwhelmingly Windows/Android. Expires when they ship Windows.
4. **You pay while idle** — their own caveat about background memory cost. Flat
   pricing attacks that directly.

**The uncomfortable part:** they ship and we don't. None of these count until
Vision AI is deployed and demoable.

---

## Open decisions

1. ~~Unblock the model diagnostic~~ — **RESOLVED**, `nemotron-3-super:cloud`.
2. ~~Tool selection: scaffolding or model?~~ — **RESOLVED**, neither; it's
   premature DONE. Fix is the required-outputs gate.
3. ~~Vision AI vs Vistron AI~~ — **RESOLVED 2026-08-09: Vision AI.** Site
   rebrand outstanding.
4. **fly.toml memory 512mb → 1gb** — recommended, do it at deploy time. Founder: **deferred, not urgent**
5. ~~Push `feat/phase-4-orchestration`?~~ — **RESOLVED**, pushed 2026-08-09.

---

## Past days log

### This session (9 Aug 2026, later)
- Cleared the model blocker for free; built the missing diagnostic runner
- Fixed the empty-200 adapter bug; overturned the tool-selection diagnosis
- Rewrote README + HANDOFF, pushed the branch

### Previous session (9 Aug 2026, earlier)
- Ran 2 live end-to-end tests, fixed everything they found
- 6 commits, 77 tests, +2 real capabilities (compute + market data)
- Ended with tool selection as the single unsolved blocker (wrongly diagnosed)

### Earlier
- Phases 4 & 5 of the audit: memory system, test coverage, Dockerfile Chromium
- Phases 1–3 of the audit; browser automation; connectors-as-config
