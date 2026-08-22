"""End-to-end smoke test for Phase 4 orchestration.

Runs the full pipeline (contract → agents → executed results) with the
MockAdapter, so it succeeds on any machine without Ollama installed.
Pass --real to test against a running Ollama instance.

    python smoke_test_phase4.py
    python smoke_test_phase4.py --real --model llama3
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
sys.path.insert(0, os.path.dirname(__file__))  # let backend.app.* resolve


def _print_section(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="Use real Ollama")
    parser.add_argument("--model", default="llama3")
    parser.add_argument(
        "--objective",
        default="Design a landing page and pricing tier for a solo-founder AI startup",
    )
    args = parser.parse_args()

    _print_section("Phase 4 smoke test — building pipeline")

    from backend.app.main import run

    snapshot = run(
        objective=args.objective,
        model=args.model,
        use_mock=not args.real,
        verbose=False,
        # Smoke test must stay non-interactive even with --real — never
        # block on stdin waiting for clarification answers.
        skip_clarify=True,
    )

    _print_section("Contract & execution summary")
    print(f"contract_id : {snapshot['contract_id']}")
    print(f"objective   : {snapshot['objective']}")
    print(f"status      : {snapshot['status']}")
    if snapshot.get("error"):
        print(f"error       : {snapshot['error']}")
    print(f"counts      : {snapshot['counts']}")
    print(f"totals      : {snapshot['totals']}")
    print(f"started_at  : {snapshot['started_at']}")
    print(f"finished_at : {snapshot['finished_at']}")

    _print_section(f"Per-agent results ({len(snapshot['results'])})")
    for i, result in enumerate(snapshot["results"], 1):
        print()
        print(f"[{i}] {result['agent_name']}  (id={result['agent_id']})")
        print(f"    status     = {result['status']}")
        print(f"    confidence = {result['confidence']}")
        print(f"    attempts   = {result['attempts']}")
        print(f"    tokens     = {result['tokens_used']}")
        print(f"    duration_s = {result['duration_seconds']}")
        if result.get("error"):
            print(f"    error      = {result['error']}")
        output_preview = json.dumps(result.get("output", {}), indent=6)
        if len(output_preview) > 500:
            output_preview = output_preview[:500] + "..."
        print(f"    output     =\n      {output_preview.replace(chr(10), chr(10) + '      ')}")

    _print_section("Verdict")
    total = snapshot["counts"]["total"]
    ok = snapshot["counts"]["completed"]

    if total > 0 and ok > 0:
        print(f"PASS: {ok}/{total} agents completed. Pipeline executes end-to-end.")
        return 0

    print(f"FAIL: {ok}/{total} agents completed. Investigate the errors above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
