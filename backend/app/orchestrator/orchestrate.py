"""orchestrate() — the actual Vision AI flow, in one function:

    user prompt
      -> (optional) clarify
      -> adaptive supervisor decides: single call, or team-of-employees?
      -> if team: spawn dynamic employees, they collaborate (Plan B),
                  merge into one deliverable
         if not:  run the existing single-call path
      -> verified output back

Every other entry point in the codebase (CLI, API, run_idea script)
should call *this*, not Pipeline directly. Pipeline is still the
low-level engine one Employee uses to do one task; this is the
top-level "handle a user's prompt however it needs to be handled."
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from backend.app.employees.employee_coordinator import EmployeeCoordinator
from backend.app.employees.employee_spawner import EmployeeSpawner
from backend.app.orchestrator.adaptive_supervisor import AdaptiveSupervisor
from backend.app.orchestrator.pipeline_controller import Pipeline

logger = logging.getLogger(__name__)


def orchestrate(
    prompt: str,
    pipeline: Pipeline,
    session_id: Optional[str] = None,
    spawner: Optional[EmployeeSpawner] = None,
    supervisor: Optional[AdaptiveSupervisor] = None,
    coordinator: Optional[EmployeeCoordinator] = None,
    force_team: bool = False,
) -> Dict[str, Any]:
    """Run a user prompt end-to-end.

    Returns a dict with:
      - `mode`: "single_call" | "single_call_critique" | "team"
      - `team`: list of {role, completeness_score, fabricated_claims, was_refined}
               (populated only when mode == "team")
      - `final_output`: the string to show the user
      - `session_id`: reuse this to make follow-up prompts share memory
                      with the same team

    `force_team` bypasses the supervisor and always spawns a team —
    useful for the "AI employees" demo experience where the point IS the
    team, not the answer. Off by default; supervisor picks the cheapest
    tier that fits.
    """
    if not prompt or not prompt.strip():
        raise ValueError("orchestrate() requires a non-empty prompt")

    session_id = session_id or f"session_{uuid.uuid4().hex[:8]}"
    supervisor = supervisor or AdaptiveSupervisor(model_adapter=pipeline.adapter)
    spawner = spawner or EmployeeSpawner(model_adapter=pipeline.adapter)
    coordinator = coordinator or EmployeeCoordinator(pipeline=pipeline)

    tier = "multi_agent_critique" if force_team else supervisor.classify(prompt)
    logger.info("[%s] orchestrate tier=%s force_team=%s", session_id, tier, force_team)

    if tier in ("single_call", "single_call_critique"):
        manager = pipeline.run_objective(prompt, tier=tier)
        snap = manager.snapshot()
        return {
            "mode": tier,
            "team": [],
            "final_output": snap.get("synthesized_output") or "",
            "session_id": session_id,
        }

    # Team path.
    team = spawner.spawn(prompt=prompt, pipeline=pipeline, session_id=session_id)
    logger.info("[%s] spawned team of %d: %s", session_id, len(team), [e.role for e in team])

    result = coordinator.run(prompt=prompt, team=team)
    return {
        "mode": "team",
        "team": result["team"],
        "final_output": result["final_output"],
        "session_id": session_id,
    }
