"""EmployeeCoordinator — runs a spawned team on a prompt and combines
their outputs into one final deliverable.

**MVP shape (Plan B): parallel-then-merged.** Each employee gets the
same user prompt plus every earlier teammate's completed output as
read-only context, works independently, and a synthesis pass merges
the team's outputs into one coherent answer. Employees do NOT
interrupt or steer each other in-flight.

**v1 shape (Plan C): true collaboration.** Swap this class (same
interface) for one where employees can see each other's *in-progress*
work, hand off dynamically, and comment on each other's drafts. That's
a real change in this file only — nothing downstream (the API,
`orchestrate.py`, DynamicEmployee itself) needs to change.

Sits between EmployeeSpawner (which decides *who* is on the team) and
the API/CLI (which asks *what did the team produce*).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.app.employees.dynamic_employee import DynamicEmployee
from backend.app.employees.supervisor import SupervisorPlanner
from backend.app.orchestrator.pipeline_controller import Pipeline
from backend.app.orchestrator.synthesis import SynthesisEngine

logger = logging.getLogger(__name__)

TEAMMATE_SUMMARY_CHARS = 2000  # bound how much of each teammate's work is quoted forward


class EmployeeCoordinator:
    """MVP coordinator: parallel-then-merged with a synthesis pass."""

    def __init__(self, pipeline: Pipeline, synthesis_engine: Optional[SynthesisEngine] = None):
        self.pipeline = pipeline
        # Reuse the same synthesis engine the multi-agent pipeline uses —
        # it already knows how to merge multiple contributions into one
        # coherent deliverable, no reason to write a second one.
        self.synthesis = synthesis_engine or SynthesisEngine(model_adapter=pipeline.adapter)

    def run(
        self,
        prompt: str,
        team: List[DynamicEmployee],
        on_role_working=None,
        on_role_done=None,
        on_synthesizing=None,
    ) -> Dict[str, Any]:
        if not team:
            raise ValueError("EmployeeCoordinator.run() requires at least one employee")

        contributions: List[Dict[str, Any]] = []
        for employee in team:
            teammates_context = self._format_prior_work(contributions)
            logger.info("Delegating to %s (%s)", employee.role, employee.employee_id)
            if on_role_working:
                try:
                    on_role_working(employee.role)
                except Exception:  # noqa: BLE001  # never let progress callbacks crash a run
                    pass
            result = employee.run_task(prompt, teammates_context=teammates_context)
            contributions.append(result)
            if on_role_done:
                try:
                    on_role_done(employee.role, self._team_entry(result))
                except Exception:  # noqa: BLE001
                    pass

        if len(contributions) == 1:
            return {
                "team": [self._team_entry(c) for c in contributions],
                "final_output": contributions[0].get("output") or "",
                "contributions": contributions,
            }

        # SynthesisEngine expects a list of AgentResult-like objects
        # (dict with an `output` key works — see how synthesis.py reads
        # results.__iter__).
        synth_input = [
            type("Contribution", (), {
                "output": c.get("output"),
                "role": c.get("role"),
                "confidence": 1.0,
                "status": "completed",
                "agent_id": self._contrib_agent_id(c),
            })()
            for c in contributions
            if c.get("output")
        ]

        if on_synthesizing:
            try:
                on_synthesizing()
            except Exception:  # noqa: BLE001
                pass

        try:
            merged = self.synthesis.synthesize(prompt, synth_input)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Synthesis merge failed, falling back to raw concat: %s", exc)
            merged = self._raw_concat(contributions)

        return {
            "team": [self._team_entry(c) for c in contributions],
            "final_output": merged or self._raw_concat(contributions),
            "contributions": contributions,
        }

    def _format_prior_work(self, contributions: List[Dict[str, Any]]) -> Optional[str]:
        if not contributions:
            return None
        blocks = []
        for c in contributions:
            role = c.get("role") or "Teammate"
            out = (c.get("output") or "").strip()
            if not out:
                continue
            blocks.append(f"[{role}]\n{out[:TEAMMATE_SUMMARY_CHARS]}")
        return "\n\n".join(blocks) if blocks else None

    def _raw_concat(self, contributions: List[Dict[str, Any]]) -> str:
        parts = []
        for c in contributions:
            out = (c.get("output") or "").strip()
            if not out:
                continue
            parts.append(f"## {c.get('role') or 'Teammate'}\n\n{out}")
        return "\n\n---\n\n".join(parts)

    def _team_entry(self, contribution: Dict[str, Any]) -> Dict[str, Any]:
        crit = contribution.get("critique") or {}
        return {
            "role": contribution.get("role"),
            "completeness_score": crit.get("completeness_score"),
            "fabricated_claims": crit.get("fabricated_claims") or [],
            "was_refined": contribution.get("was_refined"),
        }

    def _contrib_agent_id(self, c: Dict[str, Any]) -> str:
        role = c.get("role") or "teammate"
        return "employee_" + role.lower().replace(" ", "_")

    # ------------------------------------------------------------------
    # Supervised run — Phase 1 of the Unit + Supervisor architecture.
    #
    # Instead of dumping the raw user prompt on every specialist, the
    # Supervisor first decides who does what, publishes a delegation
    # plan, and hands each specialist a specific sub-task. After the
    # specialists finish, the Supervisor synthesizes.
    # ------------------------------------------------------------------

    def run_with_supervisor(
        self,
        prompt: str,
        supervisor: DynamicEmployee,
        specialists: List[DynamicEmployee],
        on_planning=None,
        on_delegated=None,
        on_role_working=None,
        on_role_done=None,
        on_synthesizing=None,
    ) -> Dict[str, Any]:
        """Supervisor-led execution.

        Flow:
          1. Supervisor designs a delegation plan (one sub-task per
             specialist it wants to use — may exclude specialists it
             doesn't need for this particular task).
          2. Each targeted specialist runs its sub-task, seeing earlier
             specialists' work as read-only context.
          3. Supervisor synthesizes all specialist outputs into one
             coherent deliverable in one voice.

        Falls back gracefully at each stage: if planning fails, every
        specialist gets the raw prompt; if synthesis fails, raw concat.
        """
        if supervisor is None:
            raise ValueError("run_with_supervisor requires a supervisor")

        # If the Unit has NO specialists yet, the Supervisor has to do
        # the work alone rather than return an empty deliverable. This
        # is a safety net — the frontend is supposed to auto-hire
        # specialists before it gets here — but crashes there
        # shouldn't silently produce empty output.
        if not specialists:
            logger.info("No specialists on Unit; Supervisor handling task solo")
            if on_role_working:
                try: on_role_working(supervisor.role)
                except Exception: pass  # noqa: BLE001
            result = supervisor.run_task(prompt)
            if on_role_done:
                try: on_role_done(supervisor.role, self._team_entry(result))
                except Exception: pass  # noqa: BLE001
            return {
                "team": [self._team_entry(result)],
                "final_output": result.get("output") or "",
                "contributions": [result],
                "plan": [],
                "supervisor_role": supervisor.role,
            }

        specialists_spec = [{"role": s.role, "mandate": s.mandate} for s in specialists]

        planner = SupervisorPlanner(model_adapter=self.pipeline.adapter)

        if on_planning:
            try: on_planning()
            except Exception: pass  # noqa: BLE001

        plan = planner.design_delegation(prompt, specialists_spec) if specialists else []
        logger.info("Supervisor plan: %d assignment(s)", len(plan))

        if on_delegated:
            try: on_delegated(plan)
            except Exception: pass  # noqa: BLE001

        # Map role -> employee for delegation
        by_role = {s.role: s for s in specialists}

        contributions: List[Dict[str, Any]] = []
        for assignment in plan:
            role = assignment.get("role")
            sub_task = assignment.get("sub_task") or prompt
            employee = by_role.get(role)
            if not employee:
                continue
            teammates_context = self._format_prior_work(contributions)
            if on_role_working:
                try: on_role_working(role)
                except Exception: pass  # noqa: BLE001
            logger.info("Supervisor delegating '%s' to %s", sub_task[:60], role)
            result = employee.run_task(sub_task, teammates_context=teammates_context)
            # Track under the specialist's role name for progress/UI
            result["role"] = role
            contributions.append(result)
            if on_role_done:
                try: on_role_done(role, self._team_entry(result))
                except Exception: pass  # noqa: BLE001

        if on_synthesizing:
            try: on_synthesizing()
            except Exception: pass  # noqa: BLE001

        # Supervisor synthesizes rather than the generic SynthesisEngine
        # so the merged result is written in the Supervisor's "one voice"
        # framing (matches the org-chart mental model).
        merged = planner.synthesize(prompt, contributions) if contributions else None
        if not merged:
            merged = self._raw_concat(contributions)

        return {
            "team": [self._team_entry(c) for c in contributions],
            "final_output": merged,
            "contributions": contributions,
            "plan": plan,
            "supervisor_role": supervisor.role,
        }
