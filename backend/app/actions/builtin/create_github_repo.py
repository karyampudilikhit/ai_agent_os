"""create_github_repo — create a repository via the GitHub REST API.

This is the product-correct way for Vision AI to create a repo: GitHub
has a first-class API, so an employee uses THAT (the way send_email uses
SMTP and post_slack uses a webhook) rather than driving a browser. The
interactive browser-automation path (browser_task) is the fallback for
sites that have NO API — GitHub is not one of them, and the giants
actively fight automated browsers, so the API is both cleaner and more
reliable here.

Auth: a GitHub token in env var GITHUB_TOKEN. The founder generates it
themselves and drops it in; Vision AI uses it, and — like every other
credential in this app (SMTP_PASS, SLACK_WEBHOOK_URL) — it lives only in
the server's environment, never in code or the chat.

  - Fine-grained token (recommended): github.com/settings/tokens?type=beta
    → Repository access: All (or select) → Permissions →
    "Administration: Read and write" → generate.
  - Classic token: github.com/settings/tokens → scope `repo`.

Mutating — enqueues on the ApprovalQueue and only fires when the founder
taps Approve, exactly like send_email/post_slack.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import httpx

from backend.app.actions.action_registry import ActionSpec

GITHUB_API = "https://api.github.com/user/repos"
_TRUTHY = {"true", "1", "yes", "y", "private", "on"}


def _as_bool(value: Any, default: bool = False) -> bool:
    """The planner passes args as strings as often as real booleans —
    coerce both. 'private', 'true', '1', 'yes' → True."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in _TRUTHY


def _github_token() -> str:
    """Prefer the OAuth-connected token; fall back to a manually-set env
    token. OAuth first because that's the path real users take — click
    Connect, approve, done. GITHUB_TOKEN stays supported for headless
    setups and for the developer's own machine."""
    try:
        from backend.app.auth.oauth_store import get_store as _get_oauth_store
        tok = _get_oauth_store().access_token("github")
        if tok:
            return tok
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("GITHUB_TOKEN", "").strip()


def _handler(args: Dict[str, Any]) -> str:
    token = _github_token()
    if not token:
        return (
            "(GitHub is not connected. The founder connects it by clicking "
            "'Connect GitHub' in the app — that opens GitHub's own approval "
            "page; no password or token is ever typed into Vision AI. "
            "Do NOT ask the founder for a password, token, or credentials, "
            "and do NOT write instructions telling them to create the repo "
            "manually. State only that GitHub needs connecting.)"
        )

    name = str(args.get("name") or "").strip()
    if not name:
        return "(missing 'name' — what should the repository be called?)"
    private = _as_bool(args.get("private"), default=False)
    description = str(args.get("description") or "").strip()
    # Default to initializing with a README so the repo is immediately
    # clonable and ready to build a site in — the common next step.
    auto_init = _as_bool(args.get("auto_init"), default=True)

    payload: Dict[str, Any] = {"name": name, "private": private, "auto_init": auto_init}
    if description:
        payload["description"] = description

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(GITHUB_API, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        return f"(GitHub request failed: {exc})"

    if resp.status_code == 201:
        data = resp.json()
        return (
            f"Created repository {data.get('full_name', name)} "
            f"({'private' if private else 'public'}).\n"
            f"URL: {data.get('html_url', '')}\n"
            f"Clone: {data.get('clone_url', '')}\n"
            f"(Ready to build in — README was initialized.)"
        )

    # Surface real GitHub errors plainly rather than pretending success.
    if resp.status_code == 401:
        return "(GitHub rejected the token (401 Unauthorized) — it may be expired or wrong. Generate a fresh GITHUB_TOKEN.)"
    if resp.status_code == 403:
        return "(GitHub returned 403 — the token likely lacks repo-creation permission. Fine-grained needs 'Administration: Read and write'; classic needs the 'repo' scope.)"
    if resp.status_code == 422:
        # Most commonly: a repo with that name already exists.
        detail = ""
        try:
            errs = resp.json().get("errors") or []
            detail = "; ".join(e.get("message", "") for e in errs if e.get("message"))
        except Exception:  # noqa: BLE001
            pass
        return f"(GitHub returned 422 — the repo could not be created. {detail or 'A repo with this name may already exist.'})"
    return f"(GitHub returned HTTP {resp.status_code}: {resp.text[:200]})"


def _preview(args: Dict[str, Any]) -> str:
    name = str(args.get("name") or "").strip() or "?"
    private = _as_bool(args.get("private"), default=False)
    description = str(args.get("description") or "").strip()
    desc_part = f" — \"{description}\"" if description else ""
    return f"Create GitHub repo: {name} ({'private' if private else 'public'}){desc_part}"


SPEC = ActionSpec(
    name="create_github_repo",
    description=(
        "Create a new GitHub repository via the GitHub API. Use this for "
        "creating repos / starting a new website or project on GitHub — "
        "it's more reliable than driving a browser. Requires a GITHUB_TOKEN "
        "env var on the server. Mutating — asks the founder for approval "
        "before creating."
    ),
    parameters=[
        {"name": "name", "type": "string", "description": "Repository name (no spaces; use hyphens).", "required": True},
        {"name": "private", "type": "boolean", "description": "True for a private repo, false for public. Default false.", "required": False},
        {"name": "description", "type": "string", "description": "Optional short repo description.", "required": False},
        {"name": "auto_init", "type": "boolean", "description": "Initialize with a README so it's immediately clonable. Default true.", "required": False},
    ],
    handler=_handler,
    preview=_preview,
    mutating=True,
    capability="repo.create",
)
