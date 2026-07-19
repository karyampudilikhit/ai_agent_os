"""Adaptive supervisor — decides how much process an objective earns
before Pipeline spends anything on it.

This is the routing half of Phase 6 ("Adaptive supervision & critique
loop") — the critique/refine half was already built. Without this,
every objective pays the full multi-agent cost (~9-10x tokens per the
benchmark work) whether or not decomposition actually helps, which is
exactly the open question the independent judge never resolved.

Three tiers, deliberately separating two things the benchmark work
conflated: DECOMPOSITION (spawning parallel agents) and VERIFICATION
(the critique/refine loop). The one proven win so far — fabrication
rate dropping from 3/10 to 1/10 checks — came from verification, not
decomposition. So a task can earn verification without earning
decomposition:

  single_call            — one coherent narrative, nothing to verify
  single_call_critique   — one coherent narrative, but has claims/stakes
                            worth fact-checking before it ships
  multi_agent_critique   — genuinely decomposes into independent lenses
                            a real team would split across specialists
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

TIERS = ("single_call", "single_call_critique", "multi_agent_critique")
DEFAULT_MAX_TOKENS = 300

CLASSIFY_PROMPT = """You are triaging a founder's request to decide whether it needs a
single-shot answer or a spun-up team of AI employees.

The product is a system that spawns AI employees on demand — that's the
whole pitch. So the default bias is: **if a real founder would hire
even TWO different people to help them with this, it's a team task.**
Prefer teams unless the request is genuinely a quick one-off.

TASK: "{objective}"

Pick exactly one tier:

- "multi_agent_critique" — DEFAULT for anything that is:
    * a real project, plan, strategy, launch, or product-shaped task
    * covers more than one domain (e.g. product + marketing, research +
      writing, technical + business, design + pricing)
    * ends in a deliverable a real founder would pay a team to produce
    * asks for multi-step work spanning days/weeks of real effort
  If in doubt between this and single_call_critique, PICK THIS.
  Building the team IS part of the value delivered — showing a founder
  their AI staff working on the task is not overhead, it's the product.

- "single_call_critique" — a short, single-domain answer that still has
  factual claims, numbers, or high-stakes wording ("validated",
  "tested", "researched", specific statistics) that need a verification
  pass before shipping. Examples: a fundraising slide with metrics, a
  press release paragraph, one email to a customer citing specific data.
  ONE person could write this in one sitting, but fabrication would
  matter.

- "single_call" — genuinely trivial or fully generic: a thank-you note,
  explaining a concept, a definition, formatting a paragraph, a
  no-stakes chat reply. Something a single message could resolve in a
  few sentences with nothing to verify.

Return JSON only:
{{"tier": "single_call" | "single_call_critique" | "multi_agent_critique", "reason": "one sentence"}}

JSON only."""


class AdaptiveSupervisor:
    """Classifies an objective into one of TIERS before Pipeline acts on it."""

    def __init__(self, model_adapter: Any, max_tokens: int = DEFAULT_MAX_TOKENS):
        self.adapter = model_adapter
        self.max_tokens = max_tokens

    def classify(self, objective: str) -> str:
        """Returns one of TIERS. Falls back to a cheap heuristic (no LLM
        call) if the classification call fails or returns something
        unparseable — routing must never be the reason a task doesn't
        run at all."""
        try:
            response = self.adapter.chat_completion(
                CLASSIFY_PROMPT.format(objective=objective),
                temperature=0.1,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Classification call failed, using heuristic: %s", exc)
            return self._heuristic(objective)

        data = self._extract_json(response)
        tier = (data or {}).get("tier")
        if tier not in TIERS:
            logger.warning("Classification unparseable/invalid (%r), using heuristic", tier)
            return self._heuristic(objective)

        reason = (data or {}).get("reason", "")
        logger.info("Adaptive supervisor: tier=%s reason=%s", tier, reason)
        return tier

    def _heuristic(self, objective: str) -> str:
        """No-LLM-call fallback. Same team-first bias as the LLM
        classifier — cheap heuristics agree with the primary rule
        rather than fighting it. Only shortcuts to a cheaper tier on
        clear trivial-ask signals; everything else earns a team.
        """
        text = objective.lower().strip()
        words = text.split()

        trivial_markers = (
            "thank you", "thank-you", "explain",
            "what is", "what's", "define", "definition of",
            "give me a joke", "write a poem",
            "reply to", "rephrase", "translate", "summarize this",
        )
        if any(m in text for m in trivial_markers) and len(words) < 40:
            # Still bump to critique if there are numbers/claims to verify.
            verify_markers = ("valid", "statistic", "%", "$", "cite", "quote")
            if any(m in text for m in verify_markers):
                return "single_call_critique"
            return "single_call"

        project_markers = (
            "launch", "build", "design", "strategy", "plan", "help me get",
            "go-to-market", "gtm", "positioning", "marketing", "product",
            "feature set", "roadmap", "raise", "fundraise", "hire",
            "onboarding", "pricing", "monetize", "grow", "acquire", "audit",
            "startup", "business", "customer", "team",
        )
        if any(m in text for m in project_markers) or len(words) >= 40:
            return "multi_agent_critique"

        # Genuinely can't tell — default to critique-with-single-call, not
        # bare single_call: verification is cheap and the product's whole
        # trust story depends on it.
        return "single_call_critique"

    def _extract_json(self, text: str) -> Optional[dict]:
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
