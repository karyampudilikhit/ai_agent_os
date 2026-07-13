"""Top-level pipeline: objective in, ExecutionState out.

This is the single entry point every caller should use — the CLI in
main.py, the smoke test, and (once Phase 9 lands) the FastAPI route.
Everything upstream that has to change providers, tune limits, or
inject fakes for testing does it here, not in the engine or executor.

Composition:
    objective
        ─▶ ExecutionContractGenerator (contracts/)
            ─▶ AgentFactory            (agents/)
                ─▶ ExecutionEngine     (orchestrator/)
                    ─▶ SynthesisEngine (orchestrator/) — combines results
                        ─▶ ExecutionState  (state_manager)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from backend.app.agents.agent_factory import AgentFactory
from backend.app.contracts.execution_contract import ExecutionContractGenerator
from backend.app.orchestrator.execution_engine import ExecutionEngine
from backend.app.orchestrator.state_manager import StateManager
from backend.app.orchestrator.synthesis import SynthesisEngine
from backend.app.critique.critique_agent import CritiqueEngine

logger = logging.getLogger(__name__)


class Pipeline:
    """End-to-end runner: objective → agents → executed results."""

    def __init__(
        self,
        model_adapter: Any,
        config: Optional[Dict[str, Any]] = None,
        contract_generator: Optional[ExecutionContractGenerator] = None,
        agent_factory: Optional[AgentFactory] = None,
        execution_engine: Optional[ExecutionEngine] = None,
        synthesis_engine: Optional[SynthesisEngine] = None,
        critique_engine: Optional[CritiqueEngine] = None,
    ):
        if model_adapter is None:
            raise ValueError("Pipeline requires a model_adapter")

        self.adapter = model_adapter
        self.config = config or {}

        self.contract_generator = contract_generator or ExecutionContractGenerator(
            model_adapter=model_adapter,
            config=self.config.get("contracts"),
        )
        self.agent_factory = agent_factory or AgentFactory(
            config=self.config.get("agents"),
        )
        self.engine = execution_engine or ExecutionEngine(
            model_adapter=model_adapter,
            config=self.config,
        )
        self.synthesis = synthesis_engine or SynthesisEngine(
            model_adapter=model_adapter,
        )
        self.critique_engine = critique_engine or CritiqueEngine(
            model_adapter=model_adapter,
        )
        self.min_completeness_threshold = self.config.get("limits", {}).get(
            "min_completeness_threshold", 0.7
        )

    # ------------------------------------------------------------------

    def run_objective(
        self,
        objective: str,
        max_refinements: Optional[int] = None,
    ) -> StateManager:
        """Take a natural-language objective, run the whole pipeline."""
        if not objective or not objective.strip():
            raise ValueError("objective must be non-empty")

        logger.info("Pipeline start: %s", objective[:80])

        contract = self.contract_generator.generate_contract(
            objective, max_refinements=max_refinements
        )
        logger.info(
            "Contract %s ready: %d deliverables, confidence=%.2f",
            contract.id, len(contract.deliverables), contract.confidence_score,
        )

        agents = self.agent_factory.create_agents_from_contract(
            contract, model_adapter=self.adapter
        )
        logger.info("Agent factory created %d agents", len(agents))

        manager = self.engine.run(contract, agents)
        self._synthesize(objective, manager)
        self._critique_and_refine(objective, manager)
        return manager

    def run_contract(self, contract: Any) -> StateManager:
        """Run an already-built contract (skip the generation step)."""
        agents = self.agent_factory.create_agents_from_contract(
            contract, model_adapter=self.adapter
        )
        manager = self.engine.run(contract, agents)
        objective = getattr(contract, "objective", "")
        self._synthesize(objective, manager)
        self._critique_and_refine(objective, manager)
        return manager

    def _synthesize(self, objective: str, manager: StateManager) -> None:
        """Combine completed agent results into one final document.
        Never raises — synthesis failure falls back to no synthesized
        output (callers/CLI fall back to the raw per-agent results)."""
        try:
            results = list(manager.state.agent_results.values())
            synthesized = self.synthesis.synthesize(objective, results)
            manager.set_synthesized_output(synthesized)
            if synthesized:
                logger.info("Synthesis produced %d chars", len(synthesized))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Synthesis stage failed, continuing without it: %s", exc)

    def _critique_and_refine(self, objective: str, manager: StateManager) -> None:
        """Review the synthesized draft; refine (up to max_refinement_depth
        times) if it's incomplete, over-engineered for the stated scale, or
        contains fabricated completed-test claims. Never raises — any
        failure here just leaves the last good synthesized output in place.

        Re-critiques after every refine pass, not just once before the
        first one. The previous version stored the PRE-refine critique and
        never updated it — so completeness_score/fabricated_claims kept
        describing a draft that no longer shipped. That let a refined
        output still containing fabricated claims get reported (and
        benchmarked) as a clean 0.9+ score, because nothing ever re-checked
        whether the rewrite actually fixed what it was told to fix.
        """
        draft = manager.state.synthesized_output
        if not draft:
            return

        max_depth = self.config.get("limits", {}).get("max_refinement_depth", 2)
        attempts = 0
        try:
            critique = self.critique_engine.critique(objective, draft)
            if not critique:
                return
            manager.set_critique(critique)

            while attempts < max_depth and self.critique_engine.needs_refinement(
                critique, self.min_completeness_threshold
            ):
                attempts += 1
                logger.info(
                    "Refining (attempt %d/%d): completeness=%.2f over_engineered=%s fabricated_claims=%d",
                    attempts, max_depth,
                    critique.get("completeness_score", 0.0),
                    critique.get("over_engineered", False),
                    len(critique.get("fabricated_claims") or []),
                )
                refined = self.critique_engine.refine(objective, draft, critique)
                if not refined:
                    break
                draft = refined
                manager.set_synthesized_output(draft)
                manager.set_was_refined(True)
                logger.info("Refinement produced %d chars", len(draft))

                # Verify against the text that will actually ship, not the
                # draft that prompted this refinement pass.
                new_critique = self.critique_engine.critique(objective, draft)
                if not new_critique:
                    break
                critique = new_critique
                manager.set_critique(critique)

            if attempts == 0:
                logger.info("Critique passed all checks, no refinement needed")
            else:
                logger.info(
                    "Post-refinement (after %d attempt(s)): completeness=%.2f fabricated_claims=%d",
                    attempts,
                    critique.get("completeness_score", 0.0),
                    len(critique.get("fabricated_claims") or []),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Critique/refine stage failed, continuing without it: %s", exc)


def build_default_pipeline(
    model_adapter: Any,
    config: Optional[Dict[str, Any]] = None,
) -> Pipeline:
    """Convenience factory: default pipeline for the given adapter."""
    return Pipeline(model_adapter=model_adapter, config=config)
