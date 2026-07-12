"""PROTOTYPE / SIMULATION SCRIPT — not part of the committed Phase 4 code.

Runs the same ambiguous objective through two paths using real Ollama
calls and the REAL, unmodified Phase 4 pipeline:

  RUN A (baseline)  objective -> contract (2 refinements) -> agents -> execution
  RUN B (grounded)  objective -> [NEW] clarification -> enriched objective
                               -> contract (1 refinement) -> agents -> execution

Nothing in backend/app/ is modified. The clarification stage here is a
throwaway prototype (ClarificationQuestion/RequirementsBrief/heuristic
gate/question generation) to validate the design from the planning
conversation before it gets built for real. Every LLM call in both
runs is token-counted so the comparison at the end is real data, not
an estimate.
"""

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.WARNING)

from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter
from backend.app.contracts.execution_contract import generate_execution_contract
from backend.app.orchestrator.pipeline_controller import Pipeline

MODEL = "phi3"
OBJECTIVE = (
    "you have to build an website for students to manage their works. "
    "and this has to be a simple mobile app"
)

# ----------------------------------------------------------------------
# Token instrumentation — wraps every real chat_completion call so both
# runs report true totals, not the state-manager's partial accounting.
# ----------------------------------------------------------------------

CALL_LOG = []


def instrument(adapter: OllamaAdapter, run_label: str) -> OllamaAdapter:
    original = adapter.chat_completion

    def wrapped(prompt, **kwargs):
        start = time.time()
        response = original(prompt, **kwargs)
        elapsed = time.time() - start
        tin, tout = len(prompt) // 4, len(response) // 4
        CALL_LOG.append(
            {
                "run": run_label,
                "tokens_in": tin,
                "tokens_out": tout,
                "seconds": round(elapsed, 2),
            }
        )
        return response

    adapter.chat_completion = wrapped
    return adapter


def _sim_config():
    """Mirror app/main.py's CLI rate-limit widening (see Phase 4 PR).
    Without this, any direct Pipeline() caller — this script included —
    hits RecursionGuard's bare 5/min default and fail-safes on a
    perfectly legitimate 6-deliverable contract. Confirmed by the first
    run of this script. Real fix (throttle instead of abort) is Phase 5.
    """
    return {"limits": {"max_spawn_rate_per_minute": 60}}


