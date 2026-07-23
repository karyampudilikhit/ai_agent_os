"""EvidenceExtractor — turns prose "unknown" markers and citations into
structured data a UI can render as visible trust signals.

Why this exists: the playbook system already trains specialists to
write "unknown — no primary source found" and to cite URLs for real
numbers. But that discipline lives buried in prose. A blind benchmark
this project ran (two Units' worth of GTM research, judged by an
independent AI) proved the failure mode directly — the judge *said*
"these numbers would need verification" and then scored the fabricated
answer higher anyway, because the verification work was left as an
exercise for the reader instead of being visible where the claim was
made.

This module closes that gap: one extra LLM pass, after synthesis,
reads the final deliverable and produces a structured claims ledger.
Three statuses, not two — the third is the important one:

  - "verified"        — a specific claim with a real source URL attached
  - "flagged_unknown"  — the specialist explicitly said unknown/unsourced
                         (the HONEST case — this is what the playbook
                         is designed to produce)
  - "unsourced_claim"  — a specific, precise-looking factual claim with
                         NO source and NOT marked unknown (the DANGEROUS
                         case — this is exactly the shape of the fake
                         OpenView/ProfitWell/Apollo citations found in
                         testing: confident, specific, uncited)

Same JSON-contract pattern as CritiqueEngine — one adapter call,
temperature low, JSON-parsed with the same fenced-block/brace-scan
fallback. Deliberately NOT a rewrite pass: extraction only, never
modifies the deliverable text itself.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 2500
MAX_CLAIMS = 20  # bound so a long report doesn't blow the response budget


EXTRACT_PROMPT = """You are auditing a deliverable for factual claims that a reader would
want to verify before acting on it.

DELIVERABLE:
{deliverable}

Find every SPECIFIC factual claim: a number, a price, a statistic, a
named source, a dated fact. For each one, classify it as exactly one
of:

- "verified" — the claim has a real source URL attached in the text
  right next to it.
- "flagged_unknown" — the text itself says the claim is unknown,
  unsourced, "no primary source found", or similar self-disclosed
  uncertainty.
- "unsourced_claim" — a specific, precise-looking number or fact with
  NO source URL attached and NOT marked unknown. This is the
  IMPORTANT category to catch: confident claims dressed as fact with
  nothing backing them.

Do not flag general statements, opinions, or qualitative claims — only
SPECIFIC, checkable facts (numbers, prices, named studies, dates).
Max {max_claims} claims — prioritize the most consequential ones if
there are more.

Return JSON only:
{{"claims": [
  {{"text": "<the claim, quoted or closely paraphrased, max 100 chars>",
    "status": "verified" | "flagged_unknown" | "unsourced_claim",
    "source": "<URL if present, else empty string>"}},
  ...
]}}

JSON only."""


class EvidenceExtractor:
    """Reads a finished deliverable and returns a structured claims
    ledger. Never modifies the deliverable — extraction only."""

    def __init__(self, model_adapter: Any, max_tokens: int = DEFAULT_MAX_TOKENS):
        self.adapter = model_adapter
        self.max_tokens = max_tokens

    def extract(self, deliverable: str) -> List[Dict[str, str]]:
        """Returns a list of {text, status, source} entries. Empty list
        (not None) on any failure — evidence is an enhancement, never a
        reason to fail the whole run."""
        if not deliverable or not deliverable.strip():
            return []
        try:
            response = self.adapter.chat_completion(
                EXTRACT_PROMPT.format(
                    deliverable=deliverable[:8000],  # bound input for very long reports
                    max_claims=MAX_CLAIMS,
                ),
                temperature=0.1,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Evidence extraction call failed: %s", exc)
            return []

        data = self._extract_json(response)
        claims = (data or {}).get("claims")
        if not isinstance(claims, list):
            return []

        cleaned: List[Dict[str, str]] = []
        valid_statuses = {"verified", "flagged_unknown", "unsourced_claim"}
        for c in claims[:MAX_CLAIMS]:
            if not isinstance(c, dict):
                continue
            text = str(c.get("text", "")).strip()
            status = str(c.get("status", "")).strip()
            source = str(c.get("source", "")).strip()
            if not text or status not in valid_statuses:
                continue
            # A "verified" claim without an actual URL is a contradiction —
            # downgrade it rather than trust the model's own label blindly.
            if status == "verified" and not source.startswith(("http://", "https://")):
                status = "unsourced_claim"
                source = ""
            cleaned.append({"text": text, "status": status, "source": source})
        return cleaned

    def summarize(self, claims: List[Dict[str, str]]) -> Dict[str, int]:
        """Cheap counts for a UI badge/header — e.g. '3 verified · 2 unknown
        · 1 unsourced'. Computed from already-extracted claims, no extra call."""
        out = {"verified": 0, "flagged_unknown": 0, "unsourced_claim": 0}
        for c in claims:
            status = c.get("status")
            if status in out:
                out[status] += 1
        return out

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
