"""Re-runs ONLY the multiagent arm, now with the verified-refine fix in
pipeline_controller.py (re-critiques after every refine pass instead of
reporting a stale pre-refine score — see commit message / README).
Single_call baseline responses are reused unchanged from
benchmark_result_large_gptoss.json (Round 5, the most recent full
regeneration on gpt-oss:120b-cloud) so this isolates the effect of the fix.
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
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter
from backend.app.orchestrator.pipeline_controller import Pipeline
from backend.app.orchestrator.synthesis import SynthesisEngine
from backend.app.critique.critique_agent import CritiqueEngine
from backend.app.contracts.clarification import ClarificationEngine

MODEL = "gpt-oss:120b-cloud"
CONFIG = {"limits": {"max_spawn_rate_per_minute": 60, "min_completeness_threshold": 0.7, "max_refinement_depth": 2}}
SYNTHESIS_MAX_TOKENS = 5000
CRITIQUE_REFINE_MAX_TOKENS = 4000
SOURCE_FILE = "benchmark_result_large_gptoss.json"
OUTPUT_FILE = "benchmark_result_large_verified.json"

SIMULATED_ANSWERS = {
    "platform": "Standard, common tools a small business would already have access to — no exotic platform requirements.",
    "audience": "The business owner and their small operations team, not end customers directly.",
    "scope": "All five listed workflows matter, but keep each one concrete and implementable, not just a bullet-point idea.",
    "output_format": "A concrete operational specification — actual triggers, decision logic, and tool choices, not just prose about the topic.",
    "constraints": "Small business budget, no existing custom software, standard SaaS tools preferred over anything requiring an in-house dev team.",
    "general": "Be concrete and specific, not conceptual — this needs to be implementable.",
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


def run_full_product(objective, adapter):
    clarification_engine = ClarificationEngine({"max_questions": 5, "min_objective_words": 25})
    questions = clarification_engine.assess(objective, adapter)

    if questions:
        qa_pairs = simulate_answers(questions)
        brief = clarification_engine.build_brief(objective, qa_pairs)
        enriched = clarification_engine.enrich_objective(brief)
    else:
        enriched = objective

    pipeline = Pipeline(
        model_adapter=adapter,
        config=CONFIG,
        synthesis_engine=SynthesisEngine(adapter, max_tokens=SYNTHESIS_MAX_TOKENS),
        critique_engine=CritiqueEngine(adapter, max_tokens=CRITIQUE_REFINE_MAX_TOKENS),
    )
    manager = pipeline.run_objective(enriched, max_refinements=1)
    snap = manager.snapshot()

    text = snap.get("synthesized_output")
    fallback = False
    if not text:
        text = "(pipeline produced no usable output)"
        fallback = True

    return text, snap, fallback, len(questions)


def main():
    with open(SOURCE_FILE) as f:
        old_data = json.load(f)

    adapter = instrument(OllamaAdapter(model=MODEL), "multiagent_verified_refine")

    results = []
    for r in old_data["results"]:
        pid = r["id"]
        objective = r["objective"]
        single_call_text = (
            r["response_a"] if r["label_map"]["A"] == "single_call" else r["response_b"]
        )

        print(f"\n[{pid}/5] {objective[:70]}...")
        print("  running full product (verified-refine fix active)...")
        t0 = time.time()
        try:
            multi_text, snap, fallback, n_q = run_full_product(objective, adapter)
            elapsed = time.time() - t0
            crit = snap.get("critique") or {}
            score = crit.get("completeness_score")
            print(
                f"    done in {elapsed:.1f}s  questions={n_q}  "
                f"({snap['counts']['completed']}/{snap['counts']['total']} agents)  "
                f"completeness={score}  over_engineered={crit.get('over_engineered')}  "
                f"fabricated={len(crit.get('fabricated_claims') or [])}  "
                f"refined={snap.get('was_refined')}  fallback={fallback}  len={len(multi_text)}"
            )
        except Exception as exc:  # noqa: BLE001
            multi_text = f"(pipeline error: {exc})"
            elapsed = time.time() - t0
            snap = {"counts": {"total": 0, "completed": 0}}
            score, n_q, fallback = None, 0, True
            print(f"    ERROR: {exc}")

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
                "label_map": label_map,
                "response_a": response_a,
                "response_b": response_b,
                "multiagent_seconds": round(elapsed, 1),
                "multiagent_agents": snap["counts"]["total"],
                "multiagent_completed": snap["counts"]["completed"],
                "clarification_questions_asked": n_q,
                "critique_score": score,
                "synthesis_fallback": fallback,
            }
        )

    total_tokens = sum(c["tokens"] for c in CALL_LOG)
    output = {
        "model": MODEL,
        "fix": "verified_refine (re-critiques after every refine pass instead of reporting a stale pre-refine score)",
        "results": results,
        "totals": {
            "single_call": old_data["totals"]["single_call"],
            "multiagent_verified_refine": {"llm_calls": len(CALL_LOG), "tokens": total_tokens},
        },
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\n" + "=" * 60)
    print("VERIFIED-REFINE RE-RUN COMPLETE")
    print("=" * 60)
    print(f"multiagent_verified_refine: {len(CALL_LOG)} LLM calls, {total_tokens} tokens")
    print(f"Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
