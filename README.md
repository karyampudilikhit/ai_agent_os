# Vision AI (repo: `ai_agent_os`)

**AI employees that actually do your work.** You give it what you're
building — a raw idea, a launch to plan, a customer to reach — and it
spins up a persistent team of AI employees who don't fabricate evidence
and actually do the work using the tools you already use.

> **Positioning note (2026-08-02):** "an AI co-founder for solo founders"
> is retired. Solo founders are the **starting wedge**, not the
> definition — the Employee model is role-generic, so the same product
> expands to any knowledge worker. Investor framing keeps the wedge
> explicit: *start with founders, built for everyone.*

**The pitch, in one sentence:** everyone else's AI writes about the work;
ours does it — sends the emails, posts the Slack, writes the file — and
every deliverable is verification-gated so nothing hallucinated ships.
Mutating actions never fire without a founder tap; the approval gate is
what makes "AI does the work" safe.

Flat-monthly SaaS pricing (~$29-49/mo unlimited, small free tier) — no
per-seat, no per-task metering.

**Naming (decided 2026-08-09): the product is Vision AI.** The live
waitlist site at https://vistron-ai.vercel.app/ still says "Vistron AI"
and is the one asset left to rebrand — everything in this repo, the deck,
and the model assets are already Vision AI.

**The hard rule:** if the AI hands work back to the user, it has failed.
Approval-gate irreversible actions only; never ask for state the system
can observe itself.

This file is the state-of-the-project snapshot. It gets rewritten every
time work is pushed so a new chat (no memory of the last session) can
read it and pick up without re-deriving anything from the diff.

---

## Vocabulary (locked, use everywhere)

- **Employee** — one AI worker, one role, persistent memory, verified
  output. **First-class object** as of Phase 1: has a UUID, lives in a
  shared registry, and can be hired into multiple Units simultaneously.
- **Team** — one Supervisor + N specialists, working together on one
  project. Still transient in code (inline in a Unit); becomes
  first-class in Phase 2.
- **Unit** — a persistent container of Employees with its own Supervisor
  as active Manager. In code: what the filesystem calls a "session_id".
- **Company** — a persistent container that owns Units. **First-class
  structural object** as of Phase 1; the active CEO Manager (a
  supervisor-of-supervisors) is Phase 2.

The "B, B" hierarchy decision — Employees exist in many containers, and
containers are active with their own Managers — is fully shipped:
Employee-first-class ✅, Unit-with-Manager ✅ (Supervisor pattern),
Company-with-CEO ✅ (CEO Manager, Phase 2), prompt-driven org design ✅
(Phase 3a — describe the company, CEO proposes the org chart, founder
approves, Units + specialists auto-hire).

---

## Where things stand

