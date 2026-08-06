"""Repo-wide pytest fixtures.

Currently just the test-fixtures HTTP server test_browser_task.py needs.
That file's own docstring documents it as a manual two-terminal step
("py -3 -m http.server 8899 --directory test_fixtures" in one terminal,
the test in another) — which is exactly the shape of thing that reads as
a mystery failure in a fresh session instead of a missing setup step.
Confirmed directly: every one of test_browser_task.py's 6 tests failed
with an unrelated-looking "Playwright Sync API inside the asyncio loop"
error when this server wasn't running (the FIRST test's connection-
refused failure left Playwright's background loop in a bad state for
every test after it in the same process) — the real cause was just a
missing server, not an environment/library bug. This fixture makes the
suite self-sufficient so `pytest test_browser_task.py` alone just works.
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import threading
from pathlib import Path

import pytest

_FIXTURES_DIR = Path(__file__).parent / "test_fixtures"
_FIXTURES_PORT = 8899


@pytest.fixture(scope="session", autouse=True)
def _test_fixtures_http_server():
    """Serves test_fixtures/*.html on 127.0.0.1:8899 for the whole test
    session. If something's already bound to that port (a manually
    started server, or a leftover from a crashed previous run), this
    just skips starting a second one rather than failing the whole
    suite — whatever's already there is presumably serving the same
    directory."""
    if not _FIXTURES_DIR.is_dir():
        yield
        return

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(_FIXTURES_DIR)
    )
    try:
        httpd = socketserver.TCPServer(("127.0.0.1", _FIXTURES_PORT), handler)
    except OSError:
        # Port already bound — assume it's already serving test_fixtures/
        # (e.g. a founder running the documented manual two-terminal setup)
        # and don't fight over it.
        yield
        return

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        httpd.shutdown()
        httpd.server_close()
