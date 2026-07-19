"""Persistent team-spec storage per session.

Until this existed, EmployeeSpawner designed a team from scratch on
every prompt — so a founder couldn't build up a stable team, tweak it,
and reuse it across follow-up tasks. This is the "roster" concept the
UI needs: a session owns a team spec (list of {role, mandate}), the
user can add/edit/remove members, and any task run instantiates
DynamicEmployees from whatever spec is current at run time.

Deliberately separated from EmployeeMemoryStore — memory is per
*employee* (per role slug), team spec is per *session* (which
employees exist right now).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TEAM_DIR = os.path.join(os.path.dirname(__file__), "team_data")


class TeamStore:
    """One JSON file per Unit.

    Persisted shape:
      {
        "session_id": "session_...",   # kept as the primary key for
                                        # backward compat with existing
                                        # data; UI-facing name is "unit"
        "name":       "Cold Outreach Unit" | null,
        "purpose":    "..." | null,
        "members":    [{role, mandate, is_supervisor?}, ...],
        "created_at": "iso...",
      }
    """

    def __init__(self, session_id: str, team_dir: str = DEFAULT_TEAM_DIR):
        self.session_id = session_id
        self.dir = team_dir
        self.path = os.path.join(team_dir, f"{session_id}.json")
        os.makedirs(team_dir, exist_ok=True)
        self._data = self._load()

    def _empty(self) -> Dict:
        return {
            "session_id": self.session_id,
            "name": None,
            "purpose": None,
            "members": [],
            "created_at": datetime.utcnow().isoformat(),
        }

    def _load(self) -> Dict:
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Backfill fields older files won't have
                data.setdefault("name", None)
                data.setdefault("purpose", None)
                return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load team spec for %s, starting fresh: %s", self.session_id, exc)
            return self._empty()

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, default=str)

    # ------------------------------------------------------------------

    def members(self) -> List[Dict[str, str]]:
        return list(self._data.get("members", []))

    def specialists(self) -> List[Dict[str, str]]:
        """All members EXCEPT the Supervisor. The workhorses."""
        return [m for m in self.members() if not m.get("is_supervisor")]

    def supervisor(self) -> Optional[Dict[str, str]]:
        return next((m for m in self.members() if m.get("is_supervisor")), None)

    def created_at(self) -> Optional[str]:
        return self._data.get("created_at")

    def name(self) -> Optional[str]:
        return self._data.get("name")

    def purpose(self) -> Optional[str]:
        return self._data.get("purpose")

    def set_metadata(self, name: Optional[str] = None, purpose: Optional[str] = None) -> None:
        if name is not None:
            self._data["name"] = name.strip() or None
        if purpose is not None:
            self._data["purpose"] = purpose.strip() or None
        self._save()

    def set_members(self, members: List[Dict[str, str]]) -> None:
        """Replace the roster. Preserves the existing Supervisor if the
        caller didn't include one — a Unit ALWAYS has a Supervisor."""
        cleaned = []
        seen_supervisor = False
        for m in members:
            role = str(m.get("role", "")).strip()
            mandate = str(m.get("mandate", "")).strip()
            if not role or not mandate:
                continue
            entry = {"role": role, "mandate": mandate}
            if m.get("is_supervisor"):
                entry["is_supervisor"] = True
                seen_supervisor = True
            cleaned.append(entry)
        if not seen_supervisor:
            existing_sup = self.supervisor()
            if existing_sup:
                cleaned.insert(0, existing_sup)
        self._data["members"] = cleaned
        self._save()

    def add_member(self, role: str, mandate: str, is_supervisor: bool = False) -> Dict[str, str]:
        role = role.strip()
        mandate = mandate.strip()
        if not role or not mandate:
            raise ValueError("Both role and mandate are required")
        # Replace if a member with the same role already exists — role
        # is the natural key inside a single Unit's team.
        members = [m for m in self._data.get("members", []) if m.get("role") != role]
        member = {"role": role, "mandate": mandate}
        if is_supervisor:
            member["is_supervisor"] = True
        members.append(member)
        self._data["members"] = members
        self._save()
        return member

    def remove_member(self, role: str, allow_supervisor_removal: bool = False) -> bool:
        """Remove a member by role. Refuses to remove the Supervisor
        unless allow_supervisor_removal=True — Units always keep at
        least one Supervisor by design."""
        before = self._data.get("members", [])
        target = next((m for m in before if m.get("role") == role), None)
        if not target:
            return False
        if target.get("is_supervisor") and not allow_supervisor_removal:
            return False
        after = [m for m in before if m.get("role") != role]
        self._data["members"] = after
        self._save()
        return True

    def clear(self, keep_supervisor: bool = True) -> None:
        if keep_supervisor:
            self._data["members"] = [m for m in self._data.get("members", []) if m.get("is_supervisor")]
        else:
            self._data["members"] = []
        self._save()

    @staticmethod
    def new_session_id() -> str:
        return f"session_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def list_sessions(team_dir: str = DEFAULT_TEAM_DIR) -> List[Dict]:
        """Cheap directory listing for the UI's "recent sessions" — no
        heavy work per session."""
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
        # Newest first
        out.sort(key=lambda d: d.get("created_at") or "", reverse=True)
        return out
