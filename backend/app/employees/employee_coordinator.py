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

TEAMMATE_SUMMARY_CHARS = 2000  # bound how much of each teammate's PROSE is quoted forward
# Raw retrieved facts get their own, larger budget: a list of papers with
# DOIs, or a pricing table, is dense and useless when clipped mid-row —
# and losing it is what made downstream specialists invent data.
TEAMMATE_FACTS_CHARS = 5000
TOTAL_FACTS_CHARS = 12000  # ceiling across all teammates, so the prompt stays bounded


def _successful_call_count() -> int:
    """Successful tool calls recorded process-wide so far.

    Sampled either side of a specialist's turn to measure what it
    actually achieved. Process-wide rather than per-run is fine for the
    delta — specialists run sequentially here, so nothing else is
    incrementing the counter in between. Never raises: a bookkeeping
    failure must not stop a run, and returning 0 simply means the
    receipt is unavailable rather than wrong.
    """
    try:
        from backend.app.tools.tool_call_ledger import get_call_ledger
        return get_call_ledger().counts().get("ok", 0)
    except Exception:  # noqa: BLE001
        return 0

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
            claims = self.evidence.extract(deliverable)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Evidence extraction failed: %s", exc)
            claims = []
        try:
            return self._flag_fabricated_citations(deliverable, claims)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Citation verification failed: %s", exc)
            return claims

    def _flag_fabricated_citations(
        self, deliverable: str, claims: List[Dict[str, str]]
    ) -> List[Dict[str, str]]:
        """Check every cited URL against what this process actually
        retrieved, and mark the ones that were never fetched or seen.

        The LLM-based EvidenceExtractor structurally cannot catch this:
        it only reads the finished text, and in text a fabricated URL
        looks exactly like a real one. It rated a report "0 fabricated
        claims" whose every business-model row cited a Wikipedia page the
        run never opened. This pass asks the network instead — see
        backend/app/tools/source_ledger.py.

        Two things happen here:
          1. Any extracted claim whose `source` URL is unknown to the
             ledger is downgraded to `fabricated_source` — strictly worse
             than `unsourced_claim`, because a fake citation actively
             buys trust an uncited guess never gets.
          2. URLs cited anywhere in the deliverable that the extractor
             didn't attach to a claim get their own entries, so a fake
             citation can't hide by not being picked up.
        """
        from backend.app.tools.source_ledger import extract_urls, get_ledger

        ledger = get_ledger()
        out: List[Dict[str, str]] = []
        accounted: set = set()

        for claim in claims:
            source = str(claim.get("source") or "").strip()
            # A source field can hold SEVERAL urls — the extractor
            # happily produces "https://a/pricing https://b/pricing" when
            # a claim draws on two pages. Treating that as one opaque
            # string made a genuine, fully-sourced Notion/Linear
            # comparison come back with a fabricated_source flag, which
            # is the false accusation this whole check must never make.
            # Split first; the claim is fabricated only if EVERY url in
            # it is unknown.
            source_urls = extract_urls(source) if source else []
            if source_urls:
                for u in source_urls:
                    accounted.add(u.rstrip(".,;:!?'\")]}>"))
                if not any(ledger.is_known(u) for u in source_urls):
                    claim = {**claim, "status": "fabricated_source"}
                    logger.warning(
                        "Fabricated citation: %s was never fetched in this process",
                        source[:120],
                    )
            out.append(claim)

        # Catch fake URLs the extractor never turned into a claim.
        from backend.app.tools.source_ledger import normalize_url

        seen_keys = {normalize_url(u) for u in accounted}
        for url in extract_urls(deliverable):
            if normalize_url(url) in seen_keys:
                continue
            if ledger.is_known(url):
                continue
            logger.warning("Fabricated citation (uncited in claims): %s", url[:120])
            out.append({
                "text": (
                    f"Cites {url} as a source, but this URL was never opened "
                    f"during this run."
                ),
                "status": "fabricated_source",
                "source": url,
            })
        return out

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
            before = _successful_call_count()
            result = employee.run_task(prompt, teammates_context=teammates_context)
            # Receipt, measured rather than self-reported. A specialist's
            # own prose is exactly what cannot be trusted here — one
            # finished with ZERO agentic steps and still wrote a
            # confident-sounding contribution that the next role built
            # on. The tool ledger knows what actually happened.
            gained = _successful_call_count() - before
            result["successful_tool_calls"] = gained
            result["produced_nothing"] = (
                gained == 0 and not (result.get("gathered_context") or "").strip()
            )
            if result["produced_nothing"]:
                logger.warning(
                    "%s produced no real data (0 successful tool calls) — "
                    "downstream specialists will be told not to rely on it",
                    employee.role,
                )
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
        """What a specialist is shown of the work already done on this run.

        Forwards each teammate's written output AND the raw material they
        retrieved (search results, fetched pages, real tool results).

        The second half is the important one and used to be missing. With
        only the prose summaries, a downstream specialist could see that
        someone had "identified five papers" but not what they were — so
        it went looking for a file on disk, then emailed a teammate who
        does not exist, and the specialist after that invented citations.
        The raw facts are what stop that: this IS the upstream handoff,
        so the prompt says so explicitly.
        """
        if not contributions:
            return None
        blocks = []
        facts_budget = TOTAL_FACTS_CHARS
        for c in contributions:
            role = c.get("role") or "Teammate"
            out = (c.get("output") or "").strip()
            facts = (c.get("gathered_context") or "").strip()
            if not out and not facts:
                continue
            # Say plainly when an upstream step retrieved NOTHING.
            #
            # Both live tests failed the same way here. A Quant Analyst
            # was told to "run a full backtest on the cleaned dataset"
            # that the Data Engineer never built; a Rule Analyst was told
            # to "infer the rule from the Game Operator's grid logs" when
            # no frame had ever been retrieved. In both cases the
            # downstream specialist read confident prose about work that
            # had not happened, assumed the artefact existed, and
            # produced filler. The prose alone cannot carry this — it has
            # to be stated.
            if c.get("produced_nothing"):
                blocks.append(
                    f"[{role}] PRODUCED NO REAL DATA — every tool call it made "
                    f"failed, or it made none. Anything it describes below is "
                    f"unverified. Do NOT assume any file, dataset or artefact "
                    f"it mentions exists. If your task depends on that output, "
                    f"say so plainly instead of proceeding as if it were there."
                )
            if out:
                blocks.append(f"[{role}] wrote:\n{out[:TEAMMATE_SUMMARY_CHARS]}")
            # Newest contributions matter most, but they're appended in
            # order, so spend the budget as we go and stop when it's out
            # rather than truncating every block to uselessness.
            if facts and facts_budget > 0:
                slice_len = min(len(facts), TEAMMATE_FACTS_CHARS, facts_budget)
                facts_budget -= slice_len
                blocks.append(
                    f"[{role}] RETRIEVED THIS SOURCE MATERIAL — these are the real "
                    f"values from real sources. Use them directly. Do NOT look for "
                    f"a file, and do NOT ask anyone to send them to you:\n"
                    f"{facts[:slice_len]}"
                )
        return "\n\n".join(blocks) if blocks else None

    # Below this, a specialist's own critique score means "this is not
    # real work" rather than "it's a bit thin". Deliberately looser than
    # the 0.7 refinement-acceptance gate used elsewhere -- that gate asks
    # "is this good enough to ship"; this one only asks "did anything
    # usable happen at all". The two live failures that motivated it (a
    # bare tool-call-JSON stub, and an explicit "I cannot produce this")
    # scored 0.05 and 0.0; genuine partial work scores well above this.
    TEAM_COMPLETENESS_FLOOR = 0.3

    def _team_completeness_warning(self, contributions):
        """Deterministic disclosure banner for the gap a live test found:
        two specialists both failed their assigned work and the
        Supervisor's synthesis produced a confident, polished business
        report anyway, with nothing telling the founder the underlying
        research never happened.

        Reads the critique engine's own recorded completeness_score --
        NOT the Supervisor's prose. A guard that pattern-matches the
        narrative for a disclaimer is the shape that has failed every
        time it has been tried in this codebase; a guard that checks a
        recorded number is the shape that holds.

        Also closes a second gap: synthesize() and _raw_concat() both
        drop contributions with empty output, so a specialist that
        returns nothing vanishes without trace. Checked here from the
        SAME list, before that filtering happens.
        """
        failed = []
        for c in contributions:
            role = c.get("role") or "Teammate"
            output = (c.get("output") or "").strip()
            if not output:
                failed.append((role, "produced no output at all"))
                continue
            crit = c.get("critique") or {}
            score = crit.get("completeness_score")
            if score is not None and score < self.TEAM_COMPLETENESS_FLOOR:
                gaps = crit.get("gaps") or []
                reason = gaps[0] if gaps else "did not complete the assigned work"
                failed.append((role, "completeness %.2f/1.0 - %s" % (score, reason)))

        if not failed:
            return ""

        lines = ["**Team completeness note (%d of %d specialist(s) did not "
                 "complete their assigned work):**" % (len(failed), len(contributions))]
        for role, reason in failed:
            lines.append("- **%s** - %s" % (role, reason))
        lines.append(
            "The deliverable below may rely on general knowledge rather than "
            "the specific research or computation that was actually requested.")
        return chr(10).join(lines) + chr(10)*2 + "---" + chr(10)*2

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

        # Checked against the SAME contributions list synthesize() saw --
        # not against whatever prose it chose to write. Prepended so it is
        # the first thing read, not something a founder has to scroll past
        # a confident report to discover.
        _warning = self._team_completeness_warning(contributions)
        merged = (_warning + merged) if _warning else merged

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
