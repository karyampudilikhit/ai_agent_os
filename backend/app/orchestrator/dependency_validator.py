"""Validates the agent dependency graph and produces a scheduling plan.

Agents can list `dependencies` (IDs of other agents they wait for). This
module checks that graph for the common failure modes — cycles, dangling
references, excessive depth — and returns a topological layering so the
execution engine can run each layer's agents together.

Phase 4's factory doesn't yet emit real dependencies (all agents come out
independent), so in practice every plan today is one layer. The machinery
is here for when Phase 6's adaptive supervisor starts wiring real
dependencies.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class DependencyError(Exception):
    """Raised when the dependency graph cannot be executed."""


class DependencyValidator:
    """Validates and topologically sorts the agent dependency graph."""

    def __init__(self, max_dependency_depth: int = 10):
        self.max_depth = max_dependency_depth

    def validate_and_plan(self, agents: List[Any]) -> List[List[Any]]:
        """Return a list of layers; agents in the same layer can run in parallel.

        Raises DependencyError on cycle, dangling reference, or depth > max.
        """
        if not agents:
            return []

        by_id = self._index_agents(agents)
        self._check_dangling(by_id)
        self._check_no_self_loops(by_id)
        layers = self._topological_layers(by_id)
        self._check_depth(layers)
        return layers

    # ------------------------------------------------------------------

    def _index_agents(self, agents: List[Any]) -> Dict[str, Any]:
        index: Dict[str, Any] = {}
        for agent in agents:
            agent_id = self._get(agent, "id")
            if not agent_id:
                raise DependencyError("Agent is missing an id")
            if agent_id in index:
                raise DependencyError(f"Duplicate agent id: {agent_id}")
            index[agent_id] = agent
        return index

    def _check_dangling(self, by_id: Dict[str, Any]) -> None:
        for agent_id, agent in by_id.items():
            for dep in self._get(agent, "dependencies", []) or []:
                if dep not in by_id:
                    raise DependencyError(
                        f"Agent {agent_id} depends on unknown agent {dep!r}"
                    )

    def _check_no_self_loops(self, by_id: Dict[str, Any]) -> None:
        for agent_id, agent in by_id.items():
            deps = self._get(agent, "dependencies", []) or []
            if agent_id in deps:
                raise DependencyError(f"Agent {agent_id} depends on itself")

    def _topological_layers(self, by_id: Dict[str, Any]) -> List[List[Any]]:
        """Kahn's algorithm, grouped by 'ready at the same time' layers."""
        indegree: Dict[str, int] = {}
        dependents: Dict[str, List[str]] = defaultdict(list)

        for agent_id, agent in by_id.items():
            deps = self._get(agent, "dependencies", []) or []
            indegree[agent_id] = len(deps)
            for dep in deps:
                dependents[dep].append(agent_id)

        layers: List[List[Any]] = []
        current = deque(aid for aid, deg in indegree.items() if deg == 0)

        placed: Set[str] = set()
        total = len(by_id)

        while current:
            layer_ids = list(current)
            layers.append([by_id[aid] for aid in layer_ids])
            placed.update(layer_ids)

            next_layer: deque[str] = deque()
            for aid in layer_ids:
                for downstream in dependents.get(aid, []):
                    indegree[downstream] -= 1
                    if indegree[downstream] == 0:
                        next_layer.append(downstream)
            current = next_layer

        if len(placed) != total:
            unresolved = set(by_id) - placed
            raise DependencyError(
                f"Dependency cycle involving: {sorted(unresolved)}"
            )

        return layers

    def _check_depth(self, layers: List[List[Any]]) -> None:
        depth = len(layers)
        if depth > self.max_depth:
            raise DependencyError(
                f"Dependency depth {depth} exceeds max_dependency_depth "
                f"{self.max_depth}"
            )

    def _get(self, obj: Any, field_name: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(field_name, default)
        return getattr(obj, field_name, default)


def plan_execution(
    agents: List[Any], max_dependency_depth: int = 10
) -> List[List[Any]]:
    """Convenience wrapper."""
    return DependencyValidator(max_dependency_depth).validate_and_plan(agents)
