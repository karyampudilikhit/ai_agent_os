"""BENCHMARK / EVALUATION SCRIPT — not part of the product, a test tool.

Tests one question: does decomposing an objective into multiple
specialized sub-agents produce a BETTER result than one direct call to
the same model? Same model (phi3) on both sides, same starting
objective on both sides (no clarification stage — that's a separately
proven improvement, kept out here so it doesn't confound this test) —
architecture is the only variable.

Produces a blind comparison report: for each of 10 objectives, two
unlabeled responses (randomly ordered so there's no positional bias).
A human reads both blind and picks a winner. Token/cost/time are
tracked automatically and revealed after voting, not before.
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

MODEL = "phi3"

BENCHMARK_PROMPTS = [
    "Design a complete onboarding flow for a fitness app, including UI screen ideas, welcome copy, and a signup form spec.",
    "Create a go-to-market plan for a new productivity SaaS tool aimed at freelancers, including target audience, pricing tiers, and a 90-day launch timeline.",
    "Design a REST API for a simple blog platform, including data models, endpoints, and an authentication approach.",
    "Write a content strategy for a personal finance YouTube channel, including video topic ideas, a posting schedule, and a thumbnail/title formula.",
    "Create a security review checklist for a small e-commerce website, covering payment handling, user data, and common vulnerabilities.",
    "Write a research summary recommending React Native or Flutter for a two-person startup building a mobile app, with reasoning.",
    "Plan a small business's first paid Instagram ad campaign, including audience targeting, budget allocation, ad copy, and success metrics.",
    "Design a database schema and basic CRUD API for a task management tool for small teams.",
    "Write a QA test plan for a mobile banking app's login and money transfer features.",
    "Write a README and setup guide for an open-source Python CLI tool that converts CSV to JSON.",
]

CALL_LOG = []


def instrument(adapter, run_label):
    original = adapter.chat_completion

    def wrapped(prompt, **kwargs):
        start = time.time()
        response = original(prompt, **kwargs)
        elapsed = time.time() - start
        CALL_LOG.append(
            {
                "run": run_label,
                "tokens_in": len(prompt) // 4,
                "tokens_out": len(response) // 4,
                "seconds": round(elapsed, 2),
            }
        )
        return response

    adapter.chat_completion = wrapped
    return adapter


def run_single_call(objective, adapter):
    """Baseline: one direct call, plain-language complete answer —
    mimics typing the objective straight into ChatGPT once.

    format=None overrides the adapter's default JSON-constrained mode
    (correct for the pipeline's structured calls, wrong here — a real
    ChatGPT answer is prose, not a JSON dump)."""
    prompt = f"""{objective}

Give a complete, thorough, well-organized answer in plain written prose
with markdown headings. Cover every part of the request. Be specific
and concrete — write as if this is the final deliverable, not a
summary of what you'd do. Do not output JSON."""
    return adapter.chat_completion(prompt, temperature=0.6, max_tokens=2500, format=None)


def run_multiagent(objective, adapter, config):
    """Full pipeline: contract -> specialized agents -> raw stitched
    output. No clarification (kept out to isolate architecture as the
    only variable) and no synthesis pass (pipeline doesn't have one
    yet — this is today's actual raw output, not an idealized version)."""
    pipeline = Pipeline(model_adapter=adapter, config=config)
    manager = pipeline.run_objective(objective, max_refinements=1)
    snap = manager.snapshot()
    return _aggregate(snap), snap


def _render_value(value, indent=0):
    pad = "  " * indent
    if isinstance(value, str):
        return f"{pad}{value}"
    if isinstance(value, list):
        lines = []
        for v in value:
            lines.append(_render_value(v, indent))
        return "\n".join(lines)
    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            rendered = _render_value(v, indent + 1)
            if "\n" in rendered or len(rendered) > 60:
                lines.append(f"{pad}- **{k}**:\n{rendered}")
            else:
                lines.append(f"{pad}- **{k}**: {rendered.strip()}")
        return "\n".join(lines)
    return f"{pad}{value}"


def _aggregate(snap):
    """Stitch completed agent outputs into one document. Deliberately
    does NOT print agent role/name (e.g. "Developer for...") since that
    would give away which arm is which in a blind read."""
    sections = []
    for i, r in enumerate(snap["results"], 1):
        if r["status"] != "completed" or not r.get("output"):
            continue
        sections.append(f"### Component {i}\n\n{_render_value(r['output'])}")
    if not sections:
        return "(pipeline produced no usable output)"
    return "\n\n".join(sections)


def _print_progress(msg):
    print(f"  {msg}", flush=True)


def main():
    adapter_a = instrument(OllamaAdapter(model=MODEL), "single_call")
    adapter_b = instrument(OllamaAdapter(model=MODEL), "multiagent")
    config = {"limits": {"max_spawn_rate_per_minute": 60}}

    results = []

    for i, objective in enumerate(BENCHMARK_PROMPTS, 1):
        print(f"\n[{i}/{len(BENCHMARK_PROMPTS)}] {objective[:70]}...")

        _print_progress("running single-call baseline...")
        t0 = time.time()
        single_output = run_single_call(objective, adapter_a)
        single_time = time.time() - t0
        _print_progress(f"  done in {single_time:.1f}s")

        _print_progress("running multi-agent pipeline...")
        t0 = time.time()
        try:
            multi_output, snap = run_multiagent(objective, adapter_b, config)
            multi_time = time.time() - t0
            multi_agents = snap["counts"]["total"]
            multi_completed = snap["counts"]["completed"]
        except Exception as exc:  # noqa: BLE001
            multi_output = f"(pipeline error: {exc})"
            multi_time = time.time() - t0
            multi_agents = 0
            multi_completed = 0
        _print_progress(f"  done in {multi_time:.1f}s ({multi_completed}/{multi_agents} agents)")

        # Randomize which is "A" and which is "B" for this prompt.
        if random.random() < 0.5:
            label_map = {"A": "single_call", "B": "multiagent"}
            response_a, response_b = single_output, multi_output
        else:
            label_map = {"A": "multiagent", "B": "single_call"}
            response_a, response_b = multi_output, single_output

        results.append(
            {
                "id": i,
                "objective": objective,
                "label_map": label_map,
                "response_a": response_a,
                "response_b": response_b,
                "single_call_seconds": round(single_time, 1),
                "multiagent_seconds": round(multi_time, 1),
                "multiagent_agents": multi_agents,
                "multiagent_completed": multi_completed,
            }
        )

    def totals_for(run_label):
        calls = [c for c in CALL_LOG if c["run"] == run_label]
        tin = sum(c["tokens_in"] for c in calls)
        tout = sum(c["tokens_out"] for c in calls)
        return len(calls), tin + tout

    calls_a, tok_a = totals_for("single_call")
    calls_b, tok_b = totals_for("multiagent")

    output = {
        "results": results,
        "totals": {
            "single_call": {"llm_calls": calls_a, "tokens": tok_a},
            "multiagent": {"llm_calls": calls_b, "tokens": tok_b},
        },
    }

    with open("benchmark_result.json", "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\n" + "=" * 60)
    print("BENCHMARK COMPLETE")
    print("=" * 60)
    print(f"single_call : {calls_a} LLM calls, {tok_a} tokens total")
    print(f"multiagent  : {calls_b} LLM calls, {tok_b} tokens total")
    print("Saved to benchmark_result.json")


if __name__ == "__main__":
    main()
