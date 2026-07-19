# Vision AI (repo: `ai_agent_os`)

**A co-founder for solo founders.** You give it what you're building — a
raw idea, a launch to plan, a customer to reach — and it spins up a
persistent team of AI employees who don't fabricate evidence and
actually do the work using the tools you already use.

**The pitch, in one sentence:** everyone else's AI writes about the work;
ours does it, and every deliverable is verification-gated so nothing
hallucinated ships.

Founders first, general audience later. Flat-monthly SaaS pricing
(~$29-49/mo unlimited, small free tier) — no per-seat, no per-task
metering.

This file is the state-of-the-project snapshot. It gets rewritten every
time work is pushed so a new chat (no memory of the last session) can
read it and pick up without re-deriving anything from the diff.

---

## Vocabulary (locked, use everywhere)

- **Employee** — one AI worker, one role, persistent memory, verified output.
- **Team** — one Supervisor + N specialists, working together on one project.
- **Unit** — a group of Teams. (First-class objects: not built yet.)
- **Company** — a group of Units. (First-class objects: not built yet.)

Sessions in the codebase are effectively Teams-with-a-Unit-wrapper today
— the Supervisor + specialists model works, but the Unit/Company
hierarchy above it is deferred until connectors are proven end to end.

---

## Where things stand

| | |
|---|---|
| **Foundation** | Phases 1-4 committed on `feat/phase-4-orchestration`: contracts, agent creation, orchestration engine, memory. |
| **Employee abstraction** | ✅ live — `Employee`, `DynamicEmployee`, per-employee memory, min/max tier verification floor. |
| **Supervisor pattern** | ✅ live — every Unit auto-hires a Supervisor. User only talks to the Supervisor. Supervisor plans delegation, dispatches sub-tasks per specialist, then synthesizes in one voice. |
| **Adaptive router** | ✅ live — team-first bias, 10/10 accuracy across trivial/verify/team-shaped tasks in the retune test. |
| **Chat with intent classification** | ✅ live — `add_employee`, `remove_employee`, `modify_employee`, `design_team`, `clear_team`, `run_task`. Kills the "add a supervisor got interpreted as a task" bug. |
| **Playground UI** | ✅ live at `frontend_mvp/index.html` — crew.ai-style Studio Chat + Canvas + Output tabs + Edit/Connectors sidebar with live per-employee progress. |
| **Verification / no-fabrication** | ✅ live — critique/refine loop, measured working (multi-agent 1/10 vs single-call 3/10 in the independent-judge benchmarks). |
| **Tavily web search** | ✅ live — real search results injected into specialists' prompts when tasks are research-shaped. |
| **Read-only web fetch** | ✅ live — any URL mentioned in a task gets fetched (BeautifulSoup extract); top-2 Tavily URLs also get deep-read. |
| **MCP client layer** | ✅ live — persistent asyncio loop + registry with cached sessions + pre-flight tool planner. `POST /api/connectors` to add any MCP server; employees discover tools and call them automatically. |
| **Notion via MCP** | ⏳ walkthrough documented, integration + `NOTION_TOKEN` not yet set up locally. |
| **Reddit / Twitter / other post-write connectors** | ❌ v2. |
| **AI hierarchy (Team/Unit/Company as first-class objects)** | ❌ deferred; ~2.5-3 week build parked until connectors are proven end to end. |
| **User accounts / auth** | ❌ deferred until we're ready to host. |

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
            │      → [{role, sub_task}, …]  (one specific sub-task per specialist)
            │
            2. For each specialist in order:
            │      DynamicEmployee.run_task(sub_task, teammates_context=…)
            │          ├─ (pre-flight) URLs in task → WebFetchTool → inject
            │          ├─ (pre-flight) research-shaped → TavilySearch → inject
            │          ├─ (pre-flight) top-2 Tavily URLs → deep-read → inject
            │          ├─ (pre-flight) MCP tools available → MCPPlanner → call → inject
            │          ├─ Pipeline.run_objective(objective, min_tier, max_tier)
            │          │      → AdaptiveSupervisor.classify(objective)
            │          │      → single_call | single_call_critique | multi_agent_critique
            │          │      → CritiqueEngine loop: verify, refine, verify shipped text
            │          └─ Store result in EmployeeMemoryStore
            │
            3. SupervisorPlanner.synthesize(task, contributions)
                   → one merged deliverable in the Supervisor's voice
