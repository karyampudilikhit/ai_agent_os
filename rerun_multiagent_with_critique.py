"""Round 3: re-runs ONLY the multiagent arm, now with critique/refine
on top of synthesis. Single_call baseline responses are reused
unchanged from round 1 (already correct — prose, format=None)."""

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

MODEL = "phi3"
CONFIG = {"limits": {"max_spawn_rate_per_minute": 60, "min_completeness_threshold": 0.7}}

CALL_LOG = []


def instrument(adapter, run_label):
    original = adapter.chat_completion

    def wrapped(prompt, **kwargs):
        response = original(prompt, **kwargs)
        CALL_LOG.append({"run": run_label, "tokens": (len(prompt) + len(response)) // 4})
        return response

    adapter.chat_completion = wrapped
    return adapter


def run_multiagent(objective, adapter):
    pipeline = Pipeline(model_adapter=adapter, config=CONFIG)
    manager = pipeline.run_objective(objective, max_refinements=1)
    snap = manager.snapshot()
    text = snap.get("synthesized_output")
    used_fallback = False
    if not text:
        text = "(pipeline produced no usable output)"
        used_fallback = True
    return text, snap, used_fallback


def main():
    with open("benchmark_result.json") as f:
        old_data = json.load(f)

    adapter = instrument(OllamaAdapter(model=MODEL), "multiagent_v3")

    results = []
    for r in old_data["results"]:
        pid = r["id"]
        objective = r["objective"]
        single_call_text = (
            r["response_a"] if r["label_map"]["A"] == "single_call" else r["response_b"]
        )

        print(f"[{pid}/10] {objective[:60]}...")
        t0 = time.time()
        multi_text, snap, fallback = run_multiagent(objective, adapter)
        elapsed = time.time() - t0
        score = (snap.get("critique") or {}).get("completeness_score")
        print(
            f"  done in {elapsed:.1f}s  ({snap['counts']['completed']}/{snap['counts']['total']} agents)"
            f"  critique_score={score}  refined={snap.get('was_refined')}  fallback={fallback}"
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
            "multiagent_v3": {"llm_calls": len(CALL_LOG), "tokens": total_tokens},
        },
    }

    with open("benchmark_result_v3.json", "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\nDone. Saved benchmark_result_v3.json")
    print(f"multiagent_v3 (synthesis+critique): {len(CALL_LOG)} LLM calls, {total_tokens} tokens")
    refined_count = sum(1 for r in results if r["was_refined"])
    print(f"Refined: {refined_count}/10 prompts (rest passed critique threshold as-is)")


if __name__ == "__main__":
    main()
