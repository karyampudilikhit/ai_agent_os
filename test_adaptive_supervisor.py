"""Real-model test of the adaptive supervisor (backend/app/orchestrator/
adaptive_supervisor.py) against gpt-oss:120b-cloud — the routing half of
Phase 6 that decides single_call / single_call_critique / multi_agent_critique
before Pipeline spends anything on a task.

Two parts:

1. Classification-only sanity checks (cheap — one short call each): a
   handful of cases with a clear expected tier, PLUS the 5 original
   benchmark objectives and the idea-validation objective, reported for
   comparison against what we already know from the benchmark history
   (informational, not pass/fail — we never got a clean "right answer"
   for those from the judge).

2. One real end-to-end run per tier extreme (single_call vs
   multi_agent_critique) through the FULL pipeline, to prove routing
   changes actual behavior (agent count, tokens) and not just the
   classification label.
"""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.WARNING)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter
from backend.app.orchestrator.adaptive_supervisor import AdaptiveSupervisor
from backend.app.orchestrator.pipeline_controller import Pipeline
from backend.app.orchestrator.synthesis import SynthesisEngine
from backend.app.critique.critique_agent import CritiqueEngine

MODEL = "gpt-oss:120b-cloud"

CLEAR_CASES = [
    ("single_call", "Write a one-paragraph thank-you note to a customer who just placed their first order."),
    ("single_call_critique", "Write our fundraising deck's traction slide, citing our exact current MRR, growth rate, and retention numbers."),
    ("multi_agent_critique",
     "A founder has a raw startup idea and needs it validated before they commit real time or money to it. "
     "Their idea: \"A subscription box for office snacks.\" Produce market research, competitor analysis, "
     "a feasibility assessment, and a clear go/no-go verdict."),
]

BENCHMARK_OBJECTIVES = [
    "Design an end-to-end automation system for a small online retail business: automatically process incoming orders, sync inventory across the website and warehouse, send shipping notifications, handle return requests, and generate weekly sales reports. Specify the concrete workflow, triggers, and error-handling for each part.",
    "Design a complete automated hiring pipeline for a 50-person company: job posting distribution across multiple boards, automated resume screening against role criteria, interview scheduling with calendar sync, automated offer letter generation, and a new-hire onboarding checklist with account provisioning. Specify the workflow and decision logic for each stage.",
    "Design an end-to-end automated operations system for a small property management company: tenant maintenance request intake and routing to vendors, automated rent collection with late payment reminders, lease renewal tracking and notifications, vendor invoice processing and payment, and a monthly owner reporting dashboard. Specify the workflow and logic for each.",
    "Design a complete automation system for a local restaurant chain's back office: automated inventory reordering based on usage patterns, staff scheduling based on demand forecasts, supplier invoice reconciliation, customer loyalty program triggers, and daily sales/labor cost reporting. Specify the workflow and logic for each component.",
    "Design an end-to-end automated customer support and fulfillment system for a subscription box business: new subscriber onboarding emails, monthly box customization based on preferences, automated shipping label generation, churn-risk detection with win-back campaigns, and support ticket triage/routing. Specify the workflow and logic for each.",
]


def part1_classification(adapter):
    print("=" * 60)
    print("PART 1: classification sanity checks")
    print("=" * 60)
    sup = AdaptiveSupervisor(model_adapter=adapter)

    correct = 0
    for expected, objective in CLEAR_CASES:
        got = sup.classify(objective)
        mark = "OK" if got == expected else "MISMATCH"
        if got == expected:
            correct += 1
        print(f"[{mark}] expected={expected:22s} got={got:22s} :: {objective[:70]}...")

    print(f"\nClear-case accuracy: {correct}/{len(CLEAR_CASES)}")

    print("\n--- Benchmark objectives (informational — no known-correct tier) ---")
    for i, objective in enumerate(BENCHMARK_OBJECTIVES, 1):
        got = sup.classify(objective)
        print(f"[{i}/5] {got:22s} :: {objective[:70]}...")


def part2_end_to_end(adapter):
    print("\n" + "=" * 60)
    print("PART 2: end-to-end routing proof (real pipeline runs)")
    print("=" * 60)

    pipeline = Pipeline(
        model_adapter=adapter,
        config={"limits": {"max_spawn_rate_per_minute": 60, "min_completeness_threshold": 0.7, "max_refinement_depth": 2}},
        synthesis_engine=SynthesisEngine(adapter, max_tokens=5000),
        critique_engine=CritiqueEngine(adapter, max_tokens=4000),
    )

    trivial = "Write a one-paragraph thank-you note to a customer who just placed their first order."
    complex_task = CLEAR_CASES[2][1]  # the idea-validation-style task

    for label, objective in (("TRIVIAL (expect single_call)", trivial), ("COMPLEX (expect multi_agent_critique)", complex_task)):
        print(f"\n--- {label} ---")
        t0 = time.time()
        manager = pipeline.run_objective(objective)  # no tier override — real routing decision
        elapsed = time.time() - t0
        snap = manager.snapshot()
        print(f"elapsed={elapsed:.1f}s  agents={snap['counts']['total']}  "
              f"tokens={snap['totals']['tokens']}  status={snap['status']}  "
              f"output_len={len(snap.get('synthesized_output') or '')}")


def main():
    adapter = OllamaAdapter(model=MODEL)
    part1_classification(adapter)
    part2_end_to_end(adapter)
    print("\nDONE")


if __name__ == "__main__":
    main()
