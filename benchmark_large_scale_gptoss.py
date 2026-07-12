"""Large-scale benchmark, take 2: same 5 business automation objectives
as benchmark_large_scale.py, but on gpt-oss:120b-cloud instead of phi3.

The phi3 run was invalid — both arms produced incoherent output at this
task size, which meant we were measuring phi3's coherence ceiling, not
the architecture. gpt-oss:120b-cloud is a genuinely capable model
(117B params, Ollama's cloud tier, same local API — no new API key),
so this is the first test in the session where the model itself isn't
the bottleneck.

gpt-oss is a reasoning model — it spends tokens on internal "thinking"
before the visible answer, so synthesis/critique get bigger token
budgets here (injected via Pipeline's existing DI constructor params,
not by editing the committed engine files) as insurance against
reasoning-token starvation truncating the real output to nothing.
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
from backend.app.orchestrator.synthesis import SynthesisEngine
from backend.app.critique.critique_agent import CritiqueEngine
from backend.app.contracts.clarification import ClarificationEngine

MODEL = "gpt-oss:120b-cloud"
CONFIG = {"limits": {"max_spawn_rate_per_minute": 60, "min_completeness_threshold": 0.7}}
SINGLE_CALL_MAX_TOKENS = 5000
SYNTHESIS_MAX_TOKENS = 5000
CRITIQUE_REFINE_MAX_TOKENS = 4000

BENCHMARK_PROMPTS = [
    "Design an end-to-end automation system for a small online retail business: automatically process incoming orders, sync inventory across the website and warehouse, send shipping notifications, handle return requests, and generate weekly sales reports. Specify the concrete workflow, triggers, and error-handling for each part.",
    "Design a complete automated hiring pipeline for a 50-person company: job posting distribution across multiple boards, automated resume screening against role criteria, interview scheduling with calendar sync, automated offer letter generation, and a new-hire onboarding checklist with account provisioning. Specify the workflow and decision logic for each stage.",
    "Design an end-to-end automated operations system for a small property management company: tenant maintenance request intake and routing to vendors, automated rent collection with late payment reminders, lease renewal tracking and notifications, vendor invoice processing and payment, and a monthly owner reporting dashboard. Specify the workflow and logic for each.",
    "Design a complete automation system for a local restaurant chain's back office: automated inventory reordering based on usage patterns, staff scheduling based on demand forecasts, supplier invoice reconciliation, customer loyalty program triggers, and daily sales/labor cost reporting. Specify the workflow and logic for each component.",
    "Design an end-to-end automated customer support and fulfillment system for a subscription box business: new subscriber onboarding emails, monthly box customization based on preferences, automated shipping label generation, churn-risk detection with win-back campaigns, and support ticket triage/routing. Specify the workflow and logic for each.",
]

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


def run_single_call(objective, adapter):
    prompt = f"""{objective}

Give a complete, thorough, well-organized answer in plain written prose
with markdown headings. Cover every part of the request in real detail
— this needs to be something a business could actually implement, not
a high-level summary. Do not output JSON."""
    return adapter.chat_completion(
        prompt, temperature=0.6, max_tokens=SINGLE_CALL_MAX_TOKENS, format=None
    )


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

    return text, snap, fallback, len(questions), enriched


def main():
    adapter_a = instrument(OllamaAdapter(model=MODEL), "single_call_large")
    adapter_b = instrument(OllamaAdapter(model=MODEL), "multiagent_large")

    results = []
    for i, objective in enumerate(BENCHMARK_PROMPTS, 1):
        print(f"\n[{i}/{len(BENCHMARK_PROMPTS)}] {objective[:70]}...")

        print("  running single-call baseline...")
        t0 = time.time()
        single_text = run_single_call(objective, adapter_a)
        single_time = time.time() - t0
        print(f"    done in {single_time:.1f}s, {len(single_text)} chars")

        print("  running full product (clarify+decompose+synthesize+critique)...")
        t0 = time.time()
        try:
            multi_text, snap, fallback, n_q, enriched = run_full_product(objective, adapter_b)
            multi_time = time.time() - t0
            score = (snap.get("critique") or {}).get("completeness_score")
            print(
                f"    done in {multi_time:.1f}s  questions={n_q}  "
                f"({snap['counts']['completed']}/{snap['counts']['total']} agents)  "
                f"critique={score}  refined={snap.get('was_refined')}  fallback={fallback}  "
                f"len={len(multi_text)}"
            )
        except Exception as exc:  # noqa: BLE001
            multi_text = f"(pipeline error: {exc})"
            multi_time = time.time() - t0
            snap = {"counts": {"total": 0, "completed": 0}}
            score, n_q, fallback = None, 0, True
            print(f"    ERROR: {exc}")

        if random.random() < 0.5:
            label_map = {"A": "single_call", "B": "multiagent"}
            response_a, response_b = single_text, multi_text
        else:
            label_map = {"A": "multiagent", "B": "single_call"}
            response_a, response_b = multi_text, single_text

        results.append(
            {
                "id": i,
                "objective": objective,
                "label_map": label_map,
                "response_a": response_a,
                "response_b": response_b,
                "single_call_seconds": round(single_time, 1),
                "multiagent_seconds": round(multi_time, 1),
                "multiagent_agents": snap["counts"]["total"],
                "multiagent_completed": snap["counts"]["completed"],
                "clarification_questions_asked": n_q,
                "critique_score": score,
                "synthesis_fallback": fallback,
            }
        )

    def totals_for(run_label):
        calls = [c for c in CALL_LOG if c["run"] == run_label]
        return len(calls), sum(c["tokens"] for c in calls)

    calls_a, tok_a = totals_for("single_call_large")
    calls_b, tok_b = totals_for("multiagent_large")

    output = {
        "model": MODEL,
        "results": results,
        "totals": {
            "single_call": {"llm_calls": calls_a, "tokens": tok_a},
            "multiagent": {"llm_calls": calls_b, "tokens": tok_b},
        },
    }

    with open("benchmark_result_large_gptoss.json", "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\n" + "=" * 60)
    print("LARGE-SCALE BENCHMARK COMPLETE (gpt-oss:120b-cloud)")
    print("=" * 60)
    print(f"single_call : {calls_a} LLM calls, {tok_a} tokens")
    print(f"multiagent  : {calls_b} LLM calls, {tok_b} tokens")
    print(f"ratio       : {tok_b/tok_a:.2f}x" if tok_a else "n/a")
    print("Saved to benchmark_result_large_gptoss.json")


if __name__ == "__main__":
    main()
