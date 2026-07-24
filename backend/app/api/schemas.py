"""Request/response models for the thin API slice (README Phase 9).

Deliberately minimal — one endpoint (validate an idea) to get the
Idea-Validation Employee in front of someone who isn't running Python
scripts, not the full manager terminal. Expand as more employee types
and phases land.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ValidateIdeaRequest(BaseModel):
    idea: str = Field(..., min_length=1, description="The founder's raw idea, in their own words.")
    employee_id: Optional[str] = Field(
        None,
        description="Reuse a specific founder's memory across follow-up ideas. "
        "Omit for a fresh one-off validation (default — different people's "
        "ideas should never share context).",
    )


class ValidateIdeaResponse(BaseModel):
    employee_id: str
    verdict: str
    completeness_score: Optional[float] = None
    fabricated_claims: List[str] = Field(default_factory=list)
    over_engineered: Optional[bool] = None
    was_refined: bool = False


class HistoryEntry(BaseModel):
    timestamp: str
    task: str
    summary: str
    completeness_score: Optional[float] = None


class HistoryResponse(BaseModel):
    employee_id: str
    entries: List[HistoryEntry]


class TeamMemberSummary(BaseModel):
    role: str
    completeness_score: Optional[float] = None
    fabricated_claims: List[str] = Field(default_factory=list)
    was_refined: Optional[bool] = None


class OrchestrateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="The user's raw request in their own words.")
    session_id: Optional[str] = Field(
        None,
        description="Reuse to make follow-up prompts share memory with the same team. "
        "Omit for a fresh session.",
    )
    force_team: bool = Field(
        False,
        description="If True, always spawn a team of employees even for simple tasks. "
        "Off by default — the supervisor picks the cheapest tier that fits.",
    )


class OrchestrateResponse(BaseModel):
    session_id: str
    mode: str  # "single_call" | "single_call_critique" | "team"
    team: List[TeamMemberSummary] = Field(default_factory=list)
    final_output: str


# --- Playground / session UI ---

class TeamMemberSpec(BaseModel):
    role: str = Field(..., min_length=1)
    mandate: str = Field(..., min_length=1)
    is_supervisor: bool = False
    # Optional: the persistent Employee id backing this roster entry.
    # Absent for legacy responses (before the Employee registry existed)
    # and for spec dicts that describe a proposed member before it has
    # been persisted. Present for any TeamStore-sourced member on or
    # after schema_version 2.
    employee_id: Optional[str] = None


class SessionSummary(BaseModel):
    session_id: str
    member_count: int
    created_at: Optional[str] = None
    name: Optional[str] = None
    purpose: Optional[str] = None


class SessionCreateResponse(BaseModel):
    session_id: str


class TeamListResponse(BaseModel):
    session_id: str
    members: List[TeamMemberSpec] = Field(default_factory=list)
    created_at: Optional[str] = None


class DesignTeamRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="What the user wants the team to help with — used to design the initial roster.")
    replace: bool = Field(True, description="If True (default), replace any existing team; if False, append the designed team to the existing one.")


class AddMemberRequest(BaseModel):
    role: str = Field(..., min_length=1)
    mandate: str = Field(..., min_length=1)


class RunTaskRequest(BaseModel):
    task: str = Field(..., min_length=1)


class EvidenceClaim(BaseModel):
    """One factual claim extracted from a deliverable, with its
    verification status made explicit instead of left buried in prose.
    See backend/app/critique/evidence_extractor.py for why this exists —
    a blind benchmark proved that verification left as an exercise for
    the reader gets skipped, even by other AI judges."""
    text: str
    status: str  # "verified" | "flagged_unknown" | "unsourced_claim"
    source: str = ""


class RunTaskResponse(BaseModel):
    session_id: str
    task: str
    team: List[TeamMemberSummary] = Field(default_factory=list)
    final_output: str
    evidence: List[EvidenceClaim] = Field(default_factory=list)


# --- MCP connectors ---

class MCPConnectionSpec(BaseModel):
    name: str = Field(..., min_length=1, description="Short slug (letters/numbers/dashes/underscores) — used as the tool namespace, e.g. 'notion' -> tools become 'notion.search_pages'.")
    transport: str = Field("stdio", description="'stdio' (subprocess) or 'http' (SSE endpoint). Only 'stdio' supported today.")
    command: Optional[str] = Field(None, description="stdio only — the binary to run, e.g. 'npx'.")
    args: List[str] = Field(default_factory=list, description="stdio only — args to the command.")
    url: Optional[str] = Field(None, description="http only — the server URL.")
    env: Dict[str, str] = Field(default_factory=dict, description="Extra env vars for the subprocess (API tokens etc.).")
    enabled: bool = True


class MCPConnectionResponse(BaseModel):
    name: str
    transport: str
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    url: Optional[str] = None
    enabled: bool = True
    added_at: Optional[str] = None
    # env is intentionally NOT returned (contains secrets)


class MCPToolInfo(BaseModel):
    qualified_name: str
    connection: str
    tool: str
    description: str = ""


class MCPListResponse(BaseModel):
    connections: List[MCPConnectionResponse] = Field(default_factory=list)
    tools: List[MCPToolInfo] = Field(default_factory=list)


# --- Custom HTTP tools (Option B: connect any API without an MCP server) ---

class HTTPToolAuthSpec(BaseModel):
    type: str = Field("none", description="'none' | 'bearer' | 'api_key_header' | 'basic'")
    token: Optional[str] = Field(None, description="bearer / api_key_header token")
    header_name: Optional[str] = Field(None, description="api_key_header only — e.g. 'X-API-Key'")
    username: Optional[str] = Field(None, description="basic only")
    password: Optional[str] = Field(None, description="basic only")


class HTTPToolParameterSpec(BaseModel):
    name: str
    in_: str = Field("query", alias="in", description="'query' | 'body' | 'path' | 'header'")
    description: str = ""
    required: bool = False

    class Config:
        populate_by_name = True


class HTTPToolSpec(BaseModel):
    name: str = Field(..., min_length=1, description="Unique alphanumeric slug; tool namespace is 'custom.<name>'.")
    description: str = ""
    method: str = Field("GET", description="GET | POST | PUT | PATCH | DELETE")
    url: str = Field(..., description="Full URL; may include {path_param} placeholders.")
    auth: HTTPToolAuthSpec = Field(default_factory=HTTPToolAuthSpec)
    parameters: List[HTTPToolParameterSpec] = Field(default_factory=list)
    headers: Dict[str, str] = Field(default_factory=dict, description="Static headers merged into every call.")
    enabled: bool = True


class HTTPToolResponse(BaseModel):
    name: str
    description: str = ""
    method: str
    url: str
    parameters: List[HTTPToolParameterSpec] = Field(default_factory=list)
    enabled: bool = True
    # auth + headers are intentionally omitted from the wire — they hold secrets
    auth_type: str = "none"


class HTTPToolListResponse(BaseModel):
    tools: List[HTTPToolResponse] = Field(default_factory=list)


# --- Hierarchy: Employee registry + Company (Phase 1 of AI hierarchy) ---

class EmployeeCreateRequest(BaseModel):
    role: str = Field(..., min_length=1)
    mandate: str = Field(..., min_length=1)
    is_supervisor: bool = False
    tags: List[str] = Field(default_factory=list)


class EmployeeUpdateRequest(BaseModel):
    role: Optional[str] = None
    mandate: Optional[str] = None
    is_supervisor: Optional[bool] = None
    tags: Optional[List[str]] = None


class EmployeeResponse(BaseModel):
    id: str
    role: str
    mandate: str
    avatar_seed: str = ""
    tags: List[str] = Field(default_factory=list)
    is_supervisor: bool = False
    created_at: Optional[str] = None


class EmployeeListResponse(BaseModel):
    employees: List[EmployeeResponse] = Field(default_factory=list)


class HireEmployeeRequest(BaseModel):
    employee_id: str
    is_supervisor: bool = False


class CompanyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)
    purpose: Optional[str] = None


class CompanyUpdateRequest(BaseModel):
    name: Optional[str] = None
    purpose: Optional[str] = None


class CompanyResponse(BaseModel):
    id: str
    name: str
    purpose: Optional[str] = None
    unit_ids: List[str] = Field(default_factory=list)
    # The CEO Employee id (Phase 3a). Absent on Companies created
    # before CEO auto-hire existed — the API endpoints treat that
    # case as "backfill on first use" so old data doesn't break.
    ceo_employee_id: Optional[str] = None
    created_at: Optional[str] = None


# --- Hierarchy design (Phase 3a — Company org chart from a description) ---

class HierarchyDesignRequest(BaseModel):
    """Founder describes their company in plain English; the CEO
    proposes an initial org chart."""
    description: str = Field(..., min_length=1, description="Founder's plain-English description of what the company is building.")


class HierarchyUnitSpec(BaseModel):
    """One proposed Unit from the CEO's design pass. Not yet persisted —
    the founder reviews/edits before it materializes."""
    name: str
    purpose: str
    specialists: List[TeamMemberSpec] = Field(default_factory=list)


class HierarchyDesignResponse(BaseModel):
    """The CEO's proposed org chart. Units are unsaved specs until the
    founder POSTs to /apply_hierarchy."""
    company_id: str
    units: List[HierarchyUnitSpec] = Field(default_factory=list)


class HierarchyApplyRequest(BaseModel):
    """Materialize a proposed (possibly edited) hierarchy: for each
    Unit in the list, create a real Unit, attach it to the Company,
    and stock its team with the given specialists. Idempotent-ish:
    a Unit with a duplicate name is fine, they just get separate IDs."""
    units: List[HierarchyUnitSpec] = Field(default_factory=list)


class HierarchyAppliedUnit(BaseModel):
    unit_id: str
    name: str
    specialist_count: int


class HierarchyApplyResponse(BaseModel):
    company_id: str
    units: List[HierarchyAppliedUnit] = Field(default_factory=list)


class CompanyListResponse(BaseModel):
    companies: List[CompanyResponse] = Field(default_factory=list)


# --- Company-level task run (Phase 2 hierarchy — CEO Manager) ---

class CompanyRunRequest(BaseModel):
    task: str = Field(..., min_length=1, description="The Company-level task to delegate across Units.")


class CompanyRunUnitContribution(BaseModel):
    unit_id: str
    unit_name: Optional[str] = None
    output: Optional[str] = None
    supervisor_role: Optional[str] = None


class CompanyRunResponse(BaseModel):
    company_id: str
    company_name: Optional[str] = None
    final_output: str
    plan: List[Dict[str, Any]] = Field(default_factory=list)
    unit_contributions: List[CompanyRunUnitContribution] = Field(default_factory=list)
    evidence: List[EvidenceClaim] = Field(default_factory=list)


# --- Chat routing ---

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)


class ChatResponse(BaseModel):
    session_id: str
    intent: str  # "add_employee" | "remove_employee" | "modify_employee" | "design_team" | "clear_team" | "run_task" | "unclear"
    reply: str   # Human-facing response to show in the chat
    # For team-changing intents: the updated team afterwards
    team: List[TeamMemberSpec] = Field(default_factory=list)
    # For run_task: pass this text back to /run to actually execute (kept
    # separate so the chat endpoint stays fast and the long run streams
    # through the existing progress polling on /run).
    task_to_run: Optional[str] = None


# --- Universal chat router (Phase 3b — prompt-first UI, kills the
# "click here, then toggle that mode" flow at the front door). ---

class UniversalChatRequest(BaseModel):
    """One entry point above Unit and Company altitudes. The client
    passes what it currently has selected; the router figures out
    the intent and executes it."""
    message: str = Field(..., min_length=1)
    current_company_id: Optional[str] = None
    current_session_id: Optional[str] = None


class UniversalChatSideEffects(BaseModel):
    """State updates the client should reflect after this turn. All
    fields optional — the client re-fetches whatever changed."""
    company_id: Optional[str] = None
    session_id: Optional[str] = None
    pending_proposal: Optional[Dict[str, Any]] = None
    applied_units: List[HierarchyAppliedUnit] = Field(default_factory=list)
    org_refreshed: bool = False
    run_output: Optional[str] = None
    evidence: List[EvidenceClaim] = Field(default_factory=list)
    # Present when the intent kicked off a long-running task in the
    # background. The client polls GET /api/runs/{run_id} until status
    # is done/failed, then renders the output + evidence.
    run_id: Optional[str] = None


class RunStatusResponse(BaseModel):
    """One row of the async run store — the shape /api/runs/{id} returns."""
    id: str
    intent: str
    status: str  # queued | running | done | failed
    session_id: Optional[str] = None
    company_id: Optional[str] = None
    task: str = ""
    created_at: Optional[float] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    output: Optional[str] = None
    evidence: List[EvidenceClaim] = Field(default_factory=list)
    error: Optional[str] = None


class UniversalChatResponse(BaseModel):
    intent: str
    reply: str
    side_effects: UniversalChatSideEffects = Field(default_factory=UniversalChatSideEffects)
