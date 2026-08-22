"""Central mutable state for one contract's execution.

An ExecutionState is threaded through the orchestrator: the pipeline
controller creates it, the execution engine mutates it as agents run,
and callers read the final snapshot. It is deliberately a plain object,
not a database — persistence is a Phase 9 concern.

Phase 4 assumes single-run in-process usage. Concurrency comes in a
later phase; when it does, add a lock around the mutating methods.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


TERMINAL_STATUSES = {"completed", "failed", "partial", "aborted"}


@dataclass
class ExecutionState:
    """Snapshot of a whole execution as it evolves."""

    contract_id: str
    objective: str
    all_agents: List[Any] = field(default_factory=list)  # AgentSchema instances
    agent_results: Dict[str, Any] = field(default_factory=dict)  # id -> AgentResult

    pending_agents: List[str] = field(default_factory=list)
    running_agents: List[str] = field(default_factory=list)
    completed_agents: List[str] = field(default_factory=list)
    failed_agents: List[str] = field(default_factory=list)

    total_cost: float = 0.0
    total_tokens: int = 0
    total_agent_seconds: float = 0.0

    status: str = "pending"  # pending | running | completed | failed | partial | aborted
    error: Optional[str] = None

    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    # Free-form context for guards & later phases (e.g. RecursionGuard reads it).
    context: Dict[str, Any] = field(default_factory=dict)

    # One coherent final document combining every completed agent's
    # output. None until Pipeline runs the synthesis stage; falls back
    # to None (never a crash) if synthesis itself fails.
    synthesized_output: Optional[str] = None

    # Critique/refine stage output (R&D, see critique/critique_agent.py).
    critique: Optional[Dict[str, Any]] = None
    was_refined: bool = False


class StateManager:
    """Owns an ExecutionState and keeps its indices consistent."""

    def __init__(self, contract: Any):
        contract_id = self._get(contract, "id", "unknown_contract")
        objective = self._get(contract, "objective", "")
        self.state = ExecutionState(contract_id=contract_id, objective=objective)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def register_agents(self, agents: List[Any]) -> None:
        """Register the full agent set for this run."""
        self.state.all_agents = list(agents)
        self.state.pending_agents = [self._get(a, "id") for a in agents]
        self.state.running_agents = []
        self.state.completed_agents = []
        self.state.failed_agents = []
        self._refresh_context()

    def mark_started(self) -> None:
        self.state.started_at = datetime.utcnow()
        self.state.status = "running"

    def mark_agent_running(self, agent_id: str) -> None:
        if agent_id in self.state.pending_agents:
            self.state.pending_agents.remove(agent_id)
        if agent_id not in self.state.running_agents:
            self.state.running_agents.append(agent_id)
        self._refresh_context()

    def mark_result(self, agent_id: str, result: Any) -> None:
        """Record the outcome of a finished agent."""
        self.state.agent_results[agent_id] = result
        if agent_id in self.state.running_agents:
            self.state.running_agents.remove(agent_id)
        if agent_id in self.state.pending_agents:
            self.state.pending_agents.remove(agent_id)

        status = self._get(result, "status", "failed")
        if status == "completed":
            self.state.completed_agents.append(agent_id)
        else:
            self.state.failed_agents.append(agent_id)

        self.state.total_cost += float(self._get(result, "cost", 0.0) or 0.0)
        self.state.total_tokens += int(self._get(result, "tokens_used", 0) or 0)
        self.state.total_agent_seconds += float(
            self._get(result, "duration_seconds", 0.0) or 0.0
        )
        self._refresh_context()

    def mark_finished(self, error: Optional[str] = None) -> None:
        self.state.finished_at = datetime.utcnow()
        if error:
            self.state.error = error
            self.state.status = "aborted"
            return

        total = len(self.state.all_agents)
        succeeded = len(self.state.completed_agents)
        failed = len(self.state.failed_agents)

        if total == 0:
            self.state.status = "failed"
        elif succeeded == total:
            self.state.status = "completed"
        elif failed == total:
            self.state.status = "failed"
        else:
            self.state.status = "partial"

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_terminal(self) -> bool:
        return self.state.status in TERMINAL_STATUSES

    def get_result(self, agent_id: str) -> Optional[Any]:
        return self.state.agent_results.get(agent_id)

    def set_synthesized_output(self, text: Optional[str]) -> None:
        self.state.synthesized_output = text

    def set_critique(self, critique: Optional[Dict[str, Any]]) -> None:
        self.state.critique = critique

    def set_was_refined(self, refined: bool) -> None:
        self.state.was_refined = refined

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serializable summary of the current state."""
        return {
            "contract_id": self.state.contract_id,
            "objective": self.state.objective,
            "status": self.state.status,
            "error": self.state.error,
            "counts": {
                "total": len(self.state.all_agents),
                "pending": len(self.state.pending_agents),
                "running": len(self.state.running_agents),
                "completed": len(self.state.completed_agents),
                "failed": len(self.state.failed_agents),
            },
            "totals": {
                "cost": round(self.state.total_cost, 6),
                "tokens": self.state.total_tokens,
                "agent_seconds": round(self.state.total_agent_seconds, 3),
            },
            "started_at": (
                self.state.started_at.isoformat() if self.state.started_at else None
            ),
            "finished_at": (
                self.state.finished_at.isoformat() if self.state.finished_at else None
            ),
            "synthesized_output": self.state.synthesized_output,
            "critique": self.state.critique,
            "was_refined": self.state.was_refined,
            "results": [
                self._result_to_dict(agent_id, res)
                for agent_id, res in self.state.agent_results.items()
            ],
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _refresh_context(self) -> None:
        """Keep state.context in sync with the derived counters.

        RecursionGuard reads this dict directly, so we mirror the keys
        it looks for (`all_agents`, `completed_agents`, `total_cost`).
        """
        self.state.context = {
            "all_agents": [self._get(a, "id") for a in self.state.all_agents],
            "completed_agents": list(self.state.completed_agents),
            "failed_agents": list(self.state.failed_agents),
            "total_cost": self.state.total_cost,
        }

    def _result_to_dict(self, agent_id: str, result: Any) -> Dict[str, Any]:
        if hasattr(result, "to_dict"):
            return result.to_dict()
        if isinstance(result, dict):
            return {"agent_id": agent_id, **result}
        return {"agent_id": agent_id, "value": str(result)}

    def _get(self, obj: Any, field_name: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(field_name, default)
        return getattr(obj, field_name, default)
