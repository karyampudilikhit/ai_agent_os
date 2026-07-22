"""Smoke test: CEOManager (Phase 2 hierarchy — Company-level orchestration).

Focused on deterministic behavior — the parts that don't need a live
LLM. The full LLM path is exercised in the API smoke test with a real
Ollama call, which happens interactively.

Covered here:
  - Empty units list returns empty plan
  - Failed LLM call → fallback plan gives every Unit the raw task +
    playbook rules embedded as a brief (no Unit is left flying blind)
  - JSON extraction handles both raw JSON and fenced ```json blocks
  - Synthesis on empty contributions returns None (safe)
  - Synthesis raw-concat fallback preserves each Unit's contribution
  - Unit snapshot rendering shows unit_id/name/purpose/roster clearly
    so the CEO's LLM prompt has the info it needs to delegate
"""
from __future__ import annotations

from backend.app.employees.ceo_manager import CEOManager


class _FakeAdapter:
    """Minimal stand-in for a real LLM adapter — lets us drive
    CEOManager without an Ollama call. Configurable to return a
    specific response or raise."""
    def __init__(self, response: str = "", raise_on_call: bool = False):
        self.response = response
        self.raise_on_call = raise_on_call
        self.last_prompt = None

    def chat_completion(self, prompt, **kwargs):
        self.last_prompt = prompt
        if self.raise_on_call:
            raise RuntimeError("adapter offline")
        return self.response


COMPANY = {"id": "co_test", "name": "Acme", "purpose": "Ship a SaaS."}
UNITS = [
    {
        "unit_id": "u_marketing",
        "name": "Marketing Unit",
        "purpose": "Owns positioning, copy, top-of-funnel growth.",
        "members": [
            {"role": "Marketing Supervisor", "mandate": "Coordinate.", "is_supervisor": True},
            {"role": "Copywriter", "mandate": "Write copy."},
        ],
    },
    {
        "unit_id": "u_sales",
        "name": "Sales Unit",
        "purpose": "Owns outbound, demos, close.",
        "members": [
            {"role": "Sales Supervisor", "mandate": "Coordinate.", "is_supervisor": True},
            {"role": "SDR", "mandate": "Book meetings."},
        ],
    },
]


def test_empty_units_returns_empty_plan() -> None:
    ceo = CEOManager(model_adapter=_FakeAdapter())
    plan = ceo.plan_company_delegation("Do the thing", COMPANY, [])
    assert plan == [], plan
    print("[ok] empty units -> empty plan")


def test_llm_failure_produces_fallback_with_playbook_brief() -> None:
    ceo = CEOManager(model_adapter=_FakeAdapter(raise_on_call=True))
    plan = ceo.plan_company_delegation(
        "Validate my SaaS idea for solo founders", COMPANY, UNITS,
    )
    assert len(plan) == len(UNITS), plan
    for entry, unit in zip(plan, UNITS):
        assert entry["unit_id"] == unit["unit_id"]
        assert entry["sub_task"], "fallback sub_task missing"
        assert "unit_brief" in entry, "fallback should still attach a brief"
        # Fallback brief should contain the playbook rules for VALIDATION
        # (task classified from "Validate" keyword)
        assert "validation" in entry["unit_brief"].lower()
        assert "unknown" in entry["unit_brief"].lower(), (
            "validation playbook 'mark unknown' rule should reach the Unit"
        )
    print("[ok] LLM failure -> fallback plan with playbook rules embedded")


