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
                    ─▶ SynthesisEngine (orchestrator/) — combines results
                        ─▶ ExecutionState  (state_manager)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from backend.app.agents.agent_factory import AgentFactory
from backend.app.contracts.execution_contract import ExecutionContract, ExecutionContractGenerator
from backend.app.orchestrator.adaptive_supervisor import TIERS, AdaptiveSupervisor
from backend.app.orchestrator.execution_engine import ExecutionEngine
from backend.app.orchestrator.state_manager import StateManager
from backend.app.orchestrator.synthesis import SynthesisEngine
from backend.app.critique.critique_agent import CritiqueEngine

logger = logging.getLogger(__name__)


def _detect_handback(text: str):
    """Wrapper so a failure in the detector can never break a run — a
    broken guard must degrade to 'no hand-back found', never to a crash
    in the middle of synthesis."""
    try:
        from backend.app.critique.handback_detector import detect_handback
        return detect_handback(text)
    except Exception:  # noqa: BLE001
        return []


def _detect_contradictions(text: str):
    """Same defensive wrapper as _detect_handback — a guard that crashes
    is worse than a guard that misses."""
    try:
        from backend.app.critique.claim_checker import detect_contradicted_claims
        return detect_contradicted_claims(text)
    except Exception:  # noqa: BLE001
        return []


def _quality(critique: Dict[str, Any]) -> tuple:
    """Rank a draft so a refinement can be rejected when it makes things
    worse. Ordered by what actually harms the founder, worst first:

      1. a hand-back  — the work was not done at all
      2. fabrications — confident invented content
      3. completeness — genuine thinness, the least bad of the three

    Returned as a tuple where LARGER is better, so callers can simply
    compare. Completeness is deliberately last: a thin honest answer
    beats a fuller invented one, which is the exact trade a live run got
    backwards when it raised completeness 0.30 -> 0.40 by adding three
    fabricated claims.
    """
    blocking = bool(critique.get("handback") or critique.get("contradicted_claims"))
    return (
        0 if blocking else 1,
        -len(critique.get("fabricated_claims") or []),
        float(critique.get("completeness_score") or 0.0),
    )


SINGLE_CALL_PROMPT = """{objective}

Give a complete, thorough, well-organized answer in plain written prose
with markdown headings. Cover everything the request asks for. Do not
output JSON."""


