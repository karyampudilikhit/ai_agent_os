"""Real usage CLI for the Idea-Validation Employee (README Phase 10) —
not a demo/test script. This is what actually gets run against a real
person's idea: Reddit replies, a friend's pitch, anything.

    python validate_idea.py "A subscription box for office snacks"
    python validate_idea.py -                                # read from stdin
    python validate_idea.py "..." --employee-id founder_alice # reuse memory
                                                                # across a founder's
                                                                # follow-up ideas
    python validate_idea.py "..." --save verdict.md           # also write to a file
    python validate_idea.py "..." --mock                      # offline sanity check

Each invocation gets a fresh, random employee_id by default — different
people's ideas should never leak into each other's validation context.
Pass --employee-id explicitly only when it's genuinely the same founder
coming back with a revision (mirrors run_idea_validation_employee.py's
memory-persistence proof, but for one real idea instead of a fixed demo).
"""

from __future__ import annotations

import argparse
import sys
import uuid

sys.path.insert(0, "backend")
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.app.main import MockAdapter, _build_adapter, _load_config
from backend.app.orchestrator.pipeline_controller import Pipeline
from backend.app.orchestrator.synthesis import SynthesisEngine
from backend.app.critique.critique_agent import CritiqueEngine
from backend.app.employees.idea_validation_employee import IdeaValidationEmployee


def _read_idea(argv_idea: str | None) -> str:
    if argv_idea and argv_idea != "-":
        return argv_idea
    text = sys.stdin.read().strip()
    if not text:
        raise SystemExit("No idea provided (empty stdin).")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="validate_idea",
        description="Run a raw idea through the Idea-Validation Employee.",
    )
    parser.add_argument("idea", nargs="?", default=None, help="The raw idea, or '-' to read from stdin.")
    parser.add_argument("--employee-id", default=None, help="Reuse a specific founder's memory (omit for a fresh one-off).")
    parser.add_argument("--model", default="gpt-oss:120b-cloud", help="Ollama model name.")
    parser.add_argument("--mock", action="store_true", help="Force MockAdapter (offline sanity check).")
    parser.add_argument("--save", default=None, help="Also write the verdict to this file (markdown).")
    parser.add_argument("--show-internals", action="store_true", help="Print completeness/fabrication metadata (stripped by default so output is copy-paste-ready).")
    args = parser.parse_args()

    idea = _read_idea(args.idea)
    employee_id = args.employee_id or f"oneoff_{uuid.uuid4().hex[:8]}"

    adapter = _build_adapter(model=args.model, use_mock=args.mock)
    config = _load_config()

    pipeline = Pipeline(
        model_adapter=adapter,
        config=config,
        synthesis_engine=SynthesisEngine(adapter, max_tokens=5000),
        critique_engine=CritiqueEngine(adapter, max_tokens=4000),
    )
    employee = IdeaValidationEmployee(employee_id=employee_id, pipeline=pipeline)

    print(f"Validating (employee_id={employee_id}, model={args.model})...\n", file=sys.stderr)
    result = employee.run_task(idea)

    verdict = result.get("output") or "(no output produced — check logs)"
    print(verdict)

    if args.show_internals:
        critique = result.get("critique") or {}
        print("\n---", file=sys.stderr)
        print(f"completeness_score = {critique.get('completeness_score')}", file=sys.stderr)
        print(f"fabricated_claims = {critique.get('fabricated_claims')}", file=sys.stderr)
        print(f"over_engineered = {critique.get('over_engineered')}", file=sys.stderr)
        print(f"was_refined = {result.get('was_refined')}", file=sys.stderr)

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            f.write(verdict)
        print(f"\nSaved to {args.save}", file=sys.stderr)

    return 0 if result.get("output") else 1


if __name__ == "__main__":
    sys.exit(main())
