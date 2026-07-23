"""Smoke test: HierarchyDesigner (Phase 3a — prompt-driven org chart).

Covers deterministic behavior: prompt shape, LLM-failure fallback,
JSON parsing (including fenced blocks), malformed responses, and the
"every Unit must have at least one specialist" guarantee.
"""
from __future__ import annotations

from backend.app.employees.hierarchy_designer import HierarchyDesigner


class _FakeAdapter:
    def __init__(self, response: str = "", raise_on_call: bool = False):
        self.response = response
        self.raise_on_call = raise_on_call
        self.last_prompt = None

    def chat_completion(self, prompt, **kwargs):
        self.last_prompt = prompt
        if self.raise_on_call:
            raise RuntimeError("adapter offline")
        return self.response


def test_empty_description_returns_fallback() -> None:
    d = HierarchyDesigner(model_adapter=_FakeAdapter())
    units = d.design("")
    assert len(units) == 1
    assert units[0]["name"] == "General Unit"
    print("[ok] empty description -> single General Unit fallback")


def test_llm_failure_returns_fallback() -> None:
    d = HierarchyDesigner(model_adapter=_FakeAdapter(raise_on_call=True))
    units = d.design("Some real description of a company")
    assert len(units) == 1
    assert units[0]["name"] == "General Unit"
    assert units[0]["specialists"], "fallback still needs a specialist"
    print("[ok] LLM failure -> founder still gets a working scaffold")


def test_parses_full_org_chart() -> None:
    response = """{
      "units": [
        {"name": "Market Research Unit",
         "purpose": "Own market sizing.",
         "specialists": [
           {"role": "Market Researcher", "mandate": "Do research."},
           {"role": "Competitive Analyst", "mandate": "Track competitors."}
         ]},
        {"name": "Go-To-Market Unit",
         "purpose": "Own positioning and launch.",
         "specialists": [
           {"role": "Positioning Strategist", "mandate": "Craft positioning."}
         ]}
      ]
    }"""
    d = HierarchyDesigner(model_adapter=_FakeAdapter(response=response))
    units = d.design("SaaS for solo founders")
    assert len(units) == 2
    assert units[0]["name"] == "Market Research Unit"
    assert len(units[0]["specialists"]) == 2
    assert units[1]["name"] == "Go-To-Market Unit"
    assert len(units[1]["specialists"]) == 1
    print("[ok] LLM success -> parsed 2 Units with distinct specialists")


def test_fenced_json_parses() -> None:
    response = """```json
{"units": [{"name": "X Unit", "purpose": "Do X.", "specialists": [{"role": "R", "mandate": "M"}]}]}
```"""
    d = HierarchyDesigner(model_adapter=_FakeAdapter(response=response))
    units = d.design("desc")
    assert len(units) == 1
    print("[ok] fenced ```json response parses cleanly")


def test_unit_without_specialists_gets_generalist() -> None:
    """No Unit should ever be materialized without at least one
    specialist — an empty Unit can't produce anything."""
    response = """{"units": [
      {"name": "Empty Unit", "purpose": "Something.", "specialists": []}
    ]}"""
    d = HierarchyDesigner(model_adapter=_FakeAdapter(response=response))
    units = d.design("desc")
    assert len(units) == 1
    assert len(units[0]["specialists"]) == 1
    assert units[0]["specialists"][0]["role"] == "Generalist"
    print("[ok] Unit with no specialists gets a Generalist auto-added")


def test_malformed_units_dropped() -> None:
    response = """{"units": [
      {"name": "Real Unit", "purpose": "Real purpose.",
       "specialists": [{"role": "R", "mandate": "M"}]},
      {"name": "", "purpose": "", "specialists": []}
    ]}"""
    d = HierarchyDesigner(model_adapter=_FakeAdapter(response=response))
    units = d.design("desc")
    assert len(units) == 1
    assert units[0]["name"] == "Real Unit"
    print("[ok] Units with empty name/purpose are dropped, not crashed on")


def test_prompt_includes_description() -> None:
    """Verify the founder's description actually reaches the LLM."""
    adapter = _FakeAdapter(response='{"units":[]}')
    d = HierarchyDesigner(model_adapter=adapter)
    d.design("A SaaS for solo founders launching AI products")
    assert "solo founders launching AI products" in (adapter.last_prompt or "")
    print("[ok] founder's description reaches the LLM prompt")


if __name__ == "__main__":
    test_empty_description_returns_fallback()
    test_llm_failure_returns_fallback()
    test_parses_full_org_chart()
    test_fenced_json_parses()
    test_unit_without_specialists_gets_generalist()
    test_malformed_units_dropped()
    test_prompt_includes_description()
    print("\nAll HierarchyDesigner checks passed.")
