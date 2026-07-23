"""write_file — write a UTF-8 text file inside an allowed workspace.

Scoped to a single directory the founder controls via env var
VISION_WORKSPACE_DIR (defaults to <repo_root>/workspace). We resolve
the target path against that root and reject anything that escapes it
(../, absolute paths pointing elsewhere, symlink traversal).

This is the "local system access" starter — safe by construction. The
next tier is Vision Desktop Agent for full-machine access.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from backend.app.actions.action_registry import ActionSpec


def _workspace_root() -> Path:
    override = os.environ.get("VISION_WORKSPACE_DIR", "").strip()
    if override:
        root = Path(override).expanduser().resolve()
    else:
        root = Path(__file__).resolve().parents[4] / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_within(root: Path, rel: str) -> Path:
    """Resolve `rel` under `root` and reject any escape."""
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {rel!r}") from exc
    return candidate


def _handler(args: Dict[str, Any]) -> str:
    rel = str(args.get("path") or "").strip()
    content = str(args.get("content") or "")
    if not rel:
        return "(missing 'path')"
    root = _workspace_root()
    try:
        target = _resolve_within(root, rel)
    except ValueError as exc:
        return f"(rejected: {exc})"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {target.relative_to(root)}"


def _preview(args: Dict[str, Any]) -> str:
    rel = str(args.get("path") or "").strip() or "?"
    content = str(args.get("content") or "")
    return f"Write {len(content)} chars → workspace/{rel}"


SPEC = ActionSpec(
    name="write_file",
    description="Create or overwrite a UTF-8 text file inside the Vision AI workspace directory (env VISION_WORKSPACE_DIR, defaults to <repo>/workspace). Path is relative to workspace root; escape attempts are rejected. Mutating — asks the founder for approval.",
    parameters=[
        {"name": "path", "type": "string", "description": "Relative path inside the workspace, e.g. 'notes/plan.md'", "required": True},
        {"name": "content", "type": "string", "description": "UTF-8 text to write", "required": True},
    ],
    handler=_handler,
    preview=_preview,
    mutating=True,
)
