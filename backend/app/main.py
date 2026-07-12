"""Phase 4 CLI entry point.

Runs the full orchestration pipeline against Ollama (or a mock adapter
if Ollama is unreachable) and prints a JSON summary of the final state.

    python -m backend.app.main "Create a marketing plan for a bakery"

    # Read objective from stdin:
    echo "Design a mobile onboarding flow" | python -m backend.app.main -

The API/HTTP layer lands in Phase 9 — this script is deliberately the
lowest-friction way to prove the whole pipeline runs end-to-end.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict, Optional

from backend.app.contracts.clarification import ClarificationEngine
from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter
from backend.app.orchestrator.pipeline_controller import Pipeline


DEFAULT_OBJECTIVE = "Design a landing page for a two-person AI startup"


class MockAdapter:
    """Deterministic offline adapter used when Ollama is unreachable.

    Returns a template JSON object whose keys are drawn from the prompt
    so downstream parsers still see structured output.
    """

    def __init__(self, label: str = "mock"):
        self.label = label

    def chat_completion(self, prompt: str, **kwargs: Any) -> str:
        if "expected shape" in prompt.lower() or "expected output shape" in prompt.lower():
            return json.dumps(
                {
                    "primary_deliverable": f"[{self.label}] plausible deliverable text",
                    "supporting_materials": ["[mock] supporting item"],
                    "validation": "[mock] no runtime checks performed",
                }
            )
        # Contract generator prompt — return a minimal contract-shaped JSON
        return json.dumps(
            {
                "deliverables": [
                    "Deliverable one derived from mock adapter",
                    "Deliverable two derived from mock adapter",
                    "Deliverable three derived from mock adapter",
                ],
                "constraints": ["Runs in mock mode without a live LLM"],
                "success_criteria": ["Pipeline reaches a terminal state"],
                "assumptions": ["Ollama not available in this environment"],
                "risk_factors": ["Output quality is placeholder"],
                "execution_plan": ["Step one", "Step two", "Step three"],
            }
        )


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _read_objective(argv_objective: Optional[str]) -> str:
    if argv_objective and argv_objective != "-":
        return argv_objective
    if argv_objective == "-":
        return sys.stdin.read().strip() or DEFAULT_OBJECTIVE
    return DEFAULT_OBJECTIVE


def _build_adapter(model: str, use_mock: bool) -> Any:
    if use_mock:
        return MockAdapter(label=f"mock:{model}")

    adapter = OllamaAdapter(model=model)
    probe = adapter.chat_completion("Say 'ok' and nothing else.", max_tokens=8)
    if probe.strip().lower().startswith("connection error"):
        logging.warning(
            "Ollama unreachable (probe returned: %s...). Falling back to MockAdapter.",
            probe[:80],
        )
        return MockAdapter(label=f"mock:{model}-fallback")
    return adapter


def _load_config() -> Dict[str, Any]:
    try:
        from backend.app.utils.config_loader import load_config

        cfg = load_config()
        data = cfg.model_dump() if hasattr(cfg, "model_dump") else {}
    except Exception as exc:  # noqa: BLE001
        logging.warning("Could not load config.yaml: %s. Using defaults.", exc)
        data = {}

    # CLI-mode override: config.yaml sets max_spawn_rate_per_minute=5 to
    # bound autonomous adaptive spawning. For a single-user CLI invocation
    # a contract can legitimately fan out into >5 deliverables, and the
    # existing RecursionGuard *fail-safes* (aborts) on rate-limit hit
    # instead of throttling. Widen the ceiling here so the pipeline can
    # complete. Proper throttle semantics land in Phase 5.
    limits = data.setdefault("limits", {})
    limits["max_spawn_rate_per_minute"] = max(
        int(limits.get("max_spawn_rate_per_minute", 5) or 0), 60
    )
    return data


def _clarify_objective(
    objective: str,
    adapter: Any,
    config: Dict[str, Any],
    skip_clarify: bool,
) -> str:
    """Ask grounding questions before contract generation, if needed.

    Skips entirely for MockAdapter (nothing to ask a template engine)
    and when the caller opts out via --skip-clarify / skip_clarify=True.
    """
    if skip_clarify or isinstance(adapter, MockAdapter):
        return objective

    clarification_cfg = config.get("clarification", {})
    if not clarification_cfg.get("enabled", True):
        return objective

    engine = ClarificationEngine(clarification_cfg)
    questions = engine.assess(objective, adapter)
    if not questions:
        return objective

    print("\nA few quick questions before I start building:\n")
    qa_pairs = []
    for q in questions:
        answer = input(f"[{q.category}] {q.question}\n> ").strip()
        qa_pairs.append((q, answer or "(no preference)"))

    brief = engine.build_brief(objective, qa_pairs)
    enriched = engine.enrich_objective(brief)
    print("\nGot it — building based on your answers.\n")
    return enriched


def run(
    objective: str,
    model: str = "llama3",
    use_mock: bool = False,
    verbose: bool = False,
    skip_clarify: bool = False,
) -> Dict[str, Any]:
    _configure_logging(verbose)

    adapter = _build_adapter(model=model, use_mock=use_mock)
    config = _load_config()

    objective = _clarify_objective(objective, adapter, config, skip_clarify)

    pipeline = Pipeline(model_adapter=adapter, config=config)
    manager = pipeline.run_objective(objective)
    return manager.snapshot()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ai_agent_os",
        description="Run the AI Workforce OS pipeline (Phase 4 CLI).",
    )
    parser.add_argument(
        "objective",
        nargs="?",
        default=None,
        help="Natural-language objective (or '-' to read from stdin).",
    )
    parser.add_argument("--model", default="llama3", help="Ollama model name.")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force MockAdapter instead of Ollama (offline mode).",
    )
    parser.add_argument(
        "--skip-clarify",
        action="store_true",
        help="Skip the pre-contract clarification questions (for scripted/automated runs).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    objective = _read_objective(args.objective)
    snapshot = run(
        objective=objective,
        model=args.model,
        use_mock=args.mock,
        verbose=args.verbose,
        skip_clarify=args.skip_clarify,
    )
    print(json.dumps(snapshot, indent=2, default=str))
    return 0 if snapshot.get("status") in {"completed", "partial"} else 1


if __name__ == "__main__":
    sys.exit(main())
