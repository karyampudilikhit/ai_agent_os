"""Independent judge for the single-call vs multi-agent benchmark.

Every prior round scored the multi-agent output with its OWN critique
agent — same model family judging its own work, unverified against
anything external. This script is the fix: judges Round 5
(benchmark_result_large_gptoss.json, both arms freshly generated on
gpt-oss:120b-cloud) using deepseek-v3.1:671b-cloud, a different model
family that never saw either arm being generated.

Each of the 5 objectives is judged TWICE, with A/B position swapped
between calls, to control for position bias (LLM judges are known to
favor whichever answer comes first). A verdict only counts as a real
win if the judge picks the same underlying response in both orders;
otherwise it's recorded as position-inconsistent and treated as a tie.

Rubric explicitly penalizes fabricated evidence (claims that a test,
pilot, or validation run already happened with specific results) since
that's a known failure mode in both arms, not just multi-agent's.
"""

import json
import os
import re
import sys
import time

# Judge reasoning can contain characters (e.g. "<=") outside the Windows
# console's default cp1252 codepage; without this, printing a verdict
# crashes the run partway through instead of just the JSON write (which
# already goes through `open(..., encoding=...)`-free UTF-8 by default
# on json.dump, so only the console print needs this).
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
sys.path.insert(0, os.path.dirname(__file__))

import logging
logging.basicConfig(level=logging.WARNING)

from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter

JUDGE_MODEL = "qwen3-coder:480b-cloud"
SOURCE_FILE = sys.argv[1] if len(sys.argv) > 1 else "benchmark_result_large_gptoss.json"
OUTPUT_FILE = sys.argv[2] if len(sys.argv) > 2 else "judge_result_large_gptoss_qwen.json"

JUDGE_PROMPT = """You are an impartial judge comparing two responses to the same request. You do not know which system produced which response.

REQUEST:
{objective}

RESPONSE A:
{response_a}

RESPONSE B:
{response_b}

Judge on:
1. COMPLETENESS — does it actually cover everything the request asked for, in real operational detail (not just headings)?
2. IMPLEMENTABILITY — could someone actually build this as described, with the tools/scope implied by the request? Penalize solutions that are needlessly over-engineered for the stated scale.
3. FABRICATED EVIDENCE — does either response claim a test, pilot, or validation run ALREADY HAPPENED with specific results (e.g. "250 orders processed, 100% success", "piloted at 3 locations")? Neither system has ever executed anything — any such claim is fabricated and should count heavily against that response, even if the rest reads well.
4. OVERALL — if you had to hand one of these to someone to actually execute, which would you hand them?

Return JSON only:
{{
  "winner": "A" or "B" or "tie",
  "fabrication_a": true or false,
  "fabrication_b": true or false,
  "reasoning": "2-3 sentences max, specific, not generic praise"
}}

JSON only."""


def extract_json(text):
    if not text:
        return None
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


def judge_pair(adapter, objective, resp_a, resp_b):
    prompt = JUDGE_PROMPT.format(objective=objective, response_a=resp_a, response_b=resp_b)
    try:
        raw = adapter.chat_completion(prompt, temperature=0.2, max_tokens=500)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    data = extract_json(raw)
    return data, raw


def main():
    with open(SOURCE_FILE) as f:
        data = json.load(f)

    adapter = OllamaAdapter(model=JUDGE_MODEL)

    verdicts = []
    for r in data["results"]:
        pid = r["id"]
        objective = r["objective"]
        resp_a, resp_b = r["response_a"], r["response_b"]
        label_map = r["label_map"]

        print(f"\n[{pid}/{len(data['results'])}] {objective[:70]}...")

        print("  pass 1 (A, B as given)...")
        t0 = time.time()
        v1, raw1 = judge_pair(adapter, objective, resp_a, resp_b)
        print(f"    done in {time.time()-t0:.1f}s -> {v1}")

        print("  pass 2 (A, B swapped)...")
        t0 = time.time()
        v2, raw2 = judge_pair(adapter, objective, resp_b, resp_a)
        print(f"    done in {time.time()-t0:.1f}s -> {v2}")

        # Un-swap pass 2's verdict back to the original A/B labeling.
        swap_map = {"A": "B", "B": "A", "tie": "tie"}
        v2_winner_unswapped = swap_map.get((v2 or {}).get("winner"), None)
        v1_winner = (v1 or {}).get("winner")

        if v1_winner and v2_winner_unswapped and v1_winner == v2_winner_unswapped:
            consistent = True
            final_winner_label = v1_winner  # "A" or "B" or "tie", in original labeling
        else:
            consistent = False
            final_winner_label = "tie"  # position-inconsistent -> don't credit either side

        final_winner_system = (
            label_map[final_winner_label] if final_winner_label in ("A", "B") else "tie"
        )

        verdicts.append(
            {
                "id": pid,
                "label_map": label_map,
                "pass1": v1,
                "pass2_swapped": v2,
                "position_consistent": consistent,
                "final_winner": final_winner_system,
            }
        )
        print(f"    FINAL: {final_winner_system}  (position_consistent={consistent})")

    tally = {"single_call": 0, "multiagent": 0, "tie": 0}
    for v in verdicts:
        tally[v["final_winner"]] += 1

    inconsistent = sum(1 for v in verdicts if not v["position_consistent"])

    output = {
        "judge_model": JUDGE_MODEL,
        "source": SOURCE_FILE,
        "verdicts": verdicts,
        "tally": tally,
        "position_inconsistent_count": inconsistent,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\n" + "=" * 60)
    print("INDEPENDENT JUDGE COMPLETE")
    print("=" * 60)
    print(f"judge model: {JUDGE_MODEL}")
    print(f"tally: {tally}")
    print(f"position-inconsistent (counted as tie): {inconsistent}/{len(verdicts)}")
    print(f"Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
