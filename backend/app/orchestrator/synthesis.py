"""Synthesis stage — turns N raw sub-agent outputs into ONE coherent
final deliverable, instead of showing the user a pile of disjointed
JSON fragments stitched together.

Validated as necessary, not optional: a blind benchmark comparing raw
stitched multi-agent output against a single direct LLM call found the
single call won 7/10 (5/8 excluding two unrelated bugged runs) even
though multi-agent spent ~2.7x the tokens doing the underlying work —
the work itself was often comparable or better, but fragments read
worse to a human than one coherent voice. This stage is the fix: same
agent work, one final pass to combine it into a single answer.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 3000


class SynthesisEngine:
    """Combines completed agent results into one final document."""

    def __init__(self, model_adapter: Any, max_tokens: int = DEFAULT_MAX_TOKENS):
        self.adapter = model_adapter
        self.max_tokens = max_tokens

    def synthesize(self, objective: str, agent_results: List[Any]) -> Optional[str]:
        """Returns the synthesized text, or None if there's nothing to
        synthesize or the call fails — callers should fall back to raw
        stitched output in that case, never crash the pipeline over it.
        """
        completed = [
            r for r in agent_results
            if self._status(r) == "completed" and self._output(r)
        ]
        if not completed:
            return None

        fragments = self._render_fragments(completed)
        prompt = self._build_prompt(objective, fragments)

        try:
            response = self.adapter.chat_completion(
                prompt, temperature=0.4, max_tokens=self.max_tokens, format=None
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Synthesis call failed: %s", exc)
            return None

        response = (response or "").strip()
        if not response or response.lower().startswith(("connection error", "error:")):
            return None
        return response

    # ------------------------------------------------------------------

    def _render_fragments(self, results: List[Any]) -> str:
        parts = []
        for i, r in enumerate(results, 1):
            parts.append(f"--- Piece {i} ---\n{self._render_value(self._output(r))}")
        return "\n\n".join(parts)

    def _render_value(self, value: Any, indent: int = 0) -> str:
        pad = "  " * indent
        if isinstance(value, str):
            return f"{pad}{value}"
        if isinstance(value, list):
            return "\n".join(self._render_value(v, indent) for v in value)
        if isinstance(value, dict):
            lines = []
            for k, v in value.items():
                rendered = self._render_value(v, indent + 1)
                if "\n" in rendered:
                    lines.append(f"{pad}{k}:\n{rendered}")
                else:
                    lines.append(f"{pad}{k}: {rendered.strip()}")
            return "\n".join(lines)
        return f"{pad}{value}"

    def _build_prompt(self, objective: str, fragments: str) -> str:
        return f"""You were asked to produce this: "{objective}"

Several specialists each worked on one piece of this. Their raw notes
are below. Combine them into ONE final, coherent deliverable — written
the way a single expert would present finished work, not a list of
separate reports.

Specialist notes:
{fragments}

Write the final combined deliverable now:
- Merge overlapping content, don't repeat the same point twice
- Resolve contradictions between pieces (pick the more sensible one)
- Organize with clear headings, in plain written prose/markdown
- Cover everything genuinely useful from the notes above
- Do not mention "specialists," "pieces," or that this was assembled — write it as one unified answer
- Do not output JSON

Final deliverable:"""

    def _status(self, r: Any) -> Optional[str]:
        return r.get("status") if isinstance(r, dict) else getattr(r, "status", None)

    def _output(self, r: Any) -> Any:
        return r.get("output") if isinstance(r, dict) else getattr(r, "output", None)