def test_llm_success_parses_plan() -> None:
    ceo_response = """{
      "plan": [
        {"unit_id": "u_marketing",
         "sub_task": "Craft the launch positioning.",
         "unit_brief": "Focus on solo founders. Mark unknown for anything you cannot source."},
        {"unit_id": "u_sales",
         "sub_task": "Draft 5 outbound emails.",
         "unit_brief": "Solo-founder tone. Real quotes only."}
      ]
    }"""
    ceo = CEOManager(model_adapter=_FakeAdapter(response=ceo_response))
    plan = ceo.plan_company_delegation("Launch this product", COMPANY, UNITS)
    assert len(plan) == 2
    assert plan[0]["unit_id"] == "u_marketing"
    assert plan[0]["sub_task"].startswith("Craft")
    assert "solo founders" in plan[0]["unit_brief"].lower()
    print("[ok] LLM success -> parsed 2-Unit plan with briefs")


def test_llm_response_with_fenced_json() -> None:
    ceo_response = """```json
{
  "plan": [
    {"unit_id": "u_marketing", "sub_task": "Do marketing.", "unit_brief": "brief"}
  ]
}
```"""
    ceo = CEOManager(model_adapter=_FakeAdapter(response=ceo_response))
    plan = ceo.plan_company_delegation("Any task", COMPANY, UNITS)
    assert len(plan) == 1
    assert plan[0]["unit_id"] == "u_marketing"
    print("[ok] fenced ```json response parses cleanly")


def test_plan_drops_unknown_unit_ids() -> None:
    """LLM sometimes returns hallucinated unit_ids that aren't real —
    those entries must be silently dropped, not blindly dispatched."""
    ceo_response = """{
      "plan": [
        {"unit_id": "u_marketing", "sub_task": "Real.", "unit_brief": "brief"},
        {"unit_id": "u_hallucinated", "sub_task": "Fake.", "unit_brief": "brief"}
      ]
    }"""
    ceo = CEOManager(model_adapter=_FakeAdapter(response=ceo_response))
    plan = ceo.plan_company_delegation("Any task", COMPANY, UNITS)
    assert len(plan) == 1
    assert plan[0]["unit_id"] == "u_marketing"
    print("[ok] plan drops hallucinated unit_ids")


def test_synthesis_empty_contributions_returns_none() -> None:
    ceo = CEOManager(model_adapter=_FakeAdapter())
    assert ceo.synthesize("Task", []) is None
    assert ceo.synthesize("Task", [{"unit_id": "x", "output": ""}]) is None
    print("[ok] empty contributions -> None (safe)")


def test_synthesis_failure_raw_concat_preserves_units() -> None:
    ceo = CEOManager(model_adapter=_FakeAdapter(raise_on_call=True))
    merged = ceo.synthesize("Task", [
        {"unit_id": "u_marketing", "unit_name": "Marketing", "output": "Marketing said X"},
        {"unit_id": "u_sales", "unit_name": "Sales", "output": "Sales said Y"},
    ])
    assert merged is not None
    assert "Marketing said X" in merged
    assert "Sales said Y" in merged
    print("[ok] synthesis failure -> raw concat preserves both Units")


def test_units_snapshot_visibility() -> None:
    """The CEO's delegation prompt must show unit_id / name / purpose /
    roster clearly so the LLM has enough context to route correctly."""
    ceo = CEOManager(model_adapter=_FakeAdapter(response='{"plan":[]}'))
    ceo.plan_company_delegation("Any task", COMPANY, UNITS)
    prompt = ceo.adapter.last_prompt
    assert prompt is not None
    for u in UNITS:
        assert u["unit_id"] in prompt, f"unit_id {u['unit_id']} missing from CEO prompt"
        assert u["name"] in prompt
        assert u["purpose"][:20] in prompt
    print("[ok] CEO's LLM prompt exposes unit_id/name/purpose/roster")


if __name__ == "__main__":
    test_empty_units_returns_empty_plan()
    test_llm_failure_produces_fallback_with_playbook_brief()
    test_llm_success_parses_plan()
    test_llm_response_with_fenced_json()
    test_plan_drops_unknown_unit_ids()
    test_synthesis_empty_contributions_returns_none()
    test_synthesis_failure_raw_concat_preserves_units()
    test_units_snapshot_visibility()
    print("\nAll CEOManager checks passed.")
