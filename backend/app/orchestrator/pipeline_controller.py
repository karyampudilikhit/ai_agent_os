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
                    ─▶ ExecutionState  (state_manager)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from backend.app.agents.agent_factory import AgentFactory
from backend.app.contracts.execution_contract import ExecutionContractGenerator
from backend.app.orchestrator.execution_engine import ExecutionEngine
from backend.app.orchestrator.state_manager import StateManager

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

        return self.engine.run(contract, agents)

    def run_contract(self, contract: Any) -> StateManager:
        """Run an already-built contract (skip the generation step)."""
        agents = self.agent_factory.create_agents_from_contract(
            contract, model_adapter=self.adapter
        )
        return self.engine.run(contract, agents)


def build_default_pipeline(
    model_adapter: Any,
    config: Optional[Dict[str, Any]] = None,
) -> Pipeline:
    """Convenience factory: default pipeline for the given adapter."""
    return Pipeline(model_adapter=model_adapter, config=config)
