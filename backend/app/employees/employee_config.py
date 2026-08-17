"""Per-employee configuration — the knobs that make one AI employee
behave differently from another.

Every employee in this system used to be identical at runtime: same step
budget, same model, same hardcoded prompt. The registry stored seven
fields and none of them changed behaviour. A founder who wanted a
stronger Quant Analyst could only rewrite its one-sentence mandate.

WHAT DROVE EACH SETTING. These are not speculative knobs -- each one maps
to a failure observed in live runs on 2026-08-15/16:

  collaboration      A Data Engineer refused the DONE gate three times
                     and never computed. Its prompt says "do only your
                     part ... if a piece belongs to a different role, say
                     so briefly and skip it" while the gate demanded
                     computation. The prompt won. That instruction was
                     hardcoded and unconditional; now it is a setting.

  required_outputs   The DONE gate only knew one requirement, derived
                     from whether the TASK mentioned Sharpe/CAGR. An
                     employee whose job is always to compute, or always
                     to fetch, could not say so.

  budget             A Quant Analyst burned 4 of its 10 steps arguing
                     with the gate and re-fetching data a teammate
                     already had. One global max_steps serves an analyst
                     and a formatter equally badly.

  domain_rules       A fabricated Sharpe ratio shipped partly because
                     domain standards (adjusted close, T+1 execution, no
                     look-ahead) had to be restated in every prompt
                     instead of living on the employee.

  prompt_override    Asked for directly by the founder: full control of
                     the persona block, with nothing protected.

RESOLUTION ORDER, lowest to highest:

  1. DEFAULTS            reproduces today's behaviour exactly
  2. role template       shared tuning for every "Quant Analyst"
  3. the employee's own  config
  4. call-time override  (employee_coordinator._SOLO_SUPERVISOR_BRIEF)

Layer 4 already exists and is deliberately left alone -- it is the
established way to override standing behaviour for ONE call.

ABSENT vs NULL. A key that is absent inherits from the layer below. A key
explicitly set to None CLEARS back to inheriting. Without that
distinction an "Advanced" panel can set a value but never un-set it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.app.orchestrator.execution_loop import (
    DEADLINE_SECONDS,
    MAX_STEPS,
    STEP_MAX_TOKENS,
)
from backend.app.orchestrator.output_contract import CONTRACT_KINDS

logger = logging.getLogger(__name__)

CONFIG_VERSION = 1


class EmployeeConfigError(ValueError):
    """Invalid configuration. Mapped to HTTP 400 by the API layer."""


# ---------------------------------------------------------------------
# Collaboration posture
# ---------------------------------------------------------------------
#
# `team` reproduces the previously-hardcoded text CHARACTER FOR CHARACTER.
# That is deliberate and is pinned by a test: the default path must not
# change behaviour at all, or every existing employee silently shifts.
COLLABORATION_MODES = ("team", "solo", "flexible")

LANE_TEXT = {
    # Verbatim from dynamic_employee.build_objective before this module
    # existed. Do not "improve" the wording -- the no-regression test
    # compares the rendered prompt exactly.
    "team": (
        "Do the part of this task that belongs to your role, and only your part.\n"
        "If a piece of the task belongs to a different role on this team, say so\n"
        "briefly and skip it — do not do work outside your mandate."
    ),
    # Condensed from _SOLO_SUPERVISOR_BRIEF, which fixed the same failure
    # for a supervisor with no team: told to plan rather than do, it
    # produced a roster of five specialists who did not exist and zero
    # actual data.
    "solo": (
        "You are working alone on this task. There is nobody to delegate to and\n"
        "nothing you assign will be picked up. Do not write a delegation plan, a\n"
        "team roster, or a timeline, and do not refer work to colleagues who do\n"
        "not exist. Use your tools and deliver the actual answer yourself."
    ),
    # The resolution of the contradiction that cost a real run. Stay in
    # your lane by default, but a lane boundary never outranks a
    # mechanical requirement.
    "flexible": (
        "Stay in your lane where a teammate clearly owns a piece of this task.\n"
        "But never refuse work your own required outputs demand: if producing a\n"
        "computed number, a fetched page, or a file is on your contract, you\n"
        "produce it yourself — whatever your mandate says about lanes."
    ),
}


# ---------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------
#
# Clamped rather than rejected: a founder typing 9999 into a step budget
# means "as many as possible", and failing their save teaches nothing.
# The ceilings exist so one misconfigured employee cannot burn an entire
# Ollama quota on a single task.
_BOUNDS = {
    "max_steps": (1, 60),
    "deadline_seconds": (10.0, 900.0),
    "step_max_tokens": (100, 4000),
}

DEFAULTS: Dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "template_id": None,
    "prompt_override": None,
    "collaboration": "team",
    "required_outputs": [],
    "domain_rules": [],
    "domain_rules_replace": False,
    # Imported from execution_loop rather than duplicated, so raising
    # AGENT_MAX_STEPS still moves the default for everyone who has not
    # overridden it. Two copies of these numbers would drift.
    "budget": {
        "max_steps": MAX_STEPS,
        "deadline_seconds": DEADLINE_SECONDS,
        "step_max_tokens": STEP_MAX_TOKENS,
    },
    "model": {"loop_model": None},
    "tools": {"allow": [], "deny": []},
    # Domains this employee's browser work may reach. Empty means "any
    # public site" -- open by default because a founder who cannot ask it
    # to visit an ordinary website without editing config first is a
    # founder who turns the guard off. The hard floor (internal
    # addresses, cloud metadata) is enforced regardless and cannot be
    # granted here. See tools/browser_policy.BrowserPolicy.
    "browser": {"allowed_domains": [], "allow_high_risk": False},
}

_NESTED_KEYS = ("budget", "model", "tools", "browser")


@dataclass(frozen=True)
class ResolvedConfig:
    """The fully-resolved settings one employee runs with."""

    collaboration: str = "team"
    prompt_override: Optional[str] = None
    required_outputs: Tuple[str, ...] = ()
    domain_rules: Tuple[str, ...] = ()
    max_steps: int = MAX_STEPS
    deadline_seconds: float = DEADLINE_SECONDS
    step_max_tokens: int = STEP_MAX_TOKENS
    loop_model: Optional[str] = None
    template_id: Optional[str] = None
    allowed_domains: Tuple[str, ...] = ()
    allow_high_risk_browser: bool = False

    def browser_policy(self):
        """This employee's browser scope, as an enforceable object.

        Built here rather than in the browser layer so the policy is
        constructed BEFORE the run and handed down — a scope assembled
        mid-run from anything the model saw is a scope an injected
        instruction can influence.
        """
        from backend.app.browser.policy import BrowserPolicy
        return BrowserPolicy.for_task(
            domains=self.allowed_domains or None,
            allow_high_risk=self.allow_high_risk_browser,
        )

    @property
    def effective_collaboration(self) -> str:
        """`team` is promoted to `flexible` when the employee has a
        required output to satisfy.

        THE BUG THIS CLOSES. The lane instruction and the DONE gate were
        two layers giving opposite orders, and the run that exposed it
        went: gate says compute -> prompt says not your lane -> refused,
        three times, then gave up having computed nothing. Leaving the
        founder to notice and fix that by hand would be shipping the
        contradiction with a settings page bolted on.
        """
        if self.required_outputs and self.collaboration == "team":
            return "flexible"
        return self.collaboration

    @property
    def lane_text(self) -> str:
        return LANE_TEXT[self.effective_collaboration]


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------

def _clamp(name: str, value: Any) -> Any:
    lo, hi = _BOUNDS[name]
    try:
        num = type(lo)(value)
    except (TypeError, ValueError):
        raise EmployeeConfigError(f"{name} must be a number, got {value!r}")
    return max(lo, min(hi, num))


def _clean_str_list(value: Any, label: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise EmployeeConfigError(f"{label} must be a list of strings")
    out: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def validate(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a config patch, dropping unknown keys.

    Unknown top-level keys are dropped rather than rejected so an older
    server can accept a payload from a newer UI without erroring -- the
    same forgiving-read bias the ledgers take.
    """
    if patch is None:
        return {}
    if not isinstance(patch, dict):
        raise EmployeeConfigError("config must be an object")

    out: Dict[str, Any] = {}
    for key, value in patch.items():
        if key not in DEFAULTS and key != "updated_at":
            logger.debug("dropping unknown employee config key %r", key)
            continue

        if value is None:
            out[key] = None          # explicit clear-to-inherit
            continue

        if key == "collaboration":
            text = str(value).strip().lower()
            if text not in COLLABORATION_MODES:
                raise EmployeeConfigError(
                    "collaboration must be one of %s" % (", ".join(COLLABORATION_MODES),))
            out[key] = text
        elif key == "required_outputs":
            kinds = _clean_str_list(value, "required_outputs")
            bad = [k for k in kinds if k not in CONTRACT_KINDS]
            if bad:
                raise EmployeeConfigError(
                    "unknown required output(s): %s. Valid: %s"
                    % (", ".join(bad), ", ".join(sorted(CONTRACT_KINDS))))
            out[key] = kinds
        elif key == "domain_rules":
            out[key] = _clean_str_list(value, "domain_rules")
        elif key == "domain_rules_replace":
            out[key] = bool(value)
        elif key == "budget":
            if not isinstance(value, dict):
                raise EmployeeConfigError("budget must be an object")
            budget: Dict[str, Any] = {}
            for bk, bv in value.items():
                if bk not in _BOUNDS:
                    continue
                budget[bk] = None if bv is None else _clamp(bk, bv)
            out[key] = budget
        elif key in ("model", "tools"):
            if not isinstance(value, dict):
                raise EmployeeConfigError(f"{key} must be an object")
            out[key] = dict(value)
        elif key == "browser":
            if not isinstance(value, dict):
                raise EmployeeConfigError("browser must be an object")
            b: Dict[str, Any] = {}
            if "allowed_domains" in value:
                doms = value["allowed_domains"]
                b["allowed_domains"] = (
                    None if doms is None
                    # Strip scheme/path if a founder pastes a whole URL —
                    # "https://vercel.com/new" is a domain they meant.
                    else [d.split("//")[-1].split("/")[0].strip().lstrip("*.").lower()
                          for d in _clean_str_list(doms, "allowed_domains")]
                )
            if "allow_high_risk" in value:
                b["allow_high_risk"] = (None if value["allow_high_risk"] is None
                                        else bool(value["allow_high_risk"]))
            out[key] = b
        elif key == "prompt_override":
            text = str(value)
            out[key] = text if text.strip() else None
        elif key == "template_id":
            out[key] = str(value).strip() or None
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------
# Merging and resolution
# ---------------------------------------------------------------------

