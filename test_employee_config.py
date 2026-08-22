"""Per-employee configuration: resolution, merging, and the guarantee
that an unconfigured employee behaves exactly as it did before.

The last of those is the one that matters most. ~40 employee records
exist on disk with no `config` key at all. If `resolve({})` drifts from
DEFAULTS by even one field, every one of them silently changes behaviour
the moment this ships.
"""
from __future__ import annotations

import pytest

from backend.app.employees import employee_config as ec
from backend.app.employees.employee_config import (
    DEFAULTS,
    EmployeeConfigError,
    merge_config,
    resolve,
    resolve_with_provenance,
    validate,
)
from backend.app.orchestrator.execution_loop import (
    DEADLINE_SECONDS,
    MAX_STEPS,
    STEP_MAX_TOKENS,
)


def _spec(config=None):
    """A registry record, optionally carrying config."""
    spec = {"id": "emp_x", "role": "Quant Analyst", "mandate": "compute things",
            "avatar_seed": "quant_analyst", "tags": [], "is_supervisor": False,
            "created_at": "2026-01-01"}
    if config is not None:
        spec["config"] = config
    return spec


# ------------------------------------------------- the no-regression test

def test_a_legacy_record_resolves_to_todays_behaviour():
    """A 7-key record written before config existed must resolve to
    exactly the defaults -- no config key at all, not even empty."""
    r = resolve(_spec())

    assert r.collaboration == "team"
    assert r.effective_collaboration == "team"
    assert r.prompt_override is None
    assert r.required_outputs == ()
    assert r.domain_rules == ()
    assert r.max_steps == MAX_STEPS
    assert r.deadline_seconds == DEADLINE_SECONDS
    assert r.step_max_tokens == STEP_MAX_TOKENS


def test_defaults_track_the_loop_constants():
    """Budget defaults are imported from execution_loop, not copied. Two
    copies of these numbers would drift the first time AGENT_MAX_STEPS
    is raised."""
    assert DEFAULTS["budget"]["max_steps"] == MAX_STEPS
    assert DEFAULTS["budget"]["deadline_seconds"] == DEADLINE_SECONDS
    assert DEFAULTS["budget"]["step_max_tokens"] == STEP_MAX_TOKENS


def test_the_default_lane_text_is_unchanged():
    """This string is what every existing employee currently sees. It is
    reproduced character-for-character, and this test is what stops a
    well-meaning reword from silently altering every prompt."""
    assert ec.LANE_TEXT["team"] == (
        "Do the part of this task that belongs to your role, and only your part.\n"
        "If a piece of the task belongs to a different role on this team, say so\n"
        "briefly and skip it — do not do work outside your mandate."
    )


# ------------------------------------------------------------ resolution

def test_employee_config_overrides_defaults():
    r = resolve(_spec({"collaboration": "solo", "budget": {"max_steps": 3}}))
    assert r.collaboration == "solo"
    assert r.max_steps == 3
    assert r.step_max_tokens == STEP_MAX_TOKENS, "untouched budget keys still inherit"


def test_required_outputs_promote_team_to_flexible():
    """THE CONTRADICTION THIS CLOSES. A Data Engineer was told 'compute'
    by the gate and 'not your lane' by the prompt. It obeyed the prompt,
    refused three times, and computed nothing. An employee that owes a
    required output cannot also be told to skip work outside its lane."""
    r = resolve(_spec({"required_outputs": ["executed_code"]}))

    assert r.collaboration == "team", "the stored setting is untouched"
    assert r.effective_collaboration == "flexible", "but the lane it runs with is not"
    assert "never refuse work your own required outputs demand" in r.lane_text


def test_explicit_solo_is_not_promoted():
    r = resolve(_spec({"collaboration": "solo", "required_outputs": ["executed_code"]}))
    assert r.effective_collaboration == "solo", "only 'team' is promoted"


# ---------------------------------------------------------------- merging