```

Every specialist has `max_tier = "single_call_critique"` — they never spawn
nested sub-teams inside themselves, which is where the previous 15+
minute run times were coming from (a 3-employee team wasn't 3 runs, it
was 3 × N nested spawns). Post-fix: ~5-8 min for a 3-4 person team.

---

## Employee abstraction — key files

```
backend/app/employees/
├── employee.py                     Base Employee class (role, memory, tier floor/ceiling)
├── dynamic_employee.py             Instance-configurable Employee — role and mandate
│                                   passed at spawn time, not baked into a subclass.
│                                   THIS is what every specialist is.
├── supervisor.py                   SupervisorPlanner (design_delegation + synthesize) +
│                                   default_supervisor_spec() used on Unit creation.
├── employee_spawner.py             EmployeeSpawner: prompt → team spec (list of roles +
│                                   mandates) via an LLM call.
├── employee_coordinator.py         Runs a team on a task. run_with_supervisor is the
│                                   Supervisor-pattern entry point.
├── chat_intent.py                  ChatIntentClassifier — routes chat messages to
│                                   team-management vs task-run.
├── team_store.py                   Per-Unit team spec persistence (name, purpose, members).
│                                   Enforces "every Unit has a Supervisor".
├── memory_store.py                 Per-Employee JSON memory (last-N-entries recall).
├── progress_store.py               In-memory progress state per session for the UI to
│                                   poll — {phase, current_role, completed, …}.
└── idea_validation_employee.py     Legacy: the first hand-coded flagship employee.
                                    Still works; DynamicEmployee is the general path now.
```

---

## Connectors — how real work happens

Every specialist runs a **pre-flight** before writing anything. Four
mechanisms, in order:

1. **URLs in the task text** → `WebFetchTool` fetches each one, extracts
   main content (`bs4`), injects up to 4000 chars per page.
2. **Research-shaped tasks** (heuristic-triggered by words like
   *research, analyze, competitor, latest, current, market*) →
   `TavilySearchTool` returns top-5 results with titles + snippets.
3. **Deep-read** → top 2 Tavily result URLs get fetched fully (not just
   snippets).
4. **MCP tools** — if any MCP servers are connected, `MCPPlanner` does
   one cheap LLM call: *"given this task and these tools, which should
   you call?"* Calls execute on the persistent asyncio loop, results
   inject as source data.

```
backend/app/tools/
├── web_search.py                   Tavily client + should_search() heuristic
├── web_fetch.py                    Read-only page fetch + extract_urls()
├── mcp_runtime.py                  Persistent background asyncio loop (MCP is
│                                   async-first; the rest of the app is sync)
├── mcp_store.py                    Per-connection specs (name, transport, command,
│                                   args, env) persisted to team_data/_mcp_connections.json
├── mcp_client.py                   MCPConnection (one live session per server) +
│                                   MCPRegistry (unified tool catalog)
└── mcp_planner.py                  Pre-flight LLM call: pick + invoke tools,
                                    format results for prompt injection.
```

Credentials live in `.env` (gitignored). Standard flow: add
`SOME_TOKEN=…` to `.env`, restart uvicorn, add the connector via
`POST /api/connectors` (or the UI's Connectors section).

---

## API

```
GET    /api/sessions                          List all Units (name it says "sessions" internally)
POST   /api/sessions                          Create a new Unit (auto-hires Supervisor)
GET    /api/sessions/{id}/team                Current roster
POST   /api/sessions/{id}/team/design         Design specialists from a prompt
POST   /api/sessions/{id}/team/members        Add one employee
DELETE /api/sessions/{id}/team/members/{role} Remove one employee
POST   /api/sessions/{id}/run                 Run a task on the team (Supervisor pattern)
GET    /api/sessions/{id}/progress            Poll target for live UI updates
POST   /api/sessions/{id}/chat                Intent classifier — routes to the right action