def merge_config(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Layer `patch` over `base`, one level deep.

    THE HAZARD THIS EXISTS FOR. Assigning `spec["config"] = patch` would
    let a UI that sends only {"collaboration": "solo"} silently wipe
    required_outputs and every budget the founder had set. A settings
    page that quietly discards settings is worse than no settings page.

    A leaf of None DELETES the key, so the value falls back to whatever
    the layer below provides. That is how "clear back to inherit" is
    expressed over a JSON API, where absent and null are the only two
    ways to say nothing.
    """
    out = dict(base or {})
    for key, value in (patch or {}).items():
        if value is None:
            out.pop(key, None)
            continue
        if key in _NESTED_KEYS and isinstance(value, dict):
            nested = dict(out.get(key) or {})
            for nk, nv in value.items():
                if nv is None:
                    nested.pop(nk, None)
                else:
                    nested[nk] = nv
            out[key] = nested
        else:
            out[key] = value
    return out


def _template_for(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Role-template layer. Stubbed to {} until increment 2.

    The call sits here from day one so adding templates is a fill-in
    rather than a refactor of every resolution path.
    """
    try:
        from backend.app.employees.role_templates import template_for_employee
    except ImportError:
        return {}
    try:
        return template_for_employee(spec) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("role template lookup failed, using defaults: %s", exc)
        return {}


def _layers(spec: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    template = _template_for(spec or {})
    own = validate((spec or {}).get("config") or {})
    return template, own


def resolve(spec: Optional[Dict[str, Any]]) -> ResolvedConfig:
    """Collapse defaults + template + employee config into one object.

    `resolve({})` on a legacy 7-key registry record must equal DEFAULTS --
    that is what makes this change invisible to every existing employee,
    and it is pinned by a test.
    """
    template, own = _layers(spec or {})
    merged = merge_config(merge_config(DEFAULTS, template), own)

    budget = merged.get("budget") or {}
    model = merged.get("model") or {}
    browser = merged.get("browser") or {}

    # Template rules come first, then the employee's own. Rules are
    # additive by nature -- a specialist's personal standard should not
    # silently drop the desk-wide one -- so this is the single key that
    # concatenates rather than replaces.
    rules: List[str] = []
    if not own.get("domain_rules_replace"):
        rules.extend(template.get("domain_rules") or [])
    for rule in own.get("domain_rules") or []:
        if rule not in rules:
            rules.append(rule)
    if not own.get("domain_rules") and not rules:
        rules = list(merged.get("domain_rules") or [])

    return ResolvedConfig(
        collaboration=merged.get("collaboration") or "team",
        prompt_override=merged.get("prompt_override"),
        required_outputs=tuple(merged.get("required_outputs") or ()),
        domain_rules=tuple(rules),
        max_steps=int(budget.get("max_steps", MAX_STEPS)),
        deadline_seconds=float(budget.get("deadline_seconds", DEADLINE_SECONDS)),
        step_max_tokens=int(budget.get("step_max_tokens", STEP_MAX_TOKENS)),
        loop_model=model.get("loop_model"),
        template_id=merged.get("template_id"),
        allowed_domains=tuple(browser.get("allowed_domains") or ()),
        allow_high_risk_browser=bool(browser.get("allow_high_risk")),
    )


def resolve_with_provenance(spec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Every setting with WHERE it came from.

    Without this an Advanced panel is a wall of blank boxes: the founder
    cannot tell an unset field from one deliberately set to the same
    value as the default, and cannot tell which of their employees is
    actually inheriting the desk-wide rule.
    """
    template, own = _layers(spec or {})
    tid = own.get("template_id") or template.get("template_id")
    template_label = "template:%s" % tid if tid else "template"

    def source(key: str, sub: Optional[str] = None) -> str:
        for layer, label in ((own, "employee"), (template, template_label)):
            block = layer.get(key)
            if sub is None:
                if key in layer:
                    return label
            elif isinstance(block, dict) and sub in block:
                return label
        return "default"

    resolved = resolve(spec)
    out: Dict[str, Any] = {
        "collaboration": {"value": resolved.collaboration, "from": source("collaboration")},
        "effective_collaboration": {
            "value": resolved.effective_collaboration,
            "from": ("auto:required_outputs"
                     if resolved.effective_collaboration != resolved.collaboration
                     else source("collaboration")),
        },
        "prompt_override": {"value": resolved.prompt_override,
                            "from": source("prompt_override")},
        "required_outputs": {"value": list(resolved.required_outputs),
                             "from": source("required_outputs")},
        "domain_rules": {"value": list(resolved.domain_rules),
                         "from": source("domain_rules")},
        "template_id": {"value": resolved.template_id, "from": source("template_id")},
    }
    for name, value in (("max_steps", resolved.max_steps),
                        ("deadline_seconds", resolved.deadline_seconds),
                        ("step_max_tokens", resolved.step_max_tokens)):
        out[name] = {"value": value, "from": source("budget", name)}
    out["loop_model"] = {"value": resolved.loop_model, "from": source("model", "loop_model")}
    return out


def format_domain_rules(rules: Sequence[str]) -> str:
    """Numbered list, matching playbooks.format_rules_for_prompt's style
    so a specialist sees one consistent shape of rule, not two."""
    return "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1))