class Pipeline:
    """End-to-end runner: objective → agents → executed results."""

    def __init__(
        self,
        model_adapter: Any,
        config: Optional[Dict[str, Any]] = None,
        contract_generator: Optional[ExecutionContractGenerator] = None,
        agent_factory: Optional[AgentFactory] = None,
        execution_engine: Optional[ExecutionEngine] = None,
        synthesis_engine: Optional[SynthesisEngine] = None,
        critique_engine: Optional[CritiqueEngine] = None,
        adaptive_supervisor: Optional[AdaptiveSupervisor] = None,
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
        self.synthesis = synthesis_engine or SynthesisEngine(
            model_adapter=model_adapter,
        )
        self.critique_engine = critique_engine or CritiqueEngine(
            model_adapter=model_adapter,
        )
        self.adaptive_supervisor = adaptive_supervisor or AdaptiveSupervisor(
            model_adapter=model_adapter,
        )
        self.min_completeness_threshold = self.config.get("limits", {}).get(
            "min_completeness_threshold", 0.7
        )
        self.single_call_max_tokens = self.config.get("limits", {}).get(
            "single_call_max_tokens", 5000
        )

    # ------------------------------------------------------------------

    def run_objective(
        self,
        objective: str,
        max_refinements: Optional[int] = None,
        tier: Optional[str] = None,
        min_tier: Optional[str] = None,
        max_tier: Optional[str] = None,
    ) -> StateManager:
        """Take a natural-language objective, run the whole pipeline.

        Routes through the adaptive supervisor first (unless `tier` is
        passed explicitly, e.g. for testing/comparison) — see
        adaptive_supervisor.py for why this exists: every objective used
        to pay the full multi-agent cost regardless of whether
        decomposition actually helped it.

        `min_tier`: a floor the classifier's choice can't go below. The
        router reads raw text and doesn't know a task's real stakes — a
        specific caller (e.g. an Employee whose whole value proposition
        is "verified, no fabricated evidence") can and should assert its
        own minimum verification level rather than silently trusting a
        generic classification. Found the hard way: a real
        IdeaValidationEmployee run got classified `single_call` and
        skipped critique entirely, on the exact task type this system
        exists to verify.
        """
        if not objective or not objective.strip():
            raise ValueError("objective must be non-empty")

        logger.info("Pipeline start: %s", objective[:80])

        chosen_tier = tier or self.adaptive_supervisor.classify(objective)
        if min_tier and TIERS.index(chosen_tier) < TIERS.index(min_tier):
            logger.info("Routing tier %s below floor %s, bumping up", chosen_tier, min_tier)
            chosen_tier = min_tier
        if max_tier and TIERS.index(chosen_tier) > TIERS.index(max_tier):
            # Ceiling — a caller that IS already a specialist (a
            # DynamicEmployee doing its slice of a team task) shouldn't
            # nest ANOTHER team decomposition inside itself. That's how a
            # 3-employee run turns into 3 × 5 = 15 minutes of sub-team
            # spawning; cap it here.
            logger.info("Routing tier %s above ceiling %s, capping down", chosen_tier, max_tier)
            chosen_tier = max_tier
        logger.info("Routing tier: %s", chosen_tier)

        if chosen_tier == "single_call":
            return self._run_single_call(objective, do_critique=False)
        if chosen_tier == "single_call_critique":
            return self._run_single_call(objective, do_critique=True)

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

        manager = self.engine.run(contract, agents)
        self._synthesize(objective, manager)
        self._critique_and_refine(objective, manager)
        return manager

    def _run_single_call(self, objective: str, do_critique: bool) -> StateManager:
        """Tier 1/2 path: no contract refinement, no agent spawning — one
        direct call, optionally followed by the same critique/refine loop
        the multi-agent path uses. Still returns a StateManager so every
        caller (Employee, CLI, benchmarks) sees the same snapshot shape
        regardless of which tier ran.
        """
        contract = ExecutionContract(objective=objective, deliverables=[objective])
        manager = StateManager(contract)
        manager.state.started_at = datetime.utcnow()

        prompt = SINGLE_CALL_PROMPT.format(objective=objective)
        try:
            output = self.adapter.chat_completion(
                prompt, temperature=0.6, max_tokens=self.single_call_max_tokens, format=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Single-call stage failed: %s", exc)
            output = None

        # mark_result() (the usual place total_tokens gets incremented) is
        # never called on this path — no agents run. Same char/4 estimate
        # the benchmark scripts use elsewhere, so tier comparisons aren't
        # comparing a real number against a misleading zero. Note this was
        # already an undercount pre-router too: synthesis/critique tokens
        # were never tracked into total_tokens on the multi-agent path
        # either, only per-agent execution tokens.
        if output:
            manager.state.total_tokens += (len(prompt) + len(output)) // 4

        manager.set_synthesized_output(output)
        # total=0 (no agents were ever spawned, by design for this tier) —
        # StateManager.mark_finished() would read that as "failed"; that
        # status logic assumes the multi-agent path, so set it directly.
        manager.state.status = "completed" if output else "failed"
        manager.state.finished_at = datetime.utcnow()

        if do_critique:
            self._critique_and_refine(objective, manager)

        return manager

    def run_contract(self, contract: Any) -> StateManager:
        """Run an already-built contract (skip the generation step)."""
        agents = self.agent_factory.create_agents_from_contract(
            contract, model_adapter=self.adapter
        )
        manager = self.engine.run(contract, agents)
        objective = getattr(contract, "objective", "")
        self._synthesize(objective, manager)
        self._critique_and_refine(objective, manager)
        return manager

    def _synthesize(self, objective: str, manager: StateManager) -> None:
        """Combine completed agent results into one final document.
        Never raises — synthesis failure falls back to no synthesized
        output (callers/CLI fall back to the raw per-agent results)."""
        try:
            results = list(manager.state.agent_results.values())
            synthesized = self.synthesis.synthesize(objective, results)
            manager.set_synthesized_output(synthesized)
            if synthesized:
                logger.info("Synthesis produced %d chars", len(synthesized))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Synthesis stage failed, continuing without it: %s", exc)

    def _critique_and_refine(self, objective: str, manager: StateManager) -> None:
        """Review the synthesized draft; refine (up to max_refinement_depth
        times) if it's incomplete, over-engineered for the stated scale, or
        contains fabricated completed-test claims. Never raises — any
        failure here just leaves the last good synthesized output in place.

        Re-critiques after every refine pass, not just once before the
        first one. The previous version stored the PRE-refine critique and
        never updated it — so completeness_score/fabricated_claims kept
        describing a draft that no longer shipped. That let a refined
        output still containing fabricated claims get reported (and
        benchmarked) as a clean 0.9+ score, because nothing ever re-checked
        whether the rewrite actually fixed what it was told to fix.
        """
        draft = manager.state.synthesized_output
        if not draft:
            return

        max_depth = self.config.get("limits", {}).get("max_refinement_depth", 2)
        attempts = 0
        try:
            critique = self.critique_engine.critique(objective, draft)
            if not critique:
                return
            # Mechanical hand-back check, run alongside the LLM critique.
            # The critique agent scored a "we cannot do this until you
            # upload a spreadsheet" draft as acceptable; this doesn't ask
            # a model's opinion. See critique/handback_detector.py.
            critique["handback"] = _detect_handback(draft)
            if critique["handback"]:
                logger.warning(
                    "Hand-back detected in draft (%d passage(s)) — forcing refinement",
                    len(critique["handback"]),
                )
            critique["contradicted_claims"] = _detect_contradictions(draft)
            if critique["contradicted_claims"]:
                logger.warning(
                    "Draft states %d cause(s) the run's own tool log contradicts "
                    "— forcing refinement",
                    len(critique["contradicted_claims"]),
                )
            manager.set_critique(critique)

            while attempts < max_depth and self.critique_engine.needs_refinement(
                critique, self.min_completeness_threshold
            ):
                attempts += 1
                logger.info(
                    "Refining (attempt %d/%d): completeness=%.2f over_engineered=%s fabricated_claims=%d",
                    attempts, max_depth,
                    critique.get("completeness_score", 0.0),
                    critique.get("over_engineered", False),
                    len(critique.get("fabricated_claims") or []),
                )
                refined = self.critique_engine.refine(objective, draft, critique)
                if not refined:
                    break
                logger.info("Refinement produced %d chars", len(refined))

                # Verify against the text that would actually ship, not
                # the draft that prompted this pass.
                new_critique = self.critique_engine.critique(objective, refined)
                if not new_critique:
                    break
                # Carry the mechanical hand-back check ONTO the new
                # critique. This used to be written to the OLD critique
                # dict one line before that dict was replaced wholesale,
                # so every post-refinement hand-back check was computed
                # and immediately discarded — the loop condition below
                # never saw it.
                new_critique["handback"] = _detect_handback(refined)
                new_critique["contradicted_claims"] = _detect_contradictions(refined)

                # Only accept a rewrite that is not worse. Observed in a
                # live run: a Data Engineer draft went 0.30 -> 0.40 by
                # INTRODUCING 3 fabricated claims, then back to 0.30,
                # and shipped at 0.20 — three passes to end up worse
                # than it started. Refinement pressure buys fabrication
                # when the specialist has nothing real to add, so the
                # loop needs the option to keep the earlier draft.
                if _quality(new_critique) < _quality(critique):
                    logger.warning(
                        "Refinement REJECTED (completeness %.2f->%.2f, "
                        "fabricated %d->%d, handback %d->%d) — keeping the "
                        "previous draft",
                        critique.get("completeness_score", 0.0),
                        new_critique.get("completeness_score", 0.0),
                        len(critique.get("fabricated_claims") or []),
                        len(new_critique.get("fabricated_claims") or []),
                        len(critique.get("handback") or []),
                        len(new_critique.get("handback") or []),
                    )
                    break

                draft = refined
                manager.set_synthesized_output(draft)
                manager.set_was_refined(True)
                critique = new_critique
                manager.set_critique(critique)

            if attempts == 0:
                logger.info("Critique passed all checks, no refinement needed")
            else:
                logger.info(
                    "Post-refinement (after %d attempt(s)): completeness=%.2f fabricated_claims=%d",
                    attempts,
                    critique.get("completeness_score", 0.0),
                    len(critique.get("fabricated_claims") or []),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Critique/refine stage failed, continuing without it: %s", exc)


def build_default_pipeline(
    model_adapter: Any,
    config: Optional[Dict[str, Any]] = None,
) -> Pipeline:
    """Convenience factory: default pipeline for the given adapter."""
    return Pipeline(model_adapter=model_adapter, config=config)
