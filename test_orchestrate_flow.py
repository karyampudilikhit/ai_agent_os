"""Real-model end-to-end test of the full Vision AI flow (backend/app/
orchestrator/orchestrate.py). One prompt in, watched all the way through:

    prompt -> AdaptiveSupervisor -> EmployeeSpawner -> DynamicEmployees
           -> EmployeeCoordinator (Plan B: parallel-then-merged)
           -> Synthesis -> final deliverable

Prints what the team looked like, each teammate's verification metadata,
and the final merged output.
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
from backend.app.orchestrator.orchestrate import orchestrate

MODEL = "gpt-oss:120b-cloud"
CONFIG = {
    "limits": {
        "max_spawn_rate_per_minute": 60,
        "min_completeness_threshold": 0.7,
        "max_refinement_depth": 2,
    }
}

PROMPT = (
    "I want to launch a paid Substack newsletter for solo founders building AI "
    "products. Help me get from zero to my first 100 paying subscribers — "
    "positioning, content, launch plan."
)


def main():
    adapter = OllamaAdapter(model=MODEL)
    pipeline = Pipeline(
        model_adapter=adapter,
        config=CONFIG,
        synthesis_engine=SynthesisEngine(adapter, max_tokens=5000),
        critique_engine=CritiqueEngine(adapter, max_tokens=4000),
    )

    result = orchestrate(PROMPT, pipeline=pipeline, force_team=True)

    print("\n" + "=" * 60)
    print("ORCHESTRATE RESULT")
    print("=" * 60)
    print(f"mode:       {result['mode']}")
    print(f"session_id: {result['session_id']}")
    print(f"team_size:  {len(result['team'])}")
    print("\n--- TEAM ---")
    for m in result["team"]:
        print(f"  * {m.get('role'):<26} "
              f"completeness={m.get('completeness_score')}  "
              f"fabricated={len(m.get('fabricated_claims') or [])}  "
              f"refined={m.get('was_refined')}")

    print("\n--- FINAL DELIVERABLE ---\n")
    print(result["final_output"])


if __name__ == "__main__":
    main()
