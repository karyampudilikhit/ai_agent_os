"""Critique + refine — a second pass that reviews the synthesized draft
against the original objective and fixes what's missing before calling
the work done.

Rubric is targeted, not generic. A first version scored generic
"completeness" and made results WORSE in a controlled benchmark (8-1
instead of 7-3) — it could rate a fully-fabricated, wildly-over-scoped
draft as "complete" because completeness doesn't measure either of the
two things later diagnosed as the actual failure modes:

  1. Scope mismatch — agents propose custom-engineered systems
     (Kubernetes, multi-cloud, hand-trained ML) for businesses that
     explicitly can't support that, when the right answer is "buy
     Greenhouse/Zapier/Stripe," not "build a microservice architecture."
  2. Fabrication — agents claim tests/pilots were already run with
     specific results, when nothing in this pipeline ever executes a
     test. A generic critique never asked about either.

This version checks both explicitly, in addition to completeness.

Scope note: still a lean R&D version, not the full Phase 6 design.
One review-and-fix cycle, not adaptive multi-cycle spawning.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 2000


class CritiqueEngine:
    """Reviews a draft, scores it, and refines it once if it's weak."""

    def __init__(self, model_adapter: Any, max_tokens: int = DEFAULT_MAX_TOKENS):
        self.adapter = model_adapter
        self.max_tokens = max_tokens

    def critique(self, objective: str, draft: str) -> Optional[Dict[str, Any]]:
        """Returns a critique dict with completeness plus the two targeted
        checks (scope mismatch, fabrication), or None if the call fails."""
        prompt = f"""You are reviewing a draft deliverable against what was requested.

Original request: "{objective}"

Draft:
{draft}

Evaluate this draft honestly, as a harsh but fair reviewer would. Check
three things:

1. COMPLETENESS — does it cover everything the request asked for?

2. SCOPE MATCH — does the proposed solution's complexity, infrastructure,
   and cost match the company size and budget implied by the request?
   A small business (roughly under 50 people, no stated engineering
   team) should get recommendations built on off-the-shelf SaaS tools
   (e.g. Zapier, Greenhouse, Stripe, QuickBooks, DocuSign) that they
   could realistically configure themselves — NOT a custom-built
   microservice architecture, Kubernetes cluster, multi-cloud setup,
   or a hand-trained ML model, which assumes an engineering team the
   business doesn't have. Flag this if the draft over-assumes.

3. FABRICATION — does the draft claim any test, pilot run, or validation
   was ALREADY COMPLETED with specific results (e.g. "95% success rate
   across 1,000 orders," "piloted at 3 locations," a past sign-off
   date)? Nothing in this system has actually executed a test — any
   such claim is fabricated.

Return JSON only. Keep every list item to ONE short sentence — a brief
paraphrase, not a verbatim quote — so the response stays compact:
{{
  "completeness_score": 0.0-1.0,
  "gaps": ["short paraphrase of each missing item, max 5"],
  "issues": ["short paraphrase of each problem, max 5"],
  "strengths": ["short paraphrase, max 3"],
  "over_engineered": true or false,
  "scope_mismatch_reason": "one short sentence if over_engineered is true, else empty string",
  "fabricated_claims": ["short paraphrase of each fabricated claim, max 5, empty list if none"]
}}

JSON only."""

        try:
            response = self.adapter.chat_completion(
                prompt, temperature=0.3, max_tokens=2000
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Critique call failed: %s", exc)
            return None

        data = self._extract_json(response)
        if not data or "completeness_score" not in data:
            logger.warning("Critique response unparseable, skipping refinement")
            return None
        return data

    def needs_refinement(
        self, critique: Dict[str, Any], completeness_threshold: float
    ) -> bool:
        """Refine if completeness is low OR either targeted check fired —
        a fabricated or over-scoped draft can still score "complete."""
        score = critique.get("completeness_score", 1.0)
        if score < completeness_threshold:
            return True
        if critique.get("over_engineered"):
            return True
        if critique.get("fabricated_claims"):
            return True
        # Mechanically-detected hand-back (see handback_detector). Not an
        # LLM judgement — a draft that tells the founder to go upload
        # something is never shippable regardless of how "complete" the
        # critique scored it.
        if critique.get("handback"):
            return True
        # A cause the run's own tool log contradicts (see
        # critique/claim_checker.py). Mechanical like the hand-back
        # check, not an opinion: the deliverable blamed a value this run
        # never sent, and no completeness score makes that shippable.
        if critique.get("contradicted_claims"):
            return True
        return False

    def refine(self, objective: str, draft: str, critique: Dict[str, Any]) -> Optional[str]:
        """One rewrite pass that fixes the specific problems found."""
        gaps = critique.get("gaps") or []
        issues = critique.get("issues") or []
        fabricated = critique.get("fabricated_claims") or []
        handback = critique.get("handback") or []
        contradicted = critique.get("contradicted_claims") or []
        problems = []
        if contradicted:
            # Ahead of everything else, including the hand-back: a draft
            # that blames the wrong thing actively sends the founder to
            # debug someone else's system. Being incomplete wastes their
            # time; being confidently wrong about the cause wastes it in
            # a specific, misleading direction.
            from backend.app.critique.claim_checker import claim_correction
            problems.append(claim_correction(contradicted))
        if handback:
            # Next: everything below is a quality nit compared with
            # "this draft doesn't do the work at all".
            from backend.app.critique.handback_detector import handback_correction
            problems.append(handback_correction(handback))
        if gaps:
            problems.append("Missing: " + "; ".join(str(g) for g in gaps))
        if issues:
            problems.append("Problems: " + "; ".join(str(i) for i in issues))
        if critique.get("over_engineered"):
            reason = critique.get("scope_mismatch_reason", "")
            problems.append(
                "OVER-ENGINEERED: this proposes more infrastructure/complexity than "
                f"the stated company can realistically build or maintain ({reason}). "
                "Replace custom-built systems with off-the-shelf SaaS tools "
                "(e.g. Zapier, Greenhouse, Stripe, QuickBooks, DocuSign) wherever "
                "a small business without an engineering team would actually use them."
            )
        if fabricated:
            problems.append(
                "FABRICATED CLAIMS — these exact claims describe tests/pilots that "
                "never happened and must be removed or rewritten as a testing PLAN, "
                "not a completed result: " + "; ".join(str(f) for f in fabricated)
            )
        problem_text = " ".join(problems) if problems else "General quality and completeness."

        prompt = f"""You wrote this draft for: "{objective}"

Draft:
{draft}

A reviewer found these specific problems: {problem_text}

Rewrite the draft to fix these specific problems. Keep everything that
was already good — this is a targeted fix, not a rewrite from scratch.
Write the complete improved version, in plain prose/markdown.
Do not output JSON. Do not mention the review or that this was revised.

Improved version:"""

        try:
            response = self.adapter.chat_completion(
                prompt, temperature=0.4, max_tokens=self.max_tokens, format=None
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Refinement call failed: %s", exc)
            return None

        response = (response or "").strip()
        if not response or response.lower().startswith(("connection error", "error:")):
            return None
        return response

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None
