"""download_asset — the bounds, not the happy path.

An employee could produce an HTML page and had no way to put a single
image next to it, because write_file writes UTF-8 and nothing else. This
action closes that, and it is deliberately NOT approval-gated — pulling a
public URL into a folder the founder already owns buys no safety by
making them tap Approve twelve times.

Everything that makes that defensible is a check, and checks are what
this file tests: sandbox containment, scheme allowlist, SSRF refusal, and
a size cap enforced while streaming rather than after.
"""
from __future__ import annotations

import os

from backend.app.actions.builtin import download_asset as da
from backend.app.actions.builtin._workspace import workspace_root


def _call(**kw):
    return da.SPEC.handler(kw)


# --------------------------------------------------------- it is not gated

def test_it_does_not_require_approval():
    """Every other write action asks first because it changes something
    outside this machine. This one does not, on purpose — and if someone
    flips it later, the twelve-taps-to-fetch-twelve-images problem is
    back."""
    assert da.SPEC.mutating is False


def test_it_is_registered_and_reachable():
    from backend.app.actions.action_registry import get_registry
    names = [t["qualified_name"] for t in get_registry().list_tools()]
    assert "action.download_asset" in names


# ------------------------------------------------------------- SSRF refusal

def test_loopback_is_refused():
    out = _call(url="http://127.0.0.1:8000/secret", path="x.png")
    assert "not a public address" in out


def test_localhost_by_name_is_refused():
    out = _call(url="http://localhost:11434/api/tags", path="x.png")
    assert "not a public address" in out


def test_cloud_metadata_endpoint_is_refused():
    """169.254.169.254 is the canonical target — an agent talked into
    fetching it hands over cloud credentials."""
    out = _call(url="http://169.254.169.254/latest/meta-data/", path="x.json")
    assert "not a public address" in out


def test_private_range_is_refused():
    out = _call(url="http://192.168.1.1/admin", path="x.png")
    assert "not a public address" in out


def test_public_host_resolving_to_loopback_is_refused():
    """The check resolves the name rather than pattern-matching it:
    plenty of ordinary public hostnames point at 127.0.0.1, so a string
    test catches nothing."""
    assert da._is_public_host("localhost") is False
    assert da._is_public_host("127.0.0.1") is False


# ------------------------------------------------------- scheme + sandbox

def test_non_http_schemes_are_refused():
    for url in ("file:///etc/passwd", "data:image/png;base64,AAAA", "ftp://x.com/a.png"):
        out = _call(url=url, path="x.png")
        assert "only http and https" in out, url


def test_path_escape_is_refused():
    out = _call(url="https://example.com/a.png", path="../../escaped.png")
    assert "escapes workspace" in out


def test_missing_arguments_are_reported_not_crashed():
    assert "missing 'url'" in _call(path="a.png")
    assert "missing 'path'" in _call(url="https://example.com/a.png")


# ------------------------------------------------------------- extensions

def test_extension_comes_from_the_url_when_it_has_one():
    assert da._guess_ext("https://x.com/hero.JPG", "") == ".jpg"
    assert da._guess_ext("https://x.com/f/logo.svg?v=2", "") == ".svg"


def test_extension_falls_back_to_content_type():
    """CDN URLs routinely end in a hash with no suffix at all."""
    assert da._guess_ext("https://cdn.x.com/a1b2c3d4", "image/webp") == ".webp"
    assert da._guess_ext("https://cdn.x.com/f/9f8e", "font/woff2") == ".woff2"


def test_unknown_type_still_produces_something_writable():
    assert da._guess_ext("https://x.com/blob", "") == ".bin"


# ------------------------------------------------------------- size cap

def test_the_size_cap_is_enforced_while_streaming(monkeypatch, tmp_path):
    """Enforced per chunk, not after the fact — checking afterwards means
    a multi-GB response has already hit the disk."""
    monkeypatch.setenv("VISION_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(da, "MAX_BYTES", 1000)
    monkeypatch.setattr(da, "_is_public_host", lambda h: True)

    class _Resp:
        status_code = 200
        headers = {"content-type": "image/png"}
        url = type("U", (), {"host": "example.com"})()
        def iter_bytes(self, n):
            for _ in range(50):
                yield b"x" * 500          # 25 KB total, cap is 1 KB
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url): return _Resp()

    monkeypatch.setattr(da.httpx, "Client", _Client)
    out = _call(url="https://example.com/huge.png", path="huge.png")

    assert "exceeds" in out
    assert not (tmp_path / "huge.png").exists(), "the partial file must be cleaned up"