def _print_section(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


# ----------------------------------------------------------------------
# PROTOTYPE clarification engine — throwaway, mirrors the design we
# agreed: pure functions (assess / build_brief), no input() inside.
# ----------------------------------------------------------------------


@dataclass
class ClarificationQuestion:
    id: str
    question: str
    category: str


@dataclass
class RequirementsBrief:
    original_objective: str
    qa_pairs: List[Tuple[ClarificationQuestion, str]] = field(default_factory=list)
    skipped: bool = False
    skip_reason: Optional[str] = None


def heuristic_needs_clarification(objective: str) -> Tuple[bool, str]:
    """Zero-token gate. Returns (needs_clarification, reason)."""
    text = objective.lower()
    word_count = len(objective.split())

    platform_words = {
        "website": "website",
        "web app": "website",
        "webapp": "website",
        "mobile app": "mobile app",
        "mobile application": "mobile app",
        "android": "mobile app",
        "ios": "mobile app",
    }
    mentioned_platforms = {v for k, v in platform_words.items() if k in text}

    if len(mentioned_platforms) > 1:
        return True, f"contradictory platforms mentioned: {sorted(mentioned_platforms)}"
    if word_count < 25:
        return True, f"objective is short ({word_count} words) — likely under-specified"
    return False, "objective appears specific enough"


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def generate_clarifying_questions(
    objective: str, adapter: OllamaAdapter, max_questions: int = 5
) -> List[ClarificationQuestion]:
    """The ONE LLM call the clarification stage costs."""
    prompt = f"""You are a product intake assistant reviewing a build request
before any work starts.

Objective as given by the user:
"{objective}"

This objective may be vague, contradictory, or missing key details.
Identify at most {max_questions} genuinely necessary clarifying questions —
skip anything that's already clear. Categories to consider: platform,
audience, scope (must-have features), output_format (code vs spec vs
both), constraints (tech stack, branding, timeline).

Return JSON only:
{{
  "questions": [
    {{"id": "q1", "category": "platform", "question": "..."}},
    {{"id": "q2", "category": "scope", "question": "..."}}
  ]
}}

JSON only. No markdown, no prose."""

    response = adapter.chat_completion(prompt, temperature=0.3, max_tokens=500)
    data = _extract_json(response) or {}
    raw_questions = data.get("questions", [])[:max_questions]

    if not raw_questions:
        print("  [DEBUG] no questions parsed. Raw response was:")
        print("  " + response[:600].replace("\n", "\n  "))

    questions = []
    for i, q in enumerate(raw_questions):
        if isinstance(q, dict) and q.get("question"):
            questions.append(
                ClarificationQuestion(
                    id=q.get("id", f"q{i+1}"),
                    question=q["question"],
                    category=q.get("category", "general"),
                )
            )
    return questions


# Stand-in answers for this simulation — clearly not a real user, but
# representative of what a founder would actually type.
SIMULATED_ANSWERS = {
    "platform": "Just a mobile app — drop the website idea entirely, mobile-only.",
    "audience": "College students juggling assignments across multiple classes.",
    "scope": "One must-have for day one: a task list with due dates and priority levels.",
    "output_format": "Working code for the core screens, not just a design spec.",
    "constraints": "Fresh build, no existing brand or backend — keep it lightweight.",
    "general": "Keep it simple and student-friendly; no extra bells and whistles.",
}


def simulate_user_answers(
    questions: List[ClarificationQuestion],
) -> List[Tuple[ClarificationQuestion, str]]:
    """SIMULATED — stands in for a real interactive user for this run."""
    answered = []
    for q in questions:
        answer = SIMULATED_ANSWERS.get(q.category, SIMULATED_ANSWERS["general"])
        answered.append((q, answer))
    return answered


def build_brief(objective: str, qa_pairs) -> RequirementsBrief:
    return RequirementsBrief(original_objective=objective, qa_pairs=qa_pairs)


def enrich_objective(brief: RequirementsBrief) -> str:
    """Fold the brief into a single string the EXISTING, unmodified
    contract generator can consume — no changes to execution_contract.py
    needed for this prototype to prove the concept.

    Plain prose, not key: value pairs — a key:value shape primes small
    models to echo that structure back verbatim in unrelated downstream
    calls (confirmed: it happened to an agent's output in an earlier
    run of this same script). Also: if contract generation ever falls
    back to its single-deliverable default, that default wraps the
    ENTIRE objective string as one deliverable — so keeping this prose
    short and free of any format the model might imitate matters twice.
    """
    if brief.skipped or not brief.qa_pairs:
        return brief.original_objective

    answer_sentences = " ".join(a.rstrip(".") + "." for _, a in brief.qa_pairs)
    return f"{brief.original_objective} Clarified by the user: {answer_sentences}"


# ----------------------------------------------------------------------
# RUN A — baseline (current pipeline, unmodified, default 2 refinements)
# ----------------------------------------------------------------------


def run_baseline():
    _print_section("RUN A — baseline (no clarification stage)")
    adapter = instrument(OllamaAdapter(model=MODEL), "A-baseline")
    pipeline = Pipeline(model_adapter=adapter, config=_sim_config())

    manager = pipeline.run_objective(OBJECTIVE)
    snap = manager.snapshot()

    print(f"contract deliverables -> agents spawned: {snap['counts']['total']}")
    print(f"status: {snap['status']}  completed: {snap['counts']['completed']}")
    for r in snap["results"]:
        print(f"  - {r['agent_name'][:60]:<60} status={r['status']} conf={r['confidence']}")
    return snap


# ----------------------------------------------------------------------
# RUN B — grounded (prototype clarification stage first)
# ----------------------------------------------------------------------


def run_grounded():
    _print_section("RUN B — with clarification stage (prototype)")
    adapter = instrument(OllamaAdapter(model=MODEL), "B-grounded")

    print(f'Objective: "{OBJECTIVE}"')
    needs_clarification, reason = heuristic_needs_clarification(OBJECTIVE)
    print(f"\n[heuristic gate] needs_clarification={needs_clarification} ({reason})")

    if needs_clarification:
        print("\n[LLM call] generating clarifying questions...")
        questions = generate_clarifying_questions(OBJECTIVE, adapter)
        print(f"  -> {len(questions)} questions returned:")
        for q in questions:
            print(f"     [{q.category}] {q.question}")

        qa_pairs = simulate_user_answers(questions)
        print("\n[SIMULATED user answers — stand-in for real input this run]:")
        for q, a in qa_pairs:
            print(f'     Q: {q.question}\n     A: "{a}"')

        brief = build_brief(OBJECTIVE, qa_pairs)
    else:
        brief = RequirementsBrief(OBJECTIVE, skipped=True, skip_reason=reason)
        print("  -> skipped, objective already specific enough")

    enriched = enrich_objective(brief)
    print("\n[enriched objective fed to contract generator]:")
    print("  " + enriched.replace("\n", "\n  "))

    print("\n[contract + agents + execution — real Phase 4 pipeline, max_refinements=1]")
    pipeline = Pipeline(model_adapter=adapter, config=_sim_config())
    manager = pipeline.run_objective(enriched, max_refinements=1)
    snap = manager.snapshot()

    print(f"\ncontract deliverables -> agents spawned: {snap['counts']['total']}")
    print(f"status: {snap['status']}  completed: {snap['counts']['completed']}")
    for r in snap["results"]:
        print(f"  - {r['agent_name'][:60]:<60} status={r['status']} conf={r['confidence']}")
    return snap


# ----------------------------------------------------------------------


def totals_for(run_label: str):
    calls = [c for c in CALL_LOG if c["run"] == run_label]
    tin = sum(c["tokens_in"] for c in calls)
    tout = sum(c["tokens_out"] for c in calls)
    return len(calls), tin, tout, tin + tout


def main():
    snap_a = run_baseline()
    snap_b = run_grounded()

    _print_section("TOKEN COMPARISON (real, instrumented — not estimated)")
    calls_a, in_a, out_a, total_a = totals_for("A-baseline")
    calls_b, in_b, out_b, total_b = totals_for("B-grounded")

    print(f"{'':<28}{'RUN A (baseline)':>20}{'RUN B (grounded)':>20}")
    print(f"{'LLM calls':<28}{calls_a:>20}{calls_b:>20}")
    print(f"{'tokens in':<28}{in_a:>20}{in_b:>20}")
    print(f"{'tokens out':<28}{out_a:>20}{out_b:>20}")
    print(f"{'TOTAL tokens':<28}{total_a:>20}{total_b:>20}")
    print(f"{'agents spawned':<28}{snap_a['counts']['total']:>20}{snap_b['counts']['total']:>20}")
    print(f"{'agents completed':<28}{snap_a['counts']['completed']:>20}{snap_b['counts']['completed']:>20}")

    if total_a > 0:
        delta = 100 * (total_a - total_b) / total_a
        print(f"\nToken delta: {'-' if delta > 0 else '+'}{abs(delta):.0f}% ({'savings' if delta > 0 else 'increase'})")

    with open("clarification_simulation_result.json", "w") as f:
        json.dump(
            {
                "run_a_baseline": snap_a,
                "run_b_grounded": snap_b,
                "call_log": CALL_LOG,
            },
            f,
            indent=2,
            default=str,
        )
    print("\nFull trace saved to clarification_simulation_result.json")


if __name__ == "__main__":
    main()
