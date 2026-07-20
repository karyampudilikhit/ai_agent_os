"""Smoke test: user-defined HTTP tools (Option B).

Round-trips a spec through the store, executes it against a real
public endpoint (postman-echo.com — no auth needed), and confirms the
MCP planner would see it in its unified tool listing.

Run:
    py -3 test_http_tools.py
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from backend.app.tools.http_tool_runner import HTTPToolRunner
from backend.app.tools.http_tool_store import HTTPToolStore, HTTPToolStoreError


def _fresh_store() -> HTTPToolStore:
    """Isolated store so this test can't collide with a user's real
    .http_tools.json at repo root."""
    tmp = Path(tempfile.mkdtemp()) / ".http_tools.json"
    return HTTPToolStore(path=tmp)


def test_store_validate() -> None:
    store = _fresh_store()

    # Rejects missing name
    try:
        store.add({"method": "GET", "url": "https://example.com"})
    except HTTPToolStoreError:
        pass
    else:
        raise AssertionError("store should reject spec with no name")

    # Rejects bad URL
    try:
        store.add({"name": "x", "method": "GET", "url": "not-a-url"})
    except HTTPToolStoreError:
        pass
    else:
        raise AssertionError("store should reject non-http URL")

    # Rejects invalid auth
    try:
        store.add({
            "name": "x", "method": "GET",
            "url": "https://example.com",
            "auth": {"type": "bearer"},  # no token
        })
    except HTTPToolStoreError:
        pass
    else:
        raise AssertionError("bearer auth without token should be rejected")

    print("[ok] store validation")


def test_store_crud() -> None:
    store = _fresh_store()
    spec = {
        "name": "echo_get",
        "description": "Test GET against httpbin",
        "method": "GET",
        "url": "https://postman-echo.com/get",
        "parameters": [
            {"name": "q", "in": "query", "description": "query", "required": True},
        ],
    }
    added = store.add(spec)
    assert added["name"] == "echo_get"
    assert store.get("echo_get") is not None
    assert len(store.list()) == 1

    # Update
    updated = store.update("echo_get", {"description": "updated"})
    assert updated["description"] == "updated"

    # Delete
    assert store.delete("echo_get") is True
    assert store.get("echo_get") is None
    print("[ok] store CRUD")


def test_runner_get_with_query() -> None:
    store = _fresh_store()
    store.add({
        "name": "echo_get",
        "description": "GET with a query arg",
        "method": "GET",
        "url": "https://postman-echo.com/get",
        "parameters": [
            {"name": "q", "in": "query", "description": "search term", "required": True},
        ],
    })
    runner = HTTPToolRunner(store=store)

    tools = runner.list_tools()
    assert len(tools) == 1
    t = tools[0]
    assert t["qualified_name"] == "custom.echo_get"
    assert t["connection"] == "custom"
    assert "q" in t["input_schema"]["properties"]
    assert "q" in t["input_schema"]["required"]

    result = runner.call("custom.echo_get", {"q": "vision-ai-test"})
    assert "HTTP 200" in result, result[:200]
    assert "vision-ai-test" in result, "query arg should appear in httpbin echo"
    print("[ok] runner GET with query param")


def test_runner_bearer_auth() -> None:
    """httpbin echoes back what headers we sent, so we can confirm
    the bearer token got applied."""
    store = _fresh_store()
    store.add({
        "name": "echo_headers",
        "description": "Echo the headers we sent",
        "method": "GET",
        "url": "https://postman-echo.com/headers",
        "auth": {"type": "bearer", "token": "test-token-xyz"},
    })
    runner = HTTPToolRunner(store=store)
    result = runner.call("custom.echo_headers", {})
    assert "HTTP 200" in result, result[:200]
    assert "Bearer test-token-xyz" in result, "bearer token should be in Authorization header"
    print("[ok] runner bearer auth")


def test_runner_missing_required() -> None:
    store = _fresh_store()
    store.add({
        "name": "echo_get",
        "method": "GET",
        "url": "https://postman-echo.com/get",
        "parameters": [
            {"name": "q", "in": "query", "required": True},
        ],
    })
    runner = HTTPToolRunner(store=store)
    result = runner.call("custom.echo_get", {})  # forgot required arg
    assert "missing required" in result.lower(), result
    print("[ok] runner rejects missing required args")


def test_runner_post_with_body() -> None:
    store = _fresh_store()
    store.add({
        "name": "echo_post",
        "method": "POST",
        "url": "https://postman-echo.com/post",
        "parameters": [
            {"name": "message", "in": "body", "required": True},
        ],
    })
    runner = HTTPToolRunner(store=store)
    result = runner.call("custom.echo_post", {"message": "hello-vision-ai"})
    assert "HTTP 200" in result, result[:200]
    assert "hello-vision-ai" in result, "POST body should echo back"
    print("[ok] runner POST with body")


if __name__ == "__main__":
    test_store_validate()
    test_store_crud()
    test_runner_get_with_query()
    test_runner_bearer_auth()
    test_runner_missing_required()
    test_runner_post_with_body()
    print("\nAll HTTP-tool checks passed.")
