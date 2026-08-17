"""deploy_vercel — publish a folder from the workspace to a live URL.

The last missing link. An employee could already read a real page, pull
down its real assets, and write a real site into the workspace -- and
then the work sat in a local folder nobody could see. "Here is the site I
built" is a screenshot; "here is the URL" is a deliverable.

Vercel over git-based hosting on purpose: the Deployments API takes files
directly, so there is no repo to create, no push, no build hook, and no
waiting for a CI run. One call chain, one URL back.

Two-step upload, which is the API's own shape and also the right one for
us: each file is POSTed to /v2/files keyed by its SHA-1, then the
deployment references those hashes. Inlining file bodies in the
deployment payload works for small text sites and falls over exactly when
a site gets interesting -- the moment it has photographs in it.

MUTATING, and this one genuinely earns the approval tap. Everything else
this employee did stayed on the founder's machine; this publishes to the
public internet under their account. That is the line the approval gate
exists to guard, and it is why local file writes should NOT be gated --
spending the founder's attention on scratch files is what teaches people
to approve without reading.

Auth: VERCEL_TOKEN in the server environment (vercel.com/account/tokens).
Optional VERCEL_TEAM_ID when the account is a team rather than personal.
Same handling as SMTP_PASS and GITHUB_TOKEN -- env only, never in code or
the chat.

NOTE: written against Vercel's documented REST API and exercised here
against a stubbed transport. The live path has not been run, because that
needs a real token.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx

from backend.app.actions.action_registry import ActionSpec
from backend.app.actions.builtin._workspace import resolve_within, workspace_root

logger = logging.getLogger(__name__)

API = "https://api.vercel.com"
UPLOAD_URL = f"{API}/v2/files"
DEPLOY_URL = f"{API}/v13/deployments"

# Bounds. A static site that trips any of these is not a static site, and
# discovering that after a 400MB upload is worse than being told now.
MAX_FILES = int(os.environ.get("VERCEL_MAX_FILES", "300"))
MAX_TOTAL_BYTES = int(os.environ.get("VERCEL_MAX_TOTAL_BYTES", str(80 * 1024 * 1024)))
TIMEOUT = float(os.environ.get("VERCEL_TIMEOUT", "60"))
READY_POLL_SECONDS = float(os.environ.get("VERCEL_READY_POLL_SECONDS", "45"))

# Never shipped: version-control internals, dependency trees, OS turds.
# Uploading node_modules to a static host is a classic way to turn a
# 12-file site into a 40,000-file deployment.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".next", ".vercel", ".venv"}
SKIP_FILES = {".DS_Store", "Thumbs.db", ".env"}


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _team_query() -> str:
    team = os.environ.get("VERCEL_TEAM_ID", "").strip()
    return f"?teamId={team}" if team else ""


def _collect(root: Path) -> Tuple[List[Tuple[str, Path, bytes]], int]:
    """Every shippable file under `root`, as (relative posix path, path, bytes)."""
    out: List[Tuple[str, Path, bytes]] = []
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_FILES:
            continue
        data = path.read_bytes()
        total += len(data)
        out.append((path.relative_to(root).as_posix(), path, data))
    return out, total


def _handler(args: Dict[str, Any]) -> str:
    token = os.environ.get("VERCEL_TOKEN", "").strip()
    if not token:
        return ("(deploy failed: VERCEL_TOKEN is not set. Create one at "
                "vercel.com/account/tokens and add VERCEL_TOKEN=... to the "
                "server's .env, then restart it.)")

    rel_dir = str(args.get("directory") or "").strip()
    if not rel_dir:
        return "(missing 'directory' — the workspace folder holding the site, e.g. 'site')"
    name = str(args.get("project_name") or "").strip().lower()
    if not name:
        return "(missing 'project_name' — the Vercel project to deploy into, e.g. 'acme-redesign')"
    production = str(args.get("production") or "").strip().lower() in ("true", "1", "yes", "y")

    root = workspace_root()
    try:
        src = resolve_within(root, rel_dir)
    except ValueError as exc:
        return f"(rejected: {exc})"
    if not src.is_dir():
        return f"(rejected: workspace/{rel_dir} is not a directory — write the site files first)"

    files, total = _collect(src)
    if not files:
        return f"(rejected: workspace/{rel_dir} has no files in it)"
    if len(files) > MAX_FILES:
        return f"(rejected: {len(files)} files exceeds the {MAX_FILES}-file cap)"
    if total > MAX_TOTAL_BYTES:
        return (f"(rejected: {total / 1048576:.1f}MB exceeds the "
                f"{MAX_TOTAL_BYTES // 1048576}MB cap)")
    if not any(f[0] == "index.html" for f in files):
        # Not fatal -- Vercel will serve whatever is there -- but a site
        # with no index is almost always a mistake worth naming.
        logger.warning("deploy_vercel: no index.html at the root of %s", rel_dir)

    q = _team_query()
    manifest: List[Dict[str, Any]] = []
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            # 1. Upload each blob, keyed by SHA-1. Vercel dedupes on the
            #    digest, so re-deploying a site only ships what changed.
            for rel, _path, data in files:
                sha = hashlib.sha1(data).hexdigest()
                up = client.post(
                    UPLOAD_URL + q,
                    headers={
                        **_headers(token),
                        "Content-Type": "application/octet-stream",
                        "x-vercel-digest": sha,
                    },
                    content=data,
                )
                if up.status_code >= 400:
                    return (f"(deploy failed uploading {rel}: HTTP "
                            f"{up.status_code} {up.text[:200]})")
                manifest.append({"file": rel, "sha": sha, "size": len(data)})

            # 2. Create the deployment from those hashes.
            payload = {
                "name": name,
                "files": manifest,
                "target": "production" if production else None,
                "projectSettings": {
                    # A plain static folder. Declaring this stops Vercel
                    # framework-detecting its way into running a build
                    # that does not exist.
                    "framework": None,
                    "buildCommand": None,
                    "outputDirectory": None,
                    "installCommand": None,
                },
            }
            payload = {k: v for k, v in payload.items() if v is not None}
            dep = client.post(DEPLOY_URL + q, headers=_headers(token), json=payload)
            if dep.status_code >= 400:
                return f"(deploy failed: HTTP {dep.status_code} {dep.text[:300]})"
            body = dep.json()
            url = body.get("url") or ""
            dep_id = body.get("id") or ""
            if not url:
                return f"(deploy failed: Vercel returned no URL — {str(body)[:200]})"

            # 3. Wait briefly for READY. A static folder needs no build,
            #    so this normally returns in a few seconds; reporting a
            #    URL that 404s for the next thirty is worse than waiting.
            state = body.get("readyState") or body.get("status") or ""
            deadline = time.time() + READY_POLL_SECONDS
            while state not in ("READY", "ERROR", "CANCELED") and time.time() < deadline:
                time.sleep(2.0)
                try:
                    chk = client.get(f"{API}/v13/deployments/{dep_id}{q}",
                                     headers=_headers(token))
                    if chk.status_code < 400:
                        state = chk.json().get("readyState") or state
                except httpx.HTTPError:
                    break
    except httpx.TimeoutException:
        return f"(deploy timed out after {TIMEOUT:.0f}s)"
    except Exception as exc:  # noqa: BLE001
        return f"(deploy failed: {exc})"

    live = f"https://{url}"
    kind = "production" if production else "preview"
    # verified=False deliberately: Vercel saying READY is Vercel's claim,
    # not evidence the page loads. It becomes verified when something
    # actually opens it — see output_contract.URL_VERIFIED.
    from backend.app.state import run_artifacts
    run_artifacts.register_url(live, producer="deploy_vercel", verified=False,
                               summary=f"{kind} deployment of {rel_dir} "
                                       f"({len(manifest)} files)")
    if state == "ERROR":
        return f"(deploy failed: Vercel reported build state ERROR for {live})"
    tail = "" if state == "READY" else f" (state: {state or 'pending'} — may take another moment)"
    return (f"Deployed {len(manifest)} file(s), {total:,} bytes to {live} "
            f"[{kind}]{tail}. This is a live public URL.")


def _preview(args: Dict[str, Any]) -> str:
    rel = str(args.get("directory") or "?")
    name = str(args.get("project_name") or "?")
    prod = str(args.get("production") or "").lower() in ("true", "1", "yes", "y")
    return (f"Publish workspace/{rel} to Vercel project {name!r} "
            f"as {'PRODUCTION' if prod else 'a preview'} — public URL")


SPEC = ActionSpec(
    name="deploy_vercel",
    description=(
        "Publish a folder from the Vision AI workspace to Vercel and return a "
        "live public URL. Call this LAST, after every page and asset has been "
        "written into the folder. Deploys as a preview by default; pass "
        "production=true only when explicitly asked to go live. Mutating — "
        "asks the founder for approval, because it publishes to the internet."
    ),
    parameters=[
        {"name": "directory", "type": "string",
         "description": "Workspace folder holding the finished site, e.g. 'site'.",
         "required": True},
        {"name": "project_name", "type": "string",
         "description": "Vercel project name, lowercase with hyphens, e.g. 'acme-redesign'.",
         "required": True},
        {"name": "production", "type": "string",
         "description": "'true' to publish to the production domain. Defaults to a preview URL.",
         "required": False},
    ],
    handler=_handler,
    preview=_preview,
    mutating=True,
)
