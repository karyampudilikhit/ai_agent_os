"""Company store — the top level of the Employee -> Team -> Unit ->
Company hierarchy.

A Company is a persistent container that owns Units. Structurally, it
gives the org-chart metaphor its top level; behaviorally (in Phase 1)
it's just a labeled bag of unit_ids that Units can point to via their
`company_id` field.

The active CEO Manager — the top-level LLM Manager that delegates
across Units — is deliberately deferred to Phase 2. Phase 1 gets the
structure right first; the Company's own intelligence comes next.

Storage: JSON files under `company_data/`, one per Company.

Automatic default: on first read of the singleton store, if there are
no Companies at all, we create a "Personal" Company so existing Units
that were created before Companies existed have a sensible parent
without the user having to think about it.
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

DEFAULT_COMPANY_DIR = os.path.join(
    os.path.dirname(__file__), "company_data"
)
DEFAULT_COMPANY_ID = "personal"


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "company"


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


class CompanyStoreError(Exception):
    pass


class CompanyStore:
    """One JSON file per Company under DEFAULT_COMPANY_DIR.

    Persisted shape:
      {
        "id":         "personal" | "co_<uuid8>",
        "name":       "Personal" | "Vision AI Inc.",
        "purpose":    "…" | null,           # what the Company is for
        "unit_ids":   ["session_abc", …],   # ordered
        "created_at": "iso"
      }
    """

    def __init__(self, company_dir: str = DEFAULT_COMPANY_DIR):
        self.dir = company_dir
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.RLock()
        self._ensure_default()

    # ---- IO --------------------------------------------------------

    def _path(self, company_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", company_id)
        return os.path.join(self.dir, f"{safe}.json")

    def _read(self, company_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(company_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read company %s: %s", company_id, exc)
            return None

    def _write(self, spec: Dict[str, Any]) -> None:
        with open(self._path(spec["id"]), "w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2, default=str)

    # ---- default company -------------------------------------------

    def _ensure_default(self) -> None:
        """Create the 'Personal' Company on first ever load, so existing
        Units have a natural parent to point at without the user having
        to set one up manually."""
        with self._lock:
            if self._read(DEFAULT_COMPANY_ID):
                return
            # Any other Company exists? Skip the default — user has
            # already set up their own topology.
            if any(f.endswith(".json") for f in os.listdir(self.dir)):
                return
            self._write({
                "id": DEFAULT_COMPANY_ID,
                "name": "Personal",
                "purpose": "Default Company for solo work — auto-created.",
                "unit_ids": [],
                "created_at": _now_iso(),
            })

    # ---- id helpers ------------------------------------------------

    @staticmethod
    def new_id() -> str:
        return f"co_{uuid.uuid4().hex[:8]}"

    # ---- public API ------------------------------------------------

    def create(
        self,
        name: str,
        purpose: Optional[str] = None,
        company_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise CompanyStoreError("name is required")
        with self._lock:
            cid = company_id or self.new_id()
            if self._read(cid):
                raise CompanyStoreError(f"Company id {cid!r} already exists")
            spec = {
                "id": cid,
                "name": name,
                "purpose": (purpose or "").strip() or None,
                "unit_ids": [],
                "created_at": _now_iso(),
            }
            self._write(spec)
            return dict(spec)

    def get(self, company_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            spec = self._read(company_id)
            return dict(spec) if spec else None

    def list(self) -> List[Dict[str, Any]]:
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

    def update(self, company_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            spec = self._read(company_id)
            if not spec:
                raise CompanyStoreError(f"no Company with id {company_id!r}")
            for field in ("name", "purpose"):
                if field in patch and patch[field] is not None:
                    val = str(patch[field]).strip()
                    spec[field] = val or None
            self._write(spec)
            return dict(spec)

    def delete(self, company_id: str) -> bool:
        """Delete a Company. Does NOT cascade-delete its Units — they
        become orphaned (company_id still set on their TeamStore, but
        that reference now dangles). We deliberately leave Units alone
        so a mistaken delete doesn't take a founder's work with it."""
        if company_id == DEFAULT_COMPANY_ID:
            raise CompanyStoreError("cannot delete the default 'Personal' Company")
        with self._lock:
            path = self._path(company_id)
            if not os.path.exists(path):
                return False
            try:
                os.remove(path)
                return True
            except OSError as exc:
                logger.warning("Could not delete company %s: %s", company_id, exc)
                return False

    def add_unit(self, company_id: str, unit_id: str) -> Dict[str, Any]:
        """Attach a Unit to a Company. Idempotent — attaching twice is
        a no-op."""
        with self._lock:
            spec = self._read(company_id)
            if not spec:
                raise CompanyStoreError(f"no Company with id {company_id!r}")
            if unit_id not in spec.get("unit_ids", []):
                spec.setdefault("unit_ids", []).append(unit_id)
                self._write(spec)
            return dict(spec)

    def remove_unit(self, company_id: str, unit_id: str) -> bool:
        with self._lock:
            spec = self._read(company_id)
            if not spec:
                return False
            units = spec.get("unit_ids", [])
            if unit_id not in units:
                return False
            spec["unit_ids"] = [u for u in units if u != unit_id]
            self._write(spec)
            return True


# ------------------------------------------------------------------
# Module singleton
# ------------------------------------------------------------------

_singleton: Optional[CompanyStore] = None
_singleton_lock = threading.Lock()


def get_store() -> CompanyStore:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = CompanyStore()
        return _singleton
