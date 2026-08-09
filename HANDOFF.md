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
| **Models** | 🔴 only `gpt-oss:120b-cloud` alive — the other two are retired upstream |
| **Nearest competitor** | Vellum (vellum.ai) — direct, shipping, $30-200/mo |

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

## BLOCKER — the diagnostic can't run: two of three models are retired

The open question is whether the tool-selection failure is **scaffolding
or model**. That diagnostic is built and wired (`AGENT_LOOP_MODEL` swaps
the model for the agentic loop ONLY, leaving synthesis/critique/evidence
on the main adapter so any difference is attributable) — but it cannot
be run, because both stronger models are dead upstream:

```
qwen3-coder:480b    -> HTTP 410, retired 2026-07-15
deepseek-v3.1:671b  -> HTTP 410, retired 2026-07-15
gpt-oss:120b-cloud  -> ALIVE (the only one)
phi3:latest         -> local, far weaker; testing with it answers nothing
```

They still appear in `/api/tags`, so a run looks healthy right up to the
first model call and then every step fails. Phase 1's adapter fix is why
this took one command to find — it raised the real HTTP 410 instead of
returning it as model output.

**Operational risk:** the product depends on ONE working cloud model with
no fallback. If gpt-oss:120b-cloud retires the same way, Vision AI stops
working entirely, with no warning.

To unblock: pull a current Ollama cloud model (cheapest), or add an
adapter for an API key you already hold (~1 hour). Then:

```bash
AGENT_LOOP_MODEL=<model> bash run_test_model.sh "<the quant prompt>"
```

Watch one number: **`run_python` call count**. It has been 0 across four
runs. ≥1 means model problem → route the loop to that model and most
further scaffolding is unnecessary. Still 0 means architecture → build
required-outputs (below).

---

## Competitor: Vellum (vellum.ai) — direct, and they ship

Checked 9 Aug 2026. **Appears to have pivoted** from an LLMOps platform
to a personal AI assistant — two live pages agree, while every search
result still shows the old positioning. Treat the pivot as inference,
not confirmed fact.

- Pitch: *"a personal AI assistant that remembers how you work, learns
  your preferences, and takes action across the tools you already use"*
- Pricing: $30 / $100 / $200 per month, metered by vCPU + GiB + credits;
  higher tiers include an assistant email and subdomain
- Has: persistent memory, self-improving skills, real actions incl.
  **code execution**, background/scheduled runs, its own email/GitHub/
  Slack identity, and permission tiers ("Strict: they ask before every
  action")

Much closer to us than Prime Agent was: hosted, paid, non-technical
buyer, our price band. Note they charge $200/mo for code execution —
the exact capability our planner refuses to use.

**Four confirmed gaps → our advantage features**

1. **No output verification.** Their trust story is entirely about
   SECURITY (Keychain, no training on your data) and says nothing about
   citations, fabrication or accuracy. Ours is built and proven. Surface
   it: a trust receipt per deliverable ("9 of 12 claims verified against
   pages actually opened"). Hardest for them to copy — it needs the
   recording substrate, not a feature flag.
2. **Single-user only.** No multi-user accounts, no shared workspaces,
   no audit logs; credentials live in one person's macOS Keychain. A
   5-person team can't share an employee. Our Employee→Team→Unit→Company
   model is a different product shape, not a feature they can add.
3. **macOS + iPhone only** — "Android and Windows are on the roadmap".
   We're a web app, working on both today. India is overwhelmingly
   Windows/Android. Free positioning, expires when they ship Windows.
4. **You pay while idle.** Their own caveat: background memory incurs
   cost "even when you're not actively using it", remedied by turning
   features off. Flat pricing is a direct attack on that.

**The uncomfortable part:** a funded team independently validated this
market (good), but they ship and we don't. None of these advantages
count until Vistron is deployed and demoable.

---

## Tomorrow's plan

### What only the founder can do

1. **Decide the tool-selection strategy** (see Open decisions #1) — this is the
   binding constraint on everything else.
2. **Get a working second model** (pull a current Ollama cloud model, or hand over an API key) — this is what unblocks the diagnostic, and the diagnostic decides the next week of work.
3. **Register the GitHub OAuth App** (~5 min) — endpoints built, app never
   registered, so the whole connect flow is dead code.
4. **`fly auth login`** if we're deploying.
5. **Settle Vision AI vs Vistron AI naming.**

### What Claude does in parallel

- Put the tool name INTO the sub-task text (`supervisor.design_delegation`) —
  the specialist reliably reads its objective, and has ignored the tool list 4×.
- Regenerate the two stale PDFs with the correction.
- Push the branch (README first, per standing rule).
- Surface the verification layer as a founder-facing trust receipt — it is
  Vellum's clearest gap and ours is already built and proven.

### Success criteria

A quant re-run where `run_python` is actually called and a real Sharpe ratio
reaches the deliverable. Everything else is secondary.

---

## Open decisions

1. **Unblock the model diagnostic** — pull a current cloud model or supply an API key. Everything else waits on this. Founder: **TBD**
2. **Tool selection: scaffolding or model?** — 4 scaffolding levers failed. Next
   options: (a) name the tool in the sub-task objective, (b) try a stronger model
   for the execution loop and measure whether selection improves. (b) tells us
   whether more scaffolding is even worth building. Founder: **TBD**
3. **Vision AI vs Vistron AI** — site says one, all repo/deck/model assets say
   the other. Founder: **TBD**
4. **fly.toml memory 512mb** — likely too small for headless Chromium; raising it
   is a billing call. Founder: **TBD**
5. **Push `feat/phase-4-orchestration`?** — 8 unpushed commits. Founder: **TBD**

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
