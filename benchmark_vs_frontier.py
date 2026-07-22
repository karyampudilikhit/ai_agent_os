"""MULTI-STEP WORK BENCHMARK — Vision AI vs. naked base vs. frontier model.

The startup go/no-go test. NOT trivia (the base model already wins that).
Real multi-step founder tasks where a single call cannot produce a
sourced, multi-section deliverable in one shot.

Three arms, same tasks:
  A) naked gpt-oss:120b     — single call (proves orchestration adds value)
  B) deepseek-v3.1:671b     — single call (the strong "normal model" bar;
                              ~5.5x bigger than the base model)
  C) Vision AI              — gpt-oss:120b + full multi-agent team + tools
                              + verification (the product)

Captures raw outputs + approx token usage + wall time per arm. Scoring
is done SEPARATELY by an independent judge (Claude — not a contestant)
reading the saved outputs, on a rubric: completeness, citation rate,
fabrication count, plus cost. Rubric-based absolute scoring, NOT pairwise
(pairwise position-bias burned the earlier benchmark thread).

The thesis this tests: Vision AI delivers work-output quality COMPARABLE
to the frontier model at a FRACTION of the cost, and BEATS the naked
base model decisively.

Run:  py -3 benchmark_vs_frontier.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(level=logging.WARNING)

from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter
from backend.app.orchestrator.pipeline_controller import Pipeline
from backend.app.orchestrator.synthesis import SynthesisEngine
from backend.app.critique.critique_agent import CritiqueEngine
from backend.app.employees.employee_spawner import EmployeeSpawner
from backend.app.employees.employee_coordinator import EmployeeCoordinator
from backend.app.employees.supervisor import default_supervisor_spec
from backend.app.main import _load_config


BASE_MODEL = "gpt-oss:120b-cloud"
# deepseek-671b and qwen3-480b were both retired 2026-07-15; most large
# cloud models now require a paid subscription. minimax-m3:cloud is a
# large frontier-class MoE model still available on the free tier — our
# "strong normal model" bar.
FRONTIER_MODEL = "minimax-m3:cloud"

# Rough relative $/token weight (bigger model = pricier per token).
# Directional only, not billing-accurate. MiniMax-M3 is a large MoE;
# exact active params unknown, so this is a conservative placeholder.
COST_WEIGHT = {BASE_MODEL: 1.0, FRONTIER_MODEL: 4.0}


TASKS = [
    "Validate this startup idea: a tool that auto-generates SOC 2 compliance "
    "evidence for early-stage SaaS companies. Research the market and top 3 "
    "competitors with their pricing, identify the biggest gap, and give a "
    "go/no-go verdict with a target price. Cite sources with URLs.",

    "Create a go-to-market plan for a new AI note-taking app aimed at "
    "independent consultants: identify the target segment, three acquisition "
    "channels with rationale, a pricing model, and a concrete 30-day launch "
    "timeline. Cite any market claims with URLs.",

    "Research the competitive landscape for AI-powered customer-support tools "
    "for e-commerce businesses: name the top 4 players, their pricing, each "
    "one's weakest gap, and recommend a specific differentiation angle for a "
    "new entrant. Cite sources with URLs.",
]


SINGLE_CALL_INSTRUCTION = (
    "You are helping a solo founder. Produce a complete, well-structured, "
    "decision-ready deliverable for the following request. Use clear sections. "
    "Where you state a specific fact, number, or competitor pricing, cite a "
    "source URL. If you cannot verify a specific number, say 'unknown' rather "
    "than inventing one.\n\nREQUEST:\n{task}"
)


class CountingAdapter:
    """Wraps an OllamaAdapter, summing approx tokens across every call so
    we can compare total token spend (Vision AI makes many internal calls;
    single-call arms make one)."""
    def __init__(self, inner):
        self.inner = inner
        self.model = inner.model
        self.tokens_in = 0
        self.tokens_out = 0
        self.calls = 0

    def chat_completion(self, prompt, **kwargs):
        self.calls += 1
        self.tokens_in += len(prompt) // 4
        resp = self.inner.chat_completion(prompt, **kwargs)
        self.tokens_out += len(resp or "") // 4
        return resp

    def reset(self):
        self.tokens_in = self.tokens_out = self.calls = 0


def build_pipeline(adapter):
    cfg = _load_config()
    return Pipeline(
        model_adapter=adapter,
        config=cfg,
        synthesis_engine=SynthesisEngine(adapter, max_tokens=5000),
        critique_engine=CritiqueEngine(adapter, max_tokens=4000),
    )


def run_single_call(adapter, task):
    prompt = SINGLE_CALL_INSTRUCTION.format(task=task)
    t0 = time.time()
    out = adapter.chat_completion(prompt, temperature=0.3, max_tokens=4000, format=None)
    return {
        "output": (out or "").strip(),
        "seconds": round(time.time() - t0, 1),
        "tokens_in": len(prompt) // 4,
        "tokens_out": len(out or "") // 4,
        "calls": 1,
    }


def run_vision_ai(base_counting_adapter, task):
    """Full product flow: design a team, run it under a Supervisor with
    tools + verification. Uses the counting adapter so we capture the
    TOTAL token spend across all the internal multi-agent calls."""
    base_counting_adapter.reset()
    pipeline = build_pipeline(base_counting_adapter)
    spawner = EmployeeSpawner(model_adapter=base_counting_adapter)
    t0 = time.time()

    team_spec = spawner.design_team(task)
    sup_spec = default_supervisor_spec()
    supervisor = spawner.instantiate([sup_spec], pipeline=pipeline, session_id="bench_vs")[0]
    specialists = spawner.instantiate(team_spec, pipeline=pipeline, session_id="bench_vs")
    coord = EmployeeCoordinator(pipeline=pipeline)
    result = coord.run_with_supervisor(prompt=task, supervisor=supervisor, specialists=specialists)

    return {
        "output": (result.get("final_output") or "").strip(),
        "seconds": round(time.time() - t0, 1),
        "tokens_in": base_counting_adapter.tokens_in,
        "tokens_out": base_counting_adapter.tokens_out,
        "calls": base_counting_adapter.calls,
        "team": [m.get("role") for m in team_spec],
    }


def cost_units(model, tokens_in, tokens_out):
    return round((tokens_in + tokens_out) / 1000.0 * COST_WEIGHT.get(model, 1.0), 2)


def main():
    print(f"Base:     {BASE_MODEL}")
    print(f"Frontier: {FRONTIER_MODEL}\n")

    base_raw = OllamaAdapter(model=BASE_MODEL)
    frontier = OllamaAdapter(model=FRONTIER_MODEL)
    base_counting = CountingAdapter(base_raw)

    # Sanity: neither on mock
    for name, ad in [("base", base_raw), ("frontier", frontier)]:
        probe = ad.chat_completion("Reply with the single word: ok", temperature=0, max_tokens=800, format=None)
        if "mock" in (probe or "").lower():
            print(f"ABORT: {name} adapter on MockAdapter. Start Ollama.")
            sys.exit(1)
    print("Both adapters live.\n")

    results = []
    for i, task in enumerate(TASKS, 1):
        print(f"=== TASK {i}/{len(TASKS)}: {task[:70]}... ===")

        print("  [A] naked gpt-oss...")
        naked = run_single_call(base_raw, task)
        print(f"      {naked['seconds']}s, ~{naked['tokens_out']} out tokens")

        print("  [B] deepseek-671b (frontier)...")
        front = run_single_call(frontier, task)
        print(f"      {front['seconds']}s, ~{front['tokens_out']} out tokens")

        print("  [C] Vision AI (team)...")
        vision = run_vision_ai(base_counting, task)
        print(f"      {vision['seconds']}s, {vision['calls']} calls, ~{vision['tokens_out']} out tokens, team={vision.get('team')}")

        results.append({
            "task": task,
            "naked": {**naked, "cost_units": cost_units(BASE_MODEL, naked["tokens_in"], naked["tokens_out"])},
            "frontier": {**front, "cost_units": cost_units(FRONTIER_MODEL, front["tokens_in"], front["tokens_out"])},
            "vision_ai": {**vision, "cost_units": cost_units(BASE_MODEL, vision["tokens_in"], vision["tokens_out"])},
        })
        print()

    out = {
        "base_model": BASE_MODEL,
        "frontier_model": FRONTIER_MODEL,
        "cost_weight": COST_WEIGHT,
        "results": results,
    }
    with open("benchmark_vs_frontier_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print("=" * 60)
    print("COST / EFFORT SUMMARY (quality judged separately by reading outputs)")
    print("=" * 60)
    print(f"{'task':6}{'arm':14}{'sec':>7}{'calls':>7}{'out_tok':>9}{'cost_u':>9}")
    for i, r in enumerate(results, 1):
        for arm in ("naked", "frontier", "vision_ai"):
            a = r[arm]
            print(f"{i:<6}{arm:14}{a['seconds']:>7}{a.get('calls',1):>7}{a['tokens_out']:>9}{a['cost_units']:>9}")
    print("\nWritten: benchmark_vs_frontier_result.json (full outputs for judging)")


if __name__ == "__main__":
    main()