| | |
|---|---|
| **Foundation** | Committed on `feat/phase-4-orchestration`: contracts, agent creation, orchestration engine, memory. Then the five audit phases — 1: adapter raises instead of returning errors as content + `calculate` built-in; 2: shared run workspace so specialists see upstream work; 3: four confirmed bugs; 4: relevance-based memory recall; 5: coverage for the four untested subsystems + the Dockerfile's missing Chromium. |
| **Employee abstraction** | ✅ live — `Employee`, `DynamicEmployee`, per-employee memory, min/max tier verification floor. |
| **AI hierarchy — Phase 1** | ✅ live — Employees have persistent UUIDs in a shared registry; same Employee can be hired into multiple Units. Companies exist as structural containers with auto-created "Personal" default. v1 team files auto-migrate to v2 on first load. |
| **Supervisor pattern** | ✅ live — every Unit auto-hires a Supervisor. User only talks to the Supervisor. Supervisor plans delegation, dispatches sub-tasks per specialist, then synthesizes in one voice. |
| **Adaptive router** | ✅ live — team-first bias, 10/10 accuracy across trivial/verify/team-shaped tasks in the retune test. |
| **Chat with intent classification** | ✅ live — `add_employee`, `remove_employee`, `modify_employee`, `design_team`, `clear_team`, `run_task`. |
| **Marketing landing page** | ✅ live at `/` (`frontend_mvp/index.html`). Editorial-dark palette lifted straight from the app (crimson `#b83043` accent on `#0c0c0c`), Instrument Serif display face, cold-open hero that draws a Company org top-down as an SVG branching tree, cycles through 3 scenarios (Fundraising / Marketing / Research). Below: bento grid (locked vocabulary + trust layer + approval gate + cross-turn memory + real actions), horizontal-scroll live-deliverable cards (personas, video ideas, cold outbound, investor xlsx, pitch deck), before/after table, 3-step how-it-works, single Founding-100 price card, FAQ. Nav points to `/app/` for the playground. |
| **Playground UI** | ✅ live at `/app/` (`frontend_mvp/app/index.html`). Studio Chat + Canvas + Org + Output tabs + Edit/Connectors sidebar with live per-employee progress. **Canvas and Org tabs render as SVG branching trees** as of this session: shared `renderTreeInto` helper does leaf-weighted horizontal layout (a Unit with 3 specialists gets 3× the band of one with 1), draws smooth S-curve branch paths with `vector-effect: non-scaling-stroke`, auto-centers the root on load, supports click-and-drag pan + wheel-to-horizontal-scroll + a visible custom scrollbar. Layout is proper 3-level (CEO → Supervisor → specialists) at Company altitude, 2-level (Supervisor → specialists) at Unit altitude. Also fixed a serious flexbox propagation bug where the tree's inline `min-width` was bleeding up through `.canvas-body` → `.center` → `.app` and inflating the whole page past the viewport (that's what made "I can't move it" happen — the founder was dragging the whole page, not the tree). Fixed with `min-width: 0` on each ancestor. Old flat-flow `.emp-node` and Unit-card DOM removed; progress polling now updates `.tree-node[data-role]` instead. |
| **Clarifier context awareness** | ✅ live at `backend/app/chat/clarifier.py` — the clarifier now reads (a) the active Company's name + purpose, (b) up to 2 most-recent finished deliverables' output snippets (up to 2500 chars each), and (c) cross-turn Q&A memory from earlier clarification cycles in the same session. Two hygiene passes strip bad LLM output before it ever reaches the founder: `_drop_resolved_reference` kills identification-shaped questions ("Who are the personas?", "Which video idea?") whenever the referenced entity already appears in a recent deliverable, and `_drop_redundant` uses token-overlap against pooled + per-source vocabularies to filter re-asks the LLM is famous for. Multi-part questions get split into atomic ones (parenthetical-safe regex — "roles, and industries" stays intact, "industries, and what insights" splits). Persistence: `ClarificationStore.record_qa` keeps the last 8 (Q, A) pairs per session/company so audience answered on turn 2 is still known on turn 5. Also fixed the root-cause bug that made this all pointless: unit runs launched inside a Company were only tagged with `session_id`, never `company_id`, so `RunStore.list_recent_done` couldn't find the personas deliverable when the next chat turn looked it up by Company scope. Now every run carries both ids. Regression tests in `scratchpad/test_clarifier_bug.py` + `scratchpad/test_end_to_end.py` cover the personas + Finance-Free Friday + audience-research bundled-reask cases. |
| **Verification / no-fabrication** | ✅ live — critique/refine loop, measured working (multi-agent 1/10 vs single-call 3/10 in the independent-judge benchmarks). |
| **Agentic execution loop** | ✅ live — `orchestrator/execution_loop.py`, THINK → ACT → OBSERVE, bounded (10 steps / 240s / repeat-guard). Replaced the old single-shot `MCPPlanner` that picked 4 tools blind before seeing any result. This is what makes dependent work possible ("read the file, then email the person named in it"). `AGENT_LOOP_MODEL` routes the loop to a different model than synthesis/critique; `AGENT_STEP_MAX_TOKENS` (default 700) sizes each step. |
| **Browser automation** | ✅ live, including inside the agentic loop. Real visible Chrome, persistent profile, founder logs in themselves (no password ever enters the codebase). Async dispatch via `action.browser_task_async` + pollable status. Screenshot-on-failure, consent-banner dismissal, classified open-failures. **One shared Playwright instance on one dedicated thread** — never start/stop per session, and marshal all `Page`/`Locator` access through `run_on_browser_thread()`. |
| **Compute + real market data** | ✅ live — `action.run_python` (separate isolated interpreter, wall-clock timeout, scratch cwd, sockets disabled, credentials stripped) and `action.fetch_market_data`. Verified composing on real data end to end **through the agentic loop**: SPY daily bars → SMA-50/200 crossover → **CAGR 11.26%, Sharpe 0.9329, MaxDD −18.76%**, computed by executed code and carried into the deliverable with a citation to the execution step. ⚠️ Reaches this outcome in **2 of 4** valid runs — see "Open problems" #1. ⚠️ **Solo only.** As of 2026-08-15 the *team* path has never produced real computed numbers (0 of 5) while the solo path is 2 of 2 — see "Open problems" §0. |
| **Guard layer (record-checking)** | ✅ live and load-bearing. `SourceLedger` (asks the network "was this URL actually fetched?" — caught 2 fabricated citations), `ToolCallLedger` + `claim_checker` (asks the call log "was this value ever sent?" — caught a deliverable blaming a `game_id` the run never sent), per-tool consecutive-failure guard, argument type validation, refinement-can't-ship-a-worse-draft, DONE-challenge when a specialist quits with ≤1 successful call, and `_gate_deliverable` — **detection can fail a run**, not merely force a rewrite, with the draft preserved on the failed record. |
| **Model dependency** | 🔴 **single point of failure.** Only `gpt-oss:120b-cloud` and `nemotron-3-super:cloud` are alive on the free Ollama tier. `qwen3-coder:480b` and `deepseek-v3.1:671b` were **retired upstream 2026-07-15 and now return HTTP 410** — they still appear in `/api/tags`, so a run looks healthy right up to the first model call. The current top-tier cloud models (`kimi-k2.7-code`, `deepseek-v4-pro`, `glm-5.2`, `minimax-m2.7`) all return **HTTP 403 — paid subscription required**. If `gpt-oss:120b-cloud` retires the same way, the product stops working with no warning. |
| **Playbook library (premium output on weaker models)** | ✅ live — Supervisor classifies each task (`validation`, `research`, `writing`, `analysis`, `strategy`, `general`), pulls task-specific quality rules ("mark unknown," "cite verbatim," "prove absence," adversarial paragraph, etc.), and composes a task-tailored briefing for each specialist. This is the mechanism that closes ~70% of the gap vs premium hosted models on local `gpt-oss:120b` — output quality moved from 6.5/10 → 7.5/10 in the validation-task A/B. Reflection-loop-driven mutations of the playbook are Phase 2. |
| **Tavily web search** | ✅ live — real search results injected into specialists' prompts when tasks are research-shaped. |
| **Read-only web fetch** | ✅ live — any URL mentioned in a task gets fetched (BeautifulSoup extract); top-2 Tavily URLs also get deep-read. |
| **Reddit read-only** | ✅ live — OAuth-backed reader for subreddit top posts and threads. Fires on `r/subreddit` mentions or reddit-shaped task language. Needs `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET`; silently disabled without them. |
| **MCP client layer** | ✅ live — persistent asyncio loop + registry with cached sessions + pre-flight tool planner. `POST /api/connectors` to add any MCP server; employees discover tools and call them automatically. |
| **Custom HTTP tools (Option B)** | ✅ live — user defines an HTTP endpoint spec (method, URL, auth, params) via `POST /api/http-tools`; the tool appears in the same planner as MCP tools, namespaced as `custom.<name>`. Auth: none / bearer / api_key_header / basic. Solves "connect to any API that has no MCP server yet." |
| **AI hierarchy — Phase 2 (CEO Manager)** | ✅ live — `CEOManager` runs the same plan → delegate → synthesize loop at Company altitude that the Supervisor runs at Unit altitude. `POST /api/companies/{id}/run` — CEO plans across Units, dispatches sub-tasks to each Unit's Supervisor, synthesizes back in the founder's voice. Every Company auto-hires a CEO on create (legacy Companies backfilled on first use). |
| **AI hierarchy — Phase 3a (prompt-driven org design)** | ✅ live — `HierarchyDesigner` turns a founder's plain-English company description into an org chart proposal (2–6 Units, each with 1–4 specialists). `POST /api/companies/{id}/design_hierarchy` returns the proposal; `POST /api/companies/{id}/apply_hierarchy` materializes it (creates Units, attaches them to the Company, auto-hires Supervisors + specialists). Founder can edit the proposal before applying. |
| **Action layer (the DOING pipe)** | ✅ live — third tool namespace alongside MCP + custom HTTP. Six built-ins covering the **send → read → reply** loop for email: `action.send_email` (SMTP, returns a stable Message-ID the caller can chain replies against), `action.read_inbox` (IMAP fetch — filters by unread / sender / subject, read-only, runs inline), `action.reply_email` (SMTP threaded reply — sets `In-Reply-To` + `References` so Gmail groups it under the original), `action.post_slack` (webhook), `action.write_file` + `action.read_file` (sandboxed workspace). Every **mutating** action enqueues on the `ApprovalQueue`; the founder taps Approve in the UI and the action fires against the real environment. Read-only actions run inline. |
| **Evidence receipts** | ✅ live — `EvidenceExtractor` produces a structured claims ledger for every deliverable (`verified` / `flagged_unknown` / `unsourced_claim`), rendered as chips above the deliverable in the UI so verification is visible instead of buried in prose. |
| **Notion via MCP** | ⏳ walkthrough documented, integration + `NOTION_TOKEN` not yet set up locally. |
| **Obsidian via HTTP tools** | ⏳ walkthrough documented, plugin + tools not yet registered locally. |
| **Vision Desktop Agent (full-system access)** | ❌ scoped, not yet built. Small Python/Electron daemon on the founder's machine, WebSocket to the backend, screenshot + click + type + allow-listed shell + arbitrary-folder file access with per-app approval on first use. Next major layer after the action MVP. |
| **Company / CEO UI (org tree Canvas, universal chat router)** | ✅ live — Phase 3b rewrote the flow to be prompt-first. No mode toggle, no "create company" form, no "design hierarchy" textarea. Everything runs through **one** chat input. A universal router (`POST /api/chat`, backed by `UniversalChatRouter` in `backend/app/chat/`) sees the current selection state and classifies each message into one of: `create_company` (extracts name + purpose + auto-designs the hierarchy in the same turn), `design_hierarchy`, `apply_proposal` (say "yes" / "apply"), `discard_proposal`, `add_unit` (extends an existing Company with one new Unit — designed by the CEO to not duplicate existing scope, materialized immediately), `run_task_company` (kicked off in the background — chat returns in seconds with a `run_id` and the client polls `/api/runs/{id}` until done), `run_task_unit` (same async pattern; auto-creates a Unit if none is selected, so a founder who just says "run X" doesn't hit a dead-end), `casual_chat`. The proposal renders inline in the chat reply — the founder just types "yes" to hire everyone. Right sidebar keeps the Company selector + purpose + Unit count as **read-only status**. Middle panel has an **Org** tab that renders the full tree (CEO node on top, Unit cards below with Supervisor + specialists inside). Canvas auto-follows the router's selection (new Unit created → Canvas loads its team). |
| **Memory system** | ✅ live at `backend/app/memory/`. Two tiers over the persisted `RunStore`: `memory_manager.py` recalls past **successful** deliverables, `mistake_repository.py` recalls past **failures**, both ranked by `retrieval.py` (Okapi BM25, dependency-free — no vector store, because a network call on the recall path is a new way to fail confidently and buys nothing at a 200-run corpus). Recall is deliberately conservative: a document must clear a normalized coverage threshold **and** match ≥2 discriminating terms, because a loose match hands an employee plausible material for a question it doesn't answer. Wired into `_build_clarifier_context` (relevance-matched deliverables of any age, alongside the recency feed) and `DynamicEmployee.build_objective` (cross-run recall, so a specialist can see work a *different* specialist did on an earlier run). Calibration was set from probing real run history, not from constructed fixtures — two false-positive classes only appeared there. Tests: `test_memory_recall.py`. |
| **Outage resilience** | ✅ live as of 2026-08-15 — three layers. (1) `ollama_adapter` retries HTTP 5xx with jittered exponential backoff on **every** call in the system. (2) `execution_loop` records `backend.unavailable` in the `ToolCallLedger` when it gives up, so the gate reports an **outage** rather than critiquing a deliverable that never had a chance to be written. (3) `employee_coordinator` re-runs a **single** dead specialist (1 retry each, 2 per run, 5s delay) when the ledger shows the backend died during its turn — and refuses to retry when there is no such record, so a genuine refusal is never looped. See Open problems §0. |
| **Per-employee configuration** | ✅ live as of 2026-08-16 — click any employee in the Canvas or Org tree and configure how it behaves. **Collaboration posture** (team / flexible / solo) makes the previously-hardcoded "do only your part" instruction a setting, and auto-promotes `team`→`flexible` when the employee owes a required output — that contradiction is what made a Data Engineer refuse the compute gate three times. **Required outputs** (`executed_code` / `fetched_url` / `file_written`) let a role own a guarantee instead of re-earning it from the task wording each run; `file_written` stats the disk. **Standing domain rules** reach the *agentic loop*, not just the prose writer — the loop never saw an employee's mandate before, so a rule about the code arrived after the arithmetic was already wrong. Plus per-employee step / time / token budgets and a **full prompt override with nothing protected** (a deliberate product decision — the ledger-based guards run outside the prompt and are unaffected). Storage: a nested `config` dict on the registry record, merged not replaced, with `null` meaning "clear back to inherit". `GET /api/employees/{id}/effective-config` returns every setting with its provenance. Role templates + per-employee override are designed and stubbed; increment 2. |
| **Test coverage** | ⚠️ partial — 26 test files; the canonical suite is **152 passing** (`test_phase6_fixes` + `test_execution_loop` + `test_phase5_coverage` + `test_memory_recall` + `test_specialist_outage_retry` + `test_done_refusal` + `test_number_provenance` + `test_ollama_empty_retry` + `test_output_contract` + `test_employee_config` + `test_dynamic_employee_prompt`). Covered: execution loop, browser automation (14 tests), memory recall (17), and as of Phase 5 the four subsystems that had nothing (`test_phase5_coverage.py`, 24 tests): the chat router's never-dead-end contract, the approval queue's **reject** path (approve was exercised constantly in development, reject never was), citation verification incl. the Phase 3a multi-URL and 3b DOI-paren regressions, and the connector layer incl. the security property that undeclared args are dropped rather than rerouted to the query string. Still uncovered: synthesis, critique/refinement loop, the clarifier's question-dropping heuristics. |
| **Deploy** | ❌ **not deployed.** `fly.toml` names `neutron-ai`; the app was never created and the name is unclaimed. Needs: `fly auth login`, a globally-unique app name, `fly volumes create data`, secrets (`OLLAMA_HOST`, `OLLAMA_API_KEY`, `TAVILY_API_KEY`), `fly deploy`. The Dockerfile was **broken for browser work** until Phase 5 — it pip-installed `playwright` but never ran `playwright install`, so Chromium would have been absent and every browser task would have failed in production while passing locally; now fixed with `--with-deps chromium`. Open risk: `memory = "512mb"` is likely too small for headless Chromium, and an OOM presents as the machine restarting mid-run rather than as a browser error. |
| **AI hierarchy — Phase 2b** | ❌ deferred: multiple Teams per Unit + persistent CEO memory. |
| **User accounts / auth** | ❌ deferred until we're ready to host. |

---

## Open problems (read this before planning work)

### 0. The team path is less reliable than one specialist (2026-08-15)

**This is now the #1 open problem, and it displaced the one below.**

Seven runs on 2026-08-15. The two that delivered real computed numbers
were both **single-specialist**; all five **team** runs failed, and no two
failed the same way. Full write-up:
`Vision_AI_Run_Failure_Incident_Report.pdf` (built from server logs, not
from prose). Six distinct defects were found, in three families:

| | Defect | Family | Status |
|---|---|---|---|
| D1 | Ollama cloud returns HTTP 500 in bursts (≥11 in a 25-min window; four consecutive 500s killed one specialist's whole turn) | infrastructure | absorbed, not fixable by us |
| D2 | Nobody was assigned to do the computation — Supervisor split fetch → design → write-up and the backtest belonged to no one | decomposition | ✅ fixed |
| D3 | Tool ranking ran on the Supervisor's *paraphrase*, so the Data Engineer was offered `fetch_market_data` and nothing else — `run_python` wasn't deprioritised, it was **not offered** | decomposition | ✅ fixed |
| D4 | Compute gate queried the process-global ledger **unscoped**, so it passed on a *previous* run's `run_python`. After the first success in any server session it was effectively disabled | guard bug | ✅ fixed |
| D5 | A run passed every guard and stated no numbers at all — "the figures are in the PDF at `<path>`", and the path did not exist | guard bug | ✅ fixed |
| D6 | `calculate` (single-expression AST walker) counted as evidence a backtest ran | guard bug | ✅ fixed |

**The framing that matters:** four of the six were infrastructure faults
or bugs in the guards themselves — not the model failing to do the work.

**Why teams fail where solo works** — three penalties compound:

1. **~4× the model calls** (~20–30 vs ~5–8, estimated from log volume)
   against a backend that fails randomly. More calls means a higher
   chance the one *critical* call — the decision to run the backtest —
   is the one that dies.
2. **Decomposition can orphan a required step** (D2). One specialist,
   one prompt, no seams.
3. **Specialists cannot hand files to each other.** There is no shared
   artifact convention, so they *guess* filenames (`data.csv`,
   `chosen_etf.txt`, `market_analysis.json`). In one run a specialist
   that could not find its input **emailed
   `dataengineer@example.com`** asking a colleague who does not exist.
   That is a correct inference from the role fiction it was given, with
   no mechanism behind it.

**Fixed since (all on this branch, `178505f` → HEAD):**

- Adapter-level 5xx retry with jittered backoff, so **every** call
  survives a blip — not just the agentic loop's step call.
- Outages are reported **as outages**. `execution_loop` records
  `backend.unavailable` in the ledger when it gives up; the deliverable
  gate checks that first and says "the backend became unreachable,
  re-run it" instead of critiquing a draft that never had a chance.
- **Turn-level recovery** (`employee_coordinator.py`): a specialist that
  produces nothing *and* has a recorded `backend.unavailable` in its turn
  is **re-run alone** — not the whole team, not the roles that already
  succeeded. Bounded: 1 retry per specialist, 2 per run, 5s delay
  (the 500s arrive in bursts, so retrying instantly hits the same one).
  No ledger evidence → **no retry**, so a genuine refusal is never
  looped. Tests: `test_specialist_outage_retry.py`.
- `ledger.calls(since=…)` + run-start timestamps, so the compute gate
  asks about *this* run.
- Substance check: a requested metric must appear **with an actual
  number** in the prose (fenced code stripped first — source code is a
  claim about what *would* be computed).
- Compute ownership assigned mechanically, weighted so it lands on the
  Quant Analyst rather than the Data Engineer.

**D7 — the third failure class, found 2026-08-16.** A run reported
"CAGR 7.65%, Sharpe 0.7565, max drawdown −20.70%" for an SMA crossover on
SPY. `run_python` genuinely ran and real figures appeared, so **both
existing gates passed**. Re-deriving from the same CSV gave Sharpe 0.4457
and drawdown −34.10% under all eight variants tried; the script the agent
supplied when asked for its code contained **no Sharpe calculation at
all**, used 20/50 windows over 5 years instead of 50/200 over 10, and
applied none of the costs it quoted. The numbers were never computed —
they were written.

Nothing could catch it, because catching it means comparing each figure
against what the code actually *printed*, and `tool_registry` clipped
every result to 200 characters — past the header, before the numbers.

So there are now **three** gates, each asking a different question:

| Gate | Question | Where |
|---|---|---|
| provenance | did a compute tool run? | `routes._unused_compute_capability` |
| substance | are numbers stated at all? | `compute_gate.missing_metric_values` |
| **correctness** | **are they the numbers it computed?** | `compute_gate.untraceable_metric_values` |

The correctness gate matches every claimed figure against the compute
tool's real stdout (now kept whole, `MAX_COMPUTE_OUTPUT_CHARS`), forgiving
rounding, percent-vs-fraction scale, and sign — but not a number that
appears nowhere at any scale. Run-scoped, same lesson as D4.

**Also fixed since:** `run_python` reports failure as prose ("Python
exited with code 1…") which `_call_failed` did not recognise, so a
**crashed script counted as a successful call** system-wide — the
degenerate-sweep guard never fired on broken Python and it counted toward
the DONE-challenge tally. And the adapter now retries an **empty HTTP
200** with a *larger token budget* rather than the same request again:
the cause is reasoning consuming `num_predict` before `response` gets
any, so an identical retry reproduces it. Fires on `done_reason='stop'`,
not just `'length'` — the live failure that motivated it reported `stop`.

**Still open, in priority order:** an artifact-passing convention with a
per-run manifest (kills the filename guessing *and* the invented
colleagues at the root); verifying that a file a deliverable references
actually exists (the `file_written` output contract does this per-turn,
but the deliverable gate does not); DONE refusals consuming the step
budget (a Quant Analyst spent 3 of 5 steps arguing); role templates and
the management tier from the config plan (increment 2).

### 1. It was never tool SELECTION — it's premature DONE

**Status as of 2026-08-09: diagnosed.** Superseded in priority by §0
above, but the analysis stands. The old framing ("the planner refuses to
call `run_python`; is it scaffolding or the model?") is **falsified**.
Both the model hypothesis and the tool-ranking hypothesis are dead.

Measured with `run_test_model.py` — 11 runs, of which **7 were
invalidated** by Ollama cloud HTTP 500s and 4 were valid:

| Loop model | Step budget | `run_python` | Outcome |
|---|---|---|---|
| `nemotron-3-super:cloud` | 3000 | **1** | real **Sharpe 0.9329**, CAGR 11.26%, MaxDD −18.76% ✅ |
| `gpt-oss:120b-cloud` | 3000 | **5** | real **Sharpe ≈0.70** ✅ |
| `gpt-oss:120b-cloud` | 700 | 0 | DONE at step 2 ❌ |
| `gpt-oss:120b-cloud` | 3000 | 0 | DONE at step 2 ❌ |

What this shows:

- **`run_python` DOES get called, and a real computed Sharpe DOES reach
  the deliverable.** That is the success criterion the previous session
  set, met twice. The prior "0 of 4" was a property of the old harness,
  not of the planner.
- **The model is not the variable.** `gpt-oss` — the model previously
  blamed — produced the *most* tool calls of any run (5).
- **The step token budget is not the variable either.** 3000 appears in
  both a success and a failure.
- **The actual variable is whether the loop accepts an early DONE.** In
  both failing runs the trace is identical: *step 1 fetch the data →
  step 2 declare DONE → loop ends*, having computed nothing. The
  existing DONE-challenge fires, challenges **once**, and then accepts.
  It worked in one run and was overridden in two.

**The fix, and it is the one this codebase's own lessons predict:** a
mechanical **required-outputs gate** on the loop. The objective asks for
a Sharpe/CAGR/drawdown; the `ToolCallLedger` already records whether any
compute tool ran; so DONE should be **refused** while that recorded fact
is missing — not challenged once and then conceded. Guards that query a
record hold; a single advisory challenge is a prompt rule wearing a
guard's clothes.

Reproduce or re-measure with:

```bash
py -3 run_test_model.py --model gpt-oss:120b-cloud --label baseline
```

The runner marks a run **INVALID** rather than reporting a confident zero
when the loop silently fell back to another model, or when a step died on
an HTTP 500 — both happened, and both would otherwise have manufactured a
clean-looking wrong answer.

⚠️ **Ollama cloud 500s are frequent under repeated runs.** Space runs out
and check the exit code: `3` means the run was infrastructure-killed and
its numbers mean nothing.

### 2. Three lessons that should shape every fix here

- **Guards that query a RECORD hold. Guards that match TEXT fail** —
  every time, on a word missing from a list. The hand-back detector
  failed 3 separate times ("loaded", then "judged"/"unavailable", then
  "could not be retrieved"); each fix was adding another word, which is
  the tell. Before writing a regex, ask *what recorded fact can I check?*
- **Detection that only forces a rewrite is half a guard.** A run once
  flagged contradictions, rejected a refinement that added 5 fabricated
  claims, exhausted its retry budget, and shipped `status: done` anyway.
- **Prompt rules are advisory to a weak model; mechanical checks are
  not.** A prompt rule against stopping early did not hold. A mechanical
  DONE-challenge did.

### 3. Not shipped

Zero design partners, zero paying customers, no demo video, not deployed.
The nearest competitor (Vellum, vellum.ai) is hosted, paid, and shipping
at $30–200/mo. Our four confirmed advantages over them — output
verification, multi-user teams, Windows/Android support, and flat pricing
— **count for nothing until this is deployed and demoable.**

---

## Architecture: how work actually flows

```
User's chat message
    ↓
ChatIntentClassifier (LLM)
    ↓
    ├── add_employee / remove_employee / modify_employee → TeamStore update, done
    ├── design_team → EmployeeSpawner writes N specialists to TeamStore
    ├── clear_team → TeamStore.clear(keep_supervisor=True)
    └── run_task ↓
        │
        EmployeeCoordinator.run_with_supervisor(task, supervisor, specialists)
            │
            1. SupervisorPlanner.design_delegation(task, specialists)
            │      ├─ Classify task type (validation | research | writing |
            │      │  analysis | strategy | general) via heuristic
            │      ├─ Pull matching playbook rules from playbooks.py
            │      └─ → [{role, sub_task, task_brief}, …]
            │         Each brief composes: mandate + relevant playbook
            │         rules + task-specific format/failure-mode instructions
            │
            2. For each specialist in order:
            │      DynamicEmployee.run_task(sub_task, task_brief=…, teammates_context=…)
            │          ├─ (pre-flight) URLs in task → WebFetchTool → inject
            │          ├─ (pre-flight) research-shaped → TavilySearch → inject
            │          ├─ (pre-flight) top-2 Tavily URLs → deep-read → inject
            │          ├─ (pre-flight) reddit-shaped → RedditReader → inject
            │          ├─ (pre-flight) MCP + HTTP tools available → unified
            │          │              planner picks & invokes → inject
            │          ├─ Pipeline.run_objective(objective, min_tier, max_tier)
            │          │      → AdaptiveSupervisor.classify(objective)
            │          │      → single_call | single_call_critique | multi_agent_critique
            │          │      → CritiqueEngine loop: verify, refine, verify shipped text
            │          └─ Store result in EmployeeMemoryStore (keyed on Employee UUID)
            │
            3. SupervisorPlanner.synthesize(task, contributions)
                   → one merged deliverable in the Supervisor's voice
```

Every specialist has `max_tier = "single_call_critique"` — they never spawn
nested sub-teams inside themselves.

---

## Employee abstraction & hierarchy — key files

```
backend/app/employees/
├── employee.py                     Base Employee class (role, memory, tier floor/ceiling)
├── dynamic_employee.py             Instance-configurable Employee — role and mandate
│                                   passed at spawn time. THIS is what every specialist is.
├── employee_registry.py            NEW (Phase 1). First-class Employee identity:
│                                   {id, role, mandate, avatar_seed, tags, created_at}
│                                   persisted per Employee under data/registry/.
│                                   Same UUID → same memory file, everywhere.
├── company_store.py                NEW (Phase 1). First-class Company container:
│                                   {id, name, purpose, unit_ids}. Default "Personal"
│                                   Company auto-created so pre-hierarchy Units have
│                                   a parent without the user thinking about it.
├── supervisor.py                   SupervisorPlanner: classifies task type, pulls
│                                   playbook rules, composes per-specialist briefings
│                                   with quality rules baked in, then synthesizes
│                                   (preserving "unknown" and URL citations from
│                                   specialists rather than smoothing them away).
├── playbooks.py                    NEW. Task-type-keyed quality rulesets — the
│                                   "how to be premium-quality" wisdom that lets
│                                   weaker models (gpt-oss:120b) produce output
│                                   near premium-hosted-model quality. Rules include
│                                   "mark unknown," "cite URLs," "triangulate,"
│                                   "quote pricing verbatim," "prove absence,"
│                                   "adversarial paragraph before verdict." Six
│                                   playbooks in v1 (validation, research, writing,
│                                   analysis, strategy, general). Reflection-loop
│                                   mutations of these rulesets are Phase 2.
├── employee_spawner.py             EmployeeSpawner: prompt → team spec via LLM call.
│                                   Now uses employee_id from the spec so memory files
│                                   stay linked across sessions.
├── employee_coordinator.py         Runs a team on a task. run_with_supervisor is the
│                                   Supervisor-pattern entry point.
├── chat_intent.py                  Routes chat messages to team-management vs task-run.
├── team_store.py                   Per-Unit team spec (v2 schema — members reference
│                                   employee_id, not inline role/mandate). Auto-migrates
│                                   v1 files on first load, preserving legacy IDs so
│                                   existing memory files keep working.
├── memory_store.py                 Per-Employee JSON memory. Relevance-ranked recall
│                                   (backend/app/memory/retrieval.py), falling back to
│                                   last-N entries when nothing scores above threshold.
├── progress_store.py               In-memory progress state per session for the UI.
└── idea_validation_employee.py     Legacy: the first hand-coded flagship employee.
```

---

## Connectors — how real work happens

Every specialist runs a **pre-flight** before writing anything. Five
mechanisms, in order:

1. **URLs in the task text** → `WebFetchTool` fetches each one, extracts
   main content (`bs4`), injects up to 4000 chars per page.
2. **Research-shaped tasks** (heuristic-triggered by words like
   *research, analyze, competitor, latest, current, market*) →
   `TavilySearchTool` returns top-5 results with titles + snippets.
3. **Deep-read** → top 2 Tavily result URLs get fetched fully.
4. **Reddit-shaped tasks** (`r/subreddit` mentions or thread URLs) →
   `RedditReader` pulls top posts / thread comments via Reddit OAuth.
5. **External tools (unified plan)** — MCP servers + user-defined HTTP
   tools + built-in actions all present the same interface to one
   planner. `MCPPlanner` does one cheap LLM call: *"given this task and
   these tools, which should you call?"*, then executes each call.
   Namespaces route to their backends: `<server>.<tool>` → MCP,
   `custom.<name>` → HTTP, `action.<name>` → action registry (may
   enqueue on `ApprovalQueue` if mutating).

```
backend/app/tools/
├── web_search.py                   Tavily client + should_search() heuristic
├── web_fetch.py                    Read-only page fetch + extract_urls()
├── reddit_reader.py                NEW. OAuth-backed subreddit + thread reader.
│                                   Needs REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET.
├── mcp_runtime.py                  Persistent background asyncio loop (MCP is
│                                   async-first; the rest of the app is sync)
├── mcp_store.py                    Per-connection specs (name, transport, command,
│                                   args, env) persisted to team_data/_mcp_connections.json
├── mcp_client.py                   MCPConnection (one live session per server) +
│                                   MCPRegistry (unified tool catalog)
├── mcp_planner.py                  Unified pre-flight LLM call: picks + invokes
│                                   tools across both MCP and HTTP-tool registries,
│                                   formats results for prompt injection.
├── http_tool_store.py              NEW (Option B). JSON-persisted user-defined
│                                   HTTP tool specs (method, URL, auth, params).
└── http_tool_runner.py             NEW (Option B). Executes an HTTP tool call —
                                    argument bucketing, path substitution, auth
                                    header injection, quiet failures. Exposes
                                    MCP-shaped list_tools/call so the planner
                                    unifies both sources.

backend/app/actions/                NEW. The DOING layer — third tool namespace.
├── action_registry.py              ActionRegistry: MCP-shape list_tools() /
│                                   call(qname, args). Mutating actions never
│                                   fire directly — they enqueue on
│                                   ApprovalQueue and return a receipt string.
│                                   Read-only actions run inline.
├── approval_queue.py               JSON-backed pending-action store at
│                                   .pending_actions.json (gitignored). States:
│                                   pending -> approved -> executed | failed
│                                                     \-> rejected
└── builtin/
    ├── send_email.py               SMTP send. Env: SMTP_HOST/PORT/USER/PASS/FROM.
    │                               Stamps and returns a Message-ID so the caller
    │                               can chain a reply_email against it later.
    ├── read_inbox.py                IMAP fetch. Env: IMAP_HOST/PORT/USER/PASS
    │                               (falls back to SMTP_USER/PASS). Filters:
    │                               unread_only, from, subject_contains, folder,
    │                               limit. Read-only — no approval.
    ├── reply_email.py               SMTP threaded reply. Sets In-Reply-To +
    │                               References so Gmail groups it under the
    │                               original. Same SMTP_* env as send_email.
    ├── post_slack.py               POST to Slack Incoming Webhook. Env: SLACK_WEBHOOK_URL.
    ├── write_file.py               Sandboxed UTF-8 file write inside
    │                               VISION_WORKSPACE_DIR (defaults to
    │                               <repo>/workspace). Path traversal rejected.
    └── read_file.py                Read-only sibling. Same sandbox, no approval.
```

Credentials live in `.env` (gitignored). MCP env-var secrets live in
`_mcp_connections.json` (also gitignored). HTTP tool specs incl. tokens
live in `.http_tools.json` (also gitignored). Pending-action state
(may contain draft email bodies, Slack messages) lives in
`.pending_actions.json` (also gitignored). The sandboxed action
workspace at `<repo>/workspace/` is gitignored.

---

## API

**Units and Teams:**
```
GET    /api/sessions                          List all Units
POST   /api/sessions                          Create a new Unit (auto-hires Supervisor)
GET    /api/sessions/{id}/team                Current roster
POST   /api/sessions/{id}/team/design         Design specialists from a prompt
POST   /api/sessions/{id}/team/members        Add one employee (new)
DELETE /api/sessions/{id}/team/members/{role} Remove one employee
POST   /api/sessions/{id}/run                 Run a task on the team (Supervisor pattern)
GET    /api/sessions/{id}/progress            Poll target for live UI updates
POST   /api/sessions/{id}/chat                Intent classifier — routes to the right action
```

**Employee registry (Phase 1 hierarchy):**
```
GET    /api/employees                         List all Employees across all Units
GET    /api/employees/{id}                    One Employee
POST   /api/employees                         Create an Employee (registry-only, no Unit)
PATCH  /api/employees/{id}                    Update role/mandate/tags
DELETE /api/employees/{id}                    Remove from registry
POST   /api/units/{unit_id}/hire              Add an existing Employee to a Unit
                                              body: {"employee_id": "...", "is_supervisor": false}
```

**Companies + CEO + hierarchy design (Phases 1, 2, 3a):**
```
GET    /api/companies                         List Companies (default "Personal" always present)
GET    /api/companies/{id}                    One Company (includes ceo_employee_id)
POST   /api/companies                         Create a Company (auto-hires CEO)
PATCH  /api/companies/{id}                    Update name/purpose
DELETE /api/companies/{id}                    Delete (default "personal" refuses)
POST   /api/companies/{cid}/units/{uid}       Attach a Unit to a Company
DELETE /api/companies/{cid}/units/{uid}       Detach a Unit from a Company
POST   /api/companies/{id}/design_hierarchy   CEO proposes org chart from a description
                                              body: {"description": "..."}
POST   /api/companies/{id}/apply_hierarchy    Materialize (edited) org chart —
                                              creates Units, hires Supervisors + specialists
                                              body: {"units": [{name, purpose, specialists}]}
POST   /api/companies/{id}/run                CEO plans → delegates across Units → synthesizes
                                              body: {"task": "..."}
```

**Action layer (the DOING pipe):**
```
GET    /api/actions                           List registered built-in actions
GET    /api/pending_actions?status=pending    List pending / resolved actions
POST   /api/pending_actions/{id}/approve      Approve AND execute inline; returns updated record
POST   /api/pending_actions/{id}/reject       Kill a pending action (optional ?reason=…)
```

**Universal chat (the front door — Phase 3b):**
```
POST   /api/chat                              body: {message, current_company_id?, current_session_id?}
                                              LLM classifies the message across Unit + Company altitudes
                                              and dispatches. Response:
                                                {intent, reply, side_effects: {company_id?, session_id?,
                                                 pending_proposal?, applied_units, org_refreshed,
                                                 run_output?, evidence}}
                                              Kills the "click here, then toggle that mode" UX —
                                              every founder action goes through this one endpoint.
```

**Connectors — MCP servers:**
```
GET    /api/connectors                        List MCP connections + available tools
POST   /api/connectors                        Add/update a connector (name, transport, command, args, env)
DELETE /api/connectors/{name}                 Remove a connector
PATCH  /api/connectors/{name}?enabled=false   Toggle without removing
```

**Connectors — custom HTTP tools (Option B):**
```
GET    /api/http-tools                        List user-defined HTTP tools
POST   /api/http-tools                        Register a new HTTP tool
DELETE /api/http-tools/{name}                 Remove
PATCH  /api/http-tools/{name}?enabled=false   Toggle without removing
```

---

## Frontend

Single-file Playground at `frontend_mvp/index.html`. Warm-minimal theme
— stone neutrals + deep metallic-red accent (oxblood/burgundy gradient),
honors `prefers-color-scheme` for automatic dark mode. Layout:

- **Left — Studio Chat.** User messages + AI replies + step-checklists as
  the team works. Chat input at the bottom.
- **Middle — Canvas / Output tabs.** Canvas shows the team as a
  horizontal sequential flow with arrows between specialist cards.
  Cards pulse blue while working, turn green with completeness/fabrication
  metadata when done. Supervisor card is visually distinct (gold ★
  avatar, "SUPERVISOR" badge, cannot be deleted). Output tab shows the
  merged deliverable + a fast team-hired report immediately after
  specialists are hired.
- **Right — Session / Team / Add Employee / Connectors.** Session
  dropdown, compact team list, role+mandate add form, expandable
  Connectors section. **UI for the Employee library and HTTP tools is
  a Phase 2 add — API is live, UI catches up next.**

Static file — served by FastAPI from `/`. No build step.

---

## Local run

```bash
# From the repo root:
pip install -r backend/requirements.txt

# .env in repo root — add API keys as you configure connectors:
#   TAVILY_API_KEY=tvly-…
#   NOTION_TOKEN=ntn_…            (once you set up Notion)
#   REDDIT_CLIENT_ID=…            (once you create a Reddit script app)
#   REDDIT_CLIENT_SECRET=…
#
# Action layer (DOING pipe) — set these to enable each built-in action:
#   SMTP_HOST=smtp.gmail.com      (send_email / reply_email — use a Gmail App
#   SMTP_PORT=587                  Password from myaccount.google.com/apppasswords)
#   SMTP_USER=you@gmail.com
#   SMTP_PASS=…                   (Gmail App Password, NOT your account password)
#   SMTP_FROM=Your Name <you@gmail.com>
#   IMAP_HOST=imap.gmail.com      (read_inbox — same App Password works for IMAP;
#   IMAP_PORT=993                  enable IMAP in Gmail: Settings → Forwarding
#   IMAP_USER=… (defaults to SMTP_USER)   and POP/IMAP → Enable IMAP)
#   IMAP_PASS=… (defaults to SMTP_PASS)
#   SLACK_WEBHOOK_URL=…           (post_slack — create at api.slack.com/apps)
#   VISION_WORKSPACE_DIR=…        (write_file / read_file sandbox root;
#                                  defaults to <repo>/workspace/)

py -3 -m uvicorn backend.app.api.main:app --port 8000
```

Playground: [http://127.0.0.1:8000](http://127.0.0.1:8000).

**Ollama** (default LLM): the code targets `gpt-oss:120b-cloud` on a
local Ollama daemon.

**Model gotchas — both cost real debugging time, don't rediscover them:**

- `/api/tags` lists **retired** models. `qwen3-coder:480b-cloud` and
  `deepseek-v3.1:671b-cloud` appear healthy and return **HTTP 410** on
  first use. Check liveness with an actual generate call, not the tag
  list.
- **Thinking models spend `num_predict` on `thinking` before
  `response`.** Exceed the budget and Ollama returns HTTP 200 with
  `response: ""` and `done_reason: "length"`. That empty string used to
  travel downstream as valid content — a swapped-in model would have
  looked like "the planner never called the tool" when in fact it never
  said anything at all. The adapter now **raises** on an empty 200 and
  names the budget in the error. If you route a reasoning-heavy model
  through the loop, raise `AGENT_STEP_MAX_TOKENS` (default 700) —
  `run_test_model.py` defaults it to 3000 for exactly this reason.

**Optional env for the agentic loop:**

```bash
AGENT_LOOP_MODEL=nemotron-3-super:cloud   # loop only; synthesis/critique unchanged
AGENT_STEP_MAX_TOKENS=3000                # per-step budget (default 700)
```

---

## Setting up connectors

### Tavily search (already live if `.env` has the key)

1. Sign up at [tavily.com](https://tavily.com), grab the API key.
2. `TAVILY_API_KEY=tvly-…` in `.env`.
3. Restart uvicorn. Done.

### Reddit (read-only, OAuth)

Reddit killed anonymous JSON access in 2024, so read-only still needs a
"script" app for client-credentials OAuth.

1. Go to [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) →
   **create app** → **script** type. Any name; redirect URI
   `http://localhost:8000` (unused but required).
2. Copy the string under the app name (`REDDIT_CLIENT_ID`) and the
   `secret` field (`REDDIT_CLIENT_SECRET`) into `.env`.
3. Restart uvicorn. Any task mentioning `r/anything` will pull
   top-of-week posts for that subreddit.

### Notion via MCP (walkthrough — not yet set up)

1. Create integration at
   [notion.so/my-integrations](https://www.notion.so/my-integrations).
   Copy the **Internal Integration Secret** (`ntn_…`).
2. Share a page/database with the integration.
3. `NOTION_TOKEN=ntn_…` in `.env`. Restart uvicorn.
4. In the Playground → Connectors ▾:
   - Name: `notion`
   - Command: `npx`
   - Args: `-y @notionhq/notion-mcp-server`
   - Env: leave blank (token inherits from `.env`).
5. First connect takes ~30s (npx download).

### Obsidian via HTTP tools (walkthrough — not yet set up)

Obsidian has no first-party MCP server — this is where the HTTP-tool
layer earns its keep.

1. In Obsidian: **Settings** → **Community plugins** → search
   **"Local REST API"** → install and enable.
2. **Settings** → **Local REST API** → copy the API key and toggle the
   non-encrypted HTTP server ON (default port `27123`). Local HTTP is
   fine — the endpoint binds to `127.0.0.1` only.
3. Register each capability as an HTTP tool via `POST /api/http-tools`.
   Example (search):
   ```json
   {
     "name": "obsidian_search",
     "description": "Search my Obsidian vault for notes matching a query.",
     "method": "POST",
     "url": "http://127.0.0.1:27123/search/simple/",
     "auth": {"type": "bearer", "token": "YOUR_KEY_HERE"},
     "parameters": [
       {"name": "query", "in": "query", "required": true}
     ]
   }
   ```
4. Add a `obsidian_read_note` (GET, path arg `filename`) and optionally
   `obsidian_write_note` (PUT, body arg + Content-Type: text/markdown).
5. Tasks that mention "my vault" or "my notes" will now surface the
   real content of your Obsidian.

### Any other MCP server

Same pattern as Notion. Any server that speaks MCP over stdio works. HTTP
transport is scaffolded but not fully implemented yet.

### Any other API with no MCP server

Use `POST /api/http-tools` — bearer / API-key-header / basic auth all
supported. See `test_http_tools.py` for postman-echo examples.

---

## Benchmark thread (context, not action items)

Older but still true: independent judge (`qwen3-coder:480b-cloud`, a
different model family from the generator) with position-swap control
showed that multi-agent's *fabrication rate* is measurably lower than
single-call — **1/10 vs 3/10** across independent checks. The broader
"is multi-agent smarter overall" question is still open. Next step
whenever we return to it: absolute-scoring judge instead of pairwise.

⚠️ **That benchmark is no longer reproducible as written** — the judge
model `qwen3-coder:480b-cloud` was retired upstream (HTTP 410). Re-running
it needs a new judge from a different family than the generator;
`nemotron-3-super:cloud` is the free-tier candidate.

---

## Picking this back up

1. Read this file — it's the ground truth as of the last push — then the
   **Open problems** section above, which is where the actual work is.
2. Check `HANDOFF.md` in the repo root — session-level details on
   what was in progress at the end of the previous work day.
3. Sanity-check:
   ```bash
   git status && py -3 -m pytest test_phase6_fixes.py test_execution_loop.py test_phase5_coverage.py test_memory_recall.py test_specialist_outage_retry.py test_done_refusal.py test_number_provenance.py test_ollama_empty_retry.py test_output_contract.py test_employee_config.py test_dynamic_employee_prompt.py -q
   ```
   Expect **152 passing**.
4. Verify the models are actually alive before trusting any run — the tag
   list lies:
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://localhost:11434/api/generate -d '{"model":"gpt-oss:120b-cloud","prompt":"hi","stream":false}'
   ```
5. **Run the server yourself in a terminal.** A background-started server
   gets reaped between turns, and a stale one on `:8000` silently serves
   the WRONG code — that nearly produced a fake benchmark result once.
   Prefer `run_test_model.py`, which runs the pipeline in-process and so
   cannot have this problem.
6. Standing rule: every `push` rewrites this README as a full state
   snapshot, not just the changed diff. Keep that going.
