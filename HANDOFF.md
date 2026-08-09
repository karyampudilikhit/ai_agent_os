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
starting wedge, not the definition. Live waitlist site is branded **Vistron AI**
(https://vistron-ai.vercel.app/) — the repo assets still say Vision AI. That
inconsistency is unresolved.

**Locked vocabulary:** Employee → Team → Unit → Company.

**The hard rule:** if the AI hands work back to the user, it has failed.

---

## Quick-start for a new session

```bash
cd "C:\Users\KARYAM~1\AppData\Local\Temp\claude\D--quant\34a3eb1d-a204-44dc-ac02-0da452892d77\scratchpad\repos\ai_agent_os"
curl http://localhost:11434/api/tags          # Ollama up?
py -3 -m uvicorn backend.app.api.main:app --port 8000
py -3 -m pytest test_phase6_fixes.py test_execution_loop.py test_phase5_coverage.py test_memory_recall.py -q   # 77 pass
```

- Marketing page: http://127.0.0.1:8000/
- Playground: http://127.0.0.1:8000/**app/**

⚠️ Run the server yourself in a terminal. Background-started servers get reaped
between turns and a stale one on :8000 will silently serve the WRONG code — that
nearly produced a fake benchmark result. The test harness now guards for it.

---

## State of the project

| | |
|---|---|
| **Branch** | `feat/phase-4-orchestration` — all work committed, **not pushed** since `12e5e9e` |
| **Last commit** | `790c797` Block a compute request that never computed anything |
| **Tests** | 77 pass |
| **Deploy** | ❌ still local only. `fly.toml` names `neutron-ai`, never created |
| **Design partners / paying** | ❌ Zero / Zero |
| **Demo video** | ❌ Not recorded |
| **Browser automation** | ✅ works, incl. inside the agentic loop |
| **Compute + market data** | ✅ tools exist and work; ⚠️ the planner won't call them |

---

## What got done this session

Two live end-to-end tests were run against the real app, then everything they
found was fixed. Full reports: `test1_quant.pdf`, `test2_arc_agi3.pdf`,
`test1_quant_rerun.pdf` (in the session scratchpad).

**The tests**
- Test 1 — build a quant model end to end: shipped **zero backtest numbers** and
  a to-do list for the founder.
- Test 2 — ARC-AGI-3 (game VC33): ARC's own scorecard **0.0**, and it blamed a
  `game_id` it had never sent.

**Three failures repeated across both**, which made them structural: a hand-back
shipped past a detector that scored 0/2; downstream specialists briefed on
artefacts nobody produced; irrelevant tools substituted for missing ones.

**The pattern that drove every fix:** guards that **query recorded facts** held
(SourceLedger caught 2 fabricated citations by asking the network). Guards that
**pattern-match text** failed — repeatedly, always on a word missing from a list.

**Shipped (6 commits, `6b01f7e` → `790c797`)**
- ✅ Hand-back detector: widened verbs + new deferred-work check (the "What to do
  next" shape), gated on co-occurrence with a not-done admission
- ✅ `ToolCallLedger` + `claim_checker` — catches a deliverable blaming a value
  the run never sent
- ✅ Refinement can no longer ship a worse draft (was: 0.30 → 0.40 by adding 3
  fabrications → shipped at 0.20)
- ✅ Argument type validation — the ARC 8-guess sweep became 1 guess + correction
- ✅ Per-tool consecutive-failure guard (old guard only caught identical calls)
- ✅ `_gate_deliverable` — detection can now **fail a run** instead of only
  forcing a rewrite; draft preserved on the failed record
- ✅ DONE-challenge when a specialist quits with ≤1 successful call
- ✅ `run_python` (sandboxed) + `fetch_market_data` — verified composing on real
  data: 1,255 SPY bars → CAGR 9.61%, Sharpe 0.92, MaxDD −13.34%
- ✅ Tool list ranked by relevance to the task (reuses the BM25 from memory)
- ✅ Compute-request gate: asks for Sharpe/CAGR, ran nothing → run fails

---

## What did NOT get done

- ❌ **The tool-selection problem is unsolved.** Four levers tried (build the
  tool, longer description, rank it first, prompt rules) — `run_python` was
  **never called** in any of 4 quant re-runs. What changed is that the failure is
  now visible: the run fails honestly instead of shipping an N/A table as `done`.
- ❌ Test 1 and Test 2 still do not pass their criteria.
- ❌ Not pushed since `12e5e9e`.
- ❌ Deploy, demo video, GitHub OAuth app registration — all untouched.
- ❌ `test1_quant.pdf` / `test2_arc_agi3.pdf` not regenerated with the correction
  that "null evidence rows" was a harness bug, not a product defect.

---

## Tomorrow's plan

### What only the founder can do

1. **Decide the tool-selection strategy** (see Open decisions #1) — this is the
   binding constraint on everything else.
2. **Register the GitHub OAuth App** (~5 min) — endpoints built, app never
   registered, so the whole connect flow is dead code.
3. **`fly auth login`** if we're deploying.
4. **Settle Vision AI vs Vistron AI naming.**

### What Claude does in parallel

- Put the tool name INTO the sub-task text (`supervisor.design_delegation`) —
  the specialist reliably reads its objective, and has ignored the tool list 4×.
- Regenerate the two stale PDFs with the correction.
- Push the branch (README first, per standing rule).

### Success criteria

A quant re-run where `run_python` is actually called and a real Sharpe ratio
reaches the deliverable. Everything else is secondary.

---

## Open decisions

1. **Tool selection: scaffolding or model?** — 4 scaffolding levers failed. Next
   options: (a) name the tool in the sub-task objective, (b) try a stronger model
   for the execution loop and measure whether selection improves. (b) tells us
   whether more scaffolding is even worth building. Founder: **TBD**
2. **Vision AI vs Vistron AI** — site says one, all repo/deck/model assets say
   the other. Founder: **TBD**
3. **fly.toml memory 512mb** — likely too small for headless Chromium; raising it
   is a billing call. Founder: **TBD**
4. **Push `feat/phase-4-orchestration`?** — 6 unpushed commits. Founder: **TBD**

---

## Past days log

### This session (9 Aug 2026)
- Ran 2 live end-to-end tests, wrote 3 PDF reports, fixed everything they found
- 6 commits, 77 tests, +2 real capabilities (compute + market data)
- Ended with tool selection as the single unsolved blocker

### Previous session (8 Aug 2026)
- Phases 4 & 5 of the audit: memory system (relevance recall), test coverage for
  4 untested subsystems, Dockerfile Chromium fix
- Pushed `12e5e9e`

### Earlier
- Phases 1–3 of the audit; browser automation; connectors-as-config
