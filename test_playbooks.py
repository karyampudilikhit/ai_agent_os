"""Smoke test: playbook classifier + rules retrieval.

Verifies the heuristic classifier picks the right playbook for the
kinds of prompts users actually send, and that rules render into a
non-empty prompt block.
"""
from __future__ import annotations

from backend.app.employees.playbooks import (
    PLAYBOOKS,
    classify_task_type,
    format_rules_for_prompt,
    get_playbook,
)


def test_classifier() -> None:
    cases = [
        # Validation — the SaaS validation demo prompt goes here
        ("Validate this SaaS idea: an app that converts support tickets", "validation"),
        ("Should I build a Chrome extension for Reddit summaries?", "validation"),
        ("Go / No-Go on launching this product", "validation"),

        # Research
        ("Research the market for AI transcription tools", "research"),
        ("Who are the top competitors in feedback tools?", "research"),
        ("Find sources for cloud storage pricing", "research"),

        # Writing
        ("Write cold outreach email to CTOs at fintechs", "writing"),
        ("Draft a Product Hunt launch post", "writing"),

        # Analysis
        ("Analyze this churn cohort data", "analysis"),
        ("Diagnose the root cause of dropoff between step 2 and step 3", "analysis"),

        # Strategy
        ("What's our go-to-market strategy for Q4", "strategy"),
        ("Prioritize this backlog", "strategy"),

        # General (nothing matched)
        ("Help me think through my week", "general"),
        ("", "general"),
    ]
    for task, expected in cases:
        got = classify_task_type(task)
        assert got == expected, f"'{task[:40]}' -> got {got!r}, expected {expected!r}"
    print(f"[ok] classifier: {len(cases)} cases pass")


def test_playbooks_populated() -> None:
    for name, book in PLAYBOOKS.items():
        rules = book.get("rules") or []
        assert rules, f"playbook {name!r} has no rules"
        # Rules should be substantive, not one-liners
        for r in rules:
            assert len(r) > 40, f"rule in {name!r} suspiciously short: {r[:60]}"
    print(f"[ok] {len(PLAYBOOKS)} playbooks all populated with substantive rules")


def test_validation_playbook_has_key_rules() -> None:
    """The validation playbook is what fires on the video demo prompt —
    it must contain the critical 'mark unknown', 'adversarial paragraph',
    and 'quote verbatim' rules."""
    rendered = format_rules_for_prompt("validation").lower()
    critical_signals = [
        "unknown",           # mark-unknown rule
        "verbatim",          # verbatim quoting rule
        "attacking",         # adversarial paragraph rule
        "url",               # URL-citation rule
        "no primary source", # prove-absence rule
    ]
    for signal in critical_signals:
        assert signal in rendered, f"validation playbook missing rule containing {signal!r}"
    print("[ok] validation playbook contains all critical anti-hallucination rules")


def test_format_for_prompt_never_empty_for_known_type() -> None:
    for name in PLAYBOOKS:
        s = format_rules_for_prompt(name)
        assert s, f"format_rules_for_prompt({name!r}) returned empty"
        assert "1." in s, "rules should be numbered"
    print("[ok] format_rules_for_prompt renders non-empty numbered lists")


def test_fallback_to_general() -> None:
    """Unknown task type falls back to 'general' playbook, not empty."""
    book = get_playbook("nonexistent_type")
    assert book == PLAYBOOKS["general"]
    print("[ok] unknown task type falls back to 'general' playbook")


if __name__ == "__main__":
    test_classifier()
    test_playbooks_populated()
    test_validation_playbook_has_key_rules()
    test_format_for_prompt_never_empty_for_known_type()
    test_fallback_to_general()
    print("\nAll playbook checks passed.")
