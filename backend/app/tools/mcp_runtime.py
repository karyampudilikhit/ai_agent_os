"""Persistent asyncio event loop for MCP calls.

MCP's Python SDK is async-first (contextmanager sessions, streaming
tool calls). The rest of Vision AI is sync — FastAPI's threadpool
handlers, sync employee execution, sync pipeline. Naively wrapping MCP
with asyncio.run() per call would spawn a fresh subprocess (for stdio
servers) or a fresh HTTP session every time, adding 500-2000ms of
overhead to every tool invocation.

This module gives us one dedicated event loop running in a background
thread for the life of the process. Every MCP call happens ON that
loop; sync callers submit coroutines and block on the result.

Not fancy, not particularly clever — just the right shape to run
"async lives inside sync" without rewriting the pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from typing import Any, Coroutine, Optional

logger = logging.getLogger(__name__)


class MCPLoop:
    """Singleton — one background asyncio loop per Python process."""

    _instance: Optional["MCPLoop"] = None
    _init_lock = threading.Lock()

    @classmethod
    def get(cls) -> "MCPLoop":
        # Fast path: no lock if already initialized
        if cls._instance is not None:
            return cls._instance
        with cls._init_lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self.thread = threading.Thread(
            target=self._run_loop, name="mcp-loop", daemon=True
        )
        self.thread.start()
        # Wait until the loop is actually running before returning — this
        # guarantees run_coroutine_threadsafe won't race with a not-yet-
        # started loop and raise "loop is not running".
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("MCP background loop failed to start within 5s")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        try:
            self.loop.run_forever()
        finally:
            try:
                self.loop.close()
            except Exception:  # noqa: BLE001
                pass

    def run(self, coro: Coroutine[Any, Any, Any], timeout: float = 60.0) -> Any:
        """Submit a coroutine to the background loop and block until it
        finishes (or the timeout fires). Raises whatever the coroutine
        would have raised, plus TimeoutError."""
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Future:
        """Fire-and-forget: submit a coroutine and return a Future. The
        caller decides whether to await it. Useful for background tasks
        we don't need to block on (e.g. keepalives)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


def get_loop() -> MCPLoop:
    return MCPLoop.get()
