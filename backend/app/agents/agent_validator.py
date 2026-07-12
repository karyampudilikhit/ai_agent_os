"""Pre-flight validation for AgentSchema instances.

Runs cheap structural checks before an agent is dispatched to the LLM,
so obviously-broken agents (empty objective, malformed traits, unknown
complexity level) fail fast without spending model tokens.

Phase 4 scope: structural + trait-range checks only. Semantic checks
(does the agent's objective actually match the deliverable?) are Phase 6.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

VALID_COMPLEXITY_LEVELS = {"low", "medium", "high"}
VALID_STATUSES = {"pending", "running", "completed", "failed"}
VALID_RISK_SENSITIVITY = {"low", "medium", "high", "neutral", "conservative"}


@dataclass
class ValidationResult:
    """Outcome of validating a single agent."""

    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.is_valid = False

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)


class AgentValidator:
    """Validates AgentSchema instances before execution."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        traits_limits = self.config.get("traits", {}).get("limits", {})
        self.max_refinement_depth = traits_limits.get("max_refinement_depth", 3)
        self.min_confidence_threshold = traits_limits.get(
            "min_confidence_threshold", 0.6
        )
        self.max_exploration_bias = traits_limits.get("max_exploration_bias", 1.0)
        self.min_exploration_bias = traits_limits.get("min_exploration_bias", 0.0)

    def validate(self, agent: Any) -> ValidationResult:
        """Run all validation checks on a single agent.

        Accepts either an AgentSchema pydantic model or a plain dict
        with the same field names.
        """
        result = ValidationResult(is_valid=True)

        data = self._as_dict(agent)

        self._check_required_fields(data, result)
        self._check_field_types(data, result)
        self._check_traits(data.get("traits") or {}, result)
        self._check_expected_output(data.get("expected_output") or {}, result)

        return result

    def validate_batch(self, agents: List[Any]) -> Dict[str, ValidationResult]:
        """Validate every agent in a batch. Returns {agent_id: result}."""
        return {
            self._agent_id(agent): self.validate(agent) for agent in agents
        }

    def _as_dict(self, agent: Any) -> Dict[str, Any]:
        if isinstance(agent, dict):
            return agent
        if hasattr(agent, "model_dump"):
            return agent.model_dump()
        if hasattr(agent, "__dict__"):
            return dict(agent.__dict__)
        return {}

    def _agent_id(self, agent: Any) -> str:
        if isinstance(agent, dict):
            return agent.get("id", "<unknown>")
        return getattr(agent, "id", "<unknown>")

    def _check_required_fields(
        self, data: Dict[str, Any], result: ValidationResult
    ) -> None:
        required = ("name", "role", "objective", "expected_output")
        for field_name in required:
            value = data.get(field_name)
            if value is None or (isinstance(value, str) and not value.strip()):
                result.add_error(f"Missing or empty required field: {field_name!r}")

    def _check_field_types(
        self, data: Dict[str, Any], result: ValidationResult
    ) -> None:
        complexity = data.get("complexity_level", "medium")
        if complexity not in VALID_COMPLEXITY_LEVELS:
            result.add_error(
                f"Invalid complexity_level {complexity!r}; "
                f"expected one of {sorted(VALID_COMPLEXITY_LEVELS)}"
            )

        status = data.get("status", "pending")
        if status not in VALID_STATUSES:
            result.add_error(
                f"Invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}"
            )

        deps = data.get("dependencies", [])
        if not isinstance(deps, list):
            result.add_error("dependencies must be a list of agent IDs")
        elif any(not isinstance(d, str) for d in deps):
            result.add_error("every dependency must be a string agent ID")

    def _check_traits(
        self, traits: Dict[str, Any], result: ValidationResult
    ) -> None:
        if not isinstance(traits, dict):
            result.add_error("traits must be a dict")
            return

        exploration_bias = traits.get("exploration_bias")
        if exploration_bias is not None:
            if not isinstance(exploration_bias, (int, float)):
                result.add_error("exploration_bias must be numeric")
            elif not (
                self.min_exploration_bias
                <= exploration_bias
                <= self.max_exploration_bias
            ):
                result.add_error(
                    f"exploration_bias {exploration_bias} out of range "
                    f"[{self.min_exploration_bias}, {self.max_exploration_bias}]"
                )

        refinement_depth = traits.get("refinement_depth")
        if refinement_depth is not None:
            if not isinstance(refinement_depth, int) or refinement_depth < 0:
                result.add_error("refinement_depth must be a non-negative int")
            elif refinement_depth > self.max_refinement_depth:
                result.add_warning(
                    f"refinement_depth {refinement_depth} exceeds configured max "
                    f"{self.max_refinement_depth}; will be clamped"
                )

        risk = traits.get("risk_sensitivity")
        if risk is not None and risk not in VALID_RISK_SENSITIVITY:
            result.add_warning(
                f"risk_sensitivity {risk!r} not in known set "
                f"{sorted(VALID_RISK_SENSITIVITY)}; treated as 'neutral'"
            )

        confidence = traits.get("confidence_threshold")
        if confidence is not None:
            if not isinstance(confidence, (int, float)):
                result.add_error("confidence_threshold must be numeric")
            elif not (0.0 <= confidence <= 1.0):
                result.add_error(
                    f"confidence_threshold {confidence} must be in [0, 1]"
                )

    def _check_expected_output(
        self, expected: Dict[str, Any], result: ValidationResult
    ) -> None:
        if not isinstance(expected, dict):
            result.add_error("expected_output must be a dict describing output shape")
            return
        if len(expected) == 0:
            result.add_warning(
                "expected_output is empty; output shape cannot be scored"
            )


def validate_agent(agent: Any, config: Optional[Dict[str, Any]] = None) -> ValidationResult:
    """Convenience wrapper: validate one agent using default config."""
    return AgentValidator(config).validate(agent)


def validate_agents(
    agents: List[Any], config: Optional[Dict[str, Any]] = None
) -> Dict[str, ValidationResult]:
    """Convenience wrapper: validate a batch of agents."""
    return AgentValidator(config).validate_batch(agents)


if __name__ == "__main__":
    from backend.app.agents.agent_schema import EXAMPLE_AGENT, create_agent_from_schema

    agent = create_agent_from_schema(EXAMPLE_AGENT)
    outcome = validate_agent(agent)
    print(f"valid={outcome.is_valid}")
    for err in outcome.errors:
        print(f"  ERROR: {err}")
    for warn in outcome.warnings:
        print(f"  WARN:  {warn}")