GET    /api/connectors                        List MCP connections + available tools
POST   /api/connectors                        Add/update a connector (name, transport, command, args, env)
DELETE /api/connectors/{name}                 Remove a connector
PATCH  /api/connectors/{name}?enabled=false   Toggle without removing
```

---

## Frontend

Single-file Playground at `frontend_mvp/index.html`. Layout:

- **Left — Studio Chat.** User messages + AI replies + step-checklists as
  the team works. Chat input at the bottom. Enter to send.
- **Middle — Canvas / Output tabs.** Canvas shows the team as a
  horizontal sequential flow with arrows between specialist cards.
  Cards pulse blue while working, turn green with completeness/fabrication
  metadata when done. Supervisor card is visually distinct (gold ★
  avatar, "SUPERVISOR" badge, cannot be deleted). Output tab shows the
  merged deliverable — and, immediately after specialists are hired, a
  team-hired report (fast, LLM-free) so the user has something to look
  at during the ~5-8 min run.
- **Right — Session / Team / Add Employee / Connectors.** Session
  dropdown, compact team list (Supervisor pinned first, no × on it),
  role+mandate add form, expandable Connectors section.

Static file — served by FastAPI from `/`. No build step.

---

## Local run

```bash
# From the repo root:
pip install -r backend/requirements.txt

# .env in repo root — add API keys as you configure connectors:
#   TAVILY_API_KEY=tvly-…
#   NOTION_TOKEN=ntn_…   (once you set up Notion)

py -3 -m uvicorn backend.app.api.main:app --port 8000
```

Playground: [http://127.0.0.1:8000](http://127.0.0.1:8000).

**Ollama** (default LLM): the code targets `gpt-oss:120b-cloud` on a
local Ollama daemon. Free tier of Ollama's cloud-hosted models works
today; verify at [ollama.com](https://ollama.com) if usage gets heavy.

---

## Setting up connectors

### Tavily search (already live if `.env` has the key)

1. Sign up at [tavily.com](https://tavily.com), grab the API key.
2. `TAVILY_API_KEY=tvly-…` in `.env`.
3. Restart uvicorn. Done — every research-shaped task now uses real search.

### Notion via MCP (walkthrough — not yet set up)

1. Create integration at
   [notion.so/my-integrations](https://www.notion.so/my-integrations).
   Copy the **Internal Integration Secret** (`ntn_…`).
2. Share a page/database with the integration (**"…"** → **Connections** →
   **Add connections** → your integration). Access cascades to child pages.
3. `NOTION_TOKEN=ntn_…` in `.env`. Restart uvicorn.
4. In the Playground → Connectors ▾:
   - Name: `notion`
   - Command: `npx`
   - Args: `-y @notionhq/notion-mcp-server`
   - Env: leave blank (token inherits from `.env`).
5. First connect takes ~30s (npx download). After that you should see
   `notion` with a tool count > 0.

### Any other MCP server

Same pattern. Any server that speaks MCP over stdio (subprocess) works
right now. HTTP transport is scaffolded but not fully implemented yet.

---

## Benchmark thread (context, not action items)

Older but still true: independent judge (`qwen3-coder:480b-cloud`, a
different model family from the generator) with position-swap control
showed that multi-agent's *fabrication rate* is measurably lower than
single-call — **1/10 vs 3/10** across independent checks. The broader
"is multi-agent smarter overall" question is still open because the
pairwise judge saturated on position bias in a second run. Next step
whenever we return to it: absolute-scoring judge instead of pairwise.

Details and the full four-round history are in prior commits and the
old README (in git history).

---

## Picking this back up

1. Read this file — it's the ground truth as of the last push.
2. Check `HANDOFF.md` in the repo root — session-level details on
   what was in progress at the end of the previous work day.
3. Sanity-check: `git status` (uncommitted work), `curl
   http://127.0.0.1:8000/api/connectors` (is the server up).
4. Standing rule: every `push` rewrites this README as a full state
   snapshot, not just the changed diff. Keep that going.
