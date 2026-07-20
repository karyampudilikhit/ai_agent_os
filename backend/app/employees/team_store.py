"""Persistent team-spec storage per Unit.

v2 (2026-07-20): Members now reference an `employee_id` in the shared
EmployeeRegistry instead of storing role/mandate inline. Same Employee
can be hired into multiple Units at once ("B" from the "B,B" hierarchy
decision) — same identity, same memory across containers.

The public API is unchanged: callers still get `members()` back as
`[{role, mandate, is_supervisor}]` (with `employee_id` added). Under
the hood we resolve IDs → registry on read. `add_member(role, mandate)`
still works — it creates a new registry Employee behind the scenes.
New: `hire(employee_id)` adds an already-existing Employee.

Legacy v1 files (inline role/mandate on members) auto-migrate on first
load: each inline member becomes a registry Employee whose id equals
the old deterministic `{session_id}__{role_slug}` — so existing memory
files at `data/{session_id}__{role_slug}.json` keep working.

Note on terminology: the "session_id" field is preserved on disk as
the primary key for filesystem compat, but conceptually this is the
Unit ID — a Unit is what the UI names it and what the Company will
reference.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from backend.app.employees.employee_registry import (
    EmployeeRegistry,
    EmployeeRegistryError,
    get_registry,
)

logger = logging.getLogger(__name__)

DEFAULT_TEAM_DIR = os.path.join(os.path.dirname(__file__), "team_data")
SCHEMA_VERSION = 2


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "role"


class TeamStore:
    """One JSON file per Unit.

    v2 persisted shape:
      {
        "session_id":     "session_abc",          # primary key on disk
        "schema_version": 2,
        "name":           "Cold Outreach Unit" | null,
        "purpose":        "…" | null,
        "company_id":     "personal" | null,      # optional parent
        "members":        [{employee_id, is_supervisor?}, …],
        "created_at":     "iso"
      }

    Denormalized `members()` return shape (unchanged from v1):
      [{employee_id, role, mandate, is_supervisor?}, …]
    """

    def __init__(
        self,
        session_id: str,
        team_dir: str = DEFAULT_TEAM_DIR,
        registry: Optional[EmployeeRegistry] = None,
    ):
        self.session_id = session_id
        self.dir = team_dir
        self.path = os.path.join(team_dir, f"{session_id}.json")
        os.makedirs(team_dir, exist_ok=True)
        self._registry = registry or get_registry()
        self._data = self._load()

    # ---- IO --------------------------------------------------------

    def _empty(self) -> Dict:
        return {
            "session_id": self.session_id,
            "schema_version": SCHEMA_VERSION,
            "name": None,
            "purpose": None,
            "company_id": None,
            "members": [],
            "created_at": datetime.utcnow().isoformat(),
        }

    def _load(self) -> Dict:
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load team spec for %s, starting fresh: %s", self.session_id, exc)
            return self._empty()

        data.setdefault("name", None)
        data.setdefault("purpose", None)
        data.setdefault("company_id", None)

        # Legacy v1 → v2 migration: any member with an inline "role"
        # field gets adopted into the registry and rewritten as
        # {employee_id, is_supervisor}. Idempotent — v2 members pass
        # through untouched.
        if data.get("schema_version") != SCHEMA_VERSION:
            migrated: List[Dict] = []
            for m in data.get("members", []):
                if not isinstance(m, dict):
                    continue
                if "employee_id" in m:
                    migrated.append(m)
                    continue
                role = str(m.get("role", "")).strip()
                mandate = str(m.get("mandate", "")).strip()
                if not role or not mandate:
                    continue
                # Legacy id — matches what EmployeeSpawner.instantiate
                # used, so existing memory files stay linked.
                legacy_id = f"{self.session_id}__{_slugify(role)}"
                try:
                    self._registry.upsert_legacy(
                        legacy_id, role, mandate,
                        is_supervisor=bool(m.get("is_supervisor")),
                    )
                except EmployeeRegistryError as exc:
                    logger.warning(
                        "Legacy migration for %s failed: %s", legacy_id, exc
                    )
                    continue
                entry = {"employee_id": legacy_id}
                if m.get("is_supervisor"):
                    entry["is_supervisor"] = True
                migrated.append(entry)
            data["members"] = migrated
            data["schema_version"] = SCHEMA_VERSION
            # Persist migration immediately so we don't re-run on next load
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str)
            except OSError as exc:
                logger.warning("Could not persist migrated team spec: %s", exc)

        return data

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, default=str)

    # ---- denormalization ------------------------------------------

    def _denormalize(self, ref: Dict) -> Optional[Dict]:
        """Resolve a stored `{employee_id, is_supervisor?}` reference
        against the registry. Returns None if the referenced Employee
        no longer exists (was deleted from the registry) — callers
        filter these out; the raw ref stays in the file until an
        explicit clear/set_members rewrites it."""
        eid = ref.get("employee_id")
        if not eid:
            return None
        spec = self._registry.get(eid)
        if not spec:
            return None
        out = {
            "employee_id": eid,
            "role": spec["role"],
            "mandate": spec["mandate"],
        }
        # An Employee's supervisor flag is either from the registry
        # (registry-level) or overridden at this Unit's ref. Prefer
        # the ref-level flag so the same Employee can serve as a
        # non-supervisor in another Unit.
        if ref.get("is_supervisor") or spec.get("is_supervisor"):
            out["is_supervisor"] = True
        return out

    # ---- public reads ---------------------------------------------

    def members(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for ref in self._data.get("members", []):
            denorm = self._denormalize(ref)
            if denorm:
                out.append(denorm)
        return out

    def specialists(self) -> List[Dict[str, str]]:
        return [m for m in self.members() if not m.get("is_supervisor")]

    def supervisor(self) -> Optional[Dict[str, str]]:
        return next((m for m in self.members() if m.get("is_supervisor")), None)

    def created_at(self) -> Optional[str]:
        return self._data.get("created_at")

    def name(self) -> Optional[str]:
        return self._data.get("name")

    def purpose(self) -> Optional[str]:
        return self._data.get("purpose")

    def company_id(self) -> Optional[str]:
        return self._data.get("company_id")

    def raw_members(self) -> List[Dict]:
        """Underlying `{employee_id, is_supervisor?}` refs. For code
        that needs the ID directly (task runner, spawner)."""
        return [dict(m) for m in self._data.get("members", [])]

    # ---- public writes --------------------------------------------

    def set_metadata(
        self,
        name: Optional[str] = None,
        purpose: Optional[str] = None,
        company_id: Optional[str] = None,
    ) -> None:
        if name is not None:
            self._data["name"] = name.strip() or None
        if purpose is not None:
            self._data["purpose"] = purpose.strip() or None
        if company_id is not None:
            self._data["company_id"] = company_id.strip() or None
        self._save()

    def add_member(
        self, role: str, mandate: str, is_supervisor: bool = False
    ) -> Dict[str, str]:
        """Create a NEW Employee in the registry and add to this Unit.
        If the Unit already has an Employee with the same role, that
        Employee is removed first (role remains the intra-Unit natural
        key, matching v1 semantics — an "edit" that changes the mandate
        replaces the Employee)."""
        # Remove existing same-role member first (preserves v1 behavior)
        existing_same_role = next(
            (m for m in self.members() if m["role"] == role.strip()), None
        )
        if existing_same_role:
            self._data["members"] = [
                m for m in self._data["members"]
                if m.get("employee_id") != existing_same_role["employee_id"]
            ]

        emp = self._registry.create(
            role=role, mandate=mandate, is_supervisor=is_supervisor
        )
        ref = {"employee_id": emp["id"]}
        if is_supervisor:
            ref["is_supervisor"] = True
        self._data["members"].append(ref)
        self._save()
        return {
            "employee_id": emp["id"],
            "role": emp["role"],
            "mandate": emp["mandate"],
            **({"is_supervisor": True} if is_supervisor else {}),
        }

    def hire(self, employee_id: str, is_supervisor: bool = False) -> Dict[str, str]:
        """Add an EXISTING registry Employee to this Unit. This is the
        "same Marketer in two Units" flow — no new registry entry, no
        new memory file, same identity everywhere."""
        emp = self._registry.get(employee_id)
        if not emp:
            raise EmployeeRegistryError(f"no Employee with id {employee_id!r}")
        # Already a member? Nothing to do (idempotent).
        for m in self._data.get("members", []):
            if m.get("employee_id") == employee_id:
                return self._denormalize(m) or {}
        ref = {"employee_id": employee_id}
        if is_supervisor:
            ref["is_supervisor"] = True
        self._data["members"].append(ref)
        self._save()
        return self._denormalize(ref) or {}

    def set_members(self, members: List[Dict]) -> None:
        """Replace the roster.

        Accepts a mixed list of:
          - {employee_id, is_supervisor?}      → hire existing
          - {role, mandate, is_supervisor?}    → create new
        Preserves any existing Supervisor if the caller didn't include
        one, matching v1's behavior — a Unit always has a Supervisor.
        """
        new_refs: List[Dict] = []
        seen_supervisor = False
        for m in members:
            if not isinstance(m, dict):
                continue
            is_sup = bool(m.get("is_supervisor"))
            if m.get("employee_id"):
                emp = self._registry.get(m["employee_id"])
                if not emp:
                    continue
                ref = {"employee_id": emp["id"]}
                if is_sup:
                    ref["is_supervisor"] = True
                    seen_supervisor = True
                new_refs.append(ref)
                continue
            role = str(m.get("role", "")).strip()
            mandate = str(m.get("mandate", "")).strip()
            if not role or not mandate:
                continue
            emp = self._registry.create(
                role=role, mandate=mandate, is_supervisor=is_sup
            )
            ref = {"employee_id": emp["id"]}
            if is_sup:
                ref["is_supervisor"] = True
                seen_supervisor = True
            new_refs.append(ref)

        if not seen_supervisor:
            existing_sup_ref = next(
                (r for r in self._data.get("members", [])
                 if r.get("is_supervisor")),
                None,
            )
            if existing_sup_ref:
                new_refs.insert(0, existing_sup_ref)

        self._data["members"] = new_refs
        self._save()

    def remove_member(
        self, role_or_id: str, allow_supervisor_removal: bool = False
    ) -> bool:
        """Remove a member from this Unit by role OR employee_id.
        Does NOT delete the underlying registry Employee — the same
        Employee may still exist in other Units. Refuses to remove the
        Supervisor unless allow_supervisor_removal=True."""
        target_ref = None
        for ref in self._data.get("members", []):
            if ref.get("employee_id") == role_or_id:
                target_ref = ref
                break
            denorm = self._denormalize(ref)
            if denorm and denorm.get("role") == role_or_id:
                target_ref = ref
                break
        if not target_ref:
            return False
        if target_ref.get("is_supervisor") and not allow_supervisor_removal:
            return False
        self._data["members"] = [
            m for m in self._data["members"]
            if m.get("employee_id") != target_ref.get("employee_id")
        ]
        self._save()
        return True

    def clear(self, keep_supervisor: bool = True) -> None:
        if keep_supervisor:
            self._data["members"] = [
                m for m in self._data.get("members", [])
                if m.get("is_supervisor")
            ]
        else:
            self._data["members"] = []
        self._save()

    # ---- session-level helpers ------------------------------------

    @staticmethod
    def new_session_id() -> str:
        return f"session_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def list_sessions(team_dir: str = DEFAULT_TEAM_DIR) -> List[Dict]:
        if not os.path.isdir(team_dir):
            return []
        out = []
        for fname in os.listdir(team_dir):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(team_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                out.append({
                    "session_id": data.get("session_id", fname[:-5]),
                    "member_count": len(data.get("members", [])),
                    "created_at": data.get("created_at"),
                    "name": data.get("name"),
                    "purpose": data.get("purpose"),
                })
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda d: d.get("created_at") or "", reverse=True)
        return out
