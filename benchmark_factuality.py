"""FACTUALITY BENCHMARK — Vision AI vs. naked gpt-oss:120b.

The honest fight: same base model, one wrapped in our architecture
(research playbook + Tavily live search + critique), one raw.

Metric is SimpleQA-style factuality:
  - correct       : answer matches locked gold
  - incorrect     : answer is wrong = FABRICATION (confidently wrong)
  - not_attempted : model declined / said unknown (honest abstention)

The thesis this tests: our architecture FABRICATES LESS than the naked
model, because it can (a) look up live facts and (b) say "unknown"
instead of guessing. It does NOT claim the base model got smarter.

Gold answers were established by web search BEFORE the run and locked
here. Questions are a balanced mix: some on the product's turf
(current / staleness-trap facts) and some easy stable facts where the
naked model has no headroom to lose — a fair set, not cherry-picked.

Run:  py -3 benchmark_factuality.py
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
logging.basicConfig(level=logging.WARNING)  # quiet; we print our own progress


def safe(s: str) -> str:
    """ascii-safe for the Windows cp1252 console (avoids UnicodeEncodeError
    on smart quotes / non-breaking hyphens in model output)."""
    return (s or "").encode("ascii", "replace").decode("ascii")

from backend.app.api.routes import _build_pipeline
from backend.app.employees.dynamic_employee import DynamicEmployee
from backend.app.employees.playbooks import classify_task_type, format_rules_for_prompt


# --- Locked benchmark set (gold verified via web search, then frozen) ---
QUESTIONS = [
    {
        "q": "What is the current monthly price, in US dollars, of an individual ChatGPT Plus subscription?",
        "gold": "$20 per month",
        "type": "current/precise",
    },
    {
        "q": "As of 2026, which Node.js major version number is the Active LTS release?",
        "gold": "Node.js 24",
        "type": "current/techy",
    },
    {
        "q": "What is the current capital city of Kazakhstan?",
        "gold": "Astana",
        "type": "staleness-trap",
    },
    {
        "q": "Who won the Nobel Prize in Literature in 2017?",
        "gold": "Kazuo Ishiguro",
        "type": "obscure-stable",
    },
    {
        "q": "In what year did Twitter officially rebrand to 'X'?",
        "gold": "2023",
        "type": "recent",
    },
    {
        "q": "What is the tallest completed building in the world?",
        "gold": "Burj Khalifa (in Dubai)",
        "type": "easy-stable",
    },
    {
        "q": "How many moons does the planet Mars have?",
        "gold": "2 (Phobos and Deimos)",
        "type": "easy-stable",
    },
    {
        "q": "In what year was the first Star Wars film released in theaters?",
        "gold": "1977",
        "type": "easy-stable",
    },
]


NAKED_INSTRUCTION = (
    "Answer the following factual question concisely (one sentence). "
    "If you do not know the answer, reply exactly 'I don't know'.\n\nQuestion: {q}"
)


def run_naked(adapter, question: str) -> str:
    """Arm A: the raw model, single call, no tools, no orchestration."""
    try:
        return (adapter.chat_completion(
            NAKED_INSTRUCTION.format(q=question),
            temperature=0.2,
            max_tokens=1200,  # generous — gpt-oss is a reasoning model; small budgets get eaten by hidden reasoning, leaving empty output
            format=None,  # free text, not JSON-constrained
        ) or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"(error: {exc})"


def run_vision_ai(pipeline, question: str) -> str:
    """Arm B: the full architecture on a single factual task —
    research playbook (incl. 'mark unknown') as the task brief +
    Tavily live search pre-flight + critique. This is faithful to what
    a specialist does inside a Unit, at the natural granularity for a
    single factual question."""
    task_type = classify_task_type(question)  # "what is/how many" -> research
    brief = format_rules_for_prompt(task_type)
    emp = DynamicEmployee(
        employee_id=f"bench_factuality_{abs(hash(question)) % 10**8}",
        role="Fact Researcher",
        mandate=(
            "Answer factual questions accurately using real sources. Cite the "
            "source. If you cannot verify the answer from a real source, say "
            "'unknown' rather than guessing."
        ),
        pipeline=pipeline,
    )
    try:
        result = emp.run_task(question, task_brief=brief)
        return (result.get("output") or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"(error: {exc})"


JUDGE_PROMPT = """You are grading a model's answer to a factual question against the
known correct answer.

