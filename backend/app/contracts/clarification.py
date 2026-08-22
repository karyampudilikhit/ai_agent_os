"""Clarification stage — asks the user what they actually want before
the execution contract gets drafted.

Design validated in a throwaway simulation before this module was
written: on the same ambiguous objective ("website... and a mobile
app"), asking one grounding question before contract generation cut
token spend ~54% and produced 6 on-target agents instead of 15 agents
covering both platforms nobody asked to combine.

Interface is deliberately two pure functions, no ``input()`` inside:

    engine.assess(objective, adapter) -> List[ClarificationQuestion]
    engine.build_brief(objective, qa_pairs) -> RequirementsBrief
    engine.enrich_objective(brief) -> str

The CLI drives collection via stdin today; a future API can drive the
same two calls across two HTTP requests without touching this module.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MAX_QUESTIONS = 5
DEFAULT_MIN_OBJECTIVE_WORDS = 25

# Platform terms that, if 2+ distinct ones appear, signal a contradiction
# worth asking about even for an otherwise-long objective.
_PLATFORM_TERMS = {
    "website": "website",
    "web app": "website",
    "webapp": "website",
    "mobile app": "mobile app",
    "mobile application": "mobile app",
    "android": "mobile app",
    "ios": "mobile app",
}


@dataclass
class ClarificationQuestion:
    id: str
    question: str
    category: str  # platform | audience | scope | output_format | constraints | general


@dataclass
class RequirementsBrief:
    original_objective: str
    qa_pairs: List[Tuple[ClarificationQuestion, str]] = field(default_factory=list)
    skipped: bool = False
    skip_reason: Optional[str] = None


class ClarificationEngine:
    """Decides whether an objective needs clarifying questions, asks
    them via one LLM call, and folds answers back into a groundable
    objective string for the existing contract generator to consume.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = config or {}
        self.max_questions = config.get("max_questions", DEFAULT_MAX_QUESTIONS)
        self.min_objective_words = config.get(
            "min_objective_words", DEFAULT_MIN_OBJECTIVE_WORDS
        )

    # ------------------------------------------------------------------
    # Stage 1: assess
    # ------------------------------------------------------------------

    def assess(self, objective: str, model_adapter: Any) -> List[ClarificationQuestion]:
        """Return questions to ask, or an empty list if none are needed.

        Zero-token heuristic gate runs first; the LLM is only called
        when the objective looks genuinely under-specified.
        """
        needs_check, reason = self._heuristic_needs_clarification(objective)
        if not needs_check:
            logger.debug("Clarification skipped (heuristic): %s", reason)
            return []

        logger.debug("Clarification triggered (heuristic): %s", reason)
        return self._generate_questions(objective, model_adapter)

    def _heuristic_needs_clarification(self, objective: str) -> Tuple[bool, str]:
        text = objective.lower()
        word_count = len(objective.split())

        mentioned_platforms = {v for k, v in _PLATFORM_TERMS.items() if k in text}
        if len(mentioned_platforms) > 1:
            return True, f"contradictory platforms mentioned: {sorted(mentioned_platforms)}"

        if word_count < self.min_objective_words:
            return True, f"objective is short ({word_count} words) — likely under-specified"

        return False, "objective appears specific enough"

    def _generate_questions(
        self, objective: str, model_adapter: Any
    ) -> List[ClarificationQuestion]:
        prompt = f"""You are a product intake assistant reviewing a build request
before any work starts.

Objective as given by the user:
"{objective}"

This objective may be vague, contradictory, or missing key details.
Identify at most {self.max_questions} genuinely necessary clarifying
questions — skip anything that's already clear. Categories to consider:
platform, audience, scope (must-have features), output_format (code vs
spec vs both), constraints (tech stack, branding, timeline).

Return JSON only:
{{
  "questions": [
    {{"id": "q1", "category": "platform", "question": "..."}},
    {{"id": "q2", "category": "scope", "question": "..."}}
  ]
}}

JSON only. No markdown, no prose."""

        try:
            response = model_adapter.chat_completion(
                prompt, temperature=0.3, max_tokens=500
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Clarification question generation failed: %s", exc)
            return []

        data = self._extract_json(response) or {}
        raw_questions = data.get("questions", [])[: self.max_questions]

        questions = []
        for i, q in enumerate(raw_questions):
            if isinstance(q, dict) and isinstance(q.get("question"), str) and q["question"].strip():
                questions.append(
                    ClarificationQuestion(
                        id=q.get("id", f"q{i+1}"),
                        question=q["question"].strip(),
                        category=q.get("category", "general"),
                    )
                )
        return questions

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

    # ------------------------------------------------------------------
    # Stage 2: fold answers into a brief, then a groundable objective
    # ------------------------------------------------------------------

    def build_brief(
        self,
        objective: str,
        qa_pairs: List[Tuple[ClarificationQuestion, str]],
    ) -> RequirementsBrief:
        return RequirementsBrief(original_objective=objective, qa_pairs=qa_pairs)

    def enrich_objective(self, brief: RequirementsBrief) -> str:
        """Fold clarified answers into a single string the existing
        contract generator can consume unchanged.

        Plain prose, not key: value pairs — a key:value shape measurably
        primed a small local model to echo that structure back verbatim
        in an unrelated downstream call during validation. Also: if
        contract generation ever falls back to its single-deliverable
        default, that default wraps the ENTIRE objective string as one
        deliverable, so keeping this short and unstructured matters twice.
        """
        if brief.skipped or not brief.qa_pairs:
            return brief.original_objective

        answer_sentences = " ".join(
            a.rstrip(".") + "." for _, a in brief.qa_pairs if a and a.strip()
        )
        if not answer_sentences:
            return brief.original_objective
        return f"{brief.original_objective} Clarified by the user: {answer_sentences}"
