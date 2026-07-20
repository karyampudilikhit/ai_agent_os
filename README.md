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
containers are active with their own Managers — is now half-shipped:
Employee-first-class ✅, Unit-with-Manager ✅ (Supervisor pattern),
Company-with-CEO ⏳ (structure yes, active Manager Phase 2).

---

## Where things stand

| | |
|---|---|
| **Foundation** | Phases 1-4 committed on `feat/phase-4-orchestration`: contracts, agent creation, orchestration engine, memory. |
| **Employee abstraction** | ✅ live — `Employee`, `DynamicEmployee`, per-employee memory, min/max tier verification floor. |
| **AI hierarchy — Phase 1** | ✅ live — Employees have persistent UUIDs in a shared registry; same Employee can be hired into multiple Units. Companies exist as structural containers with auto-created "Personal" default. v1 team files auto-migrate to v2 on first load. |
| **Supervisor pattern** | ✅ live — every Unit auto-hires a Supervisor. User only talks to the Supervisor. Supervisor plans delegation, dispatches sub-tasks per specialist, then synthesizes in one voice. |
| **Adaptive router** | ✅ live — team-first bias, 10/10 accuracy across trivial/verify/team-shaped tasks in the retune test. |
| **Chat with intent classification** | ✅ live — `add_employee`, `remove_employee`, `modify_employee`, `design_team`, `clear_team`, `run_task`. |
| **Playground UI** | ✅ live at `frontend_mvp/index.html` — crew.ai-style Studio Chat + Canvas + Output tabs + Edit/Connectors sidebar with live per-employee progress. |
| **Verification / no-fabrication** | ✅ live — critique/refine loop, measured working (multi-agent 1/10 vs single-call 3/10 in the independent-judge benchmarks). |
| **Tavily web search** | ✅ live — real search results injected into specialists' prompts when tasks are research-shaped. |
| **Read-only web fetch** | ✅ live — any URL mentioned in a task gets fetched (BeautifulSoup extract); top-2 Tavily URLs also get deep-read. |
| **Reddit read-only** | ✅ live — OAuth-backed reader for subreddit top posts and threads. Fires on `r/subreddit` mentions or reddit-shaped task language. Needs `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET`; silently disabled without them. |
| **MCP client layer** | ✅ live — persistent asyncio loop + registry with cached sessions + pre-flight tool planner. `POST /api/connectors` to add any MCP server; employees discover tools and call them automatically. |
| **Custom HTTP tools (Option B)** | ✅ live — user defines an HTTP endpoint spec (method, URL, auth, params) via `POST /api/http-tools`; the tool appears in the same planner as MCP tools, namespaced as `custom.<name>`. Auth: none / bearer / api_key_header / basic. Solves "connect to any API that has no MCP server yet." |
| **Notion via MCP** | ⏳ walkthrough documented, integration + `NOTION_TOKEN` not yet set up locally. |
| **Obsidian via HTTP tools** | ⏳ walkthrough documented, plugin + tools not yet registered locally. |
| **AI hierarchy — Phase 2** | ❌ multiple Teams per Unit + active CEO Manager LLM + UI hierarchy tree view. Next major build. |
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
├── supervisor.py                   SupervisorPlanner (design_delegation + synthesize) +
│                                   default_supervisor_spec() used on Unit creation.
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
├── memory_store.py                 Per-Employee JSON memory (last-N-entries recall).
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
   tools present the same interface to one planner. `MCPPlanner` does
   one cheap LLM call: *"given this task and these tools, which should
   you call?"*, then executes each call. HTTP tools are namespaced
   `custom.<name>`; MCP tools are `<server>.<tool>`.

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
```

Credentials live in `.env` (gitignored). MCP env-var secrets live in
`_mcp_connections.json` (also gitignored). HTTP tool specs incl. tokens
live in `.http_tools.json` (also gitignored).

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

**Companies (Phase 1 hierarchy):**
```
GET    /api/companies                         List Companies (default "Personal" always present)
GET    /api/companies/{id}                    One Company
POST   /api/companies                         Create a Company
PATCH  /api/companies/{id}                    Update name/purpose
DELETE /api/companies/{id}                    Delete (default "personal" refuses)
POST   /api/companies/{cid}/units/{uid}       Attach a Unit to a Company
DELETE /api/companies/{cid}/units/{uid}       Detach a Unit from a Company
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

Single-file Playground at `frontend_mvp/index.html`. Layout:

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

py -3 -m uvicorn backend.app.api.main:app --port 8000
```

Playground: [http://127.0.0.1:8000](http://127.0.0.1:8000).

**Ollama** (default LLM): the code targets `gpt-oss:120b-cloud` on a
local Ollama daemon.

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

---

## Picking this back up

1. Read this file — it's the ground truth as of the last push.
2. Check `HANDOFF.md` in the repo root — session-level details on
   what was in progress at the end of the previous work day.
3. Sanity-check: `git status` (uncommitted work), `curl
   http://127.0.0.1:8000/api/employees` (server up, hierarchy live).
4. Standing rule: every `push` rewrites this README as a full state
   snapshot, not just the changed diff. Keep that going.
