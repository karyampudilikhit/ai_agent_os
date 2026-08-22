"""Phase 5 — coverage for the four subsystems that had none.

The audit counted 15 test files against 119 backend modules, with
nothing at all covering the router, the approval queue's reject path,
citation verification, or the connector layer. Those four are not an
arbitrary selection: each is a place where a silent failure is either
invisible or actively harmful.

  - Router      — its whole contract is "never dead-end the chat". A
                  regression here looks like the product ignoring you.
  - Reject path — approve was exercised constantly during development;
                  reject never was. It is the half that protects the
                  founder from an action they did NOT want.
  - Citations   — the check that says a source is fake. A false
                  accusation here is worse than the bug it hunts.
  - Connectors  — the layer new integrations are added through, so a
                  break here breaks every future integration at once.

Everything runs offline. No LLM, no network, no shared state with the
founder's real data — the approval-queue tests inject their own file
path rather than touching .pending_actions.json.

Run:
    py -3 test_phase5_coverage.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, List

import httpx

from backend.app.actions.approval_queue import ApprovalQueue, ApprovalQueueError
from backend.app.chat.universal_router import UniversalChatRouter
from backend.app.tools import http_tool_runner as runner
from backend.app.tools.source_ledger import get_ledger


# ====================================================================
# A. Router — the contract is "never dead-end the chat"
# ====================================================================

class _Adapter:
    """Fake model adapter. `reply` is returned verbatim, or raised if it
    is an exception instance."""

    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls = 0

    def chat_completion(self, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_router_survives_a_dead_model() -> None:
    """Phase 1 made the adapter RAISE instead of returning errors as
    content. That fix is only safe because callers handle the raise —
    this is the router's half of it. A dead Ollama must produce a
    readable reply, not a 500."""
    r = UniversalChatRouter(model_adapter=_Adapter(RuntimeError("connection refused")))
    out = r.classify("build me an AI agency", "(no state)")
    assert out["intent"] == "casual_chat"
    assert out.get("reply"), "fallback must carry a message for the founder"


def test_router_rejects_an_invented_intent() -> None:
    """The intent string is dispatched on. An unrecognised one must not
    reach the dispatcher — it would either KeyError or, worse, silently
    match nothing and leave the founder with no response at all."""
    r = UniversalChatRouter(model_adapter=_Adapter('{"intent": "launch_missiles"}'))
    out = r.classify("do something", "(no state)")
    assert out["intent"] == "casual_chat"
    assert out.get("reply")


def test_router_survives_unparseable_output() -> None:
    for junk in ("", "I think you want to build a company!", "{broken json"):
        r = UniversalChatRouter(model_adapter=_Adapter(junk))
        out = r.classify("hello", "(no state)")
        assert out["intent"] == "casual_chat", f"junk {junk!r} broke the router"


def test_router_passes_through_a_valid_intent() -> None:
    """The negative tests above would all pass on a router that hardcoded
    casual_chat. This is the one that proves it still routes."""
    r = UniversalChatRouter(
        model_adapter=_Adapter('{"intent": "run_task_unit", "task": "research X"}')
    )
    out = r.classify("research X", "(no state)")
    assert out["intent"] == "run_task_unit"
    assert out["task"] == "research X"


def test_router_reads_json_out_of_prose_and_fences() -> None:
    """Real models wrap JSON in commentary or ```json fences. Failing to
    unwrap it silently downgrades every routed task to casual_chat."""
    for raw in (
        'Sure! ```json\n{"intent": "run_task_unit", "task": "research X"}\n```',
        'Here you go:\n{"intent": "run_task_unit", "task": "research X"}\nHope that helps!',
    ):
        out = UniversalChatRouter(model_adapter=_Adapter(raw)).classify("x", "(none)")
        assert out["intent"] == "run_task_unit", f"failed to unwrap: {raw[:40]}"


# ====================================================================
# B. Approval queue — the reject path
# ====================================================================

def _queue() -> ApprovalQueue:
    """Isolated queue. The real one lives at .pending_actions.json and
    holds actions the founder has not decided on yet; a test must never
    read or write it."""
    return ApprovalQueue(path=Path(tempfile.mkdtemp()) / ".pending_actions.json")


def _enqueue(q: ApprovalQueue) -> str:
    rec = q.enqueue("send_email", {"to": "a@b.com"}, "Send email to a@b.com")
    return rec["id"]


def test_reject_records_status_and_reason() -> None:
    q = _queue()
    action_id = _enqueue(q)
    updated = q.set_status(action_id, "rejected", error="founder said no")
    assert updated["status"] == "rejected"
    assert updated["error"] == "founder said no"
    assert updated["resolved_at"] is not None


def test_rejected_action_leaves_the_pending_list() -> None:
    """The UI's pending list drives what the founder is asked to decide.
    A rejected action still showing there means being asked twice about
    something already refused."""
    q = _queue()
    action_id = _enqueue(q)
    assert len(q.list(status="pending")) == 1
    q.set_status(action_id, "rejected")
    assert q.list(status="pending") == []
    assert len(q.list(status="rejected")) == 1


def test_rejected_action_is_never_executed() -> None:
    """The core safety property. Nothing may flip a rejected action back
    to pending, because the executor runs whatever is approved."""
    q = _queue()
    action_id = _enqueue(q)
    q.set_status(action_id, "rejected")
    assert q.get(action_id)["status"] == "rejected"
    assert q.list(status="approved") == []


def test_rejecting_an_unknown_action_raises() -> None:
    """Must not silently no-op — the caller turns this into a 404, and a
    swallowed error would report success for an action that never
    existed."""
    q = _queue()
    try:
        q.set_status("pend_doesnotexist", "rejected")
    except ApprovalQueueError:
        return
    raise AssertionError("rejecting an unknown id silently succeeded")


def test_pruning_keeps_pending_and_drops_resolved() -> None:
    """Pending actions are undecided founder business and must survive
    pruning at any age."""
    q = _queue()
    pending_id = _enqueue(q)
    rejected_id = _enqueue(q)
    q.set_status(rejected_id, "rejected")

    assert q.prune_resolved(older_than_seconds=-1) == 1
    assert q.get(pending_id) is not None, "pruning deleted an undecided action"
    assert q.get(rejected_id) is None


def test_route_guards_reject_of_an_already_resolved_action() -> None:
    """Route-level guards: 404 for unknown, 409 for already-resolved.
    Without the 409, a double-click in the UI would overwrite the
    resolution timestamp of a decision already made."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.app.api import routes

    q = _queue()
    action_id = _enqueue(q)

    app = FastAPI()
    app.include_router(routes.router)
    original = routes.get_approval_queue
    routes.get_approval_queue = lambda: q  # isolate from the real queue
    try:
        client = TestClient(app)
        assert client.post("/pending_actions/pend_nope/reject").status_code == 404

        first = client.post(f"/pending_actions/{action_id}/reject")
        assert first.status_code == 200, first.text
        assert first.json()["action"]["status"] == "rejected"

        second = client.post(f"/pending_actions/{action_id}/reject")
        assert second.status_code == 409, second.text
    finally:
        routes.get_approval_queue = original


# ====================================================================
# C. Citation verification
# ====================================================================

class _StubPipeline:
    adapter = None


def _coordinator() -> Any:
    from backend.app.employees.employee_coordinator import EmployeeCoordinator
    return EmployeeCoordinator(pipeline=_StubPipeline())


def test_citation_to_a_fetched_page_is_not_flagged() -> None:
    """The false-accusation case. This must come first in spirit: the
    check exists to catch fabrication, and its worst failure is calling
    a real citation fake."""
    get_ledger().record_fetched("https://example.com/real-page")
    claims = [{"claim": "X is true", "source": "https://example.com/real-page",
               "status": "sourced"}]
    out = _coordinator()._flag_fabricated_citations("see https://example.com/real-page", claims)
    assert out[0]["status"] == "sourced", out


def test_citation_to_a_never_fetched_page_is_flagged() -> None:
    claims = [{"claim": "Company X is a chemicals firm",
               "source": "https://en.wikipedia.org/wiki/Never_Opened_By_This_Process",
               "status": "sourced"}]
    out = _coordinator()._flag_fabricated_citations("body text", claims)
    assert out[0]["status"] == "fabricated_source", out


def test_multi_url_source_field_is_split_before_checking() -> None:
    """Phase 3a regression. A claim drawing on two pages arrives as
    'https://a https://b' in one field; treating it as one opaque string
    flagged a genuinely-sourced Notion/Linear comparison as fabricated."""
    get_ledger().record_fetched("https://notion.so/pricing")
    claims = [{
        "claim": "Notion is $15 and Linear is $14",
        "source": "https://notion.so/pricing https://linear.app/pricing",
        "status": "sourced",
    }]
    out = _coordinator()._flag_fabricated_citations("body", claims)
    assert out[0]["status"] == "sourced", (
        "a claim citing one real page and one unknown page was flagged fabricated"
    )


def test_doi_with_parentheses_stays_one_citation() -> None:
    """Phase 3b regression. 10.1016/0304-405X(94)90112-5 is a real DOI
    shape; splitting on the paren turned one good citation into two
    phantom fabrications."""
    doi = "https://doi.org/10.1016/0304-405X(94)90112-5"
    get_ledger().record_fetched(doi)
    out = _coordinator()._flag_fabricated_citations(f"See {doi} for details.", [])
    fabricated = [c for c in out if c.get("status") == "fabricated_source"]
    assert not fabricated, f"real DOI reported as fabricated: {fabricated}"


def test_uncited_fake_url_in_the_body_still_gets_flagged() -> None:
    """A fabricated link must not escape by simply not being picked up as
    a claim by the extractor."""
    out = _coordinator()._flag_fabricated_citations(
        "According to https://fake-source-never-fetched.example/report, revenue tripled.",
        [],
    )
    assert any(c.get("status") == "fabricated_source" for c in out), out


# ====================================================================
# D. Connector layer
# ====================================================================

def test_undeclared_arguments_are_dropped_not_rerouted() -> None:
    """Security property, not tidiness. Rerouting unknown args to the
    query string would let a model append arbitrary parameters to a
    real API call."""
    params = [{"name": "q", "in": "query", "required": True}]
    buckets = runner._split_arguments(params, {"q": "hello", "api_key": "leaked"})
    assert buckets["query"] == {"q": "hello"}
    assert "api_key" not in buckets["query"]
    assert all("api_key" not in b for b in (buckets["body"], buckets["header"], buckets["path"]))


def test_missing_required_arguments_are_reported() -> None:
    params = [
        {"name": "q", "in": "query", "required": True},
        {"name": "rows", "in": "query", "required": False},
    ]
    assert runner._split_arguments(params, {})["missing"] == ["q"]
    assert runner._split_arguments(params, {"q": "x"})["missing"] == []


def test_arguments_route_to_their_declared_location() -> None:
    params = [
        {"name": "id", "in": "path", "required": True},
        {"name": "q", "in": "query", "required": False},
        {"name": "payload", "in": "body", "required": False},
        {"name": "X-Trace", "in": "header", "required": False},
    ]
    b = runner._split_arguments(
        params, {"id": "42", "q": "x", "payload": {"a": 1}, "X-Trace": "abc"}
    )
    assert b["path"] == {"id": "42"}
    assert b["query"] == {"q": "x"}
    assert b["body"] == {"payload": {"a": 1}}
    assert b["header"] == {"X-Trace": "abc"}


def test_path_templating_substitutes_every_placeholder() -> None:
    url = runner._substitute_path(
        "https://api.example.com/v1/{owner}/{repo}/issues", {"owner": "acme", "repo": "web"}
    )
    assert url == "https://api.example.com/v1/acme/web/issues"
    assert "{" not in url


def test_auth_modes_produce_the_right_headers() -> None:
    h: Dict[str, str] = {}
    assert runner._apply_auth({"type": "bearer", "token": "tok"}, h) is None
    assert h["Authorization"] == "Bearer tok"

    h2: Dict[str, str] = {}
    runner._apply_auth({"type": "api_key_header", "header_name": "X-API-Key", "token": "k"}, h2)
    assert h2["X-API-Key"] == "k"

    h3: Dict[str, str] = {}
    assert runner._apply_auth({"type": "basic", "username": "u", "password": "p"}, h3) == ("u", "p")

    h4: Dict[str, str] = {}
    assert runner._apply_auth({"type": "none"}, h4) is None
    assert h4 == {}


def test_empty_auth_token_adds_no_header() -> None:
    """A blank token must not produce 'Authorization: Bearer ' — that
    reads as an auth attempt and gets a 401 instead of the anonymous
    access many public APIs allow."""
    h: Dict[str, str] = {}
    runner._apply_auth({"type": "bearer", "token": ""}, h)
    assert h == {}


def test_response_truncation_is_marked_not_silent() -> None:
    """A silently-cut payload is how an employee concludes a field is
    absent from a page that does list it."""
    spec = {"method": "GET"}
    big = "x" * (runner.MAX_RESPONSE_CHARS + 5000)
    resp = httpx.Response(200, text=big, request=httpx.Request("GET", "https://e.com"))
    out = runner._format_response(spec, resp)
    assert "truncated" in out.lower()
    assert len(out) < len(big)


def test_empty_response_is_stated_not_blank() -> None:
    resp = httpx.Response(204, text="", request=httpx.Request("GET", "https://e.com"))
    out = runner._format_response({"method": "GET"}, resp)
    assert "empty response" in out.lower()
    assert "HTTP 204" in out


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(list(globals().items())):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
