"""Targeted re-test of the multi-cycle critique/refine fix in
pipeline_controller.py, against the ONE objective the independent judge
(judge_benchmark.py) confirmed had fabricated evidence survive refinement
under the old single-pass code (task 5 of benchmark_result_large_gptoss.json:
'tested at 3 locations', a fabricated health-check tool).

Not a full benchmark re-run — just checks whether the fix actually closes
this specific, proven gap before paying for a full 5-task re-run.
"""

import json
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

MODEL = "gpt-oss:120b-cloud"
OBJECTIVE = (
    "Design an end-to-end automated customer support and fulfillment system "
    "for a subscription box business: new subscriber onboarding emails, "
    "monthly box customization based on preferences, automated shipping "
    "label generation, churn-risk detection with win-back campaigns, and "
    "support ticket triage/routing. Specify the workflow and logic for each."
)
CONFIG = {"limits": {"max_spawn_rate_per_minute": 60, "min_completeness_threshold": 0.7, "max_refinement_depth": 2}}


def main():
    adapter = OllamaAdapter(model=MODEL)
    pipeline = Pipeline(
        model_adapter=adapter,
        config=CONFIG,
        synthesis_engine=SynthesisEngine(adapter, max_tokens=5000),
        critique_engine=CritiqueEngine(adapter, max_tokens=4000),
    )
    manager = pipeline.run_objective(OBJECTIVE, max_refinements=1)
    snap = manager.snapshot()

    critique = snap.get("critique") or {}
    print("\n" + "=" * 60)
    print("FINAL STATE")
    print("=" * 60)
    print(f"was_refined = {snap.get('was_refined')}")
    print(f"completeness_score = {critique.get('completeness_score')}")
    print(f"fabricated_claims = {critique.get('fabricated_claims')}")
    print(f"over_engineered = {critique.get('over_engineered')}")

    output_text = snap.get("synthesized_output") or ""
    with open("test_verify_refine_fix_output.json", "w") as f:
        json.dump({"objective": OBJECTIVE, "final_critique": critique,
                    "was_refined": snap.get("was_refined"),
                    "output": output_text}, f, indent=2, default=str)
    print(f"\nFull output saved to test_verify_refine_fix_output.json ({len(output_text)} chars)")


if __name__ == "__main__":
    main()
