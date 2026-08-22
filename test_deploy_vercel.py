"""deploy_vercel — the upload contract and the bounds.

The live path is not exercised here: that needs a real VERCEL_TOKEN and a
real account. What IS exercised is everything that decides what gets sent
and whether the call is made at all — file collection, the skip rules,
the caps, the SHA-1 manifest, and the preview-vs-production default.

Those are the parts that can quietly ship the wrong thing.
"""
from __future__ import annotations

import hashlib

from backend.app.actions.builtin import deploy_vercel as dv


def _call(**kw):
    return dv.SPEC.handler(kw)


# ------------------------------------------------------- it stays gated

def test_publishing_still_asks_for_approval():
    """Local writes were un-gated so a founder is not tapping through a
    dozen scratch files. This one publishes to the public internet under
    their account, which is exactly the line the gate is for."""
    assert dv.SPEC.mutating is True


def test_write_file_is_no_longer_gated():
    """The other half of that trade — and worth pinning, because the two
    only make sense together."""
    from backend.app.actions.builtin import write_file
    assert write_file.SPEC.mutating is False


def test_both_are_registered():
    from backend.app.actions.action_registry import get_registry
    names = [t["qualified_name"] for t in get_registry().list_tools()]
    assert "action.deploy_vercel" in names
    assert "action.download_asset" in names


# ------------------------------------------------- refuses before calling

def test_missing_token_is_explained_not_crashed(monkeypatch):
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    out = _call(directory="site", project_name="x")
    assert "VERCEL_TOKEN is not set" in out
    assert "vercel.com/account/tokens" in out


def test_missing_arguments_are_reported(monkeypatch):
    monkeypatch.setenv("VERCEL_TOKEN", "t")
    assert "missing 'directory'" in _call(project_name="x")
    assert "missing 'project_name'" in _call(directory="site")


def test_a_path_escape_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("VERCEL_TOKEN", "t")
    monkeypatch.setenv("VISION_WORKSPACE_DIR", str(tmp_path))
    assert "escapes workspace" in _call(directory="../../etc", project_name="x")


def test_a_missing_directory_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("VERCEL_TOKEN", "t")
    monkeypatch.setenv("VISION_WORKSPACE_DIR", str(tmp_path))
    out = _call(directory="nope", project_name="x")
    assert "not a directory" in out


def test_an_empty_directory_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("VERCEL_TOKEN", "t")
    monkeypatch.setenv("VISION_WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "site").mkdir()
    assert "no files in it" in _call(directory="site", project_name="x")


# ------------------------------------------------------- what gets shipped

def _site(tmp_path):
    site = tmp_path / "site"
    (site / "img").mkdir(parents=True)
    (site / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (site / "style.css").write_text("h1{color:red}", encoding="utf-8")
    (site / "img" / "hero.png").write_bytes(b"\x89PNG binary")
    return site


def test_collection_walks_subdirectories_and_keeps_binaries(tmp_path):
    site = _site(tmp_path)
    files, total = dv._collect(site)
    names = sorted(f[0] for f in files)

    assert names == ["img/hero.png", "index.html", "style.css"]
    assert total == 11 + 13 + 11
    # Binary survives as bytes — the whole point of pairing this with
    # download_asset.
    blob = dict((f[0], f[2]) for f in files)["img/hero.png"]
    assert blob == b"\x89PNG binary"


def test_junk_directories_are_never_uploaded(tmp_path):
    site = _site(tmp_path)
    (site / "node_modules" / "pkg").mkdir(parents=True)
    (site / "node_modules" / "pkg" / "a.js").write_text("x", encoding="utf-8")
    (site / ".git").mkdir()
    (site / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (site / ".DS_Store").write_text("junk", encoding="utf-8")
    (site / ".env").write_text("SECRET=1", encoding="utf-8")

    names = sorted(f[0] for f in dv._collect(site)[0])
    assert names == ["img/hero.png", "index.html", "style.css"], names


def test_dotenv_is_never_shipped(tmp_path):
    """Deploying a .env to a public host publishes every credential in
    it. Worth its own test rather than trusting the set above."""
    site = _site(tmp_path)
    (site / ".env").write_text("VERCEL_TOKEN=live_secret", encoding="utf-8")
    assert not any(".env" in f[0] for f in dv._collect(site)[0])


def test_file_count_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("VERCEL_TOKEN", "t")
    monkeypatch.setenv("VISION_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(dv, "MAX_FILES", 2)
    _site(tmp_path)
    assert "exceeds the 2-file cap" in _call(directory="site", project_name="x")


def test_total_size_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("VERCEL_TOKEN", "t")
    monkeypatch.setenv("VISION_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(dv, "MAX_TOTAL_BYTES", 10)
    _site(tmp_path)
    assert "exceeds the" in _call(directory="site", project_name="x")


# ------------------------------------------------ the API call it makes

class _FakeClient:
    """Records what would have gone to Vercel."""
    uploads: list = []
    deploy_payload: dict = {}

    def __init__(self, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def post(self, url, headers=None, content=None, json=None):
        if url.startswith(dv.UPLOAD_URL):
            _FakeClient.uploads.append((headers.get("x-vercel-digest"), content))
            return _R(200, {})
        _FakeClient.deploy_payload = json
        return _R(200, {"url": "acme-redesign-abc.vercel.app",
                        "id": "dpl_1", "readyState": "READY"})

    def get(self, url, headers=None):
        return _R(200, {"readyState": "READY"})


class _R:
    def __init__(self, code, body): self.status_code = code; self._b = body; self.text = str(body)
    def json(self): return self._b


def _run_fake(monkeypatch, tmp_path, **kw):
    monkeypatch.setenv("VERCEL_TOKEN", "tok")
    monkeypatch.delenv("VERCEL_TEAM_ID", raising=False)
    monkeypatch.setenv("VISION_WORKSPACE_DIR", str(tmp_path))
    _site(tmp_path)
    _FakeClient.uploads = []
    monkeypatch.setattr(dv.httpx, "Client", _FakeClient)
    return _call(directory="site", project_name="acme-redesign", **kw)


def test_every_file_is_uploaded_under_its_sha1(monkeypatch, tmp_path):
    out = _run_fake(monkeypatch, tmp_path)

    assert len(_FakeClient.uploads) == 3
    for sha, content in _FakeClient.uploads:
        assert sha == hashlib.sha1(content).hexdigest(), "digest must match the body"

    manifest = {f["file"]: f for f in _FakeClient.deploy_payload["files"]}
    assert set(manifest) == {"index.html", "style.css", "img/hero.png"}
    assert manifest["index.html"]["size"] == 11
    assert "https://acme-redesign-abc.vercel.app" in out


def test_it_deploys_as_a_preview_by_default(monkeypatch, tmp_path):
    out = _run_fake(monkeypatch, tmp_path)
    assert "target" not in _FakeClient.deploy_payload, "no target == preview"
    assert "[preview]" in out


def test_production_only_when_asked(monkeypatch, tmp_path):
    out = _run_fake(monkeypatch, tmp_path, production="true")
    assert _FakeClient.deploy_payload["target"] == "production"
    assert "[production]" in out


def test_it_declares_no_framework(monkeypatch, tmp_path):
    """Left to detect, Vercel will try to run a build this folder has no
    script for."""
    _run_fake(monkeypatch, tmp_path)
    assert _FakeClient.deploy_payload["projectSettings"]["framework"] is None
