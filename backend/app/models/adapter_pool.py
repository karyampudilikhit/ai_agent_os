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
            from backend.app.models.provider_adapters.ollama_adapter import OllamaAdapter
            adapter = OllamaAdapter(
                base_url=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                model=key,
                api_key=os.environ.get("OLLAMA_API_KEY") or None,
            )
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
