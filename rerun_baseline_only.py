"""Re-runs ONLY the single_call baseline arm with the format=None fix
and patches the results back into benchmark_result.json in place.
Multi-agent responses are untouched — they were correct all along."""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
sys.path.insert(0, os.path.dirname(__file__))

import logging
logging.basicConfig(level=logging.WARNING)

from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter

MODEL = "phi3"


def run_single_call(objective, adapter):
    prompt = f"""{objective}

Give a complete, thorough, well-organized answer in plain written prose
with markdown headings. Cover every part of the request. Be specific
and concrete — write as if this is the final deliverable, not a
summary of what you'd do. Do not output JSON."""
    return adapter.chat_completion(prompt, temperature=0.6, max_tokens=2500, format=None)


def main():
    with open("benchmark_result.json") as f:
        data = json.load(f)

    adapter = OllamaAdapter(model=MODEL)
    new_tokens = 0
    new_calls = 0

    for r in data["results"]:
        print(f"[{r['id']}/10] re-running baseline: {r['objective'][:60]}...")
        t0 = time.time()
        new_response = run_single_call(r["objective"], adapter)
        elapsed = time.time() - t0
        print(f"  done in {elapsed:.1f}s, {len(new_response)} chars")

        new_tokens += (len(new_response) + len(r["objective"])) // 4
        new_calls += 1

        # Figure out which slot (A or B) held the single_call response
        # and overwrite just that slot, leaving multiagent's slot alone.
        if r["label_map"]["A"] == "single_call":
            r["response_a"] = new_response
        else:
            r["response_b"] = new_response

    data["totals"]["single_call"] = {"llm_calls": new_calls, "tokens": new_tokens}

    with open("benchmark_result.json", "w") as f:
        json.dump(data, f, indent=2, default=str)

    print("\nDone. benchmark_result.json updated with fixed baseline responses.")


if __name__ == "__main__":
    main()
