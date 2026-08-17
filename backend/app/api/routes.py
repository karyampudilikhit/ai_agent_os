"""Routes for the thin API slice (README Phase 9).

One real endpoint: validate an idea through IdeaValidationEmployee. A
fresh Pipeline is built per request rather than shared/cached — this is
the MVP-thin version; connection pooling / a shared pipeline instance is
a later concern once there's real traffic to justify it.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse

from backend.app.api.schemas import (
    AddMemberRequest,
    ChatRequest,
    ChatResponse,
    CompanyCreateRequest,
    CompanyListResponse,
    EvidenceClaim,
    CompanyResponse,
    CompanyRunRequest,
    CompanyRunResponse,
    CompanyRunUnitContribution,
    CompanyUpdateRequest,
    DesignTeamRequest,
    EmployeeCreateRequest,
    EmployeeListResponse,
    EmployeeResponse,
    EmployeeUpdateRequest,
    HierarchyAppliedUnit,
    HierarchyApplyRequest,
    HierarchyApplyResponse,
    HierarchyDesignRequest,
    HierarchyDesignResponse,
    HierarchyUnitSpec,
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
    RunStatusResponse,
    TeamMemberSpec,
    TeamMemberSummary,
    UniversalChatRequest,
    UniversalChatResponse,
    UniversalChatSideEffects,
    ValidateIdeaRequest,
    ValidateIdeaResponse,
)
from backend.app.employees import progress_store
from backend.app.employees.chat_intent import ChatIntentClassifier
from backend.app.employees.employee_coordinator import EmployeeCoordinator
from backend.app.employees.employee_spawner import EmployeeSpawner
from backend.app.employees.idea_validation_employee import IdeaValidationEmployee
from backend.app.employees.memory_store import EmployeeMemoryStore
from backend.app.employees.ceo_manager import CEOManager, default_ceo_spec
from backend.app.employees.hierarchy_designer import HierarchyDesigner
from backend.app.employees.supervisor import SupervisorPlanner, default_supervisor_spec
from backend.app.employees.team_store import TeamStore
from backend.app.employees.company_store import CompanyStoreError
from backend.app.employees.company_store import get_store as get_company_store
from backend.app.employees.employee_config import (
    EmployeeConfigError,
    resolve_with_provenance,
)
from backend.app.state import run_artifacts as _artifacts
from backend.app.employees.employee_registry import EmployeeRegistryError
from backend.app.employees.employee_registry import get_registry as get_employee_registry
from backend.app.tools.http_tool_store import HTTPToolStoreError
from backend.app.tools.http_tool_store import get_store as get_http_tool_store

from backend.app.actions.action_registry import get_registry as get_action_registry
from backend.app.actions.approval_queue import ApprovalQueueError, get_queue as get_approval_queue

from backend.app.chat.pending_proposal_store import get_store as get_pending_proposal_store
from backend.app.chat.universal_router import (
    UniversalChatRouter,
    build_state_snapshot,
    format_proposal_for_chat,
)
from backend.app.chat.async_runs import get_store as get_run_store, submit as submit_run
from backend.app.chat.clarifier import (
    Clarifier,
    enrich_task_with_qa,
    format_questions_for_chat,
)
from backend.app.chat.clarification_store import get_store as get_clarification_store
from backend.app.memory.memory_manager import get_memory_manager
from backend.app.chat.plan_store import PlanStore, get_store as get_plan_store
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

    # Count this run against the Unit for the "most-used Units" metric.
    # Bumped up-front so a failed run still shows as usage — that's the
    # honest view (workload attempted, not workload succeeded).
    store.bump_run_count()

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

    # Same run record the async path creates, so this endpoint's work
    # shows up in history and in the clarifier's memory.
    sync_run_id = get_run_store().create(
        intent="run_task_unit", session_id=session_id, task=req.task,
    )
    get_run_store().set_running(sync_run_id)

    # Progress tracking lists SPECIALISTS (the Supervisor's planning
    # and synthesis phases show up as separate phase events, not roles).
    progress_store.start_run(session_id, [e.role for e in specialists])
    # Scope the deliverable gate and the artifact registry to THIS run,
    # exactly as the async path does. This endpoint used to do neither,
    # so every check that asks "what did this run actually do" saw the
    # whole process history instead.
    _sync_started = time.time()
    _artifacts.set_current_run(sync_run_id)
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
        get_run_store().set_failed(sync_run_id, str(exc))
        raise HTTPException(status_code=502, detail=f"Task run failed: {exc}") from exc

    final_output = result.get("final_output") or ""
    evidence = result.get("evidence", [])

    # Gate the deliverable, same as the async path.
    #
    # This endpoint shipped ungated until now, which meant every check
    # the product depends on -- compute ran, numbers trace to what the
    # code printed, a ranking was actually sorted -- applied only to
    # tasks started from chat. A guard that covers one of two entry
    # points is a guard with a documented way around it, and the way
    # around it was the plain REST API.
    from backend.app.chat.async_runs import DeliverableBlocked
    try:
        final_output = _gate_deliverable(final_output, req.task, since=_sync_started)
    except DeliverableBlocked as exc:
        get_run_store().set_failed(sync_run_id, str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Record in RunStore like the async path does. Without this, work
    # started here never appeared in "Past outputs" and was invisible to
    # the clarifier's recent-deliverables memory — the same work, simply
    # forgotten because of which endpoint kicked it off.
    get_run_store().set_done(sync_run_id, final_output, evidence)

    return RunTaskResponse(
        session_id=session_id,
        task=req.task,
        team=[TeamMemberSummary(**m) for m in result.get("team", [])],
        final_output=final_output,
        evidence=[EvidenceClaim(**c) for c in evidence],
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
        config=spec.get("config") or {},
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
            config=req.config.model_dump(exclude_unset=True) if req.config else None,
        )
    except EmployeeConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except EmployeeRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _employee_to_response(spec)


@router.patch("/employees/{employee_id}", response_model=EmployeeResponse)
def update_employee(employee_id: str, req: EmployeeUpdateRequest) -> EmployeeResponse:
    # exclude_unset, NOT exclude_none. An explicit null means "clear this
    # setting back to inherited", and exclude_none cannot tell that apart
    # from "field not sent" -- so a founder could set a value but never
    # un-set it. The flat-field loop in EmployeeRegistry.update already
    # skips None, so this does not change role/mandate/tags behaviour.
    patch = req.model_dump(exclude_unset=True)
    try:
        spec = get_employee_registry().update(employee_id, patch)
    except EmployeeConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except EmployeeRegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _employee_to_response(spec)


@router.get("/build")
def build_info() -> Dict[str, Any]:
    """When the app shell on disk was last written.

    The UI shows this so a stale browser cache is visible at a glance.
    Twice now a shipped feature was reported as "nothing happens" purely
    because the tab was running HTML from before the feature existed, and
    there was no way to tell that apart from a real bug by looking at it.
    """
    import time as _time
    from backend.app.utils.paths import repo_root
    shell = os.path.join(repo_root(), "frontend_mvp", "app", "index.html")
    try:
        stat = os.stat(shell)
        return {
            "built_at": _time.strftime("%H:%M:%S", _time.localtime(stat.st_mtime)),
            "built_at_epoch": stat.st_mtime,
            "bytes": stat.st_size,
        }
    except OSError as exc:
        logger.warning("could not stat app shell: %s", exc)
        return {"built_at": "unknown", "built_at_epoch": 0, "bytes": 0}


@router.get("/tools")
def list_all_tools() -> Dict[str, Any]:
    """Every tool an employee can reach, grouped by where it comes from.

    Three namespaces present one interface to the planner -- built-in
    actions, MCP servers the founder connected, and their own HTTP tools
    -- and until now there was no way to SEE that union. The founder
    could not answer "what can this employee actually do", which is the
    first question anyone asks about an AI worker.

    Availability is process-wide today: every employee reaches the same
    registry. Reported honestly as such rather than implying a per-role
    catalogue that does not exist yet.
    """
    from backend.app.tools.tool_registry import get_registry as _tools
    try:
        tools = _tools().list_tools(for_planner=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool catalogue unavailable: %s", exc)
        tools = []

    groups: Dict[str, List[Dict[str, Any]]] = {"action": [], "mcp": [], "custom": []}
    for t in tools:
        qname = str(t.get("qualified_name") or "")
        ns = qname.split(".", 1)[0] if "." in qname else ""
        bucket = "action" if ns == "action" else ("custom" if ns == "custom" else "mcp")
        schema = t.get("input_schema") or {}
        # ActionRegistry encodes the approval requirement as a
        # "[MUTATING — asks approval] " prefix on the description rather
        # than a field, because that string is written FOR the model.
        # Read it back out here so the UI can badge it, and strip both
        # prefixes so the founder sees a clean sentence.
        desc = str(t.get("description") or "")
        mutating = bool(t.get("mutating")) or desc.startswith("[MUTATING")
        for prefix in ("[MUTATING — asks approval] ", "[MUTATING - asks approval] ", "[READ] "):
            if desc.startswith(prefix):
                desc = desc[len(prefix):]
                break
        groups[bucket].append({
            "qualified_name": qname,
            "name": qname.split(".", 1)[-1],
            "description": desc,
            "params": list((schema.get("properties") or {}).keys())[:12],
            "mutating": mutating,
        })
    for bucket in groups.values():
        bucket.sort(key=lambda t: t["qualified_name"])
    return {
        "groups": groups,
        "total": sum(len(v) for v in groups.values()),
        "scope": "process-wide — every employee currently reaches the same catalogue",
    }


@router.get("/employees/{employee_id}/system-prompt")
def employee_system_prompt(employee_id: str) -> Dict[str, Any]:
    """The system prompt this employee ACTUALLY runs on.

    Rendered by the same `build_objective` the real run uses, with a
    placeholder task, rather than reconstructed in the frontend. A copy
    of the prompt maintained in JavaScript would drift from the real one
    the first time either changed, and the founder would be editing a
    fiction.
    """
    spec = get_employee_registry().get(employee_id)
    if not spec:
        raise HTTPException(status_code=404, detail=f"No Employee with id {employee_id!r}.")

    cfg = spec.get("config") or {}
    try:
        from backend.app.employees.dynamic_employee import DynamicEmployee
        from backend.app.employees.employee_config import resolve

        class _NoMemory:
            def relevant_context(self, task): return ""
            def record(self, task, result): pass

        emp = DynamicEmployee(
            employee_id=employee_id,
            role=spec.get("role", ""),
            mandate=spec.get("mandate", ""),
            pipeline=None,
            memory_store=_NoMemory(),
            config=resolve(spec),
        )
        rendered = emp.build_objective("{task}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not render prompt for %s: %s", employee_id, exc)
        raise HTTPException(status_code=500, detail=f"Could not render prompt: {exc}") from exc

    return {
        "employee_id": employee_id,
        "role": spec.get("role", ""),
        "rendered": rendered,
        "is_custom": bool(cfg.get("prompt_override")),
        "override": cfg.get("prompt_override"),
        "chars": len(rendered),
    }


@router.get("/employees/{employee_id}/effective-config")
def employee_effective_config(employee_id: str) -> Dict[str, Any]:
    """Every setting this employee actually runs with, and WHERE each
    came from — default, role template, or set on the employee.

    Without provenance an Advanced panel is a wall of blank boxes: a
    founder cannot tell an unset field from one deliberately set to the
    same value as the default, nor see which employees are inheriting a
    desk-wide rule.
    """
    spec = get_employee_registry().get(employee_id)
    if not spec:
        raise HTTPException(status_code=404, detail=f"No Employee with id {employee_id!r}.")
    return {"employee_id": employee_id, "settings": resolve_with_provenance(spec)}


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
        ceo_employee_id=spec.get("ceo_employee_id"),
        created_at=spec.get("created_at"),
    )


def _ensure_ceo(company: dict) -> dict:
    """Backfill a CEO on old Companies that were created before Phase 3a.
    Returns the (possibly-updated) company spec. Idempotent."""
    if company.get("ceo_employee_id"):
        return company
    registry = get_employee_registry()
    spec = default_ceo_spec(company_name=company.get("name"))
    ceo = registry.create(
        role=spec["role"], mandate=spec["mandate"], is_supervisor=False,
        tags=["ceo", f"company:{company['id']}"],
    )
    updated = get_company_store().set_ceo(company["id"], ceo["id"])
    return updated


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
    """Create a Company AND auto-hire its CEO (Phase 3a). Mirrors how
    a new Unit auto-hires a Supervisor. The CEO is a real Employee in
    the registry — persistent id, memory, appears in the org tree."""
    try:
        spec = get_company_store().create(name=req.name, purpose=req.purpose)
    except CompanyStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Auto-hire the CEO right after Company creation.
    spec = _ensure_ceo(spec)
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


# --- Prompt-driven hierarchy design (Phase 3a) ----------------------
#
# Founder tells the CEO what the company is building. The CEO proposes
# a whole org chart in one call — Units + each Unit's initial roster.
# Nothing is persisted at design time; the founder reviews/edits, then
# POSTs the (possibly-modified) hierarchy to /apply_hierarchy to
# materialize it. Two-step so the founder is always in control of what
# actually gets created.


@router.post("/companies/{company_id}/design_hierarchy",
             response_model=HierarchyDesignResponse)
def design_company_hierarchy(
    company_id: str, req: HierarchyDesignRequest,
) -> HierarchyDesignResponse:
    """CEO proposes an org chart from a plain-English company
    description. Returns unsaved specs — nothing exists in the store
    yet. The founder reviews/edits before POSTing to apply."""
    company = get_company_store().get(company_id)
    if not company:
        raise HTTPException(
            status_code=404, detail=f"No Company with id {company_id!r}.",
        )
    company = _ensure_ceo(company)  # backfill CEO on legacy Companies

    pipeline = _build_pipeline()
    designer = HierarchyDesigner(model_adapter=pipeline.adapter)
    proposed = designer.design(req.description)

    return HierarchyDesignResponse(
        company_id=company_id,
        units=[
            HierarchyUnitSpec(
                name=u["name"],
                purpose=u["purpose"],
                specialists=[
                    TeamMemberSpec(role=s["role"], mandate=s["mandate"])
                    for s in u.get("specialists", [])
                ],
            )
            for u in proposed
        ],
    )


@router.post("/companies/{company_id}/apply_hierarchy",
             response_model=HierarchyApplyResponse)
def apply_company_hierarchy(
    company_id: str, req: HierarchyApplyRequest,
) -> HierarchyApplyResponse:
    """Materialize a proposed hierarchy. For each Unit in the request:
    creates a real Unit (TeamStore), attaches it to the Company, adds
    its Supervisor (auto), and hires the given specialists.

    Idempotent-ish: each call creates fresh Unit ids, so calling twice
    just doubles the Units. The frontend is responsible for the flow
    (propose → review → apply once)."""
    company = get_company_store().get(company_id)
    if not company:
        raise HTTPException(
            status_code=404, detail=f"No Company with id {company_id!r}.",
        )
    if not req.units:
        raise HTTPException(
            status_code=400,
            detail="apply_hierarchy needs at least one Unit to materialize.",
        )

    materialized: List[HierarchyAppliedUnit] = []
    for unit_spec in req.units:
        # 1. Fresh Unit id
        unit_id = TeamStore.new_session_id()
        store = TeamStore(unit_id)
        # 2. Metadata (name + purpose + parent Company)
        store.set_metadata(
            name=unit_spec.name,
            purpose=unit_spec.purpose,
            company_id=company_id,
        )
        # 3. Auto-hire the Supervisor. Pass the Unit's NAME (not its
        #    full purpose sentence) so the role reads cleanly, e.g.
        #    "Product Development Unit Supervisor" rather than the whole
        #    purpose ".Title()'d" into a giant string.
        sup_hint = unit_spec.name.strip()
        if sup_hint.lower().endswith(" unit"):
            sup_hint = sup_hint[: -len(" unit")].strip()
        sup = default_supervisor_spec(unit_purpose=sup_hint or None)
        store.add_member(sup["role"], sup["mandate"], is_supervisor=True)
        # 4. Add specialists from the (possibly-edited) spec
        for s in unit_spec.specialists:
            if s.is_supervisor:
                continue  # already added the Supervisor
            store.add_member(s.role, s.mandate)
        # 5. Attach Unit to the Company
        get_company_store().add_unit(company_id, unit_id)

        materialized.append(HierarchyAppliedUnit(
            unit_id=unit_id,
            name=unit_spec.name,
            specialist_count=len(unit_spec.specialists),
        ))

    return HierarchyApplyResponse(
        company_id=company_id,
        units=materialized,
    )


# --- Company-level task run (Phase 2 hierarchy — CEO Manager) -------
#
# The CEO takes a Company-wide task, decides which Units handle which
# piece, delegates to each Unit's Supervisor (which runs its own team
# of specialists via the Phase 1 flow), then synthesizes across Units
# into one Company-level deliverable.


@router.post("/companies/{company_id}/run", response_model=CompanyRunResponse)
def run_task_on_company(
    company_id: str, req: CompanyRunRequest
) -> CompanyRunResponse:
    """Run a task through a Company's CEO Manager. The CEO plans
    delegation across Units, each Unit runs its normal Supervisor
    flow, then the CEO synthesizes."""
    company = get_company_store().get(company_id)
    if not company:
        raise HTTPException(
            status_code=404, detail=f"No Company with id {company_id!r}."
        )
    unit_ids = list(company.get("unit_ids") or [])
    if not unit_ids:
        raise HTTPException(
            status_code=400,
            detail=(
                "This Company has no Units attached. Create Units first, "
                "attach them via POST /companies/{id}/units/{unit_id}."
            ),
        )

    # Build the units snapshot the CEO's planner needs: id + name +
    # purpose + roster. Silently skip any unit_ids whose team store
    # is empty (dangling references from a deleted Unit).
    units_for_ceo: List[Dict] = []
    for uid in unit_ids:
        store = TeamStore(uid)
        members = store.members()
        if not members:
            continue
        units_for_ceo.append({
            "unit_id": uid,
            "name": store.name() or uid,
            "purpose": store.purpose() or "",
            "members": [
                {
                    "role": m.get("role"),
                    "mandate": m.get("mandate"),
                    "is_supervisor": bool(m.get("is_supervisor")),
                }
                for m in members
            ],
        })
    if not units_for_ceo:
        raise HTTPException(
            status_code=400,
            detail=(
                "None of this Company's Units have staffed teams. "
                "Design a team on at least one Unit first."
            ),
        )

    # Build ONE pipeline shared across the CEO plan + all Unit runs,
    # so we're not paying per-Unit init cost. Each Unit still gets its
    # own EmployeeSpawner call (Supervisor + specialists instantiated
    # fresh per run to pick up any roster edits since last run).
    pipeline = _build_pipeline()
    coordinator = EmployeeCoordinator(pipeline=pipeline)
    spawner = EmployeeSpawner(model_adapter=pipeline.adapter)

    def _run_one_unit(uid: str, sub_task: str, unit_brief: Optional[str]):
        """The CEO hands us (unit_id, sub_task, unit_brief). We
        materialize that Unit's Supervisor + specialists and run its
        normal Phase 1 flow. Unit brief is prepended to the sub_task so
        the Unit's Supervisor sees the Company-level context above the
        specialist-level detail."""
        store = TeamStore(uid)
        # Count this against the Unit for the "most-used Units" metric
        # (CEO delegations count too — a Unit the CEO leans on hard
        # should show up as high-usage).
        store.bump_run_count()
        supervisor_spec = store.supervisor()
        if not supervisor_spec:
            sup = default_supervisor_spec()
            supervisor_spec = store.add_member(
                sup["role"], sup["mandate"], is_supervisor=True
            )
        specialist_specs = store.specialists()

        supervisor_employee = spawner.instantiate(
            [supervisor_spec], pipeline=pipeline, session_id=uid
        )[0]
        specialists = spawner.instantiate(
            specialist_specs, pipeline=pipeline, session_id=uid
        )

        enriched_prompt = sub_task
        if unit_brief:
            enriched_prompt = (
                f"CONTEXT FROM YOUR COMPANY'S CEO:\n{unit_brief}\n\n"
                f"YOUR UNIT'S ASSIGNMENT:\n{sub_task}"
            )

        return coordinator.run_with_supervisor(
            prompt=enriched_prompt,
            supervisor=supervisor_employee,
            specialists=specialists,
            # The founder's TRUE original Company-level prompt — not the
            # CEO's per-Unit sub_task (enriched_prompt above), which may
            # have paraphrased away any URLs or trigger words. Lets each
            # specialist's pre-flight heuristics (web-fetch, deep
            # research, Tavily) still fire on what the founder actually
            # typed, even after two layers of delegation rewriting.
            founder_task=req.task,
        )

    try:
        result = coordinator.run_with_ceo(
            prompt=req.task,
            company=company,
            units=units_for_ceo,
            unit_runner=_run_one_unit,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"Company run failed: {exc}"
        ) from exc

    return CompanyRunResponse(
        company_id=result.get("company_id") or company_id,
        company_name=result.get("company_name"),
        final_output=result.get("final_output") or "",
        plan=result.get("plan") or [],
        unit_contributions=[
            CompanyRunUnitContribution(
                unit_id=c["unit_id"],
                unit_name=c.get("unit_name"),
                output=c.get("output"),
                supervisor_role=c.get("supervisor_role"),
            )
            for c in result.get("unit_contributions", [])
        ],
        evidence=[EvidenceClaim(**c) for c in result.get("evidence", [])],
    )


# ---------------------------------------------------------------------------
# Action tools (send email, post Slack, write file …)
# ---------------------------------------------------------------------------

@router.get("/actions")
def list_action_tools():
    """Enumerate the built-in action tools available to specialists."""
    return {"tools": get_action_registry().list_tools()}


@router.get("/pending_actions")
def list_pending_actions(status: Optional[str] = None):
    """List pending / resolved actions. Filter by status if provided."""
    items = get_approval_queue().list(status=status)
    return {"items": items}


@router.post("/pending_actions/{action_id}/approve")
def approve_pending_action(action_id: str):
    """Mark a pending action approved AND execute it inline. Returns
    the updated record so the UI can show the result."""
    queue = get_approval_queue()
    record = queue.get(action_id)
    if not record:
        raise HTTPException(404, f"no pending action {action_id!r}")
    if record["status"] != "pending":
        raise HTTPException(409, f"action already {record['status']!r}, not pending")
    try:
        queue.set_status(action_id, "approved")
    except ApprovalQueueError as exc:
        raise HTTPException(400, str(exc))
    updated = get_action_registry().execute_now(action_id)
    return {"action": updated}


@router.post("/pending_actions/{action_id}/reject")
def reject_pending_action(action_id: str, reason: Optional[str] = None):
    queue = get_approval_queue()
    record = queue.get(action_id)
    if not record:
        raise HTTPException(404, f"no pending action {action_id!r}")
    if record["status"] != "pending":
        raise HTTPException(409, f"action already {record['status']!r}, not pending")
    try:
        updated = queue.set_status(action_id, "rejected", error=reason or None)
    except ApprovalQueueError as exc:
        raise HTTPException(400, str(exc))
    return {"action": updated}


# ---------------------------------------------------------------------------
# Universal chat router (Phase 3b — prompt-first UI)
#
# One endpoint above Unit and Company altitudes. The client passes what
# it currently has selected; the router classifies the intent and does
# the right thing. No mode toggle, no wrong-path errors — the founder
# just types.
# ---------------------------------------------------------------------------

@router.post("/chat", response_model=UniversalChatResponse)
def universal_chat(req: UniversalChatRequest) -> UniversalChatResponse:
    pipeline = _build_pipeline()

    # Loud short-circuit: the router relies on a real LLM to classify
    # intents; MockAdapter can't. If we're on the mock, tell the user
    # in plain English instead of returning the generic "not sure what
    # you meant" fallback (which they interpret as "the AI is dumb").
    from backend.app.main import MockAdapter
    if isinstance(pipeline.adapter, MockAdapter):
        return UniversalChatResponse(
            intent="casual_chat",
            reply=(
                "Ollama isn't running — Vision AI is on the mock model, so "
                "nothing you type will do real work. Open a terminal and run "
                "`ollama serve`, then send your message again."
            ),
            side_effects=UniversalChatSideEffects(),
        )

    router_llm = UniversalChatRouter(model_adapter=pipeline.adapter)
    proposal_store = get_pending_proposal_store()
    company_store = get_company_store()
    clar_store = get_clarification_store()
    clar_key = clar_store.key_for(
        company_id=req.current_company_id, session_id=req.current_session_id,
    )

    # --- build the state snapshot the router needs to classify ---
    current_company = None
    if req.current_company_id:
        current_company = company_store.get(req.current_company_id)
    current_members: List[Dict[str, Any]] = []
    if req.current_session_id:
        try:
            store = TeamStore(req.current_session_id)
            current_members = list(store.members())
        except Exception:
            current_members = []
    known_companies = company_store.list()
    pending = None
    if current_company:
        pending = proposal_store.get(current_company["id"])

    # ================================================================
    # PENDING CLARIFICATION SHORT-CIRCUIT
    # If we're mid-clarification for this context, treat the founder's
    # message as answers to the pending questions instead of a new
    # intent. Ask the clarifier if we now have enough — if so, enrich
    # the original task with the Q&A and dispatch the real run. If not,
    # return more questions (hard-capped at 10 total).
    # ================================================================
    pending_clar = clar_store.get(clar_key)
    if pending_clar:
        clarifier = Clarifier(model_adapter=pipeline.adapter)
        updated = clar_store.append_answer(clar_key, req.message)
        context = _build_clarifier_context(
            current_company=current_company,
            session_id=req.current_session_id,
            clar_key=clar_key,
            task=updated["task"],
        )
        result = clarifier.next_step(
            original_task=updated["task"],
            questions_asked=updated["questions"],
            answers=updated["answers"],
            context=context,
        )
        if result["ready"] or not result["questions"]:
            # Enough context — enrich the original task and dispatch.
            # Persist the (Q, A) pairs to cross-turn memory FIRST so
            # the next task in this session still sees them.
            clar_store.record_qa(
                clar_key, updated["questions"], updated["answers"],
            )
            enriched_task = enrich_task_with_qa(
                updated["task"], updated["questions"], updated["answers"],
            )
            saved_intent = updated["intent"]
            clar_store.clear(clar_key)
            return _dispatch_run(
                intent=saved_intent,
                task=enriched_task,
                current_company=current_company,
                current_session_id=req.current_session_id,
                mode=req.mode,
            )
        # More questions
        clar_store.append_questions(clar_key, result["questions"])
        reply = format_questions_for_chat(result["questions"], first_pass=False)
        return UniversalChatResponse(
            intent="clarify",
            reply=reply,
            side_effects=UniversalChatSideEffects(),
        )

    # Enumerate the current Company's Units by name so the router can
    # match "add to tech unit" against the actual Unit names.
    current_units_snapshot: List[Dict[str, Any]] = []
    if current_company:
        for uid in current_company.get("unit_ids") or []:
            try:
                ts = TeamStore(uid)
                current_units_snapshot.append({
                    "unit_id": uid, "name": ts.name() or uid,
                })
            except Exception:
                current_units_snapshot.append({"unit_id": uid, "name": uid})

    # ================================================================
    # PLAN -> WORK HANDOFF
    # In Work mode, if a plan was drafted earlier for this context and
    # the founder says an execution word ("go", "execute", "do it"),
    # run the stored plan directly — don't let the router misfile "go"
    # as casual chat.
    # ================================================================
    if req.mode == "work":
        plan_key = get_plan_store().key_for(
            company_id=req.current_company_id, session_id=req.current_session_id,
        )
        stored_plan = get_plan_store().get(plan_key)
        if stored_plan:
            msg_norm = req.message.strip().lower().rstrip("!.")
            EXEC_TRIGGERS = {
                "go", "execute", "execute it", "execute the plan", "do it",
                "run it", "run the plan", "proceed", "start", "ship it",
                "make it happen", "go ahead", "let's go", "lets go",
                "yes go", "run", "do the work", "begin",
            }
            if msg_norm in EXEC_TRIGGERS:
                exec_intent = (
                    "run_task_company"
                    if stored_plan.get("altitude") == "company"
                    else "run_task_unit"
                )
                return _dispatch_run(
                    intent=exec_intent,
                    task=stored_plan.get("task") or req.message,
                    current_company=current_company,
                    current_session_id=req.current_session_id,
                    mode="work",
                )

    snapshot = build_state_snapshot(
        current_company=current_company,
        current_unit_members=current_members,
        current_session_id=req.current_session_id,
        pending_proposal=pending,
        known_companies=known_companies,
        current_company_units=current_units_snapshot,
    )
    verdict = router_llm.classify(req.message, snapshot)
    intent = verdict.get("intent") or "casual_chat"
    side_effects = UniversalChatSideEffects()

    # ================================================================
    # PRE-RUN CLARIFICATION
    # For run_task_* intents, always give the clarifier a chance to ask
    # sharp questions first. If it decides the prompt is already tight,
    # it returns ready=true and we dispatch immediately.
    # ================================================================
    if intent in ("run_task_unit", "run_task_company"):
        task_str = str(verdict.get("task") or req.message).strip()
        # Remember this raw prompt so 'this / that / the plan' in the
        # NEXT turn can resolve via clarifier context.
        clar_store.record_prompt(clar_key, req.message)
        clarifier = Clarifier(model_adapter=pipeline.adapter)
        context = _build_clarifier_context(
            current_company=current_company,
            session_id=req.current_session_id,
            clar_key=clar_key,
            task=task_str,
        )
        initial = clarifier.initial_questions(task_str, context=context)
        if initial["questions"] and not initial["ready"]:
            clar_store.set(
                clar_key,
                task=task_str,
                intent=intent,
                questions=initial["questions"],
                answers=[],
                company_id=req.current_company_id,
                session_id=req.current_session_id,
            )
            reply = format_questions_for_chat(initial["questions"], first_pass=True)
            return UniversalChatResponse(
                intent="clarify",
                reply=reply,
                side_effects=side_effects,
            )
        # No clarification needed — fall through to the normal run path

    if intent == "casual_chat":
        return UniversalChatResponse(
            intent=intent,
            reply=str(verdict.get("reply") or "").strip() or "How can I help?",
            side_effects=side_effects,
        )

    # --- create_company (optionally chained with a hierarchy design) ---
    if intent == "create_company":
        name = str(verdict.get("name") or "").strip() or "New Company"
        purpose = str(verdict.get("purpose") or "").strip() or None
        description = str(verdict.get("description") or req.message).strip()
        auto_design = bool(verdict.get("auto_design", True))
        try:
            company = company_store.create(name=name, purpose=purpose)
            company = _ensure_ceo(company)
        except CompanyStoreError as exc:
            raise HTTPException(400, str(exc))
        side_effects.company_id = company["id"]
        side_effects.org_refreshed = True
        current_company = company

        if not auto_design:
            return UniversalChatResponse(
                intent=intent,
                reply=(
                    f"Company '{company['name']}' created and CEO hired. "
                    f"Tell me what you'd like the org chart to look like "
                    f"and the CEO will propose it."
                ),
                side_effects=side_effects,
            )
        # Fall through to design_hierarchy against the just-created Company
        intent = "design_hierarchy"
        verdict.setdefault("description", description)

    # --- design_hierarchy: CEO proposes ---
    if intent == "design_hierarchy":
        if not current_company:
            return UniversalChatResponse(
                intent=intent,
                reply=(
                    "There's no Company yet. Say something like "
                    "'build me an AI agency' first and I'll create one, "
                    "then design the org chart in the same turn."
                ),
                side_effects=side_effects,
            )
        description = str(verdict.get("description") or req.message).strip()
        designer = HierarchyDesigner(model_adapter=pipeline.adapter)
        proposed = designer.design(description)
        proposal_store.set(current_company["id"], proposed)
        side_effects.pending_proposal = {
            "company_id": current_company["id"],
            "units": proposed,
        }
        side_effects.company_id = current_company["id"]
        reply = format_proposal_for_chat(proposed)
        return UniversalChatResponse(intent=intent, reply=reply, side_effects=side_effects)

    # --- apply_proposal: materialize the pending proposal ---
    if intent == "apply_proposal":
        if not current_company:
            return UniversalChatResponse(
                intent=intent, reply="No Company selected.", side_effects=side_effects,
            )
        pending = proposal_store.get(current_company["id"])
        if not pending or not pending.get("units"):
            return UniversalChatResponse(
                intent=intent,
                reply=(
                    "Nothing to apply — I don't have a pending proposal "
                    "for this Company. Describe your company and I'll draft one."
                ),
                side_effects=side_effects,
            )
        applied_req = HierarchyApplyRequest(
            units=[
                HierarchyUnitSpec(
                    name=u["name"],
                    purpose=u.get("purpose", ""),
                    specialists=[
                        TeamMemberSpec(role=s.get("role", ""), mandate=s.get("mandate", ""))
                        for s in u.get("specialists", [])
                    ],
                )
                for u in pending["units"]
                if u.get("name")
            ]
        )
        result = apply_company_hierarchy(current_company["id"], applied_req)
        proposal_store.clear(current_company["id"])
        side_effects.applied_units = list(result.units)
        side_effects.company_id = current_company["id"]
        side_effects.org_refreshed = True
        n_specialists = sum(u.specialist_count for u in result.units)
        reply = (
            f"Done. Hired the CEO, {len(result.units)} Supervisor"
            f"{'s' if len(result.units) != 1 else ''}, and "
            f"{n_specialists} specialist{'s' if n_specialists != 1 else ''}. "
            f"Everyone shows up on the Org tab. Now tell me what to get done."
        )
        return UniversalChatResponse(intent=intent, reply=reply, side_effects=side_effects)

    # --- discard_proposal ---
    if intent == "discard_proposal":
        if current_company:
            proposal_store.clear(current_company["id"])
        return UniversalChatResponse(
            intent=intent,
            reply="Scrapped. Tell me what you'd like instead and the CEO will draft again.",
            side_effects=side_effects,
        )

    # --- add_unit: design ONE new Unit and attach it to the Company. ---
    if intent == "add_unit":
        if not current_company:
            return UniversalChatResponse(
                intent=intent,
                reply=(
                    "No Company yet — create one first (e.g. 'build me "
                    "an AI agency') and then ask me to add Units."
                ),
                side_effects=side_effects,
            )
        description = str(verdict.get("description") or req.message).strip()

        # Gather existing Units so the CEO doesn't duplicate scope.
        existing_specs: List[Dict[str, Any]] = []
        for uid in current_company.get("unit_ids") or []:
            try:
                s = TeamStore(uid)
                existing_specs.append({
                    "name": s.name() or uid,
                    "purpose": s.purpose() or "",
                })
            except Exception:
                existing_specs.append({"name": uid, "purpose": ""})

        designer = HierarchyDesigner(model_adapter=pipeline.adapter)
        unit_spec = designer.design_one_unit(description, existing_units=existing_specs)

        # Materialize immediately — the founder just said "add",
        # they're the confirmation.
        unit_id = TeamStore.new_session_id()
        store = TeamStore(unit_id)
        store.set_metadata(
            name=unit_spec["name"],
            purpose=unit_spec["purpose"],
            company_id=current_company["id"],
        )
        sup_hint = unit_spec["name"].strip()
        if sup_hint.lower().endswith(" unit"):
            sup_hint = sup_hint[: -len(" unit")].strip()
        sup = default_supervisor_spec(unit_purpose=sup_hint or None)
        store.add_member(sup["role"], sup["mandate"], is_supervisor=True)
        for s in unit_spec.get("specialists", []):
            store.add_member(s["role"], s["mandate"], is_supervisor=False)

        # Attach to Company
        try:
            company_store.add_unit(current_company["id"], unit_id)
        except Exception as exc:  # noqa: BLE001
            # Not fatal — the Unit is created either way; the founder can
            # re-attach manually if needed.
            print(f"[warn] add_unit failed for {unit_id}: {exc}")

        specialist_count = len(unit_spec.get("specialists", []))
        applied = HierarchyAppliedUnit(
            unit_id=unit_id, name=unit_spec["name"], specialist_count=specialist_count,
        )
        side_effects.applied_units = [applied]
        side_effects.session_id = unit_id
        side_effects.company_id = current_company["id"]
        side_effects.org_refreshed = True

        roster = ", ".join(s["role"] for s in unit_spec.get("specialists", []))
        reply = (
            f"Added **{unit_spec['name']}** — {unit_spec['purpose']}\n\n"
            f"Roster: Supervisor + {specialist_count} specialist"
            f"{'s' if specialist_count != 1 else ''} ({roster}). "
            f"They're loaded into Canvas now. Send them a task or ask me "
            f"to keep evolving the org."
        )
        return UniversalChatResponse(intent=intent, reply=reply, side_effects=side_effects)

    # --- add_employees_to_unit: attach specialists to an EXISTING Unit ---
    if intent == "add_employees_to_unit":
        if not current_company:
            return UniversalChatResponse(
                intent=intent,
                reply="No Company yet — create one first, then I can add employees to its Units.",
                side_effects=side_effects,
            )
        hint = str(verdict.get("target_unit_hint") or "").strip()
        specialists = verdict.get("specialists") or []
        if not isinstance(specialists, list) or not specialists:
            return UniversalChatResponse(
                intent=intent,
                reply="I couldn't figure out which roles to add. Say something like 'add a Frontend Engineer and a Backend Engineer to the Tech Unit'.",
                side_effects=side_effects,
            )
        target = _resolve_unit_by_hint(current_company, hint)
        if not target:
            unit_names = ", ".join(u["name"] for u in current_units_snapshot) or "(none yet)"
            return UniversalChatResponse(
                intent=intent,
                reply=(
                    f"I couldn't find a Unit matching {hint!r}. "
                    f"Existing Units: {unit_names}. Rephrase with the Unit's name."
                ),
                side_effects=side_effects,
            )
        target_unit_id, target_unit_name = target["unit_id"], target["name"]

        store = TeamStore(target_unit_id)
        added_roles: List[str] = []
        for s in specialists[:8]:  # sane cap
            if not isinstance(s, dict):
                continue
            role = str(s.get("role") or "").strip()
            mandate = str(s.get("mandate") or "").strip() or (
                f"Handle work assigned to the {target_unit_name} in the {role} area."
            )
            if not role:
                continue
            store.add_member(role, mandate, is_supervisor=False)
            added_roles.append(role)

        if not added_roles:
            return UniversalChatResponse(
                intent=intent,
                reply="Nothing to add — every role I saw was empty. Give me at least one role name.",
                side_effects=side_effects,
            )

        side_effects.session_id = target_unit_id
        side_effects.company_id = current_company["id"]
        side_effects.org_refreshed = True
        roster_str = ", ".join(added_roles)
        reply = (
            f"Added {len(added_roles)} to **{target_unit_name}**: {roster_str}. "
            f"They're loaded into Canvas."
        )
        return UniversalChatResponse(intent=intent, reply=reply, side_effects=side_effects)

    # --- delete_unit: detach + remove a Unit from the current Company ---
    if intent == "delete_unit":
        if not current_company:
            return UniversalChatResponse(
                intent=intent,
                reply="No Company selected — nothing to delete from.",
                side_effects=side_effects,
            )
        hint = str(verdict.get("target_unit_hint") or "").strip()
        target = _resolve_unit_by_hint(current_company, hint)
        if not target:
            unit_names = ", ".join(u["name"] for u in current_units_snapshot) or "(none)"
            return UniversalChatResponse(
                intent=intent,
                reply=(
                    f"Couldn't find a Unit matching {hint!r}. "
                    f"Existing: {unit_names}."
                ),
                side_effects=side_effects,
            )
        target_unit_id, target_unit_name = target["unit_id"], target["name"]
        # Detach from Company, then wipe the team-store JSON so the
        # Unit stops appearing in dropdowns and usage listings.
        try:
            company_store.remove_unit(current_company["id"], target_unit_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] remove_unit failed for {target_unit_id}: {exc}")
        try:
            path = TeamStore(target_unit_id).path
            if os.path.exists(path):
                os.remove(path)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] deleting team_data file failed for {target_unit_id}: {exc}")

        side_effects.company_id = current_company["id"]
        side_effects.org_refreshed = True
        return UniversalChatResponse(
            intent=intent,
            reply=f"Deleted **{target_unit_name}** and released its employees.",
            side_effects=side_effects,
        )

    # --- run_task_* dispatch (task may already be clarifier-enriched) ---
    if intent in ("run_task_company", "run_task_unit"):
        task_str = str(verdict.get("task") or req.message).strip()
        return _dispatch_run(
            intent=intent,
            task=task_str,
            current_company=current_company,
            current_session_id=req.current_session_id,
            mode=req.mode,
        )

    # Should never reach here — VALID_INTENTS enforces the set
    return _fallthrough_response(side_effects)


def _resolve_unit_by_hint(
    company: Dict[str, Any], hint: str,
) -> Optional[Dict[str, str]]:
    """Fuzzy-match a Unit inside a Company by a name substring.

    'tech' -> matches 'Tech Unit'; case-insensitive contains-check.
    Ranking (best -> worst):
      1. Exact name match (case-insensitive)
      2. Unit name that starts with the hint
      3. Any unit name containing the hint
    Returns {"unit_id", "name"} or None if no plausible match.
    """
    hint = (hint or "").strip().lower()
    if not hint:
        return None
    unit_ids = company.get("unit_ids") or []
    exact, prefix, contains = [], [], []
    for uid in unit_ids:
        try:
            ts = TeamStore(uid)
            name = ts.name() or uid
        except Exception:
            name = uid
        nlow = name.lower()
        # Also match against a "core" version of the hint (strip
        # trailing "unit" so "tech" and "tech unit" both hit "Tech Unit")
        core = hint
        if core.endswith(" unit"):
            core = core[: -len(" unit")].strip()
        if nlow == hint or nlow == f"{core} unit":
            exact.append({"unit_id": uid, "name": name})
        elif nlow.startswith(core):
            prefix.append({"unit_id": uid, "name": name})
        elif core in nlow:
            contains.append({"unit_id": uid, "name": name})
    for bucket in (exact, prefix, contains):
        if bucket:
            return bucket[0]
    return None


def _format_unit_plan_markdown(
    task: str, unit_name: str, plan: List[Dict[str, str]],
) -> str:
    """Render a Supervisor delegation plan as a readable markdown plan."""
    lines = [f"## Plan — {unit_name}", "", f"**Task:** {task}", "", "### Steps"]
    if not plan:
        lines.append("_The Supervisor would handle this solo — no specialists to delegate to yet._")
    for i, step in enumerate(plan, 1):
        role = step.get("role", "?")
        sub_task = step.get("sub_task", "")
        lines.append(f"{i}. **{role}** — {sub_task}")
        brief = step.get("task_brief")
        if brief:
            lines.append(f"   - _Brief:_ {brief}")
    lines += ["", "---", "_This is a plan only. Switch to **Work** mode and say \"go\" to execute it._"]
    return "\n".join(lines)


def _format_company_plan_markdown(
    task: str, company_name: str, plan: List[Dict[str, Any]],
    unit_names: Dict[str, str],
) -> str:
    """Render a CEO cross-Unit delegation plan as readable markdown."""
    lines = [f"## Plan — {company_name}", "", f"**Task:** {task}", "", "### Delegation across Units"]
    if not plan:
        lines.append("_No Units have staffed teams yet — nothing to delegate to._")
    for i, step in enumerate(plan, 1):
        uid = step.get("unit_id", "")
        uname = unit_names.get(uid, uid)
        sub_task = step.get("sub_task", "")
        lines.append(f"{i}. **{uname}** — {sub_task}")
        brief = step.get("unit_brief")
        if brief:
            lines.append(f"   - _Brief:_ {brief}")
    lines += ["", "---", "_This is a plan only. Switch to **Work** mode and say \"go\" to execute it._"]
    return "\n".join(lines)


def _dispatch_plan(
    *,
    intent: str,
    task: str,
    current_company: Optional[Dict[str, Any]],
    current_session_id: Optional[str],
) -> UniversalChatResponse:
    """PLAN mode: produce a plan only. No specialists run, no actions
    fire. The plan is stored (PlanStore) so a later Work-mode run can
    execute it. Runs async like a normal run so the frontend polling
    is identical."""
    side_effects = UniversalChatSideEffects()
    plan_store = get_plan_store()

    if intent == "run_task_company" and not current_company:
        intent = "run_task_unit"

    if intent == "run_task_company":
        company = current_company
        company_id = company["id"]
        clar_key = PlanStore.key_for(company_id=company_id)
        run_id = get_run_store().create(intent="plan_company", company_id=company_id, task=task)

        def _company_plan():
            pipeline = _build_pipeline()
            # Build the units snapshot the CEO planner needs.
            units_for_ceo: List[Dict[str, Any]] = []
            unit_names: Dict[str, str] = {}
            for uid in company.get("unit_ids") or []:
                ts = TeamStore(uid)
                members = ts.members()
                if not members:
                    continue
                nm = ts.name() or uid
                unit_names[uid] = nm
                units_for_ceo.append({
                    "unit_id": uid, "name": nm, "purpose": ts.purpose() or "",
                    "members": [
                        {"role": m.get("role"), "mandate": m.get("mandate"),
                         "is_supervisor": bool(m.get("is_supervisor"))}
                        for m in members
                    ],
                })
            ceo = CEOManager(model_adapter=pipeline.adapter)
            plan = ceo.plan_company_delegation(task, company, units_for_ceo)
            md = _format_company_plan_markdown(task, company.get("name") or "Company", plan, unit_names)
            plan_store.set(clar_key, task=task, plan_markdown=md, altitude="company")
            return (md, [])

        submit_run(run_id, _company_plan)
        side_effects.run_id = run_id
        side_effects.company_id = company_id
        return UniversalChatResponse(
            intent="plan_company",
            reply=(
                "Planning mode — the CEO is drafting a delegation plan across "
                "your Units. No work runs yet. The plan lands on the Output "
                "tab shortly; review it, then flip to Work mode and say \"go\"."
            ),
            side_effects=side_effects,
        )

    # Unit-level plan
    session_id = current_session_id
    if not session_id:
        session_id = TeamStore.new_session_id()
        store = TeamStore(session_id)
        sup = default_supervisor_spec()
        store.add_member(sup["role"], sup["mandate"], is_supervisor=True)
    clar_key = PlanStore.key_for(session_id=session_id)
    run_id = get_run_store().create(intent="plan_unit", session_id=session_id, task=task)
    _sid = session_id
    _task = task

    def _unit_plan():
        pipeline = _build_pipeline()
        ts = TeamStore(_sid)
        specialists = ts.specialists()
        planner = SupervisorPlanner(model_adapter=pipeline.adapter)
        plan = planner.design_delegation(_task, specialists)
        unit_name = ts.name() or "Unit"
        md = _format_unit_plan_markdown(_task, unit_name, plan)
        plan_store.set(clar_key, task=_task, plan_markdown=md, altitude="unit")
        return (md, [])

    submit_run(run_id, _unit_plan)
    side_effects.run_id = run_id
    side_effects.session_id = session_id
    return UniversalChatResponse(
        intent="plan_unit",
        reply=(
            "Planning mode — the Supervisor is drafting a plan. No work runs "
            "yet. The plan lands on the Output tab shortly; review it, then "
            "flip to Work mode and say \"go\"."
        ),
        side_effects=side_effects,
    )


PPTX_TRIGGER_WORDS = ("pptx", "powerpoint", "power point", "slide deck", "pitch deck")
DOCX_TRIGGER_WORDS = ("docx", "word doc", "word document")
XLSX_TRIGGER_WORDS = ("xlsx", "excel", "spreadsheet")
# Missing until a live test asked for "a complete report... deliver the
# whole thing as a PDF file" and got nothing back: no PDF_TRIGGER_WORDS
# list existed, so "pdf" matched none of the three lists above and
# _maybe_generate_document silently did nothing. Verified directly --
# identical deliverable text produced a real .docx when the task said
# "docx" and produced '' when it said "pdf".
PDF_TRIGGER_WORDS = ("pdf",)


def _slugify_filename(text: str, max_len: int = 40) -> str:
    import re as _re
    slug = _re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (slug[:max_len] or "deliverable")


def _maybe_generate_document(task: str, final_output: str) -> str:
    """Deterministic post-synthesis conversion — no LLM call. If the
    task text asked for a specific file format, convert the ALREADY
    -WRITTEN deliverable into a real pptx/docx/xlsx/pdf and append a
    download link. Returns '' if no format was requested (the common
    case) so callers can blindly append the result.

    This exists because asking the LLM to freehand full document
    content inside the pre-flight tool-call step produced empty
    placeholder output (see ActionSpec.planner_excluded). Converting
    the finished, properly-reasoned text sidesteps that entirely."""
    from backend.app.actions.builtin import create_pptx, create_docx, create_xlsx, create_pdf
    from backend.app.actions.builtin._markdown_convert import (
        markdown_to_pptx_slides, markdown_to_xlsx_table,
    )

    task_lower = task.lower()
    base_name = _slugify_filename(task[:60]) or "deliverable"
    notes: List[str] = []

    if any(kw in task_lower for kw in PPTX_TRIGGER_WORDS):
        title, slides = markdown_to_pptx_slides(final_output, fallback_title=task[:80])
        if slides:
            result = create_pptx._handler({
                "filename": f"{base_name}.pptx", "title": title, "slides": slides,
            })
            notes.append(result)
        else:
            notes.append("(Asked for a .pptx, but the deliverable had no clear slide structure to convert — no file created.)")

    if any(kw in task_lower for kw in DOCX_TRIGGER_WORDS):
        result = create_docx._handler({
            "filename": f"{base_name}.docx", "title": task[:80], "content": final_output,
        })
        notes.append(result)

    if any(kw in task_lower for kw in PDF_TRIGGER_WORDS):
        result = create_pdf._handler({
            "filename": f"{base_name}.pdf", "title": task[:80], "content": final_output,
        })
        notes.append(result)

    if any(kw in task_lower for kw in XLSX_TRIGGER_WORDS):
        headers, rows = markdown_to_xlsx_table(final_output)
        if headers:
            result = create_xlsx._handler({
                "filename": f"{base_name}.xlsx", "headers": headers, "rows": rows,
            })
            notes.append(result)
        else:
            notes.append("(Asked for a .xlsx, but the deliverable had no table to export — no file created.)")

    if not notes:
        return ""
    return "\n\n---\n📎 **Generated file(s):**\n" + "\n".join(f"- {n}" for n in notes)


# How much of a prior deliverable the clarifier gets to read. Long
# enough to carry the names/labels the founder will refer to next.
CLARIFIER_SNIPPET_CHARS = 2500

# Ceiling on specialists auto-hired for a first task. Deliberately small:
# the session that prompted this saw an 11-specialist auto-built company
# produce a "we cannot start until you upload a spreadsheet" non-answer,
# while a single Supervisor produced the best deliverable of the day.
AUTO_UNIT_MAX_SPECIALISTS = 3


# Words that mean "a number had to be COMPUTED", not merely looked up.
# Deliberately narrow: these are results you can only get by running
# something over a dataset, so their presence in the ask plus the
# absence of any compute call is a provable gap rather than a guess.
# Canonical list now lives in critique/compute_gate.py so the delegation
# guarantee and this gate can never drift apart.
from backend.app.critique.compute_gate import (
    COMPUTED_METRIC_WORDS as _COMPUTED_METRIC_WORDS,
    COMPUTE_TOOLS as _COMPUTE_TOOLS,
)




def _unused_compute_capability(task: str, output: str,
                               since: Optional[float] = None) -> Optional[str]:
    """Block a deliverable that was asked to COMPUTE something and never
    ran anything.

    Three runs in a row wrote *about* computing metrics and never called
    a compute tool. Ranking the tool to the top of the list, lengthening
    its description and adding prompt rules all failed to change that.
    The last run then blamed `action.calculate` for "returning no
    values" — a tool it had not called, for a job that tool cannot do.

    So this stops asking the model to choose well and checks the ledger
    instead: if the founder asked for a Sharpe ratio and nothing was
    ever executed, the run did not do the work, whatever the prose says.
    That is the same move as every guard here that has actually held —
    verify against something recorded, not against text.

    Deliberately requires the metric words to appear in the TASK. A
    deliverable that merely mentions volatility in passing is not a
    computation request, and blocking that would be the false positive
    this is not worth.
    """
    ask = (task or "").lower()
    if not any(w in ask for w in _COMPUTED_METRIC_WORDS):
        return None
    try:
        from backend.app.tools.tool_call_ledger import get_call_ledger
        # `since` scopes this to THIS run. Without it the query asks
        # 'has run_python ever run since server start', which one live
        # run passed on a PREVIOUS run's call while its own text said
        # 'no backtest executed'.
        calls = get_call_ledger().calls(since=since)
    except Exception:  # noqa: BLE001
        return None
    # Dataset metrics accept ONLY run_python: `calculate` evaluates a
    # single arithmetic expression and cannot derive a Sharpe ratio
    # from a price series, so accepting it lets a run pass this gate
    # without ever backtesting anything.
    from backend.app.critique.compute_gate import DATASET_COMPUTE_TOOLS
    ran = [
        c for c in calls
        if c.get("ok") and any(t in str(c.get("tool") or "")
                               for t in DATASET_COMPUTE_TOOLS)
    ]
    if ran:
        return None
    return (
        "the task asked for computed figures but no computation was ever "
        "run — run_python was never called, so any metric in this "
        "deliverable is asserted rather than calculated"
    )


def _gate_deliverable(output: str, task: str = "",
                      since: Optional[float] = None) -> str:
    """Last check before a run is reported as succeeded.

    The refinement loop already forces a rewrite when it finds a
    hand-back or a claim the tool log contradicts. What it could not do
    was STOP: when the retry budget ran out with the problem still
    present, the draft shipped as `status: done`. A live ARC run did
    exactly that — it detected two contradicted claims, rejected a
    refinement that tried to add five fabricated ones, and then handed
    the founder a deliverable saying the game "was never reset" (it was)
    alongside a to-do list.

    So this is deliberately the LAST word rather than another nudge. It
    does not rewrite anything; it decides whether the result may be
    called a success. Returns the output unchanged when the deliverable
    is clean.
    """
    text = output or ""
    if not text.strip():
        return output

    # An outage is not a bad answer. If the agentic loop gave up because
    # the model backend was unreachable, say THAT -- do not hand the
    # founder a critique of a deliverable that never had a chance to be
    # written. Checked first so it wins over every downstream complaint.
    try:
        from backend.app.tools.tool_call_ledger import get_call_ledger
        outages = [
            c for c in get_call_ledger().calls(since=since)
            if c.get("tool") == "backend.unavailable"
        ]
    except Exception:  # noqa: BLE001
        outages = []
    if outages:
        from backend.app.chat.async_runs import DeliverableBlocked
        roles = sorted({str(c.get("role") or "a specialist") for c in outages})
        raise DeliverableBlocked(
            "The model backend (Ollama) became unreachable mid-run, so "
            + ", ".join(roles)
            + " could not finish its work. This is an infrastructure "
              "outage, not a problem with the answer -- the run stopped "
              "early rather than guessing. Re-run it; nothing needs "
              "changing. The partial draft is kept below.",
            draft=text,
        )

    problems = []
    try:
        from backend.app.critique.handback_detector import detect_handback
        hb = detect_handback(text)
        if hb:
            problems.append(f"hands the work back to you ({hb[0][:160]})")
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.app.critique.claim_checker import detect_contradicted_claims
        cc = detect_contradicted_claims(text)
        if cc:
            problems.append(cc[0][:200])
    except Exception:  # noqa: BLE001
        pass

    # Fabricated citations. The source ledger has always DETECTED these
    # — a quant re-run shipped four, including a Yahoo bulk-download URL
    # and a pushshift endpoint the run never opened — but detection only
    # coloured a badge. A citation to a page that was never fetched is
    # the most trust-damaging thing this product can emit, because it
    # reads as the most verified line in the report.
    try:
        from backend.app.tools.source_ledger import get_ledger
        unretrieved = get_ledger().unretrieved_urls(text)
        if len(unretrieved) >= 2:
            problems.append(
                f"cites {len(unretrieved)} source(s) that were never actually "
                f"opened during this run (e.g. {unretrieved[0][:80]})"
            )
    except Exception:  # noqa: BLE001
        pass

    unused = _unused_compute_capability(task, text, since=since)
    if unused:
        problems.append(unused)

    # Asked-for figures that never appear as an actual number. A run
    # can call run_python, satisfy every provenance guard, and still
    # ship 'the numbers are in the PDF' with no numbers anywhere.
    try:
        from backend.app.critique.compute_gate import missing_metric_values
        missing = missing_metric_values(task, text)
        if missing:
            problems.append(
                "the deliverable never states a value for: "
                + ", ".join(missing)
                + " -- it was asked for these figures and reports none of them"
            )
    except Exception:  # noqa: BLE001
        pass

    # Figures that ARE stated but appear nowhere in what the code
    # actually printed. The gate above proves a number is PRESENT; this
    # one proves it was COMPUTED. A run cleared both checks above while
    # reporting a Sharpe ratio its own script had no code to calculate --
    # see compute_gate.untraceable_metric_values for that run.
    try:
        from backend.app.critique.compute_gate import untraceable_metric_values
        from backend.app.tools.tool_call_ledger import get_call_ledger
        computed_text = get_call_ledger().compute_output(since=since)
        untraceable = untraceable_metric_values(task, text, computed_text)
        if untraceable:
            problems.append(
                "these figures appear nowhere in what the code actually "
                "printed: " + "; ".join(untraceable)
                + " -- a compute tool did run, but these are not the numbers "
                "it produced, so they cannot be trusted"
            )
    except Exception:  # noqa: BLE001
        pass

    # A ranked answer whose page was never sorted.
    #
    # The loop already refuses DONE for this, but a loop can end other
    # ways -- the run that motivated this hit the repeat-call guard two
    # steps after being refused, so the contract was never satisfied and
    # the deliverable shipped anyway. A guard that only lives in the loop
    # protects the loop, not the founder.
    try:
        from backend.app.orchestrator.output_contract import (
            RANKED_RESULT, satisfied_kinds, task_wants_ranking,
        )
        from backend.app.tools.tool_call_ledger import get_call_ledger
        if task_wants_ranking(task):
            calls = get_call_ledger().calls(since=since)
            browsed = any("browser" in str(c.get("tool") or "") for c in calls)
            if browsed and RANKED_RESULT not in satisfied_kinds(set(), False, calls):
                problems.append(
                    "this task asked for a ranked result (top/best/worst/sorted) "
                    "but the page was never actually sorted or filtered -- the "
                    "figures reported are whatever the site displayed by default, "
                    "which is not the ranking that was asked for"
                )
    except Exception:  # noqa: BLE001
        pass

    if not problems:
        return output

    from backend.app.chat.async_runs import DeliverableBlocked
    raise DeliverableBlocked(
        "This run did not produce a usable deliverable: "
        + "; ".join(problems)
        + ". The draft is kept below so you can see what it did produce, "
          "but it was not completed and is not being reported as done.",
        draft=text,
    )


def _build_clarifier_context(
    *,
    current_company: Optional[Dict[str, Any]],
    session_id: Optional[str],
    clar_key: str,
    task: str = "",
) -> Dict[str, Any]:
    """Assemble the context the Clarifier reads on every call.

    Four feeds — each closes a specific 'why is it asking me that
    again' bug the founder reported:
      1. Company purpose  → clarifier stops re-asking who/what the
         founder builds when a Company is already loaded.
      2. Recent deliverables (last 2 completed runs on this session
         or company)  → 'give me a script for THIS video idea' can
         resolve to whatever the last run produced.
      3. Prior Q&A memory (across turns)  → an answer given two
         turns ago about audience still counts as answered today.
      4. RECALLED deliverables (relevance-matched, any age)  → 'add
         Asana to that pricing table' still resolves after three
         unrelated runs have pushed the pricing table out of feed 2.

    Feeds 2 and 4 land in the same `recent_deliverables` list on
    purpose. Three separate consumers read it — the context block, the
    resolved-reference dropper, and the redundancy dropper — and all
    three want the same thing from a deliverable (its text). A fourth
    key would have meant teaching each of them about it separately, and
    the one that got missed would be the bug.
    """
    company_purpose = ""
    if current_company:
        parts = []
        name = str(current_company.get("name") or "").strip()
        if name:
            parts.append(f"{name}")
        purpose = str(current_company.get("purpose") or "").strip()
        if purpose:
            parts.append(purpose)
        desc = str(current_company.get("description") or "").strip()
        if desc and desc != purpose:
            parts.append(desc)
        company_purpose = " — ".join(parts)

    recent = get_run_store().list_recent_done(
        session_id=session_id,
        company_id=(current_company or {}).get("id"),
        limit=2,
    )
    recent_deliverables: List[Dict[str, str]] = []
    for r in recent:
        # Generous snippet — a personas / ideas deliverable buries the
        # names the founder will refer to next ("each persona", "the
        # Finance-Free Friday one") well past the first few hundred
        # chars. Truncating too early is what makes the clarifier ask
        # "which personas?" about personas it just wrote.
        recent_deliverables.append({
            "task": str(r.get("task") or ""),
            "snippet": str(r.get("output") or "")[:CLARIFIER_SNIPPET_CHARS],
            "kind": "recent",
        })

    # Relevance recall, over ALL history rather than the recency window.
    # Excludes whatever feed 2 already supplied so a deliverable never
    # appears twice. Returns nothing for most tasks, which is correct —
    # see memory_manager on why a loose match is worse than no match.
    try:
        recalled = get_memory_manager().recall(
            task,
            limit=2,
            exclude_run_ids=[str(r.get("id") or "") for r in recent],
        )
        for hit in recalled:
            recent_deliverables.append({
                "task": hit.task,
                "snippet": hit.snippet(),
                "kind": "recalled",
            })
    except Exception as exc:  # noqa: BLE001
        # Recall is additive. Losing it costs the founder a re-ask;
        # letting it raise here would cost them the whole turn.
        print(f"[clarifier] memory recall failed: {exc}")

    mem = get_clarification_store().get_memory(clar_key)
    return {
        "company_purpose": company_purpose,
        "recent_deliverables": recent_deliverables,
        "prior_qa": mem.get("prior_qa") or [],
        "last_user_prompt": mem.get("last_user_prompt") or "",
    }


def _dispatch_run(
    *,
    intent: str,
    task: str,
    current_company: Optional[Dict[str, Any]],
    current_session_id: Optional[str],
    mode: str = "work",
) -> UniversalChatResponse:
    """Kick the async run pipeline. Shared by the direct-run path and
    the clarification-ready path — same behavior either way.

    mode == "plan"  -> produce a plan only (delegated to _dispatch_plan).
    mode == "work"  -> full execution. If a plan was drafted earlier for
                       this context (PlanStore), fold it into the task so
                       the run executes the reviewed plan rather than
                       re-planning from scratch.

    If intent is run_task_company but no Company is selected, silently
    downgrade to run_task_unit (the founder wanted the work done, not a
    lecture about altitudes)."""
    if mode == "plan":
        return _dispatch_plan(
            intent=intent, task=task,
            current_company=current_company,
            current_session_id=current_session_id,
        )

    side_effects = UniversalChatSideEffects()
    plan_store = get_plan_store()
    auto_company_reply_prefix = ""

    if intent == "run_task_company" and not current_company:
        intent = "run_task_unit"

    # Auto-team-on-first-task. When truly nothing is selected, spin up
    # ONE Unit sized to the task and run it there.
    #
    # This used to invent a whole COMPANY from a single first message —
    # a fictional name and purpose, a CEO, and 3-6 Units of specialists.
    # Changed to one Unit on the founder's call (2026-08-07, option B of
    # the long-open A/B/C decision), after two problems:
    #
    #   1. It accumulated junk. 24 companies existed by then — "Pixel
    #      Forge Studios", "Alpha Research Labs", "Equity Insight Labs" —
    #      including four duplicate pairs, none of which the founder
    #      asked for or named.
    #   2. The big teams produced WORSE work. An 11-specialist company
    #      answered "find me 5 research papers" with "we cannot until you
    #      upload a spreadsheet of candidate papers", while a bare
    #      Supervisor on one Unit produced the session's best deliverable
    #      — a real stock report with verified figures.
    #
    # The original justification was that a lone Supervisor narrates a
    # hypothetical delegation instead of working. That was true, and is
    # now fixed at the source: employee_coordinator injects
    # _SOLO_SUPERVISOR_BRIEF whenever a Unit has no specialists, which
    # cancels the "you only plan and synthesize" mandate. So the reason
    # to invent an org chart no longer exists.
    #
    # Scoped to "nothing at all selected" — a founder who already has a
    # standalone Unit made that choice deliberately; this doesn't touch
    # that path.
    if intent == "run_task_unit" and not current_session_id and not current_company:
        try:
            auto_pipeline = _build_pipeline()
            new_session_id = TeamStore.new_session_id()
            store = TeamStore(new_session_id)
            sup = default_supervisor_spec()
            store.add_member(sup["role"], sup["mandate"], is_supervisor=True)

            # Specialists only where the task warrants them, and capped —
            # the observed failure mode is too many roles coordinating,
            # not too few doing the work.
            spawner = EmployeeSpawner(model_adapter=auto_pipeline.adapter)
            designed = (spawner.design_team(task) or [])[:AUTO_UNIT_MAX_SPECIALISTS]
            for m in designed:
                role = str(m.get("role") or "").strip()
                if role:
                    store.add_member(role, str(m.get("mandate") or "").strip())

            current_session_id = new_session_id
            intent = "run_task_unit"
            n = len(designed)
            auto_company_reply_prefix = (
                f"First task in a new workspace, so I set up a team for it — "
                f"{n} specialist{'s' if n != 1 else ''} under a Supervisor. "
                f"Check the Org tab to see (and reshape) it.\n\n"
            ) if n else (
                "First task in a new workspace — running it on a fresh Unit.\n\n"
            )
        except Exception as exc:  # noqa: BLE001
            # Fail open to the bare-Supervisor path rather than blocking
            # the founder's first task on a team-design bug. That path is
            # genuinely fine now (see _SOLO_SUPERVISOR_BRIEF).
            print(f"[warn] auto-team-on-first-task failed, falling back: {exc}")

    if intent == "run_task_company":
        company_id = current_company["id"]
        # Fold in any reviewed plan for this Company.
        stored_plan = plan_store.get(PlanStore.key_for(company_id=company_id))
        effective_task = task
        if stored_plan and stored_plan.get("altitude") == "company":
            effective_task = (
                f"{stored_plan['task']}\n\n"
                f"APPROVED PLAN — execute this delegation:\n{stored_plan['plan_markdown']}"
            )
        run_id = get_run_store().create(
            intent=intent,
            company_id=company_id,
            session_id=current_session_id,
            task=effective_task,
        )

        def _company_work():
            # Scopes the compute gate to this run -- see
            # ToolCallLedger.calls(since=...) for why.
            _run_started = time.time()
            # Bind this thread to the run so producers (write_file,
            # download_asset, deploy_vercel) register what they make
            # without every one of them needing a run_id parameter --
            # they are called by the model, which has no idea what a run
            # is. See state/run_artifacts.set_current_run.
            _artifacts.set_current_run(run_id)
            result = run_task_on_company(company_id, CompanyRunRequest(task=effective_task))
            # Clear the plan once executed so it doesn't leak into the next task.
            plan_store.clear(PlanStore.key_for(company_id=company_id))
            output = result.final_output or ""
            try:
                output += _maybe_generate_document(task, output)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] document auto-generation failed: {exc}")
            _gate_deliverable(output, effective_task, since=_run_started)
            return (output, [e.model_dump() for e in (result.evidence or [])])

        submit_run(run_id, _company_work)
        side_effects.run_id = run_id
        side_effects.company_id = company_id
        if auto_company_reply_prefix:
            side_effects.org_refreshed = True
        return UniversalChatResponse(
            intent=intent,
            reply=(
                auto_company_reply_prefix +
                "CEO is on it — planning, delegating across Units, and "
                "synthesizing. Company-level runs take a few minutes on "
                "gpt-oss; the deliverable will pop onto the Output tab as "
                "soon as it's ready. Keep chatting or watch the badge."
            ),
            side_effects=side_effects,
        )

    # run_task_unit — auto-create a Unit if none is selected
    session_id = current_session_id
    if not session_id:
        session_id = TeamStore.new_session_id()
        store = TeamStore(session_id)
        sup = default_supervisor_spec()
        store.add_member(sup["role"], sup["mandate"], is_supervisor=True)
    stored_plan = plan_store.get(PlanStore.key_for(session_id=session_id))
    effective_task = task
    if stored_plan and stored_plan.get("altitude") == "unit":
        effective_task = (
            f"{stored_plan['task']}\n\n"
            f"APPROVED PLAN — execute this delegation:\n{stored_plan['plan_markdown']}"
        )
    # Tag the run with BOTH scopes. A Unit run launched while a Company
    # is selected must still be findable by company_id — otherwise the
    # next turn's clarifier can't see the deliverable it just produced
    # ("give me a message for each persona" -> "which personas?").
    run_id = get_run_store().create(
        intent="run_task_unit",
        session_id=session_id,
        company_id=(current_company or {}).get("id"),
        task=effective_task,
    )
    _sid = session_id
    _task = effective_task

    def _unit_work():
        _run_started = time.time()
        _artifacts.set_current_run(run_id)
        result = run_task_on_team(_sid, RunTaskRequest(task=_task))
        plan_store.clear(PlanStore.key_for(session_id=_sid))
        output = result.final_output or ""
        try:
            output += _maybe_generate_document(task, output)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] document auto-generation failed: {exc}")
        _gate_deliverable(output, _task, since=_run_started)
        return (output, [e.model_dump() for e in (result.evidence or [])])

    submit_run(run_id, _unit_work)
    side_effects.run_id = run_id
    side_effects.session_id = session_id
    return UniversalChatResponse(
        intent="run_task_unit",
        reply=(
            "On it — the Unit is running now. The deliverable will "
            "appear on the Output tab as soon as it's ready. Keep "
            "chatting while it works."
        ),
        side_effects=side_effects,
    )


def _fallthrough_response(side_effects: UniversalChatSideEffects) -> UniversalChatResponse:
    return UniversalChatResponse(
        intent="casual_chat",
        reply="I wasn't sure what to do with that. Try rephrasing.",
        side_effects=side_effects,
    )


# ---------------------------------------------------------------------------
# Workspace files — uploads (founder -> AI) and downloads (AI -> founder)
#
# Both sides of the same sandbox the action layer already writes into
# (backend.app.actions.builtin._workspace). Uploads land under
# workspace/uploads/<random>_<name>; generated documents
# (create_pptx/docx/xlsx) land at the workspace root or wherever the
# LLM named them. Download just streams anything inside that sandbox.
# ---------------------------------------------------------------------------

UPLOAD_SUBDIR = "uploads"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15MB — generous for docs, not for video
MAX_UPLOAD_PREVIEW_CHARS = 6000


def _extract_upload_preview(path: Path, suffix: str) -> str:
    """Best-effort text preview so the AI actually sees what's in the
    file instead of just a filename. Unsupported/binary types get a
    plain notice — the file still exists on disk and is still
    referenceable, just not auto-read."""
    suffix = suffix.lower()
    try:
        if suffix in (".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml"):
            return path.read_text(encoding="utf-8", errors="replace")[:MAX_UPLOAD_PREVIEW_CHARS]
        if suffix == ".docx":
            import docx
            d = docx.Document(str(path))
            text = "\n".join(p.text for p in d.paragraphs if p.text.strip())
            return text[:MAX_UPLOAD_PREVIEW_CHARS]
        if suffix == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
            lines: List[str] = []
            for ws in wb.worksheets[:3]:
                lines.append(f"= {ws.title} =")
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= 200:
                        lines.append("... (truncated)")
                        break
                    lines.append(", ".join("" if c is None else str(c) for c in row))
            return "\n".join(lines)[:MAX_UPLOAD_PREVIEW_CHARS]
        if suffix == ".pptx":
            from pptx import Presentation
            prs = Presentation(str(path))
            lines = []
            for i, slide in enumerate(prs.slides, 1):
                lines.append(f"--- Slide {i} ---")
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False):
                        t = shape.text_frame.text.strip()
                        if t:
                            lines.append(t)
            return "\n".join(lines)[:MAX_UPLOAD_PREVIEW_CHARS]
    except Exception as exc:  # noqa: BLE001
        return f"(could not extract a text preview: {exc})"
    return "(binary file — no text preview available; the file is saved and can still be referenced by path)"


@router.post("/uploads")
async def upload_file(file: UploadFile = File(...)):
    """Founder -> AI. Saves into workspace/uploads/, extracts a text
    preview for known formats (txt/md/csv/json/docx/xlsx/pptx), and
    returns enough for the frontend to fold the content into the next
    chat message. PDF extraction is NOT yet supported — flagged in the
    response so the founder isn't surprised."""
    from backend.app.actions.builtin._workspace import workspace_root

    root = workspace_root()
    upload_dir = root / UPLOAD_SUBDIR
    upload_dir.mkdir(parents=True, exist_ok=True)

    safe_name = os.path.basename(file.filename or "upload.bin")
    file_id = uuid.uuid4().hex[:10]
    stored_name = f"{file_id}_{safe_name}"
    target = upload_dir / stored_name

    size = 0
    try:
        with open(target, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    f.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(413, f"File too large — max {MAX_UPLOAD_BYTES // (1024*1024)}MB")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        target.unlink(missing_ok=True)
        raise HTTPException(400, f"Upload failed: {exc}")

    suffix = Path(safe_name).suffix
    if suffix.lower() == ".pdf":
        preview = "(PDF text extraction isn't supported yet — the file is saved, but its contents won't be auto-read into the chat.)"
    else:
        preview = _extract_upload_preview(target, suffix)
    rel = target.relative_to(root)
    return {
        "file_id": file_id,
        "filename": safe_name,
        "stored_path": rel.as_posix(),
        "size_bytes": size,
        "preview": preview,
        "download_url": f"/api/workspace/download/{rel.as_posix()}",
    }


@router.get("/workspace/download/{file_path:path}")
def download_workspace_file(file_path: str):
    """AI -> founder. Streams any file inside the sandboxed workspace
    (generated documents, uploaded files) as a real download — this is
    what turns 'Created deck.pptx' in the chat into a clickable link."""
    from backend.app.actions.builtin._workspace import resolve_within, workspace_root

    root = workspace_root()
    try:
        target = resolve_within(root, file_path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not target.exists() or not target.is_file():
        raise HTTPException(404, f"No such file: {file_path}")
    return FileResponse(str(target), filename=target.name)


@router.get("/units/usage")
def list_unit_usage():
    """Which Units get used most. Powers the right-sidebar leaderboard.
    Returns every Unit sorted by run_count desc, with a resolved
    company_name so the UI can label rows.
    Shape: {units: [{unit_id, name, company_id, company_name, run_count, last_run_at}]}"""
    rows = TeamStore.list_usage()
    company_names: Dict[str, str] = {}
    for c in get_company_store().list():
        company_names[c["id"]] = c.get("name") or c["id"]
    out = []
    for r in rows:
        cid = r.get("company_id")
        out.append({
            **r,
            "company_name": company_names.get(cid) if cid else None,
        })
    return {"units": out}


@router.get("/runs")
def list_runs(limit: int = 50):
    """Finished runs, newest first — backs the Output tab's history
    picker. Returns summaries only (no output body) so listing stays
    cheap; the client fetches /api/runs/{id} for the deliverable it
    actually wants to reopen."""
    out = []
    for r in get_run_store().list_history(limit=limit):
        out.append({
            "id": r["id"],
            "status": r.get("status"),
            "task": (r.get("task") or "")[:200],
            "intent": r.get("intent"),
            "finished_at": r.get("finished_at"),
            "session_id": r.get("session_id"),
            "company_id": r.get("company_id"),
        })
    return {"runs": out}


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
def get_run_status(run_id: str) -> RunStatusResponse:
    """Poll a background run kicked off by /api/chat. Client polls
    every few seconds until status is done or failed, then renders
    output + evidence."""
    record = get_run_store().get(run_id)
    if not record:
        raise HTTPException(404, f"no run with id {run_id!r}")
    return RunStatusResponse(
        id=record["id"],
        intent=record["intent"],
        status=record["status"],
        session_id=record.get("session_id"),
        company_id=record.get("company_id"),
        task=record.get("task") or "",
        created_at=record.get("created_at"),
        started_at=record.get("started_at"),
        finished_at=record.get("finished_at"),
        output=record.get("output"),
        evidence=[EvidenceClaim(**e) for e in (record.get("evidence") or [])],
        error=record.get("error"),
    )


