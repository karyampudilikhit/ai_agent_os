"""Runs a single agent through an LLM adapter and returns a structured result.

This is the atom the orchestrator composes. It knows how to:
  - render an agent's role/objective/expected_output into a prompt,
  - call the adapter with the agent's own trait-derived parameters,
  - parse the JSON response against expected_output,
  - score confidence heuristically,
  - retry on transient failures,
  - never crash the caller — always return an AgentResult.

Phase 4 keeps token/cost accounting rough: we count characters as a
proxy for tokens (roughly ~4 chars per token for English) since the
Ollama endpoint we use here does not return usage counts. Phase 5's
usage_logger will replace this with real token counts.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


CHARS_PER_TOKEN = 4  # rough approximation for English text
COST_PER_1K_TOKENS_OLLAMA = 0.0  # local model; kept for interface consistency


@dataclass
class AgentResult:
    """Outcome of executing a single agent."""

    agent_id: str
    agent_name: str
    status: str  # "completed" | "failed"
    output: Dict[str, Any] = field(default_factory=dict)
    raw_output: str = ""
    confidence: float = 0.0
    tokens_used: int = 0
    cost: float = 0.0
    duration_seconds: float = 0.0
    attempts: int = 0
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "status": self.status,
            "output": self.output,
            "raw_output_preview": self.raw_output[:300],
            "confidence": round(self.confidence, 3),
            "tokens_used": self.tokens_used,
            "cost": round(self.cost, 6),
            "duration_seconds": round(self.duration_seconds, 3),
            "attempts": self.attempts,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class AgentExecutor:
    """Executes a single agent against an LLM adapter."""

    def __init__(
        self,
        model_adapter: Any,
        config: Optional[Dict[str, Any]] = None,
        max_retries: int = 2,
        timeout_seconds: int = 120,
    ):
        self.adapter = model_adapter
        self.config = config or {}
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds

    def execute(self, agent: Any) -> AgentResult:
        """Run one agent, return an AgentResult. Never raises."""
        agent_id = self._get(agent, "id", "<unknown>")
        agent_name = self._get(agent, "name", "<unnamed>")

        result = AgentResult(
            agent_id=agent_id,
            agent_name=agent_name,
            status="failed",
            started_at=datetime.utcnow(),
        )

        prompt = self._build_prompt(agent)
        temperature, max_tokens = self._llm_params(agent)

        last_error: Optional[str] = None
        raw_text = ""

        for attempt in range(1, self.max_retries + 2):  # 1 try + N retries
            result.attempts = attempt
            try:
                start = time.time()
                raw_text = self.adapter.chat_completion(
                    prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                elapsed = time.time() - start

                if self._looks_like_connection_error(raw_text):
                    last_error = raw_text.strip()
                    logger.warning(
                        "Agent %s attempt %d saw connection error: %s",
                        agent_id, attempt, last_error[:120],
                    )
                    time.sleep(min(2 ** (attempt - 1), 4))
                    continue

                parsed, parse_error = self._parse_output(raw_text)
                if parse_error and attempt <= self.max_retries:
                    last_error = parse_error
                    logger.info(
                        "Agent %s attempt %d parse failure, retrying: %s",
                        agent_id, attempt, parse_error,
                    )
                    time.sleep(0.5)
                    continue

                # Success path (or final attempt: keep whatever we got).
                result.output = parsed
                result.raw_output = raw_text
                result.duration_seconds = elapsed
                result.tokens_used = self._estimate_tokens(prompt, raw_text)
                result.cost = self._estimate_cost(result.tokens_used)
                result.confidence = self._score_confidence(
                    parsed, self._get(agent, "expected_output", {}) or {}
                )
                result.status = "completed" if parsed else "failed"
                if parse_error:
                    result.error = f"parse_incomplete: {parse_error}"
                result.finished_at = datetime.utcnow()
                return result

            except Exception as exc:  # noqa: BLE001 — we deliberately swallow
                last_error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "Agent %s attempt %d raised", agent_id, attempt
                )
                time.sleep(min(2 ** (attempt - 1), 4))

        # All attempts exhausted.
        result.raw_output = raw_text
        result.error = last_error or "unknown error"
        result.finished_at = datetime.utcnow()
        if result.started_at:
            result.duration_seconds = (
                result.finished_at - result.started_at
            ).total_seconds()
        return result

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(self, agent: Any) -> str:
        role = self._get(agent, "role", "Specialist")
        name = self._get(agent, "name", "Agent")
        objective = self._get(agent, "objective", "")
        expected_output = self._get(agent, "expected_output", {}) or {}
        traits = self._get(agent, "traits", {}) or {}

        traits_summary = ", ".join(
            f"{k}={v}" for k, v in traits.items()
        ) or "(defaults)"

        expected_json = json.dumps(expected_output, indent=2)

        return f"""You are {name}, a {role} inside an autonomous agent workforce.

