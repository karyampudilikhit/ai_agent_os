"""Routes for the thin API slice (README Phase 9).

One real endpoint: validate an idea through IdeaValidationEmployee. A
fresh Pipeline is built per request rather than shared/cached — this is
the MVP-thin version; connection pooling / a shared pipeline instance is
a later concern once there's real traffic to justify it.
"""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, HTTPException

from backend.app.api.schemas import (
    AddMemberRequest,
    ChatRequest,
    ChatResponse,
    CompanyCreateRequest,
    CompanyListResponse,
    CompanyResponse,
    CompanyUpdateRequest,
    DesignTeamRequest,
    EmployeeCreateRequest,
    EmployeeListResponse,
    EmployeeResponse,
    EmployeeUpdateRequest,
    HireEmployeeRequest,
    HistoryEntry,
    HistoryResponse,
    HTTPToolListResponse,
    HTTPToolResponse,
    HTTPToolSpec,
    MCPConnectionResponse,
    MCPConnectionSpec,
    MCPListResponse,
    MCPToolInfo,
    OrchestrateRequest,
    OrchestrateResponse,
    RunTaskRequest,
    RunTaskResponse,
    SessionCreateResponse,
    SessionSummary,
    TeamListResponse,
    TeamMemberSpec,
    TeamMemberSummary,
    ValidateIdeaRequest,
    ValidateIdeaResponse,
)
from backend.app.employees import progress_store
from backend.app.employees.chat_intent import ChatIntentClassifier
from backend.app.employees.employee_coordinator import EmployeeCoordinator
from backend.app.employees.employee_spawner import EmployeeSpawner
from backend.app.employees.idea_validation_employee import IdeaValidationEmployee
from backend.app.employees.memory_store import EmployeeMemoryStore
from backend.app.employees.supervisor import default_supervisor_spec
from backend.app.employees.team_store import TeamStore
from backend.app.employees.company_store import CompanyStoreError
from backend.app.employees.company_store import get_store as get_company_store
from backend.app.employees.employee_registry import EmployeeRegistryError
from backend.app.employees.employee_registry import get_registry as get_employee_registry
from backend.app.tools.http_tool_store import HTTPToolStoreError
from backend.app.tools.http_tool_store import get_store as get_http_tool_store
from backend.app.tools.mcp_client import get_registry as get_mcp_registry
from backend.app.tools.mcp_store import get_store as get_mcp_store
from backend.app.main import _build_adapter, _load_config
from backend.app.orchestrator.orchestrate import orchestrate
from backend.app.orchestrator.pipeline_controller import Pipeline
from backend.app.orchestrator.synthesis import SynthesisEngine
from backend.app.critique.critique_agent import CritiqueEngine

router = APIRouter()

DEFAULT_MODEL = "gpt-oss:120b-cloud"


def _build_pipeline() -> Pipeline:
    adapter = _build_adapter(model=DEFAULT_MODEL, use_mock=False)
    config = _load_config()
    return Pipeline(
        model_adapter=adapter,
        config=config,
        synthesis_engine=SynthesisEngine(adapter, max_tokens=5000),
        critique_engine=CritiqueEngine(adapter, max_tokens=4000),
    )


@router.post("/validate", response_model=ValidateIdeaResponse)
def validate_idea(req: ValidateIdeaRequest) -> ValidateIdeaResponse:
    employee_id = req.employee_id or f"oneoff_{uuid.uuid4().hex[:8]}"
    pipeline = _build_pipeline()
    employee = IdeaValidationEmployee(employee_id=employee_id, pipeline=pipeline)

    try:
        result = employee.run_task(req.idea)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Validation failed: {exc}") from exc

    if not result.get("output"):
        raise HTTPException(status_code=502, detail="Pipeline produced no output.")

    critique = result.get("critique") or {}
    return ValidateIdeaResponse(
        employee_id=employee_id,
        verdict=result["output"],
        completeness_score=critique.get("completeness_score"),
        fabricated_claims=critique.get("fabricated_claims") or [],
        over_engineered=critique.get("over_engineered"),
        was_refined=bool(result.get("was_refined")),
    )


