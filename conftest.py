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


@pytest.fixture(autouse=True)
def _isolate_on_disk_stores(tmp_path, monkeypatch):
    """Point the JSON stores at a temp directory for every test.

    Without this they read the FOUNDER'S REAL FILES. test_http_tools
    asserted "one tool in the store" and failed with three, because the
    developer running it had three tools saved — a test that passes or
    fails depending on whose machine it is on, and which reads as a bug
    in the code under test.

    Browser playbooks get the same treatment for a sharper reason: a test
    could otherwise WRITE a recipe into the real store, and a later real
    run would be handed a path invented by a fixture.
    """
    from backend.app.tools import http_tool_store
    from backend.app.browser import playbook

    monkeypatch.setattr(http_tool_store, "_store_path",
                        lambda: tmp_path / "http_tools.json")
    monkeypatch.setattr(playbook, "_store_path",
                        lambda: tmp_path / "browser_playbooks.json")
    # Both modules cache a singleton built from the path above.
    monkeypatch.setattr(playbook, "_store", None, raising=False)
    for mod, attr in ((http_tool_store, "_store"), (http_tool_store, "_STORE")):
        if hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, None, raising=False)
    yield


@pytest.fixture(autouse=True)
def _close_browser_sessions_between_tests():
    """Close any browser session a test left open.

    The interactive browser tests share ONE on-disk Chrome profile, and a
    persistent profile can only be held by one context at a time. So a
    single test that fails partway through — leaving its session open —
    used to cascade: every later test died with "profile is already in
    use by another instance of Chromium", which reads like a bug in the
    code under test rather than leftover state from the previous test.
    """
    yield
    try:
        from backend.app.browser.session_manager import get_manager
        mgr = get_manager()
        for token in list(mgr._live._resources.keys()):
            mgr.close(token)
    except Exception:  # noqa: BLE001
        pass


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
