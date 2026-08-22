"""The workhorse that runs a whole batch of agents to completion.

Given a contract + pre-created agents, the engine:
  1. Validates each agent's structure.
  2. Builds a topological execution plan.
  3. For each layer, checks the RecursionGuard, executes each agent
     via AgentExecutor, and folds the result into the ExecutionState.

Sequential-within-layer is deliberate for Phase 4 — parallel dispatch
lands in Phase 5 once the guardrails are enforced end-to-end.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.app.agents.agent_executor import AgentExecutor, AgentResult
from backend.app.agents.agent_validator import AgentValidator, ValidationResult
from backend.app.orchestrator.dependency_validator import (
    DependencyError,
    DependencyValidator,
)
from backend.app.orchestrator.state_manager import StateManager

try:
    from backend.app.safety.recursion_guard import RecursionGuard, SpawnReason
    RECURSION_GUARD_AVAILABLE = True
except Exception:  # noqa: BLE001 — module has odd imports; degrade gracefully
    RECURSION_GUARD_AVAILABLE = False

    class SpawnReason:  # type: ignore[no-redef]
        USER_REQUEST = "user_request"

    class RecursionGuard:  # type: ignore[no-redef]
        def __init__(self, config: Dict[str, Any]):
            self.config = config

        def can_spawn(self, ctx: Dict[str, Any]) -> bool:  # noqa: ARG002
            return True

        def record_spawn(self, agent: Dict, reason: Any, ctx: Dict) -> None:
            pass


logger = logging.getLogger(__name__)


class ExecutionEngine:
    """Runs a set of agents against an LLM adapter and aggregates results."""

    def __init__(
        self,
        model_adapter: Any,
        config: Optional[Dict[str, Any]] = None,
        agent_executor: Optional[AgentExecutor] = None,
        agent_validator: Optional[AgentValidator] = None,
        dependency_validator: Optional[DependencyValidator] = None,
        recursion_guard: Optional[RecursionGuard] = None,
        strict_validation: bool = False,
    ):
        self.config = config or {}
        limits = self.config.get("limits", {})

        self.adapter = model_adapter
        self.executor = agent_executor or AgentExecutor(
            model_adapter,
            config=self.config,
            timeout_seconds=self.config.get("api", {}).get("timeout_seconds", 120),
        )
        self.validator = agent_validator or AgentValidator(self.config)
        self.planner = dependency_validator or DependencyValidator(
            max_dependency_depth=limits.get("max_dependency_depth", 10)
        )

        # RecursionGuard needs a flat dict of limits, not the full config.
        guard_config = {
            "max_total_agents": limits.get("max_total_agents", 50),
            "max_adaptive_cycles": limits.get("max_adaptive_cycles", 2),
            "max_agents_per_cycle": limits.get("max_agents_per_cycle", 3),
            "max_spawn_rate_per_minute": limits.get(
                "max_spawn_rate_per_minute", 5
            ),
            "max_total_cost": limits.get("max_total_cost", 100.0),
        }
        self.guard = recursion_guard or RecursionGuard(guard_config)
        self.strict_validation = strict_validation

    # ------------------------------------------------------------------

    def run(self, contract: Any, agents: List[Any]) -> StateManager:
        """Execute every agent for a contract, in dependency order.

        Returns the StateManager holding the terminal ExecutionState.
        """
        manager = StateManager(contract)
        manager.register_agents(agents)
        manager.mark_started()

        try:
            validation_report = self._validate_all(agents)
            executable_agents = self._filter_valid(agents, validation_report)

            if not executable_agents:
                manager.mark_finished(error="No agents passed validation")
                return manager

            layers = self.planner.validate_and_plan(executable_agents)
            logger.info(
                "Execution plan: %d layers, %d total agents",
                len(layers), sum(len(l) for l in layers),
            )

            full_objective = self._get(contract, "objective", "")

            for layer_index, layer in enumerate(layers):
                logger.info(
                    "Running layer %d/%d (%d agents)",
                    layer_index + 1, len(layers), len(layer),
                )
                for agent in layer:
                    self._run_one(agent, manager, full_objective)

        except DependencyError as exc:
            logger.error("Dependency plan invalid: %s", exc)
            manager.mark_finished(error=f"dependency_error: {exc}")
            return manager
        except Exception as exc:  # noqa: BLE001
            logger.exception("Execution aborted by unexpected error")
            manager.mark_finished(error=f"internal_error: {exc}")
            return manager

        manager.mark_finished()
        return manager

    # ------------------------------------------------------------------

    def _validate_all(self, agents: List[Any]) -> Dict[str, ValidationResult]:
        report = self.validator.validate_batch(agents)
        for agent_id, res in report.items():
            for err in res.errors:
                logger.warning("Agent %s validation error: %s", agent_id, err)
            for warn in res.warnings:
                logger.info("Agent %s validation warning: %s", agent_id, warn)
        return report

    def _filter_valid(
        self, agents: List[Any], report: Dict[str, ValidationResult]
    ) -> List[Any]:
        if self.strict_validation:
            invalid = [aid for aid, r in report.items() if not r.is_valid]
            if invalid:
                raise DependencyError(
                    f"strict_validation=True and {len(invalid)} agents invalid: "
                    f"{invalid}"
                )
            return list(agents)

        valid = []
        for agent in agents:
            aid = self._agent_id(agent)
            res = report.get(aid)
            if res is None or res.is_valid:
                valid.append(agent)
            else:
                logger.warning(
                    "Skipping invalid agent %s: %s", aid, res.errors
                )
        return valid

    def _run_one(self, agent: Any, manager: StateManager, full_objective: str = "") -> None:
        agent_id = self._agent_id(agent)

        if not self.guard.can_spawn(manager.state.context):
            logger.warning("RecursionGuard blocked agent %s", agent_id)
            manager.mark_result(
                agent_id,
                AgentResult(
                    agent_id=agent_id,
                    agent_name=self._get(agent, "name", "<unnamed>"),
                    status="failed",
                    error="blocked_by_recursion_guard",
                ),
            )
            return

        # Record spawn AFTER guard check but BEFORE running, so rate-limit
        # accounting is correct even if execution takes a long time.
        try:
            self.guard.record_spawn(
                {"id": agent_id},
                SpawnReason.USER_REQUEST,
                manager.state.context,
            )
        except Exception:  # noqa: BLE001
            logger.exception("record_spawn failed; continuing")

        manager.mark_agent_running(agent_id)
        # Prior completed results, in the order they finished — by the
        # time agent N runs, manager.state.agent_results already holds
        # every result from agents 1..N-1 (this loop is sequential).
        prior_results = list(manager.state.agent_results.values())
        result = self.executor.execute(agent, full_objective=full_objective, prior_results=prior_results)
        manager.mark_result(agent_id, result)
        logger.info(
            "Agent %s finished: status=%s confidence=%.2f",
            agent_id, result.status, result.confidence,
        )

    # ------------------------------------------------------------------

    def _agent_id(self, agent: Any) -> str:
        if isinstance(agent, dict):
            return agent.get("id", "<unknown>")
        return getattr(agent, "id", "<unknown>")

    def _get(self, agent: Any, field_name: str, default: Any = None) -> Any:
        if isinstance(agent, dict):
            return agent.get(field_name, default)
        return getattr(agent, field_name, default)
