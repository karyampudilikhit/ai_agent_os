"""Employee registry — first-class Employee identity.

Before this existed, an Employee was only a Python object constructed
at task-run time from a role+mandate stored inline in a Unit's team
file. That meant:
  - Same role in two Units → two different Employees, two memories
  - No way to "hire the same Marketer into a second Unit"
  - No stable ID to reference in the UI or across containers

This module makes Employees first-class persistent objects with UUIDs.
The registry is the authoritative source of identity. Units and Teams
now reference Employees by ID, not by inline role/mandate. Same
Employee → same memory, everywhere they appear.

Storage: one JSON file per employee under `data/registry/`. Kept
separate from `data/{employee_id}.json` (the EmployeeMemoryStore
files) so identity and history stay decoupled — losing one doesn't
corrupt the other.

Backwards-compat: legacy IDs like `session_abc__growth_strategist` are
still valid identities (they're what appear in existing memory files).
The registry can adopt them as-is when TeamStore migrates old files
to the new format.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_DIR = os.path.join(
    os.path.dirname(__file__), "data", "registry"
)


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "role"


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


class EmployeeRegistryError(Exception):
    pass


class EmployeeRegistry:
    """One JSON file per Employee under DEFAULT_REGISTRY_DIR.

    Persisted shape:
      {
        "id":          "emp_<uuid8>" | legacy id,
        "role":        "Growth Strategist",
        "mandate":     "…",
        "avatar_seed": "growth_strategist",     # for UI avatar hashing
        "tags":        ["startup", "growth"],   # user-supplied labels
        "is_supervisor": false,
        "created_at":  "iso"
      }
    """

    def __init__(self, registry_dir: str = DEFAULT_REGISTRY_DIR):
        self.dir = registry_dir
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.RLock()

    # ---- IO --------------------------------------------------------

    def _path(self, employee_id: str) -> str:
        # Safe filename — the id itself is a slug/uuid, but belt+braces
        safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", employee_id)
        return os.path.join(self.dir, f"{safe}.json")

    def _read(self, employee_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(employee_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read employee %s: %s", employee_id, exc)
            return None

    def _write(self, spec: Dict[str, Any]) -> None:
        path = self._path(spec["id"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2, default=str)

    # ---- id helpers ------------------------------------------------

    @staticmethod
    def new_id() -> str:
        """Fresh UUID-shaped id for a brand-new Employee. Prefixed so
        legacy `session_abc__role_slug` ids are still distinguishable."""
        return f"emp_{uuid.uuid4().hex[:12]}"

    # ---- public API ------------------------------------------------

    def create(
        self,
        role: str,
        mandate: str,
        is_supervisor: bool = False,
        tags: Optional[List[str]] = None,
        employee_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new Employee record. Returns the persisted spec.
        Pass `employee_id` to adopt a legacy id (used during migration
        of old TeamStore files); omit for a fresh UUID."""
        role = (role or "").strip()
        mandate = (mandate or "").strip()
        if not role or not mandate:
            raise EmployeeRegistryError("role and mandate are both required")
        with self._lock:
            eid = employee_id or self.new_id()
            if self._read(eid):
                raise EmployeeRegistryError(
                    f"Employee id {eid!r} already exists"
                )
            spec = {
                "id": eid,
                "role": role,
                "mandate": mandate,
                "avatar_seed": _slugify(role),
                "tags": list(tags or []),
                "is_supervisor": bool(is_supervisor),
                "created_at": _now_iso(),
            }
            self._write(spec)
            return dict(spec)

    def upsert_legacy(
        self,
        legacy_id: str,
        role: str,
        mandate: str,
        is_supervisor: bool = False,
    ) -> Dict[str, Any]:
        """Migration helper: adopt an old inline-member entry as an
        Employee in the registry, preserving the legacy id so its
        existing EmployeeMemoryStore file keeps working. Idempotent."""
        existing = self._read(legacy_id)
        if existing:
            return existing
        return self.create(
            role=role,
            mandate=mandate,
            is_supervisor=is_supervisor,
            employee_id=legacy_id,
        )

    def get(self, employee_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            spec = self._read(employee_id)
            return dict(spec) if spec else None

    def list(self) -> List[Dict[str, Any]]:
        """Every Employee in the registry, newest first."""
        out: List[Dict[str, Any]] = []
        if not os.path.isdir(self.dir):
            return out
        with self._lock:
            for fname in os.listdir(self.dir):
                if not fname.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(self.dir, fname), "r", encoding="utf-8") as f:
                        out.append(json.load(f))
                except (json.JSONDecodeError, OSError):
                    continue
        out.sort(key=lambda d: d.get("created_at") or "", reverse=True)
        return out

    def update(self, employee_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Partial update on mutable fields (role, mandate, tags,
        is_supervisor). id and created_at are immutable."""
        with self._lock:
            spec = self._read(employee_id)
            if not spec:
                raise EmployeeRegistryError(f"no Employee with id {employee_id!r}")
            for field in ("role", "mandate", "tags", "is_supervisor"):
                if field in patch and patch[field] is not None:
                    spec[field] = patch[field]
            if "role" in patch:
                spec["avatar_seed"] = _slugify(spec["role"])
            self._write(spec)
            return dict(spec)

    def delete(self, employee_id: str) -> bool:
        """Remove the Employee record. Does NOT touch its memory file —
        that's a separate decision (you might want to hire back the same
        employee later and preserve history)."""
        with self._lock:
            path = self._path(employee_id)
            if not os.path.exists(path):
                return False
            try:
                os.remove(path)
                return True
            except OSError as exc:
                logger.warning("Could not delete employee %s: %s", employee_id, exc)
                return False


# ------------------------------------------------------------------
# Module singleton — one registry per process
# ------------------------------------------------------------------

_registry_singleton: Optional[EmployeeRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> EmployeeRegistry:
    global _registry_singleton
    with _registry_lock:
        if _registry_singleton is None:
            _registry_singleton = EmployeeRegistry()
        return _registry_singleton
