"""Employee — a persistent agent with a role, scoped memory, and a task
inbox, sitting on top of the Phase 4 orchestration engine (Pipeline).

This is the core new abstraction behind Vision AI. Pipeline.run_objective
is stateless: give it an objective, get a result, it forgets everything.
An Employee remembers what it did for a specific founder/startup across
multiple calls and folds that history back into how it approaches the
next task — the difference between "a tool you invoke" and "an employee
you work with over time."

Deliberately role-generic, not startup-specific: subclasses only override
build_objective() to shape how a raw task becomes the objective string the
engine runs. Everything else (memory, execution, recording) is shared, so
a later general-audience employee type doesn't require touching this
class — see README "Audience rollout: founders first, everyone later."
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.app.orchestrator.pipeline_controller import Pipeline
from backend.app.employees.memory_store import EmployeeMemoryStore

logger = logging.getLogger(__name__)


class Employee:
    """Base class for a persistent, role-based AI employee."""

    role: str = "General Employee"
    # Floor for the adaptive router's tier choice (see
    # pipeline_controller.Pipeline.run_objective's min_tier). None means
    # "trust the router fully." Subclasses whose whole value proposition
    # depends on verification (e.g. IdeaValidationEmployee) should set
    # this rather than risk the router silently skipping critique on a
    # task it reads as low-stakes text.
    min_tier: Optional[str] = None
    # Ceiling for the router's tier choice. None = uncapped. A specialist
    # employee (see DynamicEmployee) sets this to single_call_critique so
    # it doesn't spawn ANOTHER team inside itself.
    max_tier: Optional[str] = None

    def __init__(
        self,
        employee_id: str,
        pipeline: Pipeline,
        memory_store: Optional[EmployeeMemoryStore] = None,
    ):
        self.employee_id = employee_id
        self.pipeline = pipeline
        self.memory = memory_store or EmployeeMemoryStore(employee_id)

    def build_objective(self, task: str) -> str:
        """Turn a raw task into the objective string the engine runs.
        Base implementation folds in relevant memory with no role
        framing; subclasses override this to shape it toward their role
        (see IdeaValidationEmployee)."""
        context = self.memory.relevant_context(task)
        if context:
            return f"{task}\n\nRelevant history for this founder/startup:\n{context}"
        return task

    def run_task(self, task: str, max_refinements: Optional[int] = None) -> Dict[str, Any]:
        """Execute one task through the underlying engine and record the
        outcome to persistent memory before returning."""
        objective = self.build_objective(task)
        logger.info("[%s/%s] running task: %s", self.role, self.employee_id, task[:80])

        manager = self.pipeline.run_objective(
            objective,
            max_refinements=max_refinements,
            min_tier=self.min_tier,
            max_tier=self.max_tier,
        )
        snap = manager.snapshot()

        result = {
            "task": task,
            "output": snap.get("synthesized_output"),
            "critique": snap.get("critique"),
            "was_refined": snap.get("was_refined"),
            "counts": snap.get("counts"),
        }
        self.memory.record(task, result)
        return result

    def history(self) -> List[Dict[str, Any]]:
        return self.memory.all_entries()
