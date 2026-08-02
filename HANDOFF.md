# Vision AI — Session Handoff

> **Purpose:** This file lets a new chat session pick up exactly where the last one ended. The README is the evergreen snapshot of the whole project; this is the fresh-in-my-head "here's what we were mid-doing" companion.

---

## What Vision AI is

Vision AI (repo still named `ai_agent_os`) is **a team of AI employees that actually does your work** — research, drafts, real actions (send email, create a repo, fill a form) — verified against sources, with founder approval on every irreversible action.

**Positioning updated this session:** no longer "an AI co-founder." Founder's call — the product shouldn't be locked to founders as it grows. Current framing: *"AI employees that actually do your work."* Founders are the **starting wedge**, not the definition.

**Locked vocabulary:** Employee → Team → Unit → Company.

**The hard rule (violated twice this session, now a standing bar):** if the AI hands work back to the user, it has failed. See memory `ai-must-do-the-work-never-hand-back`.

---

## Quick-start for a new session

```bash
cd "C:\Users\KARYAM~1\AppData\Local\Temp\claude\D--quant\34a3eb1d-a204-44dc-ac02-0da452892d77\scratchpad\repos\ai_agent_os"
curl http://localhost:11434/api/tags          # Ollama up?
py -3 -m uvicorn backend.app.api.main:app --port 8000
```

- Marketing page: http://127.0.0.1:8000/
- Playground: http://127.0.0.1:8000/**app/**

---

## State of the project

| | |
|---|---|
| **Branch** | `feat/phase-4-orchestration` — **large uncommitted delta, NOT pushed** |
| **Last commit** | `484a046` context-aware clarifier, landing page, tree Canvas/Org |
| **Waitlist site** | ✅ Live on Vercel (founder deployed). Source in `landing/` |
| **Live app deploy** | ❌ Still local only. `fly.toml` set to `neutron-ai`, never launched |
| **Design partners / paying** | ❌ Zero / Zero |
| **Deck** | ✅ `Vision_AI_Pitch_Deck.pptx` (12 slides) — on Desktop |
| **Financial model** | ✅ `Vision_AI_3Yr_Financial_Model.xlsx` — on Desktop |
| **Demo video** | ❌ Not recorded |

---

## What got done this session

- ✅ **Deep browser research (Phase 1)** — `browser_automation.py`, Playwright, JS-rendered multi-page crawls. Proven: 7-page linear.app crawl with real pricing pulled into a report.
- ✅ **Interactive browser automation (Phase 2)** — `browser_task.py` + `browser_session_manager.py`. Opens a real visible Chrome (persistent profile), founder logs in themselves, AI fills the form, **pauses before submit**. Verified end-to-end on a local test form.
- ✅ **Multi-step execution loop** — `orchestrator/execution_loop.py`. Replaced the single-shot 4-tool planner with a real THINK→ACT→OBSERVE loop. **This is the "does any work" unlock.** Proven: read a file, then used a name found *inside* it in the next tool call.
- ✅ **Auto-Company on first task** — `hierarchy_designer.design_from_task()`. ⚠️ **Founder objected — see Open Decisions.**
- ✅ **`create_github_repo` action tool** — GitHub REST API, approval-gated.
- ✅ **OAuth2 "Connect GitHub" flow** — `backend/app/auth/` + `api/oauth_routes.py`. Provider-agnostic (Google stubbed). CSRF state verified working. **Endpoints live; no UI button yet.**
- ✅ **Universal playbook rules** — `_UNIVERSAL_RULES` prepended to every playbook: never write how-to instructions, never ask for credentials, report queued≠done.
- ✅ **Clarifier hardening** — `SYSTEM_CAPABILITIES` block + `_drop_credential_requests` regex filter.
- ✅ **Marketing/growth assets** — landing page + waitlist (`landing/`), investor pitch deck, 3-yr financial model, investor cold email, build-in-public post drafts.
- ✅ **Security fix (this turn):** `browser_profile/`, `.oauth_tokens.json`, `.pending_actions.json` added to `.gitignore` — they hold **live session cookies and tokens** and were previously committable.

---

## What did NOT get done

- ❌ **Nothing committed or pushed.** Whole session's work is local-only.
- ❌ **Connect GitHub UI button** — endpoints work, no button in the app.
- ❌ **GitHub OAuth App not registered** — founder-only step, blocks the whole connect flow.
- ❌ **Desktop agent (Phase 3)** — scoped, deliberately not started.
- ❌ **Loop can't trigger browser_task** — if it *discovers* a URL mid-task it can't act on it (browser is `planner_excluded`).
- ❌ **19 junk auto-created companies** in the workspace, incl. 2 duplicate "Pixel Forge Studios".

---

## Tomorrow's plan

### What only the founder can do

1. **Register the GitHub OAuth App** (~5 min, unblocks everything GitHub):
   - [github.com/settings/developers](https://github.com/settings/developers) → New OAuth App
   - Callback URL **exactly**: `http://127.0.0.1:8000/api/oauth/github/callback`
   - Put `GITHUB_OAUTH_CLIENT_ID` + `GITHUB_OAUTH_CLIENT_SECRET` in `.env`
2. **Answer the auto-Company decision** (A/B/C below) — blocking.
3. **Drive waitlist signups** — post the build-in-public drafts from this session.

### What Claude does in parallel

1. Build the **Connect GitHub button** in the Connectors sidebar.
2. Apply the **auto-Company decision** once given.
3. Optionally clean up the 19 junk companies.

### Success criteria

OAuth App registered + Connect button working = "create a repo" works reliably, no browser, no tokens pasted.

---

## Open decisions

1. **Auto-Company overreach — A / B / C. BLOCKING, founder TBD.**
   It auto-invented *"Pixel Forge Studios"* (a fictional name + purpose + 6 Units) **twice, 9 min apart**, from a single task. Founder: *"why is this defined i didnt even tell the ai to do this."* Options:
   - **A** — revert auto-Company entirely
   - **B** — cut to the real fix: no invented company identity, one Unit sized to the task
   - **C** — propose-and-confirm before creating anything
2. **Delete the 19 junk companies?** — founder TBD.
3. **Desktop agent scope** — full OS control (screenshot/click/type/shell). Security model undecided. Deferred by agreement.
4. **Should the loop be able to call browser_task?** — would enable "find a URL, then go fill that form", but risks stalling the loop on a human login.

---

## Past days log

### This session
- Built browser automation (both phases), the agentic execution loop, GitHub API tool, OAuth2 layer, auto-Company
- Produced all the fundraising/growth assets (deck, model, landing page, emails)
- **Two hard lessons from founder testing:** (1) the clarifier asked for GitHub credentials — fixed at the clarifier layer after an earlier fix missed it; (2) a failed browser task produced a *how-to guide telling the founder to do it manually* — now banned via universal playbook rules
- Founder pivoted positioning away from "AI co-founder"

### Previous session
- Plan/Work modes, file upload, pptx/docx/xlsx generation, router truncation fix, `+ New` hard reset