def test_merge_does_not_wipe_untouched_keys():
    """The hazard the nested-config design exists for: a UI sending one
    field must not discard the rest."""
    base = {"required_outputs": ["executed_code"], "budget": {"max_steps": 3}}
    merged = merge_config(base, {"collaboration": "solo"})

    assert merged["required_outputs"] == ["executed_code"]
    assert merged["budget"]["max_steps"] == 3
    assert merged["collaboration"] == "solo"


def test_nested_budget_keys_merge_rather_than_replace():
    base = {"budget": {"max_steps": 3, "step_max_tokens": 900}}
    merged = merge_config(base, {"budget": {"max_steps": 5}})

    assert merged["budget"] == {"max_steps": 5, "step_max_tokens": 900}


def test_explicit_null_clears_back_to_inherit():
    """Absent inherits; null CLEARS. Without the distinction an Advanced
    panel can set a value but never un-set it."""
    base = {"collaboration": "solo", "budget": {"max_steps": 3}}

    assert "collaboration" not in merge_config(base, {"collaboration": None})
    assert "max_steps" not in merge_config(base, {"budget": {"max_steps": None}})["budget"]

    r = resolve(_spec(merge_config(base, {"budget": {"max_steps": None}})))
    assert r.max_steps == MAX_STEPS, "cleared budget falls back to the default"


# ------------------------------------------------------------ domain rules

def test_domain_rules_from_the_employee_are_kept_in_order():
    r = resolve(_spec({"domain_rules": ["always adjusted close", "T+1 execution"]}))
    assert r.domain_rules == ("always adjusted close", "T+1 execution")


def test_domain_rules_deduplicate():
    r = resolve(_spec({"domain_rules": ["252 trading days", "252 trading days"]}))
    assert r.domain_rules == ("252 trading days",)


def test_format_domain_rules_numbers_them():
    assert ec.format_domain_rules(["a", "b"]) == "1. a\n2. b"


# ------------------------------------------------------------- validation

def test_budgets_are_clamped_not_rejected():
    """A founder typing 9999 means 'as many as possible'. Failing their
    save teaches nothing; the ceiling exists so one bad employee cannot
    burn the whole model quota on a single task."""
    out = validate({"budget": {"max_steps": 9999, "step_max_tokens": 1}})
    assert out["budget"]["max_steps"] == 60
    assert out["budget"]["step_max_tokens"] == 100


def test_unknown_required_output_is_rejected():
    with pytest.raises(EmployeeConfigError) as exc:
        validate({"required_outputs": ["executed_code", "make_coffee"]})
    assert "make_coffee" in str(exc.value)


def test_unknown_collaboration_is_rejected():
    with pytest.raises(EmployeeConfigError):
        validate({"collaboration": "whatever"})


def test_unknown_top_level_keys_are_dropped_not_rejected():
    """Forgiving read: an older server should accept a payload from a
    newer UI rather than 400 on a field it has not learned yet."""
    out = validate({"collaboration": "solo", "future_feature": {"x": 1}})
    assert out == {"collaboration": "solo"}


def test_blank_prompt_override_is_stored_as_none():
    """Clearing the textarea must mean 'use the default', not 'run with
    an empty persona'."""
    assert validate({"prompt_override": "   "})["prompt_override"] is None


# ------------------------------------------------------------ provenance

def test_provenance_distinguishes_set_from_default():
    prov = resolve_with_provenance(_spec({"budget": {"max_steps": 4}}))

    assert prov["max_steps"] == {"value": 4, "from": "employee"}
    assert prov["step_max_tokens"]["from"] == "default"
    assert prov["collaboration"]["from"] == "default"


def test_provenance_flags_the_automatic_promotion():
    """The founder set 'team' but it runs as 'flexible'. Showing that as
    'employee' would be a lie about their own settings."""
    prov = resolve_with_provenance(_spec({"required_outputs": ["executed_code"]}))
    assert prov["effective_collaboration"]["value"] == "flexible"
    assert prov["effective_collaboration"]["from"] == "auto:required_outputs"
