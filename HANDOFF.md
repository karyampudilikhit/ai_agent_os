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

Playground is at http://127.0.0.1:8000

---

## State of the project

| | |
|---|---|
| **Branch** | `feat/phase-4-orchestration` — everything committed + pushed |
| **Latest commit** | `d6532bd` Fix router misclassifying long inputs as casual_chat |
| **Live deploy** | ❌ Not yet on a public URL. `fly.toml` app name set to `neutron-ai`. Deploy files (Dockerfile, .dockerignore, GitHub Actions) all in the repo, ready to `fly launch`. |
| **Design partners** | ❌ Zero onboarded |
| **Paying customers** | ❌ Zero |
| **Deck** | Investor deck v2 (13 slides) exists — in scratchpad at `vision_ai_deck_v2.pptx`. Placeholders `{{FOUNDER_EMAIL}}`, `{{FOUNDER_LINKEDIN}}`, `{{PROD_URL}}`, `{{FOUNDER_BG}}` still need filling. |
| **Demo video** | ❌ 90-sec silent demo not yet recorded |

---

## What got done this session

- ✅ **Plan / Work modes** — toggle in chat header (later moved below input); Plan drafts a delegation-only plan, Work executes; "go"/"execute" in Work mode picks up any stored Plan-mode plan. Backed by `backend/app/chat/plan_store.py`.
- ✅ **File upload** — `POST /api/uploads` (multipart), text preview extraction for txt/md/csv/json/docx/xlsx/pptx (PDF flagged as not-yet-supported). Frontend attach button + chip UI + fold into next chat message.
- ✅ **Document generation** — `action.create_pptx`, `action.create_docx`, `action.create_xlsx` action tools. Auto-triggered post-synthesis via keyword detection ("pptx"/"pitch deck", "docx"/"word doc", "xlsx"/"excel"/"spreadsheet") — the FINISHED specialist deliverable is deterministically converted, no LLM re-write. Shared `_workspace.py` + new `_markdown_convert.py`.
- ✅ **Empty-slide pptx bug fixed** — `create_pptx` was in the pre-flight tool listing, so the LLM had to freehand slide content in one cramped call and returned empty `{}` slides. Added `ActionSpec.planner_excluded=True` to hide the doc generators from the planner; they now fire from a post-synthesis converter.
- ✅ **Router truncation bug fixed** — long inputs made the router echo the whole task back and hit the 600-token cap mid-JSON. Classifier now told to omit `task` field, `max_tokens` bumped to 900, `_extract_json` gained truncation recovery (closes unterminated strings + balances braces).
- ✅ **Unit Usage leaderboard removed** — founder didn't want it.
- ✅ **`+ New` is a hard reset** — no ghost sessions in dropdowns, no background poll re-populating stale rankings.
- ✅ **`add_employees_to_unit` + `delete_unit` intents** — router can now add specialists to an existing Unit (fuzzy name match) and delete a wrong Unit.
- ✅ **HierarchyDesigner de-biased** — no more defaulting to "Product / Engineering / Design" for every company; industry-appropriate roles.
- ✅ **Fly.io deploy scaffolding + `neutron-ai` app name** — all wired, just needs the founder to run `fly launch` (still open).

---

## What did NOT get done today

- ❌ **Auto-Company on first task** — bare-Supervisor state persists. When a fresh chat's first message is a task and no Company is selected, the router auto-creates just a Supervisor and no specialists; Supervisor writes the deliverable solo (often narrating a hypothetical delegation instead of doing the work). Founder flagged this hard this session — top-priority next build.
- ❌ **Sequential task queue** — Work mode still runs one task per turn. A 30-day plan can't be handed to the app and executed autonomously across days. This is the "hands-off Work mode" gap that's been in the deferred pile since day one.
- ❌ **Live deploy to Fly.io** — `neutron-ai` app not yet created via `fly apps create`.
- ❌ **Design partners onboarded** — still zero.
- ❌ **Clarifier over-asking** — occasionally re-asks something the founder already answered. Flagged, not yet fixed.

---

## Tomorrow's plan (in leverage order)

### What only the founder can do

