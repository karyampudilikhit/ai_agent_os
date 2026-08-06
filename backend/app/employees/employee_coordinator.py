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

from backend.app.critique.evidence_extractor import EvidenceExtractor
from backend.app.employees.ceo_manager import CEOManager
from backend.app.employees.dynamic_employee import DynamicEmployee
from backend.app.employees.supervisor import SupervisorPlanner
from backend.app.orchestrator.pipeline_controller import Pipeline
from backend.app.orchestrator.synthesis import SynthesisEngine

logger = logging.getLogger(__name__)

TEAMMATE_SUMMARY_CHARS = 2000  # bound how much of each teammate's work is quoted forward

# Injected ahead of the Supervisor's mandate when it has no specialists,
# to cancel the mandate's standing "you plan, you don't do the work"
# instruction for that one call. See the comment at its use site in
# run_with_supervisor for the real failure this fixes.
_SOLO_SUPERVISOR_BRIEF = (
    "YOU ARE WORKING ALONE ON THIS TASK. There are no specialists on "
    "this Unit — nobody exists to delegate to, and nothing you assign "
    "will ever be picked up. For THIS task, ignore the part of your "
    "mandate that says you only plan and synthesize: you are the one "
    "doing the work, start to finish.\n\n"
    "This means:\n"
    "- DO NOT write a delegation plan, a team roster, a table of roles "
    "and responsibilities, a timeline, or 'Day 1 / Day 2' steps. Every "
    "one of those is a description of work instead of the work.\n"
    "- DO NOT invent colleagues ('the Research Analyst will...'). There "
    "is no Research Analyst. There is you.\n"
    "- USE YOUR TOOLS to get the real information yourself — browse to "
    "a real source and read it, search, call an API. You have them; a "
    "plan to use them later is not a deliverable.\n"
    "- DELIVER THE ACTUAL ANSWER the founder asked for, with real, "
    "specific values you actually retrieved (real names, real numbers, "
    "real dates), each traceable to the source you got it from.\n"
    "- If a source is blocked or you genuinely cannot get the data, say "
    "so in one line, state plainly what you DID manage to find, and "
    "name the single thing that blocked you. A short honest partial "
    "answer beats a polished plan every time."
)


