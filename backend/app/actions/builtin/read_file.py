"""read_file — read a text file from the Vision AI workspace.

Read-only sibling of write_file. Because it's read-only, mutating=False
so it runs immediately without approval.
"""

from __future__ import annotations

from typing import Any, Dict

from backend.app.actions.action_registry import ActionSpec
from backend.app.actions.builtin._workspace import resolve_within, workspace_root

MAX_READ_BYTES = 200_000


def _handler(args: Dict[str, Any]) -> str:
    rel = str(args.get("path") or "").strip()
    if not rel:
        return "(missing 'path')"
    root = workspace_root()
    try:
        target = resolve_within(root, rel)
    except ValueError as exc:
        return f"(rejected: {exc})"
    if not target.exists():
        return f"(no such file: workspace/{rel})"
    if not target.is_file():
        return f"(not a file: workspace/{rel})"
    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        return f"(file too large: {size} bytes > {MAX_READ_BYTES}; refusing to read)"
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"(binary file, not readable as UTF-8)"
    return f"=== workspace/{rel} ===\n{text}"


def _preview(args: Dict[str, Any]) -> str:
    return f"Read workspace/{args.get('path')!r}"


SPEC = ActionSpec(
    name="read_file",
    description="Read a UTF-8 text file from the Vision AI workspace directory. Read-only — runs immediately without approval.",
    parameters=[
        {"name": "path", "type": "string", "description": "Relative path inside the workspace", "required": True},
    ],
    handler=_handler,
    preview=_preview,
    mutating=False,
)
