# Vision AI — Session Handoff

> **Purpose:** This file lets a new chat session pick up exactly where the last one ended. The README is the evergreen snapshot of the whole project; this is the fresh-in-my-head "here's what we were mid-doing" companion.

---

## What Vision AI is

Vision AI (repo still named `ai_agent_os`) is a co-founder for solo founders. You give it what you're building, and it spins up a persistent team of AI employees — a Supervisor plus specialists — who don't fabricate evidence and actually do the work using the tools you already use (web, Notion, Slack, etc.). Founders first, general audience later. Flat-monthly SaaS pricing (~$29-49/mo unlimited), free tier to try.

**Locked vocabulary:** Employee → Team → Unit → Company. Employee is a role. Team is a Supervisor + specialists working on one thing. Unit is a group of Teams. Company is a group of Units. Company was originally "Factory" — swapped for consistency with the org-chart metaphor.

**Hero differentiator:** verification / no fabricated evidence. Every deliverable goes through the critique/refine loop; specialists that hallucinate stats get caught and rewritten. This is the one thing no competitor is systematically doing, and it's what the pitch leads with.

---

## Quick-start for a new session

```bash
# 1. Locate the project
cd "C:\Users\KARYAM~1\AppData\Local\Temp\claude\D--quant\34a3eb1d-a204-44dc-ac02-0da452892d77\scratchpad\repos\ai_agent_os"

# 2. Check server state
tasklist | grep python.exe        # is uvicorn running?
curl http://127.0.0.1:8000/api/connectors     # smoke test

# 3. Restart uvicorn if needed
py -3 -m uvicorn backend.app.api.main:app --port 8000
```

Playground is at http://127.0.0.1:8000

---

## State of the project

| | |
|---|---|
| **Committed on `feat/phase-4-orchestration`** | Phases 1-4 + Employee abstraction + Idea Validation Employee (up through commit `a0f40a7`) |
| **Uncommitted local work** | Playground UI, Supervisor pattern, chat intent classifier, MCP client, tools layer — see "Uncommitted work" below |
| **Web search connector** | ✅ Tavily live (`TAVILY_API_KEY` in `.env`) |
| **Web fetch connector** | ✅ Read-only page fetch live (BeautifulSoup) |
| **MCP client layer** | ✅ Built and reachable at `/api/connectors`; zero servers connected by default |
| **Notion via MCP** | ⏳ User has the walkthrough (see "Open questions"), but hasn't confirmed setup |
| **Reddit MCP / real posting** | ⏳ Day 7 of the connectors plan, not started |
| **Auth / user accounts** | ❌ Deliberately deferred until we're ready to host |
| **AI hierarchy (Team/Unit/Company as first-class objects)** | ❌ Deferred; user chose connectors work over hierarchy work this session |

---

## Uncommitted work (substantial — decide whether to push next session)

Every file created or modified this session is uncommitted. Notable additions:
- `frontend_mvp/` — the entire Playground UI (chat + canvas + edit panel + Connectors section)
- `backend/app/employees/` — supervisor.py, chat_intent.py, dynamic_employee.py, employee_coordinator.py, employee_spawner.py, progress_store.py, team_store.py
- `backend/app/tools/` — web_search.py, web_fetch.py, mcp_runtime.py, mcp_store.py, mcp_client.py, mcp_planner.py
- `backend/app/orchestrator/orchestrate.py`
- `backend/app/api/main.py`, updates to `routes.py` / `schemas.py`
- `.env` (gitignored — contains `TAVILY_API_KEY`, will contain `NOTION_TOKEN` once user completes setup)
- Various test scripts (`test_router_v2.py`, `test_adaptive_supervisor.py`, etc.)

Also: `fastapi` was upgraded to `0.139.2` (mcp SDK pulled in a newer starlette that broke the old fastapi). Update `backend/requirements.txt` accordingly before pushing.

---

## What got done today (long session — high-signal only)

- ✅ **Product positioning locked:** co-founder framing, solo founders first, verification as the hero differentiator, flat-monthly SaaS, "one prompt in, team of employees out."
- ✅ **Vocabulary locked:** Employee → Team → Unit → Company (Company swapped from Factory).
- ✅ **Adaptive supervisor router retuned** with team-first bias — LLM classifier + safe heuristic fallback, tested at 10/10 against a mix of task types.
- ✅ **Supervisor pattern shipped (Phase 1 of Unit + Supervisor architecture):** every Unit auto-hires a Supervisor on creation, user talks to the Supervisor, Supervisor designs a delegation plan and dispatches specific sub-tasks to each specialist, then synthesizes.
- ✅ **Chat intent classifier** built (`add_employee`, `remove_employee`, `modify_employee`, `design_team`, `clear_team`, `run_task`) — fixed the "'add a supervisor' got interpreted as a task" bug.
- ✅ **Playground UI:** crew.ai-inspired layout — Studio Chat left, Canvas + Output tabs middle, Edit/Connectors right, live per-employee progress polling with working badges, Supervisor visually distinct (gold star avatar).
- ✅ **Team-hired report** on Output tab: user sees who was hired + each employee's mandate immediately after auto-hire, ~10s instead of waiting minutes for the full run.
- ✅ **Speed fix (~3-5x):** DynamicEmployees now have `max_tier = "single_call_critique"` — no more nested team spawning inside each specialist.
- ✅ **Synthesis prompt tightened:** no more filler sections, no duplicate tables, silently omit missing data. Killed the consultant-report tone.
- ✅ **Tavily search connector shipped** (`web_search.py`) — real search results injected into specialists' prompts when tasks look research-shaped.
- ✅ **Read-only web fetch connector shipped** (`web_fetch.py`) — any URL mentioned in a task gets fetched; top-2 Tavily URLs get deep-read.
- ✅ **MCP client layer shipped** (Days 3-4 of connectors plan):
  - `mcp_runtime.py`: persistent asyncio loop in a background thread
  - `mcp_client.py`: MCPConnection + MCPRegistry with cached live sessions
  - `mcp_store.py`: JSON-persisted connector specs (gitignored, may hold API tokens)
  - `mcp_planner.py`: cheap pre-flight LLM call picks which tools to fire for each task, executes them, injects results as source data
  - API: `GET/POST/DELETE/PATCH /api/connectors`
  - UI: Connectors section in the right sidebar with add/remove