class EmployeeCoordinator:
    """MVP coordinator: parallel-then-merged with a synthesis pass."""

    def __init__(self, pipeline: Pipeline, synthesis_engine: Optional[SynthesisEngine] = None):
        self.pipeline = pipeline
        # Reuse the same synthesis engine the multi-agent pipeline uses —
        # it already knows how to merge multiple contributions into one
        # coherent deliverable, no reason to write a second one.
        self.synthesis = synthesis_engine or SynthesisEngine(model_adapter=pipeline.adapter)
        # Evidence receipts: one extra pass after synthesis that turns
        # prose "unknown" markers and citations into a structured claims
        # ledger the UI can render as visible trust signals, instead of
        # leaving verification as an exercise for the reader.
        self.evidence = EvidenceExtractor(model_adapter=pipeline.adapter)

    def _extract_evidence(self, deliverable: str) -> List[Dict[str, str]]:
        """Never let evidence extraction break a run — on any failure,
        return an empty ledger and let the deliverable stand on its own."""
        try:
            return self.evidence.extract(deliverable)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Evidence extraction failed: %s", exc)
            return []

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
            solo_output = contributions[0].get("output") or ""
            return {
                "team": [self._team_entry(c) for c in contributions],
                "final_output": solo_output,
                "contributions": contributions,
                "evidence": self._extract_evidence(solo_output),
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

        final_output = merged or self._raw_concat(contributions)
        return {
            "team": [self._team_entry(c) for c in contributions],
            "final_output": final_output,
            "contributions": contributions,
            "evidence": self._extract_evidence(final_output),
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
        founder_task: Optional[str] = None,
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

        `founder_task`: the TRUE top-level founder prompt, before ANY
        rewriting. Only needed when `prompt` here is itself already a
        rewrite (the Company path passes a CEO-composed Unit brief as
        `prompt`, not the founder's original wording). Defaults to
        `prompt` — the common case where this function's caller passes
        the founder's raw task directly, so no explicit value is needed.
        Threaded to every specialist as `original_task` so a Supervisor's
        paraphrase (which frequently drops URLs and keywords the pre-
        flight heuristics key off) can't silently disable web-fetch,
        Tavily search, or deep browser research the founder's own
        wording would have triggered.
        """
        effective_founder_task = founder_task or prompt
        if supervisor is None:
            raise ValueError("run_with_supervisor requires a supervisor")

        # If the Unit has NO specialists yet, the Supervisor has to do
        # the work alone rather than return an empty deliverable. This
        # is a safety net — the frontend is supposed to auto-hire
        # specialists before it gets here — but crashes there
        # shouldn't silently produce empty output.
        #
        # It also needs _SOLO_SUPERVISOR_BRIEF below. The Supervisor's
        # standing mandate says "you do NOT do specialist work — you plan
        # and synthesize", which is right when it has a team and
        # catastrophic when it doesn't. Observed twice on a real "report
        # stocks up >30%" task: alone on the Unit, it obediently produced
        # a delegation plan — a roster of five specialists who do not
        # exist, plus a Day 1-4 timeline — and zero actual stock data,
        # despite having working browser tools and having already pulled
        # a page of search results. It was following its mandate
        # correctly; the mandate was simply wrong for this case.
        if not specialists:
            logger.info("No specialists on Unit; Supervisor handling task solo")
            if on_role_working:
                try: on_role_working(supervisor.role)
                except Exception: pass  # noqa: BLE001
            result = supervisor.run_task(prompt, task_brief=_SOLO_SUPERVISOR_BRIEF)
            if on_role_done:
                try: on_role_done(supervisor.role, self._team_entry(result))
                except Exception: pass  # noqa: BLE001
            solo_output = result.get("output") or ""
            return {
                "team": [self._team_entry(result)],
                "final_output": solo_output,
                "contributions": [result],
                "plan": [],
                "supervisor_role": supervisor.role,
                "evidence": self._extract_evidence(solo_output),
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
            task_brief = assignment.get("task_brief")
            employee = by_role.get(role)
            if not employee:
                continue
            teammates_context = self._format_prior_work(contributions)
            if on_role_working:
                try: on_role_working(role)
                except Exception: pass  # noqa: BLE001
            logger.info("Supervisor delegating '%s' to %s", sub_task[:60], role)
            result = employee.run_task(
                sub_task,
                teammates_context=teammates_context,
                task_brief=task_brief,
                original_task=effective_founder_task,
            )
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
            "evidence": self._extract_evidence(merged),
        }

    # ------------------------------------------------------------------
    # Company-level run — Phase 2 of the hierarchy.
    #
    # The CEO takes a Company-wide task, decides which Units handle
    # which pieces, delegates to each Unit's Supervisor (which runs its
    # own team of specialists), then synthesizes across Units into one
    # Company-level deliverable in the CEO's voice.
    #
    # Pattern mirrors run_with_supervisor exactly — same lifecycle, one
    # altitude higher.
    # ------------------------------------------------------------------

    def run_with_ceo(
        self,
        prompt: str,
        company: Dict[str, Any],
        units: List[Dict[str, Any]],
        unit_runner,
        on_planning=None,
        on_delegated=None,
        on_unit_working=None,
        on_unit_done=None,
        on_synthesizing=None,
    ) -> Dict[str, Any]:
        """CEO-led Company-wide execution.

        Params:
          company     : dict with {id, name, purpose} — for the CEO's
                        situational awareness in the delegation prompt.
          units       : list of dicts describing each Unit the CEO can
                        delegate to. Each item:
                            {unit_id, name, purpose, members: [...]}
                        Same shape CEOManager expects.
          unit_runner : callable (unit_id, sub_task, unit_brief) ->
                        {output, team, plan, contributions, ...}
                        The route provides this — it constructs the
                        Unit's pipeline + supervisor + specialists and
                        calls run_with_supervisor(). Keeping the runner
                        injectable means this method has no dependency
                        on the API/route wiring.

        Falls back gracefully at each stage: if CEO planning fails,
        every Unit gets the raw prompt; if synthesis fails, raw concat
        of Unit outputs.
        """
        if not units:
            raise ValueError("run_with_ceo requires at least one Unit")

        ceo = CEOManager(model_adapter=self.pipeline.adapter)

        if on_planning:
            try: on_planning()
            except Exception: pass  # noqa: BLE001

        plan = ceo.plan_company_delegation(prompt, company, units)
        logger.info("CEO plan: %d Unit assignment(s)", len(plan))

        if on_delegated:
            try: on_delegated(plan)
            except Exception: pass  # noqa: BLE001

        # Map unit_id -> unit dict for name/roster lookup during dispatch
        by_unit_id = {u["unit_id"]: u for u in units}

        unit_contributions: List[Dict[str, Any]] = []
        for assignment in plan:
            uid = assignment.get("unit_id")
            sub_task = assignment.get("sub_task") or prompt
            unit_brief = assignment.get("unit_brief")
            unit_info = by_unit_id.get(uid)
            if not unit_info:
                continue

            if on_unit_working:
                try: on_unit_working(uid, unit_info)
                except Exception: pass  # noqa: BLE001

            logger.info(
                "CEO delegating to Unit %s (%s): %s",
                uid, unit_info.get("name") or "(unnamed)", sub_task[:60]
            )

            try:
                unit_result = unit_runner(uid, sub_task, unit_brief)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Unit %s run failed: %s", uid, exc)
                unit_result = {"output": f"(Unit {uid} failed: {exc})"}

            entry = {
                "unit_id": uid,
                "unit_name": unit_info.get("name"),
                "output": (unit_result or {}).get("final_output")
                    or (unit_result or {}).get("output"),
                "supervisor_role": (unit_result or {}).get("supervisor_role"),
                "team": (unit_result or {}).get("team") or [],
            }
            unit_contributions.append(entry)

            if on_unit_done:
                try: on_unit_done(uid, entry)
                except Exception: pass  # noqa: BLE001

        if on_synthesizing:
            try: on_synthesizing()
            except Exception: pass  # noqa: BLE001

        merged = ceo.synthesize(prompt, unit_contributions) if unit_contributions else None
        if not merged:
            merged = ceo._raw_concat(unit_contributions)  # noqa: SLF001 — internal fallback

        return {
            "company_id": company.get("id"),
            "company_name": company.get("name"),
            "final_output": merged,
            "plan": plan,
            "unit_contributions": unit_contributions,
            "evidence": self._extract_evidence(merged),
        }
