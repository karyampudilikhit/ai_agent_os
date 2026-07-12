"""Round 4: adds the real clarification stage on top of round 3's
synthesis + critique/refine. Single_call baseline is UNCHANGED from
round 1 — it represents the real-world "type once into ChatGPT"
experience, which doesn't get pre-answered clarifying questions either.

This tests the actual product claim: does the full real product
experience (clarify -> decompose -> synthesize -> critique/refine)
beat typing straight into ChatGPT? Unlike rounds 1-3, this is NOT an
architecture-isolation test — clarification's contribution is
deliberately included, since that's what a real product user gets.
"""

import json
import logging
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.WARNING)

from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter
from backend.app.orchestrator.pipeline_controller import Pipeline
from backend.app.contracts.clarification import ClarificationEngine

MODEL = "phi3"
CONFIG = {"limits": {"max_spawn_rate_per_minute": 60, "min_completeness_threshold": 0.7}}

# Stand-in answers — same role as in the original clarification
# validation run: representative of what a founder would actually
# type, not tuned per-prompt.
SIMULATED_ANSWERS = {
    "platform": "Whatever fits a lean two-person team shipping fast — keep it simple, don't try to support everything at once.",
    "audience": "The primary end users already described in the request — keep scope tight to their core need, nothing broader.",
    "scope": "Focus on the single most essential feature first; nice-to-haves can come later.",
    "output_format": "Working code and a short spec, not just prose — something that could actually be handed to a developer.",
    "constraints": "No existing brand or tech stack to match. Keep it lightweight and use standard, common choices.",
    "general": "Keep it simple, practical, and focused on the core of what was asked.",
}

CALL_LOG = []


def instrument(adapter, run_label):
    original = adapter.chat_completion

    def wrapped(prompt, **kwargs):
        response = original(prompt, **kwargs)
        CALL_LOG.append({"run": run_label, "tokens": (len(prompt) + len(response)) // 4})
        return response

    adapter.chat_completion = wrapped
    return adapter


def simulate_answers(questions):
    return [(q, SIMULATED_ANSWERS.get(q.category, SIMULATED_ANSWERS["general"])) for q in questions]


def run_multiagent_full(objective, adapter):
    clarification_engine = ClarificationEngine({"max_questions": 5, "min_objective_words": 25})
    questions = clarification_engine.assess(objective, adapter)

    if questions:
        qa_pairs = simulate_answers(questions)
        brief = clarification_engine.build_brief(objective, qa_pairs)
        enriched = clarification_engine.enrich_objective(brief)
    else:
        enriched = objective

    pipeline = Pipeline(model_adapter=adapter, config=CONFIG)
    manager = pipeline.run_objective(enriched, max_refinements=1)
    snap = manager.snapshot()

    text = snap.get("synthesized_output")
    used_fallback = False
    if not text:
        text = "(pipeline produced no usable output)"
        used_fallback = True

    return text, snap, used_fallback, len(questions), enriched


def main():
    with open("benchmark_result.json") as f:
        old_data = json.load(f)

    adapter = instrument(OllamaAdapter(model=MODEL), "multiagent_v4")

    results = []
    for r in old_data["results"]:
        pid = r["id"]
        objective = r["objective"]
        single_call_text = (
            r["response_a"] if r["label_map"]["A"] == "single_call" else r["response_b"]
        )

        print(f"[{pid}/10] {objective[:60]}...")
        t0 = time.time()
        multi_text, snap, fallback, n_questions, enriched = run_multiagent_full(objective, adapter)
        elapsed = time.time() - t0
        score = (snap.get("critique") or {}).get("completeness_score")
        print(
            f"  done in {elapsed:.1f}s  questions={n_questions}  "
            f"({snap['counts']['completed']}/{snap['counts']['total']} agents)  "
            f"critique_score={score}  refined={snap.get('was_refined')}  fallback={fallback}"
        )

        if random.random() < 0.5:
            label_map = {"A": "single_call", "B": "multiagent"}
            response_a, response_b = single_call_text, multi_text
        else:
            label_map = {"A": "multiagent", "B": "single_call"}
            response_a, response_b = multi_text, single_call_text

        results.append(
            {
                "id": pid,
                "objective": objective,
                "enriched_objective": enriched,
                "clarification_questions_asked": n_questions,
                "label_map": label_map,
                "response_a": response_a,
                "response_b": response_b,
                "multiagent_seconds": round(elapsed, 1),
                "multiagent_agents": snap["counts"]["total"],
                "multiagent_completed": snap["counts"]["completed"],
                "critique_score": score,
                "was_refined": snap.get("was_refined"),
                "synthesis_fallback": fallback,
            }
        )

    total_tokens = sum(c["tokens"] for c in CALL_LOG)
    output = {
        "results": results,
        "totals": {
            "single_call": old_data["totals"]["single_call"],
            "multiagent_v4": {"llm_calls": len(CALL_LOG), "tokens": total_tokens},
        },
    }

    with open("benchmark_result_v4.json", "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\nDone. Saved benchmark_result_v4.json")
    print(f"multiagent_v4 (clarify+synthesis+critique): {len(CALL_LOG)} LLM calls, {total_tokens} tokens")
    refined_count = sum(1 for r in results if r["was_refined"])
    clarified_count = sum(1 for r in results if r["clarification_questions_asked"] > 0)
    print(f"Clarified: {clarified_count}/10, Refined: {refined_count}/10")


if __name__ == "__main__":
    main()