1. **Deploy to Fly.io (~20 min)** — `fly auth signup` → `fly apps create neutron-ai` → `fly volumes create data --size 1 --region bom` → `fly secrets set OLLAMA_HOST=https://ollama.com OLLAMA_API_KEY=...` → `fly tokens create deploy -a neutron-ai` → paste into GitHub repo secret `FLY_API_TOKEN` → trigger the "Deploy to Fly.io" GitHub Action.
2. **Record the 90-second silent demo** using OBS + the scripted shot list from an earlier session (in the compressed history). Cut jump-cuts on long runs.
3. **Get 3-5 design partners** to actually try the deployed app. Indie hackers, solo SaaS founders.

### What Claude does in parallel (in the next chat)

1. **Build auto-Company-on-first-task** (~half a day). When the first prompt in a fresh chat is task-shaped and no Company is selected, auto-detect the domain (fundraising, launch, sales outreach, product research), create a right-sized Company + Units + specialists in one shot, then run the task through them. Kills the bare-Supervisor bug + eliminates every "no employees / no org design" moment the founder flagged this session.
2. **Sequential task queue** (~1-2 days). Turn a Plan-mode plan into an executable sequence; each step runs on founder approval. Bridges plan/work for multi-day work.
3. **Fill the investor deck placeholders** once the founder gives their email / LinkedIn / prod URL / one-line bio.

### Success criteria

- Deploy live at `https://neutron-ai.fly.dev`
- Auto-Company built and pushed (fresh chat → type a task → see a real team form and Canvas fill)
- At least 1 design-partner outreach message sent from the founder

---

## Open decisions

1. **Should the AI ever act autonomously without an approval tap?** Founder pushed back this session on the current "every mutating action gates for a tap" default — argued things like login-to-YC-and-submit or auto-record-a-demo-video are technically feasible today, just not wired. Real decision: which of these do we build the browser-automation layer for, and does the founder want to hand over Gmail/YC/Stripe credentials to the AI? User TBD.
2. **Scheduler layer scope.** Sequential-task-queue (Claude executes step-by-step on approval) is smaller than a real cron-driven scheduler (Vision AI wakes up on its own to poll inboxes, continue threads). Which to build first? Recommend sequential first, cron second. User TBD.
3. **Fly.io app name.** Set to `neutron-ai` in `fly.toml`. If the name is taken when `fly apps create` runs, founder picks a new one and updates the toml. User TBD only if the name conflict actually happens.

---

## The continue-prompt the founder was about to test (Work mode, xlsx generation)

Kept here in case the next session wants to run it before doing anything else — it's the smallest concrete task from the funding-plan output that's actually AI-executable today, and it exercises the new xlsx auto-generation path:

```
Build me an investor target spreadsheet as a real xlsx file. Follow the plan we just drafted for Vision AI's pre-seed round.

Requirements:
- 30 rows: 18 India-based, 12 US-based
- Focus on angel investors and micro-VCs who actively back solo technical founders building AI products at the pre-seed stage ($25k-$150k check sizes)
- Columns (in this order): Name, Firm/Fund, Investment Focus, Geography (India/US), Est. Check Size, Twitter Handle, LinkedIn URL, Warm Intro Source (leave blank), Priority (Tier 1/2/3), Notes
- Only include real, verifiable people — never invent names or handles
- If you don't know a Twitter or LinkedIn URL, put "unknown" — don't fabricate one
- Priority Tier 1: known AI-first angels who have publicly backed pre-seed AI startups. Tier 2: micro-VCs with an AI thesis. Tier 3: general early-stage investors who might fit
- In Notes, put a one-sentence "why this investor for Vision AI specifically"
```

---

## Past days log

### Day (this session)
- Shipped Plan/Work modes, file upload, document generation (pptx/docx/xlsx), auto-post-synthesis conversion
- Fixed empty-slide pptx bug + router truncation bug + `+ New` hard reset + Unit Usage removal
- Founder ran fundraising plan test → got a solid Supervisor-solo plan (no employees showed up — this is the bare-Supervisor bug)
- Founder pushed back on "cannot execute" framing → agreed most of those are "not wired yet" not permanent

### Day (previous)
- Added `add_employees_to_unit` + `delete_unit` intents to the router
- De-biased HierarchyDesigner (killed generic-SaaS-startup default)
- Wrote investor deck v2 (13 slides) with due-diligence use case
- Wrote Dockerfile + fly.toml + GitHub Actions CI, made state files DATA_DIR-aware, made Ollama env-configurable
- Founder chose Fly.io + name `neutron-ai`, but hasn't run `fly apps create` yet
