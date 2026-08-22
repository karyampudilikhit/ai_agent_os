"""Who gets told to run the code.

Per-employee `required_outputs` stopped a specialist REFUSING to compute.
But the Supervisor still ELECTED the owner from role-word weights
(`quant` 5, `analyst` 4, `data` 1, `engineer` 1) with no idea any
employee had declared anything — so a founder could configure their Quant
Analyst to own computation and still watch the mandate land on the Data
Engineer. Half a fix.

The rule under test: a stated declaration beats every inference. The
founder defines the architecture; the heuristics are only for when they
haven't said.
"""
from __future__ import annotations

from backend.app.critique.compute_gate import (
    COMPUTE_MANDATE,
    choose_compute_owner_index,
    declared_compute_roles,
    ensure_compute_owner,
)

BACKTEST = "backtest SPY and report CAGR, Sharpe and max drawdown"
PLAIN = "summarise the attached meeting notes"

PLAN = [
    {"role": "Data Engineer", "sub_task": "fetch the price series"},
    {"role": "Quant Analyst", "sub_task": "design the crossover rules"},
    {"role": "Report Writer", "sub_task": "write it up"},
]


def _specs(**declared):
    """Flattened specs, as EmployeeCoordinator builds them."""
    return [
        {"role": r, "mandate": "…",
         "required_outputs": ["executed_code"] if declared.get(r.split()[0].lower()) else []}
        for r in ("Data Engineer", "Quant Analyst", "Report Writer")
    ]


# ------------------------------------------------- declaration wins

def test_a_declaration_overrides_the_role_word_heuristic():
    """Data Engineer scores 2 on hints; Quant Analyst scores 9. The
    declaration has to beat that, or configuring an employee is theatre."""
    assert choose_compute_owner_index(PLAN) == 1, "heuristic picks Quant Analyst"

    idx = choose_compute_owner_index(PLAN, _specs(data=True))
    assert idx == 0, "the declared Data Engineer must win"


def test_a_declaration_overrides_the_presentation_exclusion():
    """A Report Writer is normally excluded outright — handing execution
    to the writer is what produced deliverables full of asserted metrics.
    A deliberate declaration still wins: it is the founder's architecture
    to define, and quietly overruling it makes the setting a lie."""
    idx = choose_compute_owner_index(PLAN, _specs(report=True))
    assert idx == 2


def test_the_heuristic_still_runs_when_nobody_declared():
    assert choose_compute_owner_index(PLAN, _specs()) == 1


def test_the_earliest_declared_role_wins_when_several_declare():
    idx = choose_compute_owner_index(PLAN, _specs(data=True, quant=True))
    assert idx == 0, "earliest, because downstream roles need the numbers to exist"


# --------------------------------------------- both spec shapes work

def test_a_raw_registry_record_is_understood():
    """TeamStore.specialists() returns registry records with the
    declaration nested under `config`; the coordinator builds a flat
    dict. Both reach this function, so both must work."""
    registry_shape = [
        {"id": "e1", "role": "Quant Analyst", "mandate": "…",
         "config": {"required_outputs": ["executed_code"]}},
        {"id": "e2", "role": "Data Engineer", "mandate": "…", "config": {}},
    ]
    assert declared_compute_roles(registry_shape) == {"quant analyst"}


def test_a_legacy_record_with_no_config_declares_nothing():
    legacy = [{"id": "e1", "role": "Quant Analyst", "mandate": "…"}]
    assert declared_compute_roles(legacy) == set()


def test_no_specialists_is_not_an_error():
    assert declared_compute_roles(None) == set()
    assert choose_compute_owner_index(PLAN, None) == 1


# ------------------------------------------------ end-to-end mandate

def test_the_mandate_lands_on_the_declared_owner():
    out = ensure_compute_owner(BACKTEST, PLAN, _specs(data=True))

    assert COMPUTE_MANDATE in out[0]["sub_task"], "Data Engineer should own it"
    assert COMPUTE_MANDATE not in out[1]["sub_task"]
    assert COMPUTE_MANDATE not in out[2]["sub_task"]


def test_a_declaration_fires_even_when_the_task_names_no_metric():
    """The point of configuring it is that the role owns computation as a
    standing fact — not one founder phrasing at a time."""
    out = ensure_compute_owner(PLAN and PLAIN, PLAN, _specs(quant=True))
    assert COMPUTE_MANDATE in out[1]["sub_task"]


def test_an_undeclared_plain_task_is_left_completely_alone():
    """The gate must not start injecting compute mandates into ordinary
    work just because the plumbing now exists."""
    assert ensure_compute_owner(PLAIN, PLAN, _specs()) == PLAN


def test_an_existing_owner_is_not_overridden():
    """If the Supervisor already assigned execution explicitly, leave its
    plan alone — the mechanical fallback is for when it did not."""
    plan = [
        {"role": "Data Engineer", "sub_task": "fetch it"},
        {"role": "Quant Analyst", "sub_task": "run_python over the series and print the metrics"},
    ]
    assert ensure_compute_owner(BACKTEST, plan, _specs(data=True)) == plan


def test_the_original_plan_is_never_mutated():
    before = [dict(e) for e in PLAN]
    ensure_compute_owner(BACKTEST, PLAN, _specs(data=True))
    assert PLAN == before
