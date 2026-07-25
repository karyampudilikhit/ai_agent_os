"""Central data-root resolver.

Every persistent store (TeamStore, CompanyStore, HTTPToolStore,
ApprovalQueue, EmployeeRegistry, EmployeeMemoryStore) writes to disk.
In local dev those files land alongside the source. On Fly / Railway,
the source tree is read-only inside the container image, so state has
to live on a mounted persistent volume.

Single knob: `DATA_DIR` env var. Set it (e.g. `/data` on Fly) and all
stores redirect. Unset, they keep their original locations so local
dev is unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def repo_root() -> Path:
    """The <repo>/ai_agent_os directory — dev default for data files."""
    return _REPO_ROOT


def data_root() -> Path:
    """Where stores put their JSON files. Defaults to repo root; env
    var override lets deployed instances point at /data (or wherever
    the persistent volume is mounted)."""
    override = os.environ.get("DATA_DIR", "").strip()
    if override:
        p = Path(override).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p
    return _REPO_ROOT


def under_data(name: str) -> Path:
    """Convenience for top-level state files (`.pending_actions.json`,
    `.http_tools.json`, etc.). Returns an absolute path inside the
    data root."""
    return data_root() / name
