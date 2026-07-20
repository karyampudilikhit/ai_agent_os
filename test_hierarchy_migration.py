"""Smoke test: Phase 1 hierarchy — Employee registry + TeamStore
migration + Employee reuse across Units.

Uses temp dirs so it never touches the real team_data / registry.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from backend.app.employees.employee_registry import EmployeeRegistry
from backend.app.employees.team_store import TeamStore


def _fresh_dirs() -> tuple[str, str]:
    tmp = tempfile.mkdtemp(prefix="viz_hier_")
    return f"{tmp}/team_data", f"{tmp}/registry"


def test_registry_crud() -> None:
    _, reg_dir = _fresh_dirs()
    reg = EmployeeRegistry(registry_dir=reg_dir)

    emp = reg.create(role="Growth Strategist", mandate="Design go-to-market.")
    assert emp["id"].startswith("emp_"), emp
    assert reg.get(emp["id"])["role"] == "Growth Strategist"

    emp2 = reg.create(role="Copywriter", mandate="Write landing pages.")
    assert emp["id"] != emp2["id"]
    assert len(reg.list()) == 2

    updated = reg.update(emp["id"], {"mandate": "New mandate."})
    assert updated["mandate"] == "New mandate."
    assert reg.get(emp["id"])["mandate"] == "New mandate."

    assert reg.delete(emp["id"]) is True
    assert reg.get(emp["id"]) is None
    assert len(reg.list()) == 1
    print("[ok] registry CRUD")


def test_legacy_migration() -> None:
    team_dir, reg_dir = _fresh_dirs()
    reg = EmployeeRegistry(registry_dir=reg_dir)

    # Simulate a v1 team file (matches the shape of the real
    # session_11ebac94.json file in the repo).
    legacy = {
        "session_id": "session_test1",
        "members": [
            {"role": "Market Researcher", "mandate": "Do market research."},
            {"role": "Product Designer",  "mandate": "Design the product."},
            {"role": "Launch Strategist", "mandate": "Plan the launch."},
        ],
        "created_at": "2026-07-14T00:00:00",
    }
    Path(team_dir).mkdir(parents=True, exist_ok=True)
    Path(team_dir, "session_test1.json").write_text(json.dumps(legacy))

    store = TeamStore(session_id="session_test1", team_dir=team_dir, registry=reg)
    members = store.members()
    assert len(members) == 3
    roles = {m["role"] for m in members}
    assert roles == {"Market Researcher", "Product Designer", "Launch Strategist"}
    for m in members:
        assert "employee_id" in m
        # Legacy IDs use session_id__role_slug — must match what the
        # spawner used to generate, so any existing memory files link up
        assert m["employee_id"].startswith("session_test1__"), m

    # File on disk should now be v2
    on_disk = json.loads(Path(team_dir, "session_test1.json").read_text())
    assert on_disk["schema_version"] == 2
    for m in on_disk["members"]:
        assert "employee_id" in m
        assert "role" not in m
        assert "mandate" not in m

    # Loading again should NOT re-migrate (idempotent)
    store2 = TeamStore(session_id="session_test1", team_dir=team_dir, registry=reg)
    assert len(store2.members()) == 3
    print("[ok] v1 -> v2 migration + idempotent reload")


def test_employee_reuse_across_units() -> None:
    """The core 'B' win: one Employee, two Units, one memory."""
    team_dir, reg_dir = _fresh_dirs()
    reg = EmployeeRegistry(registry_dir=reg_dir)

    unit_a = TeamStore(session_id="unit_a", team_dir=team_dir, registry=reg)
    hired = unit_a.add_member(role="Marketer", mandate="Handle marketing.")
    marketer_id = hired["employee_id"]

    # Hire the SAME Marketer into a second Unit
    unit_b = TeamStore(session_id="unit_b", team_dir=team_dir, registry=reg)
    reused = unit_b.hire(marketer_id)
    assert reused["employee_id"] == marketer_id, reused

    # Both Units see the same employee_id
    assert any(m["employee_id"] == marketer_id for m in unit_a.members())
    assert any(m["employee_id"] == marketer_id for m in unit_b.members())
    # Registry has ONE entry (not duplicated)
    assert len([e for e in reg.list() if e["role"] == "Marketer"]) == 1
    print("[ok] one Employee hired into two Units, single identity")


def test_delete_from_unit_preserves_registry() -> None:
    """Removing an Employee from a Unit does NOT delete them from the
    registry — they might still be in other Units, or hired back later."""
    team_dir, reg_dir = _fresh_dirs()
    reg = EmployeeRegistry(registry_dir=reg_dir)
    unit = TeamStore(session_id="u1", team_dir=team_dir, registry=reg)
    hired = unit.add_member(role="Analyst", mandate="Do analysis.")
    eid = hired["employee_id"]

    assert unit.remove_member(eid) is True
    assert len(unit.members()) == 0
    # Registry entry still exists
    assert reg.get(eid) is not None
    print("[ok] removing from Unit preserves registry Employee")


def test_supervisor_flag_via_add_member() -> None:
    team_dir, reg_dir = _fresh_dirs()
    reg = EmployeeRegistry(registry_dir=reg_dir)
    unit = TeamStore(session_id="u2", team_dir=team_dir, registry=reg)
    sup = unit.add_member(role="Supervisor", mandate="Delegate.", is_supervisor=True)
    assert sup.get("is_supervisor") is True
    stored_sup = unit.supervisor()
    assert stored_sup is not None
    assert stored_sup["employee_id"] == sup["employee_id"]
    print("[ok] supervisor flag round-trips")


if __name__ == "__main__":
    test_registry_crud()
    test_legacy_migration()
    test_employee_reuse_across_units()
    test_delete_from_unit_preserves_registry()
    test_supervisor_flag_via_add_member()
    print("\nAll Phase 1 hierarchy checks passed.")