@router.post("/orchestrate", response_model=OrchestrateResponse)
def orchestrate_prompt(req: OrchestrateRequest) -> OrchestrateResponse:
    """The real Vision AI flow: user prompt -> supervisor decides team-
    or-not -> if team, spawn on-the-fly employees -> they collaborate
    (Plan B) -> merged deliverable back.

    Blocks the request for the whole run (multiple minutes for a team).
    Streaming/queue-based execution is a Phase 9-follow-up, not MVP.
    """
    pipeline = _build_pipeline()
    try:
        result = orchestrate(
            prompt=req.prompt,
            pipeline=pipeline,
            session_id=req.session_id,
            force_team=req.force_team,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Orchestrate failed: {exc}") from exc

    return OrchestrateResponse(
        session_id=result["session_id"],
        mode=result["mode"],
        team=[TeamMemberSummary(**m) for m in result.get("team", [])],
        final_output=result.get("final_output") or "",
    )


@router.get("/history/{employee_id}", response_model=HistoryResponse)
def get_history(employee_id: str) -> HistoryResponse:
    store = EmployeeMemoryStore(employee_id)
    entries = [
        HistoryEntry(
            timestamp=e["timestamp"],
            task=e["task"],
            summary=e["summary"],
            completeness_score=e.get("completeness_score"),
        )
        for e in store.all_entries()
    ]
    return HistoryResponse(employee_id=employee_id, entries=entries)


# --- Playground / session endpoints ---
#
# The team-persistence flow the playground UI needs. A "session" owns a
# team spec (list of {role, mandate}); the user can design one from a
# prompt, hand-edit it, add/delete members, and later run tasks against
# the current roster. Team spec lives in TeamStore; each task run
# instantiates fresh DynamicEmployees from that spec via the spawner.


@router.get("/sessions", response_model=List[SessionSummary])
def list_sessions() -> List[SessionSummary]:
    return [SessionSummary(**s) for s in TeamStore.list_sessions()]


@router.post("/sessions", response_model=SessionCreateResponse)
def create_session() -> SessionCreateResponse:
    """Create a fresh Unit. Every Unit gets a default Supervisor hired
    automatically — the Supervisor receives all tasks from the user and
    delegates to specialists. This is enforced by TeamStore too, but
    doing it here means the Supervisor is present the very first time
    the UI reads the Unit's roster."""
    session_id = TeamStore.new_session_id()
    store = TeamStore(session_id)
    sup = default_supervisor_spec()
    store.add_member(sup["role"], sup["mandate"], is_supervisor=True)
    return SessionCreateResponse(session_id=session_id)


@router.get("/sessions/{session_id}/team", response_model=TeamListResponse)
def get_team(session_id: str) -> TeamListResponse:
    store = TeamStore(session_id)
    return TeamListResponse(
        session_id=session_id,
        members=[TeamMemberSpec(**m) for m in store.members()],
        created_at=store.created_at(),
    )


@router.post("/sessions/{session_id}/team/design", response_model=TeamListResponse)
def design_team(session_id: str, req: DesignTeamRequest) -> TeamListResponse:
    """Ask the spawner to design a team of SPECIALISTS from a prompt.
    The Supervisor is preserved automatically — set_members() re-inserts
    the existing Supervisor if the designed list doesn't include one,
    and Units enforce having exactly one Supervisor at all times."""
    adapter = _build_adapter(model=DEFAULT_MODEL, use_mock=False)
    spawner = EmployeeSpawner(model_adapter=adapter)
    designed = spawner.design_team(req.prompt)

    store = TeamStore(session_id)
    # Make sure a Supervisor is present. If this Unit somehow doesn't
    # have one yet (older sessions created before the Supervisor was a
    # thing), hire one now — never leave a Unit without one.
    if not store.supervisor():
        sup = default_supervisor_spec()
        store.add_member(sup["role"], sup["mandate"], is_supervisor=True)

    if req.replace:
        store.set_members(designed)  # set_members preserves the Supervisor
    else:
        for m in designed:
            store.add_member(m["role"], m["mandate"])

    return TeamListResponse(
        session_id=session_id,
        members=[TeamMemberSpec(**m) for m in store.members()],
        created_at=store.created_at(),
    )


@router.post("/sessions/{session_id}/team/members", response_model=TeamListResponse)
def add_team_member(session_id: str, req: AddMemberRequest) -> TeamListResponse:
    store = TeamStore(session_id)
    store.add_member(req.role, req.mandate)
    return TeamListResponse(
        session_id=session_id,
        members=[TeamMemberSpec(**m) for m in store.members()],
        created_at=store.created_at(),
    )


@router.delete("/sessions/{session_id}/team/members/{role}", response_model=TeamListResponse)
def remove_team_member(session_id: str, role: str) -> TeamListResponse:
    store = TeamStore(session_id)
    removed = store.remove_member(role)
    if not removed:
        raise HTTPException(status_code=404, detail=f"No team member with role '{role}' in this session.")
    return TeamListResponse(
        session_id=session_id,
        members=[TeamMemberSpec(**m) for m in store.members()],
        created_at=store.created_at(),
    )


@router.post("/sessions/{session_id}/run", response_model=RunTaskResponse)
def run_task_on_team(session_id: str, req: RunTaskRequest) -> RunTaskResponse:
    """Run a task on this Unit through the Supervisor pattern:
      1. Supervisor designs a delegation plan (who does what).
      2. Specialists execute their assigned sub-tasks in order.
      3. Supervisor synthesizes the final deliverable.

    If the Unit somehow has no specialists yet, the Supervisor handles
    the task alone."""
    store = TeamStore(session_id)
    if not store.members():
        raise HTTPException(status_code=400, detail="This Unit has no team yet. Design one first, or add members manually.")

    supervisor_spec = store.supervisor()
    if not supervisor_spec:
        # Backfill for old sessions created before Supervisors existed
        sup = default_supervisor_spec()
        supervisor_spec = store.add_member(sup["role"], sup["mandate"], is_supervisor=True)

    specialist_specs = store.specialists()

    pipeline = _build_pipeline()
    spawner = EmployeeSpawner(model_adapter=pipeline.adapter)
    supervisor_employee = spawner.instantiate(
        [supervisor_spec], pipeline=pipeline, session_id=session_id
    )[0]
    specialists = spawner.instantiate(
        specialist_specs, pipeline=pipeline, session_id=session_id
    )
    coordinator = EmployeeCoordinator(pipeline=pipeline)

    # Progress tracking lists SPECIALISTS (the Supervisor's planning
    # and synthesis phases show up as separate phase events, not roles).
    progress_store.start_run(session_id, [e.role for e in specialists])
    try:
        result = coordinator.run_with_supervisor(
            prompt=req.task,
            supervisor=supervisor_employee,
            specialists=specialists,
            on_planning=lambda: progress_store.mark_synthesizing(session_id),  # reuses "synthesizing" phase for the planning banner
            on_role_working=lambda role: progress_store.mark_role_working(session_id, role),
            on_role_done=lambda role, meta: progress_store.mark_role_done(session_id, role, meta),
            on_synthesizing=lambda: progress_store.mark_synthesizing(session_id),
        )
        progress_store.mark_complete(session_id)
    except Exception as exc:  # noqa: BLE001
        progress_store.mark_error(session_id, str(exc))
        raise HTTPException(status_code=502, detail=f"Task run failed: {exc}") from exc

    return RunTaskResponse(
        session_id=session_id,
        task=req.task,
        team=[TeamMemberSummary(**m) for m in result.get("team", [])],
        final_output=result.get("final_output") or "",
    )


@router.get("/sessions/{session_id}/progress")
def get_progress(session_id: str):
    """Cheap poll target for the UI — how far into the current run
    (if any) has this session gotten? Returns {phase, current_role,
    completed: [...], team, error}."""
    return progress_store.get(session_id)


@router.post("/sessions/{session_id}/chat", response_model=ChatResponse)
def chat(session_id: str, req: ChatRequest) -> ChatResponse:
    """Route a chat message to the right action instead of dumping every
    message into the team as a task. Team-management intents (add /
    remove / modify / design / clear) execute here inline. run_task
    returns the extracted task text and the frontend triggers /run
    separately so it can stream progress through the existing polling
    infrastructure — this endpoint stays fast and predictable.
    """
    adapter = _build_adapter(model=DEFAULT_MODEL, use_mock=False)
    classifier = ChatIntentClassifier(model_adapter=adapter)
    store = TeamStore(session_id)
    current_members = store.members()

    intent_data = classifier.classify(req.message, current_members)
    intent = intent_data.get("intent", "run_task")

    def _members_specs() -> List[TeamMemberSpec]:
        return [TeamMemberSpec(**m) for m in store.members()]

    if intent == "add_employee":
        role = (intent_data.get("role") or "").strip()
        mandate = (intent_data.get("mandate") or "").strip()
        if not role:
            return ChatResponse(session_id=session_id, intent="unclear",
                                reply="I couldn't figure out which role you wanted to add. Could you tell me the role name?",
                                team=_members_specs())
        if not mandate:
            mandate = f"Own the responsibilities of a {role} on this team."
        store.add_member(role, mandate)
        return ChatResponse(session_id=session_id, intent="add_employee",
                            reply=f"Added {role} to your team.",
                            team=_members_specs())

    if intent == "remove_employee":
        role = (intent_data.get("role") or "").strip()
        if not role:
            return ChatResponse(session_id=session_id, intent="unclear",
                                reply="I couldn't figure out which role to remove. Which employee did you mean?",
                                team=_members_specs())
        removed = store.remove_member(role)
        if not removed:
            # Try case-insensitive match against current roles
            match = next((m["role"] for m in current_members if m["role"].lower() == role.lower()), None)
            if match:
                store.remove_member(match)
                return ChatResponse(session_id=session_id, intent="remove_employee",
                                    reply=f"Removed {match} from your team.",
                                    team=_members_specs())
            return ChatResponse(session_id=session_id, intent="unclear",
                                reply=f"There's no employee called \"{role}\" on this team.",
                                team=_members_specs())
        return ChatResponse(session_id=session_id, intent="remove_employee",
                            reply=f"Removed {role} from your team.",
                            team=_members_specs())

    if intent == "modify_employee":
        role = (intent_data.get("role") or "").strip()
        new_mandate = (intent_data.get("new_mandate") or intent_data.get("mandate") or "").strip()
        if not role or not new_mandate:
            return ChatResponse(session_id=session_id, intent="unclear",
                                reply="Tell me which employee to modify and what their new job should be.",
                                team=_members_specs())
        match = next((m for m in current_members if m["role"].lower() == role.lower()), None)
        if not match:
            return ChatResponse(session_id=session_id, intent="unclear",
                                reply=f"There's no employee called \"{role}\" on this team.",
                                team=_members_specs())
        store.add_member(match["role"], new_mandate)  # add_member replaces on same role
        return ChatResponse(session_id=session_id, intent="modify_employee",
                            reply=f"Updated {match['role']}'s mandate.",
                            team=_members_specs())

    if intent == "clear_team":
        store.clear()
        return ChatResponse(session_id=session_id, intent="clear_team",
                            reply="Cleared the team. Tell me what you're working on and I'll design a new one.",
                            team=[])

    if intent == "design_team":
        prompt = (intent_data.get("prompt") or req.message).strip()
        spawner = EmployeeSpawner(model_adapter=adapter)
        designed = spawner.design_team(prompt)
        store.set_members(designed)
        specs = _members_specs()
        roles_line = ", ".join([m.role for m in specs])
        return ChatResponse(session_id=session_id, intent="design_team",
                            reply=f"Designed a fresh team of {len(specs)}: {roles_line}",
                            team=specs)

    # Default / run_task: return the extracted task text so the frontend
    # can hand it to /run and pick up progress polling from there.
    task_text = (intent_data.get("task") or req.message).strip()
    return ChatResponse(session_id=session_id, intent="run_task",
                        reply="On it — sending this to your team.",
                        team=_members_specs(),
                        task_to_run=task_text)


# --- MCP connectors: manage tool servers your employees can use ---
#
# Connections are shared across all Units for now (single-user local
# deployment). When we add auth + hosted, per-user scoping goes here.


def _conn_to_response(spec: dict) -> MCPConnectionResponse:
    """Sanitize a stored spec for the wire — never send env (secrets)."""
    return MCPConnectionResponse(
        name=spec["name"],
        transport=spec["transport"],
        command=spec.get("command"),
        args=list(spec.get("args") or []),
        url=spec.get("url"),
        enabled=spec.get("enabled", True),
        added_at=spec.get("added_at"),
    )


@router.get("/connectors", response_model=MCPListResponse)
def list_connectors() -> MCPListResponse:
    """Every configured MCP connection plus the union of tools currently
    reachable across them. Opening the servers to enumerate tools
    happens lazily on first request; if a server isn't reachable it
    just contributes zero tools (no error)."""
    store = get_mcp_store()
    registry = get_mcp_registry()
    conns = [_conn_to_response(c) for c in store.connections()]
    tools: List[MCPToolInfo] = []
    try:
        for t in registry.list_all_tools():
            tools.append(MCPToolInfo(
                qualified_name=t["qualified_name"],
                connection=t["connection"],
                tool=t["tool"],
                description=t.get("description") or "",
            ))
    except Exception as exc:  # noqa: BLE001
        # Never let a broken connection block the list endpoint
        pass
    return MCPListResponse(connections=conns, tools=tools)


@router.post("/connectors", response_model=MCPConnectionResponse)
def add_connector(spec: MCPConnectionSpec) -> MCPConnectionResponse:
    """Add or update a connector by name. Replaces an existing entry
    with the same name (name is the natural key)."""
    try:
        added = get_mcp_store().add(
            name=spec.name,
            transport=spec.transport,
            command=spec.command,
            args=spec.args,
            url=spec.url,
            env=spec.env,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_mcp_registry().reload()
    return _conn_to_response(added)


@router.delete("/connectors/{name}")
def remove_connector(name: str):
    if not get_mcp_store().remove(name):
        raise HTTPException(status_code=404, detail=f"No connector named {name!r}.")
    get_mcp_registry().reload()
    return {"removed": name}


@router.patch("/connectors/{name}")
def toggle_connector(name: str, enabled: bool):
    if not get_mcp_store().set_enabled(name, enabled):
        raise HTTPException(status_code=404, detail=f"No connector named {name!r}.")
    get_mcp_registry().reload()
    return {"name": name, "enabled": enabled}


# --- Custom HTTP tools: connect any API without an MCP server -------
#
# Option B: instead of requiring users to install / find an MCP server
# for every service, they can paste a curl-shaped spec (URL, method,
# auth, params) into the UI and it becomes a callable tool for their
# employees. The MCP planner sees these alongside MCP tools and picks
# from a unified list.


def _http_tool_to_response(spec: dict) -> HTTPToolResponse:
    """Sanitize a stored HTTP tool spec for the wire — never send
    auth token or static headers (may contain secrets)."""
    return HTTPToolResponse(
        name=spec["name"],
        description=spec.get("description", ""),
        method=spec["method"],
        url=spec["url"],
        parameters=[
            {
                "name": p["name"],
                "in": p["in"],
                "description": p.get("description", ""),
                "required": p.get("required", False),
            }
            for p in spec.get("parameters", [])
        ],
        enabled=spec.get("enabled", True),
        auth_type=(spec.get("auth") or {}).get("type", "none"),
    )


@router.get("/http-tools", response_model=HTTPToolListResponse)
def list_http_tools() -> HTTPToolListResponse:
    """Every user-defined HTTP tool. Secrets (tokens, basic-auth
    credentials, static headers) are stripped before returning."""
    tools = [_http_tool_to_response(t) for t in get_http_tool_store().list()]
    return HTTPToolListResponse(tools=tools)


@router.post("/http-tools", response_model=HTTPToolResponse)
def add_http_tool(spec: HTTPToolSpec) -> HTTPToolResponse:
    """Register a new HTTP tool. Store validates method / URL / auth
    shape and rejects malformed specs with a 400."""
    payload = spec.model_dump(by_alias=True)
    try:
        added = get_http_tool_store().add(payload)
    except HTTPToolStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _http_tool_to_response(added)


@router.delete("/http-tools/{name}")
def remove_http_tool(name: str):
    if not get_http_tool_store().delete(name):
        raise HTTPException(status_code=404, detail=f"No HTTP tool named {name!r}.")
    return {"removed": name}


@router.patch("/http-tools/{name}")
def toggle_http_tool(name: str, enabled: bool):
    try:
        get_http_tool_store().update(name, {"enabled": enabled})
    except HTTPToolStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"name": name, "enabled": enabled}


# --- Hierarchy: Employees + Companies (Phase 1) ---------------------
#
# Employee is now a first-class object with a persistent UUID (or a
# legacy `session_id__role_slug` id for migrated records). The same
# Employee can be hired into multiple Units. Company is the top-level
# container that owns Units — Phase 1 gives it structure, Phase 2 will
# add an active CEO Manager LLM role.


def _employee_to_response(spec: dict) -> EmployeeResponse:
    return EmployeeResponse(
        id=spec["id"],
        role=spec.get("role", ""),
        mandate=spec.get("mandate", ""),
        avatar_seed=spec.get("avatar_seed", ""),
        tags=list(spec.get("tags", [])),
        is_supervisor=bool(spec.get("is_supervisor", False)),
        created_at=spec.get("created_at"),
    )


@router.get("/employees", response_model=EmployeeListResponse)
def list_employees() -> EmployeeListResponse:
    """Every Employee across all Units — the shared identity pool."""
    return EmployeeListResponse(
        employees=[_employee_to_response(e) for e in get_employee_registry().list()]
    )


@router.get("/employees/{employee_id}", response_model=EmployeeResponse)
def get_employee(employee_id: str) -> EmployeeResponse:
    spec = get_employee_registry().get(employee_id)
    if not spec:
        raise HTTPException(status_code=404, detail=f"No Employee with id {employee_id!r}.")
    return _employee_to_response(spec)


@router.post("/employees", response_model=EmployeeResponse)
def create_employee(req: EmployeeCreateRequest) -> EmployeeResponse:
    """Create an Employee without attaching to any Unit. Useful when a
    user wants to build a roster before assigning to projects."""
    try:
        spec = get_employee_registry().create(
            role=req.role,
            mandate=req.mandate,
            is_supervisor=req.is_supervisor,
            tags=req.tags,
        )
    except EmployeeRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _employee_to_response(spec)


@router.patch("/employees/{employee_id}", response_model=EmployeeResponse)
def update_employee(employee_id: str, req: EmployeeUpdateRequest) -> EmployeeResponse:
    try:
        spec = get_employee_registry().update(
            employee_id, req.model_dump(exclude_none=True)
        )
    except EmployeeRegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _employee_to_response(spec)


@router.delete("/employees/{employee_id}")
def delete_employee(employee_id: str):
    """Remove an Employee from the registry entirely. Does NOT remove
    them from any Unit's roster — those references will just fail to
    resolve on next Unit read and get filtered out."""
    if not get_employee_registry().delete(employee_id):
        raise HTTPException(status_code=404, detail=f"No Employee with id {employee_id!r}.")
    return {"removed": employee_id}


@router.post("/units/{unit_id}/hire", response_model=TeamListResponse)
def hire_into_unit(unit_id: str, req: HireEmployeeRequest) -> TeamListResponse:
    """Add an EXISTING registry Employee to a Unit. This is the
    Employee-in-many-places win: same identity, same memory, now
    working in a second Unit too."""
    store = TeamStore(unit_id)
    try:
        store.hire(req.employee_id, is_supervisor=req.is_supervisor)
    except EmployeeRegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return TeamListResponse(
        session_id=unit_id,
        members=[TeamMemberSpec(**m) for m in store.members()],
    )


def _company_to_response(spec: dict) -> CompanyResponse:
    return CompanyResponse(
        id=spec["id"],
        name=spec.get("name", ""),
        purpose=spec.get("purpose"),
        unit_ids=list(spec.get("unit_ids", [])),
        created_at=spec.get("created_at"),
    )


@router.get("/companies", response_model=CompanyListResponse)
def list_companies() -> CompanyListResponse:
    return CompanyListResponse(
        companies=[_company_to_response(c) for c in get_company_store().list()]
    )


@router.get("/companies/{company_id}", response_model=CompanyResponse)
def get_company(company_id: str) -> CompanyResponse:
    spec = get_company_store().get(company_id)
    if not spec:
        raise HTTPException(status_code=404, detail=f"No Company with id {company_id!r}.")
    return _company_to_response(spec)


@router.post("/companies", response_model=CompanyResponse)
def create_company(req: CompanyCreateRequest) -> CompanyResponse:
    try:
        spec = get_company_store().create(name=req.name, purpose=req.purpose)
    except CompanyStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _company_to_response(spec)


@router.patch("/companies/{company_id}", response_model=CompanyResponse)
def update_company(company_id: str, req: CompanyUpdateRequest) -> CompanyResponse:
    try:
        spec = get_company_store().update(company_id, req.model_dump(exclude_none=True))
    except CompanyStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _company_to_response(spec)


@router.delete("/companies/{company_id}")
def delete_company(company_id: str):
    try:
        removed = get_company_store().delete(company_id)
    except CompanyStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"No Company with id {company_id!r}.")
    return {"removed": company_id}


@router.post("/companies/{company_id}/units/{unit_id}", response_model=CompanyResponse)
def attach_unit_to_company(company_id: str, unit_id: str) -> CompanyResponse:
    """Attach a Unit to a Company. Also writes company_id back to the
    Unit's TeamStore so the linkage is bidirectional."""
    try:
        spec = get_company_store().add_unit(company_id, unit_id)
    except CompanyStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    TeamStore(unit_id).set_metadata(company_id=company_id)
    return _company_to_response(spec)


@router.delete("/companies/{company_id}/units/{unit_id}")
def detach_unit_from_company(company_id: str, unit_id: str):
    if not get_company_store().remove_unit(company_id, unit_id):
        raise HTTPException(
            status_code=404,
            detail=f"Unit {unit_id!r} not attached to Company {company_id!r}.",
        )
    return {"detached": unit_id, "from": company_id}