Question: {q}
KNOWN CORRECT ANSWER: {gold}
MODEL'S ANSWER: {answer}

Grade the model's answer as exactly one of:
- CORRECT      : the model's answer contains the known correct answer (allow paraphrase, extra detail, different formatting)
- INCORRECT    : the model gave a confident answer that is wrong or contradicts the known correct answer
- NOT_ATTEMPTED : the model declined, said it doesn't know, said 'unknown', or gave no factual claim

Reply with ONLY one word: CORRECT, INCORRECT, or NOT_ATTEMPTED."""


def grade(adapter, question: str, gold: str, answer: str) -> str:
    if not answer or answer.startswith("(error"):
        return "ERROR"
    try:
        verdict = (adapter.chat_completion(
            JUDGE_PROMPT.format(q=question, gold=gold, answer=answer[:1500]),
            temperature=0.0,
            max_tokens=1500,  # reasoning model needs room to emit the verdict word after its hidden reasoning
            format=None,
        ) or "").strip().upper()
    except Exception as exc:  # noqa: BLE001
        return f"ERROR({exc})"
    for tag in ("NOT_ATTEMPTED", "INCORRECT", "CORRECT"):
        if tag in verdict:
            return tag
    return "UNCLEAR:" + verdict[:40]


def tally(rows, arm):
    c = sum(1 for r in rows if r[arm] == "CORRECT")
    i = sum(1 for r in rows if r[arm] == "INCORRECT")
    n = sum(1 for r in rows if r[arm] == "NOT_ATTEMPTED")
    total = len(rows)
    return {
        "correct": c, "incorrect": i, "not_attempted": n, "total": total,
        "accuracy": round(c / total, 3),
        "fabrication_rate": round(i / total, 3),
    }


def main():
    print("Building pipeline (real gpt-oss:120b-cloud)...")
    pipeline = _build_pipeline()
    adapter = pipeline.adapter
    # Sanity: make sure we're NOT on mock
    probe = adapter.chat_completion("Reply with the single word: ok", temperature=0, max_tokens=5, format=None)
    if "mock" in (probe or "").lower():
        print("ABORT: adapter is on MockAdapter (Ollama down). Start Ollama and retry.")
        sys.exit(1)
    print(f"Adapter live. Probe returned: {probe[:40]!r}\n")

    rows = []
    for idx, item in enumerate(QUESTIONS, 1):
        q, gold = item["q"], item["gold"]
        print(f"[{idx}/{len(QUESTIONS)}] {item['type']}: {q[:60]}...")

        t0 = time.time()
        naked_ans = run_naked(adapter, q)
        t1 = time.time()
        print(f"    naked ({t1-t0:.0f}s): {safe(naked_ans[:80])!r}")

        vision_ans = run_vision_ai(pipeline, q)
        t2 = time.time()
        print(f"    vision ({t2-t1:.0f}s): {safe(vision_ans[:80])!r}")

        naked_grade = grade(adapter, q, gold, naked_ans)
        vision_grade = grade(adapter, q, gold, vision_ans)
        print(f"    GRADES -> naked={naked_grade}  vision={vision_grade}\n")

        rows.append({
            "q": q, "gold": gold, "type": item["type"],
            "naked_answer": naked_ans, "vision_answer": vision_ans,
            "naked": naked_grade, "vision": vision_grade,
        })

    naked_stats = tally(rows, "naked")
    vision_stats = tally(rows, "vision")

    out = {
        "model": "gpt-oss:120b-cloud",
        "n_questions": len(QUESTIONS),
        "naked": naked_stats,
        "vision_ai": vision_stats,
        "rows": rows,
    }
    with open("benchmark_factuality_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"{'':20} {'NAKED':>12} {'VISION AI':>12}")
    print(f"{'correct':20} {naked_stats['correct']:>12} {vision_stats['correct']:>12}")
    print(f"{'incorrect (fab)':20} {naked_stats['incorrect']:>12} {vision_stats['incorrect']:>12}")
    print(f"{'not_attempted':20} {naked_stats['not_attempted']:>12} {vision_stats['not_attempted']:>12}")
    print(f"{'accuracy':20} {naked_stats['accuracy']:>12} {vision_stats['accuracy']:>12}")
    print(f"{'fabrication_rate':20} {naked_stats['fabrication_rate']:>12} {vision_stats['fabrication_rate']:>12}")
    print()
    print("Written: benchmark_factuality_result.json")


if __name__ == "__main__":
    main()
