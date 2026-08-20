"""An OpenAI-compatible adapter — OpenRouter, Together, vLLM, and the rest.

WHY THIS EXISTS. Until now every model in Vision AI had to be an Ollama
model, because `_loop_adapter` and `adapter_pool` both constructed
`OllamaAdapter` by name. That made the product's single largest risk
unfixable from the outside: only two models are alive on the free Ollama
tier, and handing over an API key for anything else did nothing, because
there was no code path that could use it. This file was zero bytes.

The contract is deliberately identical to OllamaAdapter's — one method,
`chat_completion(prompt, temperature=, max_tokens=)`, returning text or
RAISING. Callers already treat a raised error as an infrastructure fact
worth recording (see execution_loop's `backend.unavailable` ledger entry),
and a provider that hands its errors back as ordinary strings is how a
502 body once ended up inside a founder's deliverable.

TWO THINGS A NAIVE PORT WOULD GET WRONG:

1. JSON MODE IS BEST-EFFORT, NOT ASSUMED. Ollama's `format: "json"`
   constrains decoding, and the agentic loop depends on parseable JSON
   coming back. OpenAI-compatible servers spell this
   `response_format={"type": "json_object"}` and plenty of models reject
   it outright. So it is attempted, and on a 4xx that mentions
   response_format the call is retried once without it — a model that
   writes good JSON unprompted beats a hard failure.

2. AN EMPTY RESPONSE RAISES. A reasoning model can spend its entire
   token budget thinking and return `content: ""` with
   `finish_reason: "length"`. The Ollama adapter learned this the hard
   way and retries with a bigger budget; the same trap exists here and
   the same answer applies. Passing "" downstream is how a run gets
   reported as "the model chose not to use tools" when the truth is it
   never got to speak.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
REQUEST_TIMEOUT = float(os.environ.get("OPENAI_ADAPTER_TIMEOUT", "180"))

# BUSY IS NOT BROKEN.
#
# A free-tier model shares an upstream pool, so 429 is an ordinary
# condition rather than a failure -- the first live call against
# z-ai/glm-5.2:free returned "temporarily rate-limited upstream ...
# limit_source: upstream_provider_shared_pool". An agentic run makes
# roughly twenty calls, so without backoff a single busy moment ends the
# whole task, and the run then gets reported as the model declining to
# work. This codebase has already had to fix that exact
# infrastructure-presenting-as-behaviour bug twice.
#
# 5xx gets the same treatment for the same reason.
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
RETRY_ATTEMPTS = int(os.environ.get("OPENAI_ADAPTER_RETRIES", "4"))
RETRY_BASE_DELAY = float(os.environ.get("OPENAI_ADAPTER_RETRY_DELAY", "2.0"))

# How much bigger a retry gets when a reasoning model spends its whole
# budget thinking. Same escalation shape as the Ollama adapter.
EMPTY_RETRY_MULTIPLIER = 3
EMPTY_RETRY_CEILING = 8000

# WHAT A REASONING MODEL ACTUALLY NEEDS, LEARNED ONCE PER PROCESS.
#
# A thinking model spends its budget reasoning and can return EMPTY
# content with finish_reason="length" -- the retry above exists for
# exactly that, and it works. What it did not do was remember.
#
# Measured on a live screener run: deepseek-v4-flash came back empty at
# the step budget SIX times, retried to 8000 and succeeded every time,
# and then began the next step at the same too-small number. Eight steps
# took 865 seconds against a 600-second deadline, and the run died with
# the data already on screen. The retries were not the failure; paying
# for them once per step was.
#
# So the first floor that works for a model is remembered and used as
# the starting point. Per process, not persisted: it is a fact about a
# model's current behaviour, not about the world.
_token_floor: Dict[str, int] = {}
_token_floor_lock = threading.Lock()


def _remembered_floor(model: str) -> int:
    with _token_floor_lock:
        return _token_floor.get(model, 0)


def _remember_floor(model: str, tokens: int) -> None:
    with _token_floor_lock:
        if tokens > _token_floor.get(model, 0):
            _token_floor[model] = tokens


class OpenAIAdapterError(Exception):
    def __init__(self, message: str, context: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.context = context or {}


# A QUOTA IS NOT A BUSY MOMENT.
#
# 429 covers two unrelated things, and treating them alike wasted a live
# run. OpenRouter's free tier allows 50 model requests PER DAY; when that
# is spent every call returns 429 with `X-RateLimit-Remaining: 0` and a
# reset timestamp at the next UTC midnight. The backoff ladder then slept
# 3s, 5s, 8s, 17s per attempt, four attempts per step, four steps -- ~90
# seconds of waiting per step for a limit that would not clear for ten
# hours -- and the founder watched a progress log that looked like
# recovery in progress.
#
# So a limit with a reset FURTHER OUT than the ladder could ever cover is
# raised immediately, naming the reset time. Waiting is right for a
# shared pool that is momentarily busy; it is never right for a quota.
_LADDER_CEILING_SECONDS = 120.0


def _quota_exhausted_until(response: httpx.Response) -> Optional[float]:
    """Seconds until the limit clears, when this 429 is a spent quota.

    None when it is an ordinary busy-upstream 429 that backing off can
    actually fix.
    """
    if response.status_code != 429:
        return None

    remaining = response.headers.get("x-ratelimit-remaining")
    reset_raw = response.headers.get("x-ratelimit-reset")
    body = ""
    try:
        body = response.text or ""
    except Exception:  # noqa: BLE001
        pass

    # OpenRouter puts the same headers inside the JSON body as well, so
    # both shapes are read rather than trusting one.
    if not reset_raw or remaining is None:
        try:
            meta = (response.json().get("error") or {}).get("metadata") or {}
            headers = meta.get("headers") or {}
            remaining = headers.get("X-RateLimit-Remaining", remaining)
            reset_raw = headers.get("X-RateLimit-Reset", reset_raw)
        except Exception:  # noqa: BLE001
            pass

    if remaining is None or str(remaining).strip() not in ("0", "0.0"):
        # Some providers omit the header entirely; fall back to wording
        # that only ever appears on a spent allowance.
        if "per-day" not in body and "daily" not in body.lower():
            return None

    if not reset_raw:
        return _LADDER_CEILING_SECONDS + 1.0 if "per-day" in body else None

    try:
        reset = float(reset_raw)
    except (TypeError, ValueError):
        return None
    # Milliseconds since the epoch when it is far too large to be seconds.
    if reset > 1e11:
        reset /= 1000.0
    seconds = reset - time.time()
    # A reset in the past, or one the ladder could genuinely wait out, is
    # an ordinary throttle.
    return seconds if seconds > _LADDER_CEILING_SECONDS else None


def _describe_wait(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} hour(s)"
    return f"{seconds / 60:.0f} minute(s)"



class OpenAICompatAdapter:
    """Any server that speaks POST /chat/completions with a Bearer token."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = "z-ai/glm-5.2:free",
        api_key: Optional[str] = None,
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY") or ""
        self.client = httpx.Client(timeout=timeout)
        # No health probe on purpose: this is built per employee per run,
        # and a probe would add a round trip to every specialist. A dead
        # endpoint fails loudly on the first real call, which the loop
        # already records as an outage rather than a refusal to work.

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        # OpenRouter asks for these and rate-limits harder without them.
        h["HTTP-Referer"] = os.environ.get("OPENROUTER_SITE", "https://vision-ai.local")
        h["X-Title"] = os.environ.get("OPENROUTER_TITLE", "Vision AI")
        return h

    def _post_with_backoff(self, payload: Dict[str, Any]) -> httpx.Response:
        """POST, waiting out a busy upstream rather than failing the run.

        Honours Retry-After when the server sends one; otherwise
        exponential with jitter, because every employee in a company
        retrying in lockstep is how a shared pool stays saturated.
        """
        last: Optional[httpx.Response] = None
        for attempt in range(RETRY_ATTEMPTS + 1):
            try:
                response = self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
            except httpx.RequestError as exc:
                if attempt >= RETRY_ATTEMPTS:
                    raise OpenAIAdapterError(
                        f"could not reach {self.base_url}: {exc}",
                        {"model": self.model, "base_url": self.base_url},
                    ) from exc
                time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue

            if response.status_code not in RETRY_STATUSES or attempt >= RETRY_ATTEMPTS:
                return response

            exhausted = _quota_exhausted_until(response)
            if exhausted is not None:
                raise OpenAIAdapterError(
                    f"{self.model}: the provider's request quota is spent — it "
                    f"does not clear for another {_describe_wait(exhausted)}. "
                    f"This is a billing limit, not a busy server, so retrying "
                    f"cannot help.",
                    {"model": self.model, "base_url": self.base_url,
                     "quota_exhausted": True,
                     "clears_in_seconds": round(exhausted)},
                )

            last = response
            try:
                delay = float(response.headers.get("retry-after") or 0)
            except ValueError:
                delay = 0.0
            if delay <= 0:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
            delay += random.uniform(0, 1.0)
            logger.warning(
                "%s: HTTP %d from %s — waiting %.1fs (attempt %d/%d)",
                self.model, response.status_code, self.base_url, delay,
                attempt + 1, RETRY_ATTEMPTS,
            )
            time.sleep(delay)

        return last  # type: ignore[return-value]

    def chat_completion(self, prompt: str, **kwargs: Any) -> str:
        temperature = kwargs.get("temperature", 0.7)
        max_tokens = int(kwargs.get("max_tokens", 2000))
        want_json = kwargs.get("format", "json") == "json"

        attempt_tokens = max(max_tokens, _remembered_floor(self.model))
        for attempt in range(3):
            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": attempt_tokens,
            }
            if want_json:
                payload["response_format"] = {"type": "json_object"}

            response = self._post_with_backoff(payload)

            # Some models reject response_format outright. Losing
            # constrained decoding is a real downgrade; losing the call
            # entirely is worse.
            if (response.status_code in (400, 404, 422)
                    and want_json
                    and "response_format" in response.text.lower()):
                logger.info("%s rejects response_format — retrying without it",
                            self.model)
                want_json = False
                continue

            if response.status_code != 200:
                raise OpenAIAdapterError(
                    f"{self.base_url} returned HTTP {response.status_code}: "
                    f"{response.text[:400]}",
                    {"status_code": response.status_code, "model": self.model},
                )

            try:
                body = response.json()
            except Exception as exc:  # noqa: BLE001
                raise OpenAIAdapterError(
                    f"non-JSON body from {self.base_url}: {response.text[:200]}",
                    {"model": self.model},
                ) from exc

            # An error can arrive inside a 200 body on OpenRouter.
            if isinstance(body, dict) and body.get("error"):
                err = body["error"]
                raise OpenAIAdapterError(
                    f"{self.model} error: "
                    f"{err.get('message') if isinstance(err, dict) else err}",
                    {"model": self.model},
                )

            choices = (body or {}).get("choices") or []
            message = (choices[0].get("message") or {}) if choices else {}
            text = (message.get("content") or "").strip()
            finish = (choices[0].get("finish_reason") if choices else "") or ""

            if text:
                return text

            # Empty content. If the budget ran out, the model was thinking
            # and never got to answer -- retry bigger rather than hand ""
            # downstream, which is how an outage gets reported as a
            # decision not to use tools.
            reasoning = str(message.get("reasoning") or "").strip()
            if attempt < 2 and (finish == "length" or reasoning):
                _remember_floor(self.model,
                                min(attempt_tokens * EMPTY_RETRY_MULTIPLIER,
                                    EMPTY_RETRY_CEILING))
                attempt_tokens = min(attempt_tokens * EMPTY_RETRY_MULTIPLIER,
                                     EMPTY_RETRY_CEILING)
                logger.warning(
                    "%s returned empty content (finish=%s, reasoning=%d chars) "
                    "— retrying with max_tokens=%d",
                    self.model, finish, len(reasoning), attempt_tokens,
                )
                continue

            raise OpenAIAdapterError(
                f"{self.model} returned an empty response "
                f"(finish_reason={finish!r}). If this is a reasoning model it "
                f"spent its whole budget thinking; raise max_tokens.",
                {"model": self.model, "finish_reason": finish,
                 "max_tokens": attempt_tokens},
            )

        raise OpenAIAdapterError(
            f"{self.model} returned nothing usable after 3 attempts",
            {"model": self.model},
        )

    def get_model_info(self) -> Dict[str, Any]:
        return {"provider": "openai-compatible", "model": self.model,
                "base_url": self.base_url}

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


# The name the rest of the codebase imports.
OpenAIAdapter = OpenAICompatAdapter
