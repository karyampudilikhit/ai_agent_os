"""write_file — write a UTF-8 text file inside an allowed workspace.

Scoped to a single directory the founder controls via env var
VISION_WORKSPACE_DIR (defaults to <repo_root>/workspace). We resolve
the target path against that root and reject anything that escapes it
(../, absolute paths pointing elsewhere, symlink traversal).

This is the "local system access" starter — safe by construction. The
next tier is Vision Desktop Agent for full-machine access.

NOT approval-gated, as of the website work. The gate exists for actions
that reach OUTSIDE the founder's machine — an email sent, a Slack posted,
a repo created, a site published. This one writes into a sandbox the
founder already owns and nobody else can see.

Two reasons it was wrong to gate:

  - It was inconsistent. `run_python` is not gated and is strictly more
    powerful — it executes arbitrary code, and can write files itself.
    Gating the narrow tool while leaving the broad one open protected
    nothing.
  - It taught the wrong habit. Building a site is a dozen file writes;
    a dozen taps in a row is how a founder learns to approve without
    reading, which is precisely the moment the gate stops working for
    the send_email sitting in the middle of them.

`deploy_vercel` is where the tap belongs, and it still has one.
"""

from __future__ import annotations

from typing import Any, Dict

from backend.app.actions.action_registry import ActionSpec
from backend.app.actions.builtin._workspace import resolve_within, workspace_root


def _handler(args: Dict[str, Any]) -> str:
    rel = str(args.get("path") or "").strip()
    content = str(args.get("content") or "")
    if not rel:
        return "(missing 'path')"
    root = workspace_root()
    try:
        target = resolve_within(root, rel)
    except ValueError as exc:
        return f"(rejected: {exc})"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    # Register it so downstream specialists are HANDED this path instead
    # of guessing one. Guessed filenames are what produced `data.csv`,
    # `chosen_etf.txt` and a run that emailed a colleague who does not
    # exist — see state/run_artifacts.py.
    from backend.app.state import run_artifacts
    run_artifacts.register_file(str(target), producer="write_file")

    return f"Wrote {len(content)} chars to {target.relative_to(root)}"


def _preview(args: Dict[str, Any]) -> str:
    rel = str(args.get("path") or "").strip() or "?"
    content = str(args.get("content") or "")
    return f"Write {len(content)} chars → workspace/{rel}"


SPEC = ActionSpec(
    name="write_file",
    description="Create or overwrite a UTF-8 text file inside the Vision AI workspace directory (env VISION_WORKSPACE_DIR, defaults to <repo>/workspace). Path is relative to workspace root; escape attempts are rejected. Use it freely for pages, stylesheets, scripts and notes — it writes only inside the sandbox and runs immediately without asking for approval.",
    parameters=[
        {"name": "path", "type": "string", "description": "Relative path inside the workspace, e.g. 'notes/plan.md'", "required": True},
        {"name": "content", "type": "string", "description": "UTF-8 text to write", "required": True},
    ],
    handler=_handler,
    preview=_preview,
    mutating=False,
)
