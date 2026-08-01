# Vision AI — Session Handoff

> **Purpose:** This file lets a new chat session pick up exactly where the last one ended. The README is the evergreen snapshot of the whole project; this is the fresh-in-my-head "here's what we were mid-doing" companion.

---

## What Vision AI is

Vision AI (repo still named `ai_agent_os`) is a co-founder for solo founders. You give it what you're building, and it spins up a persistent team of AI employees — a Supervisor plus specialists — who don't fabricate evidence and actually do the work using the tools you already use (Gmail, Slack, files, etc.). Founders first, general audience later. Flat-monthly SaaS pricing (~$29-49/mo unlimited), free tier to try.

**Locked vocabulary:** Employee → Team → Unit → Company. Employee is a role. Team is a Supervisor + specialists working on one thing. Unit is a group of Teams. Company is a group of Units.

**Hero differentiator:** verification / no fabricated evidence + an approval-gated action layer that actually DOES the work (sends emails, drafts docs, generates real files) instead of just writing about it.

---

## Quick-start for a new session

```bash
# 1. Locate the project
cd "C:\Users\KARYAM~1\AppData\Local\Temp\claude\D--quant\34a3eb1d-a204-44dc-ac02-0da452892d77\scratchpad\repos\ai_agent_os"

# 2. Make sure Ollama is up (returns models list)
curl http://localhost:11434/api/tags

# 3. Restart uvicorn
py -3 -m uvicorn backend.app.api.main:app --port 8000
```

- **Marketing page:** http://127.0.0.1:8000/  → editorial-dark landing with a live animated tree hero.
- **Playground:** http://127.0.0.1:8000/app/  → the actual product.

---

## State of the project

| | |
|---|---|
| **Branch** | `feat/phase-4-orchestration` — everything committed + pushed |
| **Live deploy** | ❌ Not yet on a public URL. `fly.toml` app name set to `neutron-ai`. Deploy files (Dockerfile, .dockerignore, GitHub Actions) all in the repo, ready to `fly launch`. |
| **Design partners** | ❌ Zero onboarded |
| **Paying customers** | ❌ Zero |
| **Deck** | Investor deck v2 (13 slides) exists — in scratchpad at `vision_ai_deck_v2.pptx`. Placeholders `{{FOUNDER_EMAIL}}`, `{{FOUNDER_LINKEDIN}}`, `{{PROD_URL}}`, `{{FOUNDER_BG}}` still need filling. |
| **Demo video** | ❌ 90-sec silent demo not yet recorded |

---

## What got done this session (2026-08-01)

- ✅ **Clarifier is now context-aware** — the founder was hitting bugs where the clarifier lost context between turns ("give me a 60s script for the Finance-Free Friday video" → clarifier asked "who are the personas?" about personas it had just written; "give me a message for each persona" → same class of bug). Fixed with three feeds into the clarifier: active Company purpose, up-to-2 recent deliverable snippets (2500 chars each), and cross-turn Q&A memory. Two hygiene passes strip bad LLM output before the founder sees it: `_drop_resolved_reference` (kills "Who are X?" when X appears in a recent deliverable) and `_drop_redundant` (token-overlap against pooled + per-source vocabularies). Multi-part questions ("A and what B?") get split into atomic ones with a parenthetical-safe regex. Also fixed a root-cause bug: unit runs launched inside a Company were only tagged with `session_id`, never `company_id`, so the clarifier's `list_recent_done` lookup by Company scope returned empty — the clarifier was context-blind because the data wasn't reaching it.
- ✅ **Regression tests** for the clarifier context bugs — `scratchpad/test_clarifier_bug.py` (unit-level) + `scratchpad/test_end_to_end.py` (real RunStore / real ClarificationStore / real `_build_clarifier_context`, with a scripted LLM that returns the exact bad responses gpt-oss gave). All pass.
- ✅ **Marketing landing page at `/`** — replaces the raw playground as the front door. Editorial-dark palette pulled from the app (crimson `#b83043` on `#0c0c0c`), Instrument Serif display face, cold-open hero that draws a Company org top-down as an animated SVG branching tree, cycles through 3 scenarios (Fundraising / Marketing / Research). Bento grid, live-deliverable cards, before/after, 3-step how-it-works, single Founding-100 price card, FAQ. Playground moved to `/app/`. `frontend_mvp/index.html` = marketing; `frontend_mvp/app/index.html` = playground. Static mount serves both without any route changes.
- ✅ **Canvas + Org tabs → SVG branching trees** — shared `renderTreeInto` helper. Leaf-weighted horizontal layout (a Unit with 3 specialists gets 3× the band of one with 1). Smooth S-curve branch paths with `vector-effect: non-scaling-stroke`. Auto-centers the root on load. Click-and-drag pan. Mouse wheel scrolls horizontally. Visible custom scrollbar. 2-level tree on Canvas (Supervisor → specialists), 3-level on Org (CEO → Supervisor → specialists). Progress polling now updates `.tree-node[data-role]` instead of the removed `.emp-node`.
- ✅ **Fixed the "I can't move it" bug** — the tree's inline `min-width: 1149px` was bleeding UP through `.canvas-body` → `.center` → `.app` and inflating the entire page past the viewport to 1900+px. What the founder was trying to "move" was actually the whole page, not the tree. Fixed by adding `min-width: 0` to each ancestor so the tree scrolls where it should — inside its own container.

