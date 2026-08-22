"""Demo/smoke-test for the Employee abstraction + IdeaValidationEmployee
(README Phase 7 + Phase 10) on real Ollama.

Runs the SAME employee_id through two tasks — an initial idea, then a
revision of it — to prove memory actually persists and gets folded back
into the second run, which is the entire point of the Employee
abstraction over a stateless Pipeline.run_objective() call.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.INFO, format="%(message)s")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter
from backend.app.orchestrator.pipeline_controller import Pipeline
from backend.app.orchestrator.synthesis import SynthesisEngine
from backend.app.critique.critique_agent import CritiqueEngine
from backend.app.employees.idea_validation_employee import IdeaValidationEmployee

MODEL = "gpt-oss:120b-cloud"
CONFIG = {
    "limits": {
        "max_spawn_rate_per_minute": 60,
        "min_completeness_threshold": 0.7,
        "max_refinement_depth": 2,
    }
}
EMPLOYEE_ID = "demo_founder_idea_validator"

IDEA_V1 = (
    "A subscription box that sends small business owners a curated set of "
    "office snacks every month."
)
IDEA_V2 = (
    "Same office-snacks subscription idea as before, but repositioned as a "
    "wellness/productivity perk employers buy for remote teams instead of "
    "a snack box owners order for themselves."
)


def run_task(employee, idea, label):
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}\n{idea}\n")
    result = employee.run_task(idea)
    print("\n--- VERDICT ---\n")
    print(result["output"])
    critique = result["critique"] or {}
    print(f"\ncompleteness_score = {critique.get('completeness_score')}")
    print(f"fabricated_claims = {critique.get('fabricated_claims')}")
    print(f"was_refined = {result['was_refined']}")
    return result


def main():
    adapter = OllamaAdapter(model=MODEL)
    pipeline = Pipeline(
        model_adapter=adapter,
        config=CONFIG,
        synthesis_engine=SynthesisEngine(adapter, max_tokens=5000),
        critique_engine=CritiqueEngine(adapter, max_tokens=4000),
    )
    employee = IdeaValidationEmployee(employee_id=EMPLOYEE_ID, pipeline=pipeline)

    print(f"Employee memory has {len(employee.history())} entrie(s) before this run.")

    run_task(employee, IDEA_V1, "Task 1: validating the original idea")
    result2 = run_task(employee, IDEA_V2, "Task 2: validating the repositioned idea")

    print(f"\n{'=' * 60}")
    print(f"Employee memory now has {len(employee.history())} entrie(s) total.")
    print("Checking whether Task 2's verdict actually referenced Task 1...")
    mentions_history = any(
        kw in (result2["output"] or "").lower()
        for kw in ("previous", "earlier", "before", "original", "revision", "repositioned", "changed")
    )
    print(f"References prior context: {mentions_history}")


if __name__ == "__main__":
    main()
