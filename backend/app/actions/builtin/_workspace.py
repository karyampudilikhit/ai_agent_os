"""Shared workspace-path helpers used by every file-touching action.

One sandbox, one set of rules: resolve a relative path against the
workspace root, reject anything that escapes it (../, absolute paths,
symlink traversal). write_file, read_file, and the document generators
(create_pptx/docx/xlsx) all import from here instead of duplicating
the logic.

Root resolution order:
  1. VISION_WORKSPACE_DIR env var, if set (explicit override).
  2. DATA_DIR-aware default via backend.app.utils.paths.data_root() —
     on a deployed instance (Fly, Railway) this lands on the mounted
     persistent volume automatically; locally it falls back to
     <repo>/workspace.
"""

from __future__ import annotations

import os
from pathlib import Path


def workspace_root() -> Path:
    override = os.environ.get("VISION_WORKSPACE_DIR", "").strip()
    if override:
        root = Path(override).expanduser().resolve()
    else:
        from backend.app.utils.paths import data_root
        root = data_root() / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_within(root: Path, rel: str) -> Path:
    """Resolve `rel` under `root` and reject any escape."""
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {rel!r}") from exc
    return candidate