---

## What did NOT get done

- ❌ **Auto-Company on first task** — bare-Supervisor state persists. When a fresh chat's first message is a task and no Company is selected, the router auto-creates just a Supervisor and no specialists; Supervisor writes the deliverable solo (often narrating a hypothetical delegation instead of doing the work). Flagged in the previous session, still open. Next-highest-leverage build.
- ❌ **Sequential task queue** — Work mode still runs one task per turn. A 30-day plan can't be handed to the app and executed autonomously across days.
- ❌ **Live deploy to Fly.io** — `neutron-ai` app not yet created via `fly apps create`.
- ❌ **Design partners onboarded** — still zero.

---

## Tomorrow's plan (in leverage order)

### What only the founder can do

1. **Deploy to Fly.io (~20 min)** — `fly auth signup` → `fly apps create neutron-ai` → `fly volumes create data --size 1 --region bom` → `fly secrets set OLLAMA_HOST=https://ollama.com OLLAMA_API_KEY=...` → `fly tokens create deploy -a neutron-ai` → paste into GitHub repo secret `FLY_API_TOKEN` → trigger the "Deploy to Fly.io" GitHub Action. Once live: `https://neutron-ai.fly.dev/` shows the marketing page and `/app/` is the product.
2. **Record the 90-second silent demo** using OBS + the scripted shot list from an earlier session (in the compressed history). Cut jump-cuts on long runs. Now that the marketing page exists, the demo can lead with the animated hero and cut to the playground for the meat.
3. **Get 3-5 design partners** to actually try the deployed app. Indie hackers, solo SaaS founders. The marketing page is the front-door pitch — send it before booking a call.

### What Claude does in parallel (in the next chat)

1. **Build auto-Company-on-first-task** (~half a day). When the first prompt in a fresh chat is task-shaped and no Company is selected, auto-detect the domain (fundraising, launch, sales outreach, product research), create a right-sized Company + Units + specialists in one shot, then run the task through them. Kills the bare-Supervisor bug + eliminates every "no employees / no org design" moment the founder flagged in the previous session.
2. **Sequential task queue** (~1-2 days). Turn a Plan-mode plan into an executable sequence; each step runs on founder approval. Bridges plan/work for multi-day work.
3. **Fill the investor deck placeholders** once the founder gives their email / LinkedIn / prod URL / one-line bio.

### Success criteria

- Deploy live at `https://neutron-ai.fly.dev`
- Auto-Company built and pushed (fresh chat → type a task → see a real team form and Canvas fill)
- At least 1 design-partner outreach message sent from the founder

---

## Open decisions

1. **Should the AI ever act autonomously without an approval tap?** Founder pushed back in an earlier session on the current "every mutating action gates for a tap" default — argued things like login-to-YC-and-submit or auto-record-a-demo-video are technically feasible today, just not wired. Real decision: which of these do we build the browser-automation layer for, and does the founder want to hand over Gmail/YC/Stripe credentials to the AI? User TBD.
2. **Scheduler layer scope.** Sequential-task-queue (Claude executes step-by-step on approval) is smaller than a real cron-driven scheduler (Vision AI wakes up on its own to poll inboxes, continue threads). Which to build first? Recommend sequential first, cron second. User TBD.
3. **Fly.io app name.** Set to `neutron-ai` in `fly.toml`. If the name is taken when `fly apps create` runs, founder picks a new one and updates the toml. User TBD only if the name conflict actually happens.

---

## Files touched this session

**Backend (clarifier context awareness):**
- `backend/app/chat/clarifier.py` — new `context` param on `initial_questions` / `next_step`, rewritten prompts, `_drop_resolved_reference` + `_drop_redundant` + `_split_multi_part` + plural stemming
- `backend/app/chat/clarification_store.py` — added `record_qa`, `record_prompt`, `get_memory`, `clear_memory` for cross-turn Q&A memory
- `backend/app/chat/async_runs.py` — added `RunStore.list_recent_done(session_id, company_id, limit)` with a same-user global fallback
- `backend/app/api/routes.py` — new `_build_clarifier_context` helper; both clarifier call sites now pass context; unit runs and company runs both tag with `session_id` AND `company_id`; snippet cap `CLARIFIER_SNIPPET_CHARS = 2500`

**Frontend (marketing page + tree UI):**
- `frontend_mvp/index.html` — NEW, marketing landing page
- `frontend_mvp/app/index.html` — MOVED from `frontend_mvp/index.html` and heavily edited: added `renderTreeInto` shared helper, `enableTreePan` drag/wheel handler, rewrote `renderTeam` (Canvas) and `refreshOrgTree` (Org) to render as SVG trees, updated `applyProgressToCards` to work on `.tree-node[data-role]`, added `min-width: 0` to `.center` and `.canvas-body` to stop layout inflation, deleted the old `.org-empty` / `.org-node.ceo` / `.org-tree-connector` / `.org-units-row` / `.org-unit-column` / `.org-unit-card` DOM template from the Org tab initial HTML

**Ephemeral (in scratchpad, not committed):**
- `scratchpad/test_clarifier_bug.py` — unit regression tests for the clarifier fixes
- `scratchpad/test_end_to_end.py` — full-stack simulation using real stores + scripted LLM