Your cognitive traits for this task: {traits_summary}.

Your objective:
{objective}

You MUST return a single JSON object matching this expected shape.
The keys must be present; replace each value with your actual answer,
keeping values grounded and concrete. If a field does not apply, return
an empty string or empty list — never null.

Expected output shape:
{expected_json}

Respond with JSON only. No markdown, no prose, no code fences.
"""

    def _llm_params(self, agent: Any) -> tuple[float, int]:
        traits = self._get(agent, "traits", {}) or {}
        # exploration_bias 0..1 maps to temperature 0.2..0.9
        exploration = float(traits.get("exploration_bias", 0.5))
        exploration = max(0.0, min(1.0, exploration))
        temperature = 0.2 + 0.7 * exploration

        complexity = self._get(agent, "complexity_level", "medium")
        # Bumped from (800/1500/2500) — phi3 & other small models truncate
        # mid-JSON on verbose expected_output shapes at the old ceilings.
        max_tokens = {
            "low": 1500,
            "medium": 3000,
            "high": 5000,
        }.get(complexity, 3000)

        return temperature, max_tokens

    # ------------------------------------------------------------------
    # Output parsing and scoring
    # ------------------------------------------------------------------

    def _parse_output(self, raw: str) -> tuple[Dict[str, Any], Optional[str]]:
        """Return (parsed_dict, error_message_or_None)."""
        if not raw or not isinstance(raw, str):
            return {}, "empty response from model"

        stripped = raw.strip()

        # Strip common code-fence wrappers.
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
            stripped = re.sub(r"\s*```$", "", stripped)

        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return data, None
            return {"_value": data}, "response was not a JSON object"
        except json.JSONDecodeError:
            pass

        # Fallback: extract the largest {...} block.
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(stripped[start : end + 1])
                if isinstance(data, dict):
                    return data, None
            except json.JSONDecodeError as exc:
                return {}, f"json decode failed after extraction: {exc}"

        return {}, "no JSON object found in response"

    def _score_confidence(
        self, output: Dict[str, Any], expected: Dict[str, Any]
    ) -> float:
        """Cheap heuristic in [0, 1].

        Phase 6's critique_agent will replace this with an LLM-judged score.
        """
        if not output:
            return 0.0

        score = 0.4  # got something structural

        if expected:
            expected_keys = set(expected.keys())
            output_keys = set(output.keys())
            overlap = len(expected_keys & output_keys)
            if overlap > 0:
                score += 0.3 * (overlap / len(expected_keys))

        non_empty_values = sum(
            1
            for v in output.values()
            if v not in (None, "", [], {})
        )
        if output:
            score += 0.3 * (non_empty_values / len(output))

        return min(1.0, score)

    # ------------------------------------------------------------------
    # Adapter helpers
    # ------------------------------------------------------------------

    def _looks_like_connection_error(self, text: str) -> bool:
        if not text:
            return True
        head = text.strip().lower()[:60]
        return head.startswith(("connection error", "error:", "http error"))

    def _estimate_tokens(self, prompt: str, response: str) -> int:
        return max(1, (len(prompt) + len(response)) // CHARS_PER_TOKEN)

    def _estimate_cost(self, tokens: int) -> float:
        return (tokens / 1000.0) * COST_PER_1K_TOKENS_OLLAMA

    def _get(self, agent: Any, field_name: str, default: Any = None) -> Any:
        if isinstance(agent, dict):
            return agent.get(field_name, default)
        return getattr(agent, field_name, default)


def execute_agent(
    agent: Any,
    model_adapter: Any,
    config: Optional[Dict[str, Any]] = None,
) -> AgentResult:
    """Convenience wrapper: run one agent with a default executor."""
    return AgentExecutor(model_adapter, config).execute(agent)
