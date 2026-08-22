"""One adapter per model name, built once and shared.

WHY THIS EXISTS. Choosing a model used to be a process-wide decision --
`AGENT_LOOP_MODEL` read once, one adapter built, done. Per-employee
configuration changes the arithmetic: a company with a Quant Analyst, a
Web Operator and a Report Producer on three different models builds an
adapter on every run of every employee.

That would be merely wasteful if construction were free. It is not:
`main._build_adapter` sends a live probe request before returning, which
is right for start-up (fail loudly when the backend is unreachable) and
wrong per-employee (a round-trip added to every specialist, and a
transient blip turning into a run that never started).

So adapters are memoised by name. Adapters here hold configuration and a
connection pool, not conversation state, so sharing one across employees
and threads is safe -- each call carries its own prompt.

NO HEALTH PROBE ON PURPOSE. If a configured model is unreachable, the
first real call fails and the loop's existing retry-then-record path
handles it -- and records it as `backend.unavailable`, which is the
distinction this codebase keeps having to defend: an infrastructure
failure must not present as the model deciding not to work.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_adapters: Dict[str, Any] = {}

# Names that could not be built. Kept so a typo'd model in one
# employee's config logs once rather than on every step of every run --
# and so the fallback is silent thereafter rather than noisy.
_failed: Dict[str, str] = {}


# DeepSeek's first-party endpoint names its models WITHOUT a vendor
# prefix -- `deepseek-v4-flash`, not `deepseek/deepseek-v4-flash` --
# because on their own API there is no other vendor to distinguish from.
# The prefixed spelling is OpenRouter's catalogue name for the same
# model. Both are in use here, and they need different endpoints.
#
# So a first-party spelling implies the first-party endpoint. This is the
# same route-by-name rule the rest of this module uses, extended to a
# provider whose names happen to be unambiguous -- nothing else is called
# `deepseek-v4-*`. An explicit OPENAI_BASE_URL still wins, so pointing at
# a proxy or a self-hosted gateway is one variable.
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _is_first_party_deepseek(name: str) -> bool:
    return (name or "").strip().lower().startswith("deepseek-")


def _base_url_for(name: str) -> str:
    explicit = os.environ.get("OPENAI_BASE_URL", "").strip()
    if explicit:
        return explicit
    if _is_first_party_deepseek(name):
        return DEEPSEEK_BASE_URL
    return OPENROUTER_BASE_URL


def _build(name: str) -> Any:
    """Pick the provider from the model NAME, and never guess wrong.

    The rule is the naming convention the providers themselves use:
    an OpenAI-compatible catalogue namespaces its models with a vendor
    prefix ("z-ai/glm-5.2:free", "meta-llama/llama-3.1-70b"), and Ollama
    never does. So a slash means "route this over HTTP to the configured
    OpenAI-compatible endpoint", and its absence means the local daemon.

    An explicit override wins over the convention, because a
    self-hosted vLLM can serve a model whose name has no slash at all.

    An EXPLICIT BASE URL also means OpenAI-compatible, whatever the name
    looks like. DeepSeek's own endpoint calls its models `deepseek-chat`
    and `deepseek-reasoner` -- no vendor prefix, because on their API
    there is no other vendor to distinguish from. Under the slash rule
    alone those names route to the local Ollama daemon, which does not
    have them, and the failure reads as "model not found" rather than
    "you pointed this at the wrong provider". Setting OPENAI_BASE_URL is
    not something anyone does by accident.
    """
    explicit = os.environ.get("MODEL_PROVIDER", "").strip().lower()
    base_url_set = bool(os.environ.get("OPENAI_BASE_URL", "").strip())
    openai_compatible = (
        explicit == "openai"
        or (not explicit and (base_url_set or "/" in name
                              or _is_first_party_deepseek(name)))
    )

    if openai_compatible:
        from backend.app.models.provider_adapters.openai_adapter import (
            OpenAICompatAdapter,
        )
        return OpenAICompatAdapter(
            base_url=_base_url_for(name),
            model=name,
            api_key=(os.environ.get("DEEPSEEK_API_KEY")
                     if _is_first_party_deepseek(name) else None)
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or None,
        )

    from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter
    return OllamaAdapter(
        base_url=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        model=name,
        api_key=os.environ.get("OLLAMA_API_KEY") or None,
    )


def get_adapter(name: str, default: Any = None) -> Any:
    """The shared adapter for `name`, or `default` if it cannot be built.

    Never raises. A bad model name is a configuration mistake, and the
    right response is to run on the pipeline's adapter and say so --
    not to fail a founder's run because a settings field has a typo.
    """
    key = (name or "").strip()
    if not key:
        return default

    with _lock:
        existing = _adapters.get(key)
        if existing is not None:
            return existing
        if key in _failed:
            return default

        try:
            adapter = _build(key)
        except Exception as exc:  # noqa: BLE001
            _failed[key] = str(exc)
            logger.warning(
                "loop model %r could not be built (%s) — falling back to the "
                "pipeline adapter", key, exc,
            )
            return default

        _adapters[key] = adapter
        logger.info("loop model %r ready (pooled)", key)
        return adapter


def reset() -> None:
    """Drop every pooled adapter. Tests only."""
    with _lock:
        _adapters.clear()
        _failed.clear()


def pooled_names() -> tuple:
    with _lock:
        return tuple(sorted(_adapters))