- ✅ **fastapi upgraded to 0.139.2** to resolve starlette compatibility after mcp SDK install

## What did NOT get done today

- ❌ **Notion connector end-to-end** (Days 5-6 of the plan) — user got the walkthrough but hasn't confirmed they've set up the Notion integration and pasted the token in `.env`. Test still pending.
- ❌ **Reddit MCP + polish** (Day 7) — queued after Notion.
- ❌ **AI hierarchy build** — user chose "B, B" (Employees in many places, containers as active with their own Managers) but agreed to prioritize connectors first. Whole ~2.5-3 week project parked.
- ❌ **Nothing pushed this session** — all the Playground / Supervisor / connectors work is still local.

---

## Tomorrow's plan

### What only the user can do

1. **Decide whether to push the uncommitted work first.** It's a substantial commit (~15+ new files). The standing rule is "every push updates the README" — the README currently reflects the state as of last push, so it should be rewritten to include the Playground, Supervisor pattern, and connectors before pushing.
2. **Complete the Notion connector setup** if they still want to test it:
   - Create Notion integration at [notion.so/my-integrations](https://www.notion.so/my-integrations), copy the token
   - Share a page/database with the integration
   - Add `NOTION_TOKEN=ntn_...` to `.env`
   - Restart uvicorn
   - Add connector in UI (name: `notion`, command: `npx`, args: `-y @notionhq/notion-mcp-server`, env blank)
3. **Confirm the Notion tool count > 0** after adding, so we know it actually connected.

### What Claude does in parallel

- Once user confirms Notion is connected, run the end-to-end test: *"research trends on r/marketing and write a summary to my Notion 'Marketing Trends' page"*. Watch for actual tool calls firing and real Notion page writes.
- If push happens first, rewrite the README (Vision AI positioning, vocabulary, Playground + Supervisor + connectors architecture, what's live now).
- If Notion works: move on to Day 7 (Reddit connector — likely a small hand-rolled Reddit reader unless there's a reliable Reddit MCP server; either fits our new client layer).

### Success criteria

If (a) uncommitted work is pushed with an updated README, and (b) at least one MCP connector beyond Tavily is live and producing real tool calls in a task run, tomorrow is a win.

---

## Open decisions

1. **Push before more building?** The uncommitted work is large enough that it's a real handoff risk. Recommend pushing first. TBD.
2. **Notion MCP working?** User hasn't confirmed setup completion. TBD.
3. **AI hierarchy (Team/Unit/Company as first-class objects) — when?** Deferred. User chose "B, B" (Employees in many parents, containers as active with their own Managers) — but that's a 2.5-3 week build and connectors are the priority for now. Revisit after connectors sequence is done.
4. **Ollama billing check** — user wanted to verify they're on the free tier, we agreed the source of truth is [ollama.com](https://ollama.com); user has the answer, didn't share, and I can't see it from here.

---

## Cross-project notes

Nothing from this session invalidated anything in memory. Existing memories still hold:
- **Positioning:** [defer-to-founder-on-positioning] — I state a view once, then defer if they push back.
- **Standing push rule:** [push-updates-readme] — every "push" rewrites the README as a state snapshot.
- **Vision AI:** [ai-agent-os-startup] — this project's memory; updated this session with the new vocabulary, Supervisor pattern, and connectors architecture.

---

## Past days log

### 2026-07-19 (this session)
- Locked product positioning (co-founder for solo founders, verification as hero)
- Locked hierarchy vocabulary (Employee → Team → Unit → Company)
- Built and shipped: Playground UI, Supervisor pattern, chat intent classifier, Tavily + web-fetch + MCP client layer, speed fix, tightened synthesis
- Notion MCP walkthrough given, not yet confirmed complete
- Zero pushes; substantial uncommitted work

### Prior work (see README on `feat/phase-4-orchestration`)
- Phases 1-4 of the original build plan, plus Employee abstraction + Idea Validation Employee, plus the benchmark suite that established the fabrication-catching case and the pairwise judge's position-bias failure
