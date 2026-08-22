"""Classification-only test of the retuned AdaptiveSupervisor
(backend/app/orchestrator/adaptive_supervisor.py).

Prompt set covers everything we've thrown at the router across this
session — the trivial ones we KNOW should stay single_call, the
verification-shaped ones that should be single_call_critique, and the
project-shaped ones (including the one the user typed live) that were
previously getting misrouted to single_call and should now land on
multi_agent_critique.

Cheap by design: one classify call per prompt, no full pipeline runs.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.WARNING)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter
from backend.app.orchestrator.adaptive_supervisor import AdaptiveSupervisor

MODEL = "gpt-oss:120b-cloud"

# (expected_tier, prompt)  — expected reflects the NEW rules
CASES = [
    # Trivial: must stay cheap
    ("single_call", "Write a one-paragraph thank-you note to a customer who just placed their first order."),
    ("single_call", "Explain what a Kalman filter is in simple terms."),
    ("single_call", "Rephrase this sentence more formally: 'we gotta ship it fast'."),

    # Verify-shaped short-form
    ("single_call_critique", "Write our fundraising deck's traction slide, citing our exact current MRR, growth rate, and retention numbers."),
    ("single_call_critique", "Write a press release paragraph announcing our 40% YoY growth and 92% retention rate."),

    # Real project-shaped — previously all misrouted to single_call
    ("multi_agent_critique",
     "I'm launching a paid mobile app that helps freelance designers manage clients, contracts, and payments. "
     "Help me design the app's core feature set, price it, position it against Bonsai and HoneyBook, "
     "and plan a 6-week launch — I'm a solo builder with a $2k budget."),
    ("multi_agent_critique",
     "I want to launch a paid Substack newsletter for solo founders building AI products. "
     "Help me get from zero to my first 100 paying subscribers — positioning, content, launch plan."),
    ("multi_agent_critique",
     "A founder has a raw startup idea and needs it validated before they commit real time or money to it. "
     "Their idea: \"A subscription box for office snacks.\" Produce market research, competitor analysis, "
     "a feasibility assessment, and a clear go/no-go verdict."),
    ("multi_agent_critique",
     "i want an strategy to find sponsorship for my hackathon"),  # user's actual live prompt
    ("multi_agent_critique",
     "Design an end-to-end automation system for a small online retail business: automatically process incoming orders, "
     "sync inventory across the website and warehouse, send shipping notifications, handle return requests, "
     "and generate weekly sales reports."),
]


def main():
    adapter = OllamaAdapter(model=MODEL)
    sup = AdaptiveSupervisor(model_adapter=adapter)

    correct = 0
    print(f"{'-' * 78}")
    print(f"{'EXPECTED':<24}{'GOT':<24}RESULT   PROMPT")
    print(f"{'-' * 78}")
    for expected, prompt in CASES:
        got = sup.classify(prompt)
        mark = "OK" if got == expected else "MISS"
        if got == expected:
            correct += 1
        print(f"{expected:<24}{got:<24}{mark:<9}{prompt[:110]}...")
    print(f"{'-' * 78}")
    print(f"Accuracy: {correct}/{len(CASES)}")


if __name__ == "__main__":
    main()
